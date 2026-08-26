"""Unit tests for the pure logic in src/learn/session.py.

Three static/module-level functions, none of which touch the database, the item bank or
the generation server on 127.0.0.1:8080:

  LearnStore._reconcile  -- repairs the grader's answer into a partition of the item's
                            own key points
  LearnStore._due_after  -- spaced-repetition interval
  _epoch                 -- timestamp parsing

LearnStore.__init__ opens sqlite and loads items, so nothing here instantiates it. Both
scheduling functions are @staticmethod and are called on the class.
"""
import pytest

import session
from session import LearnStore


POINTS = ["Friction makes the apparently easy difficult",
          "Fog obscures the situation the commander sees",
          "Enemy will opposes every action taken"]


# ---------------------------------------------------------------------------
# _reconcile -- the partition invariant
# ---------------------------------------------------------------------------

def test_merged_covered_line_with_correct_verdict_credits_every_point():
    # The measured failure this function exists for: for an answer that earned all three
    # key points the grader returned ONE merged line -- "covered all three specified
    # types of friction" -- so the Marine saw a single tick and two points that had
    # simply vanished. The item's own key points are what gets shown back.
    covered, missed = LearnStore._reconcile(
        POINTS, ["covered all three specified types of friction"], [], "correct")
    assert covered == POINTS
    assert missed == []


def test_incorrect_with_empty_missed_credits_nothing():
    # A grader that says "incorrect" and then lists nothing missed is contradicting
    # itself. Awarding credit on the strength of an empty list would tell a Marine they
    # got something right when they did not, which the grading prompt explicitly ranks as
    # the worst outcome.
    covered, missed = LearnStore._reconcile(POINTS, [], [], "incorrect")
    assert covered == []
    assert missed == POINTS


def test_partial_verdict_splits_on_the_missed_side():
    # `missed` is authoritative because the grading prompt already tells the grader to
    # resolve genuine doubt against the student. Anything not called missed is credited.
    missed_claim = ["the student never mentioned that fog obscures the situation"]
    covered, missed = LearnStore._reconcile(POINTS, [], missed_claim, "partial")
    assert missed == ["Fog obscures the situation the commander sees"]
    assert covered == ["Friction makes the apparently easy difficult",
                       "Enemy will opposes every action taken"]


def test_correct_verdict_with_a_missed_point_still_honours_missed():
    # An internally inconsistent verdict in the other direction. `missed` wins, because
    # over-crediting is the failure that matters.
    covered, missed = LearnStore._reconcile(
        POINTS, POINTS, ["nothing was said about the enemy will opposing every action"],
        "correct")
    assert missed == ["Enemy will opposes every action taken"]
    assert covered == POINTS[:2]


@pytest.mark.parametrize("covered,missed,verdict", [
    ([], [], "correct"),
    ([], [], "partial"),
    ([], [], "incorrect"),
    (["covered all three specified types of friction"], [], "correct"),
    (["everything"], ["nothing"], "partial"),
    (["```json"], ["{\"covered\": []}"], "incorrect"),
    (["%%%", ""], ["\n\n", "   "], "partial"),
    (["I cannot answer that"], ["I cannot answer that"], "correct"),
    ([None and ""], ["unparseable model output 12345"], "incorrect"),
])
def test_reconcile_always_partitions_the_key_points(covered, missed, verdict):
    # The invariant, over every shape of model output including garbage: what is shown
    # back to the learner is the item's key points, each in exactly one bucket. A
    # dropped point is invisible -- the learner never learns they missed it -- and a
    # duplicated one inflates the score.
    keep, miss = LearnStore._reconcile(POINTS, covered, missed, verdict)
    assert len(keep) + len(miss) == len(POINTS)
    assert set(keep) | set(miss) == set(POINTS)
    assert set(keep) & set(miss) == set()
    for p in keep + miss:
        assert p in POINTS          # nothing the model wrote leaks into the result


def test_reconcile_on_an_item_with_no_key_points_is_empty():
    # Degenerate but reachable: review.py flags "no key points" rather than blocking, so
    # such an item can still be graded. It must not raise on the way through.
    assert LearnStore._reconcile([], ["anything"], ["anything"], "incorrect") == ([], [])


def test_reconcile_needs_half_the_point_to_call_it_missed():
    # A grader line that merely shares a topic word with a key point is not a claim
    # about that point. Requiring half of the point's content tokens is what stops one
    # vague sentence from wiping out every point in the item.
    points = ["Friction makes the apparently easy difficult"]
    weak = ["the answer mentioned difficult conditions"]
    covered, missed = LearnStore._reconcile(points, [], weak, "partial")
    # Only 'difficult' overlaps, which is below half -- so this is not a match, and the
    # incorrect/partial fallback is what condemns the point instead.
    assert missed == points
    assert covered == []


