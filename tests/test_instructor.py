"""Unit tests for the instructor class aggregate (E4/E5, privacy boundary D-050).

Two things are under test, and the second is the point of the feature:

  * LearnStore.class_overview() computes per-section mastery summed across ALL learners,
    scored by the same "last attempt wins" rule progress() uses, over approved items only.
  * The privacy boundary is a REAL constraint in the code, not a UI choice: the aggregate
    must never carry a learner identifier or a per-learner row, so an instructor cannot
    drill into which named Marine is weak. D-050: aggregate to the instructor, individual
    to the Marine.

Unlike test_session.py these instantiate LearnStore, because the aggregate is a query, not
a pure function -- but it is still offline: a temp sqlite file and a temp items file, no
model server, no socket, no device. Grading is never invoked (record() is fed synthetic
grades), so 127.0.0.1:8080 is never touched.
"""
import json

import pytest

import main
from session import LearnStore, VERDICT_SCORE


def _grade(verdict):
    """A synthetic grade dict of the shape record() consumes."""
    return {"verdict": verdict, "covered": [], "missed": []}


@pytest.fixture
def store(tmp_path):
    items = {
        "track": [
            {"section": "Friction", "chapter": 1, "chapter_title": "The Nature of War",
             "pub_id": "MCDP1", "pub_title": "Warfighting",
             "item_ids": ["i1", "i2"]},
            {"section": "Boldness", "chapter": 2, "chapter_title": "The Theory of War",
             "pub_id": "MCDP1", "pub_title": "Warfighting",
             "item_ids": ["i3"]},
            # A section whose only item is never approved: it must not appear in the
            # aggregate, because a learner can never be served it either.
            {"section": "Unapproved", "chapter": 3, "chapter_title": "Preparation",
             "pub_id": "MCDP1", "pub_title": "Warfighting",
             "item_ids": ["i4"]},
        ],
        "items": [
            {"id": iid, "question": "q" + iid, "key_points": ["k"], "chapter": ch,
             "chapter_title": "t", "section": sec, "pub_id": "MCDP1",
             "source_text": "s", "citation": "c"}
            for iid, ch, sec in [("i1", 1, "Friction"), ("i2", 1, "Friction"),
                                 ("i3", 2, "Boldness"), ("i4", 3, "Unapproved")]
        ],
    }
    ip = tmp_path / "items.json"
    ip.write_text(json.dumps(items), encoding="utf-8")
    st = LearnStore(str(tmp_path / "records.sqlite"), str(ip))
    for iid in ("i1", "i2", "i3"):        # i4 left pending on purpose
        st.review.decide(iid, "approved")
    return st


def _rec(st, learner, iid, verdict):
    st.record(learner, st.items[iid], "ans", 3, _grade(verdict), 0.1)


def test_verdict_score_table_is_what_the_aggregate_counts():
    # Guards the assumption the section maths below rests on.
    assert VERDICT_SCORE["correct"] == 1.0 and VERDICT_SCORE["incorrect"] == 0.0


def test_class_overview_sums_mastery_across_learners(store):
    # alpha: i1 correct, i2 incorrect.  bravo: i1 correct, i3 partial.
    _rec(store, "alpha", "i1", "correct")
    _rec(store, "alpha", "i2", "incorrect")
    _rec(store, "bravo", "i1", "correct")
    _rec(store, "bravo", "i3", "partial")

    ov = store.class_overview()
    assert ov["n_learners"] == 2

    secs = {s["section"]: s for s in ov["sections"]}
    # Friction: i1 mastered by both (2/2), i2 attempted once and missed (0/1) -> 2 of 3.
    assert secs["Friction"]["attempts"] == 3
    assert secs["Friction"]["mastered"] == 2
    assert secs["Friction"]["mastery_pct"] == round(100 * 2 / 3)
    # Boldness: one partial attempt, not mastered.
    assert secs["Boldness"]["attempts"] == 1
    assert secs["Boldness"]["mastered"] == 0
    assert secs["Boldness"]["mastery_pct"] == 0

    # Class totals: 4 (learner, item) attempts, 2 now correct.
    assert ov["attempts_total"] == 4
    assert ov["mastered_total"] == 2
    assert ov["mastery_pct"] == 50
    assert ov["items_total"] == 3            # i4 is unapproved and does not count


def test_unapproved_section_is_absent_even_if_attempted(store):
    # Force an attempt on the unapproved item, then confirm it stays out of the aggregate.
    _rec(store, "alpha", "i4", "correct")
    ov = store.class_overview()
    assert "Unapproved" not in {s["section"] for s in ov["sections"]}
    assert ov["items_total"] == 3


def test_latest_attempt_wins_per_learner(store):
    # A Marine who missed then nailed the same item counts once, as mastered.
    _rec(store, "alpha", "i1", "incorrect")
    _rec(store, "alpha", "i1", "correct")
    ov = store.class_overview()
    friction = next(s for s in ov["sections"] if s["section"] == "Friction")
    assert friction["attempts"] == 1 and friction["mastered"] == 1


def test_empty_class_is_zeroed_not_broken(store):
    ov = store.class_overview()
    assert ov["n_learners"] == 0
    assert ov["attempts_total"] == 0 and ov["mastery_pct"] == 0
    # Sections with approved items still list, at zero -- the instructor sees the shape of
    # the course before anyone has practised.
    assert {"Friction", "Boldness"} <= {s["section"] for s in ov["sections"]}


def test_aggregate_carries_no_learner_identity(store):
    """The privacy boundary, enforced in the data the method returns (D-050).

    No learner name and no per-learner row may leave class_overview(), or an instructor
    could reconstruct which Marine is weak -- exactly the drill-down D-050 forbids.
    """
    _rec(store, "sgtsmith", "i1", "incorrect")
    _rec(store, "cpljones", "i2", "correct")
    ov = store.class_overview()

    blob = json.dumps(ov)
    assert "sgtsmith" not in blob and "cpljones" not in blob
    # No section (or any nested structure) may key anything by learner.
    for s in ov["sections"]:
        assert "learner" not in s and "learners" not in s
        assert not any("smith" in str(v).lower() or "jones" in str(v).lower()
                       for v in s.values())
    # The only per-learner fact allowed out is the class size -- a count, never a name.
    assert ov["n_learners"] == 2


def test_api_endpoint_returns_the_aggregate_without_names(store, monkeypatch):
    """End to end through the route: /api/instructor/overview serves the aggregate and no
    learner identity, and takes no learner parameter to drill down with."""
    from fastapi.testclient import TestClient

    _rec(store, "sgtsmith", "i1", "correct")
    _rec(store, "cpljones", "i3", "incorrect")
    monkeypatch.setitem(main.STATE, "learn", store)
    monkeypatch.setitem(main.STATE, "gap_path", "/nonexistent/gap_report.json")

    d = TestClient(main.app).get("/api/instructor/overview").json()
    assert d["available"] is True
    assert d["n_learners"] == 2
    assert "sgtsmith" not in json.dumps(d) and "cpljones" not in json.dumps(d)
    # The gap report is reused and, with no report on disk, degrades to unavailable rather
    # than erroring.
    assert d["gaps"]["available"] is False