# ---------------------------------------------------------------------------
# _due_after -- spaced repetition
# ---------------------------------------------------------------------------

def test_confidently_wrong_repeats_in_the_same_session():
    # Being *certain* and wrong is a different failure from being *unsure* and wrong: the
    # first means a Marine is carrying a belief they would act on. Zero days means the
    # item comes back before they leave the terminal.
    assert LearnStore._due_after(0, "incorrect", 4) == 0
    assert LearnStore._due_after(0, "incorrect", 3) == 0
    # ...and a long correct streak does not buy any grace once they are confidently wrong.
    assert LearnStore._due_after(9, "incorrect", 4) == 0


def test_unsure_and_wrong_waits_a_day():
    # Knowing you do not know is not the same failure, so it gets an ordinary interval.
    assert LearnStore._due_after(0, "incorrect", 2) == 1
    assert LearnStore._due_after(0, "incorrect", 1) == 1


def test_missing_confidence_is_treated_as_low():
    # confidence is nullable in learn_attempts. `(confidence or 0)` must not throw and
    # must not silently promote a null into the same-session bucket.
    assert LearnStore._due_after(0, "incorrect", None) == 1
    assert LearnStore._due_after(3, "correct", None) == 3


def test_partial_always_waits_one_day():
    # A half-known item is not scheduled by streak: the streak was reset to zero by the
    # non-correct verdict anyway, and a fixed day is the honest answer.
    assert LearnStore._due_after(0, "partial", 4) == 1
    assert LearnStore._due_after(9, "partial", 1) == 1


@pytest.mark.parametrize("streak,expected", [
    (1, 1), (2, 3), (3, 7), (4, 21), (5, 60),
])
def test_correct_answers_walk_the_interval_ladder(streak, expected):
    # INTERVALS = [1, 3, 7, 21, 60]. _schedule() increments the streak BEFORE calling
    # this, so streak=1 is the first correct answer and must land on 1 day, not 3.
    assert LearnStore._due_after(streak, "correct", 4) == expected


def test_streak_past_the_end_clamps_to_the_longest_interval():
    # Nothing is ever pushed beyond 60 days, and an out-of-range index must not raise.
    # A learner who has had an item right ten times running still sees it twice a year.
    assert LearnStore._due_after(6, "correct", 4) == 60
    assert LearnStore._due_after(50, "correct", 4) == 60


def test_streak_zero_is_clamped_up_rather_than_indexing_backwards():
    # min(max(streak, 1), ...) - 1. Without the max() a streak of 0 would index
    # INTERVALS[-1] and schedule a just-recovered item 60 days out.
    assert LearnStore._due_after(0, "correct", 4) == 1


def test_correct_but_guessing_is_capped_at_three_days():
    # Correct but guessing is not knowledge; do not push it out a month on the strength
    # of a coin flip that landed right.
    assert LearnStore._due_after(5, "correct", 1) == 3
    assert LearnStore._due_after(4, "correct", 1) == 3
    # Below the cap the interval is unaffected -- the rule caps, it does not override.
    assert LearnStore._due_after(2, "correct", 1) == 3
    assert LearnStore._due_after(1, "correct", 1) == 1


def test_due_after_is_never_negative():
    # _schedule() multiplies this by 86400 and adds it to the attempt time. A negative
    # would make an item due in the past forever and it would never leave the queue.
    for streak in range(0, 8):
        for verdict in ("correct", "partial", "incorrect", "unknown-verdict"):
            for conf in (None, 1, 2, 3, 4):
                assert LearnStore._due_after(streak, verdict, conf) >= 0


# ---------------------------------------------------------------------------
# _epoch
# ---------------------------------------------------------------------------

def test_epoch_parses_the_stored_format():
    # The exact format record() writes: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()).
    # It must be read back as UTC regardless of the device's local timezone -- the Jetson
    # is not guaranteed to be on Zulu, and a local-time parse would shift every due date.
    assert session._epoch("2026-08-24T12:34:56Z") == 1787574896


def test_epoch_of_none_sorts_as_ancient():
    # A missing timestamp must make the item maximally overdue, not crash the schedule.
    assert session._epoch(None) == 0.0
    assert session._epoch("") == 0.0


@pytest.mark.parametrize("bad", [
    "yesterday",
    "2026-08-24 12:34:56",      # space instead of T, no Z: a hand-edited row
    "2026-08-24T12:34:56",      # missing the Z
    "2026-13-45T99:99:99Z",     # in-format but impossible
    "not a timestamp at all",
])
def test_epoch_of_malformed_input_is_zero_not_an_exception(bad):
    # These come out of a TEXT column that nothing constrains. One bad row must not take
    # down the whole due-count query for every item the learner has.
    assert session._epoch(bad) == 0.0
