"""Unit tests for precheck() in src/learn/review.py -- the OFFLINE flags only.

precheck() has four checks. Three are pure text arithmetic; the fourth, the grounding
check, calls the embedding server on 127.0.0.1:8081. Every item here is built so that the
`if kps and src` guard closes that branch -- with an empty `source_text` for the question
and key-point checks, and with an empty key-point list for the SOURCE_NOT_TEACHABLE
checks, which are the ones that need a real source. That is what keeps this a unit test
rather than an integration test. Anything asserted about UNGROUNDED_KEY_POINT belongs in
scripts/boot_test.py, not here.

What is being protected: these flags are the only thing directing a human reviewer's
attention across 74 items, and a checker that cries wolf gets ignored -- which is worse
than not having one. So the false-positive cases matter as much as the true ones.
"""
import review


def codes(flags):
    return [f["code"] for f in flags]


def item(question, key_points, source_text=""):
    """A generated practice item. source_text stays empty on purpose -- see the module
    docstring; a non-empty one would open a socket to the embedding server."""
    return {"question": question, "key_points": key_points, "source_text": source_text}


# ---------------------------------------------------------------------------
# ANSWER_LEAKED
# ---------------------------------------------------------------------------

def test_question_restating_a_key_point_is_flagged_as_leaked():
    # A question that contains its own answer tests reading, not recall, which defeats
    # the entire purpose of the item bank.
    flags = review.precheck(item(
        "Why is friction the force that makes the apparently easy so difficult in war?",
        ["Friction is the force that makes the apparently easy so difficult"]))
    assert "ANSWER_LEAKED" in codes(flags)


def test_leak_detail_names_the_offending_key_point():
    # The reviewer has to be able to see WHICH point leaked without re-reading the item;
    # a bare code sends them back to the source.
    kp = "Friction is the force that makes the apparently easy so difficult"
    flags = review.precheck(item(
        "Why is friction the force that makes the apparently easy so difficult in war?",
        [kp]))
    leaked = [f for f in flags if f["code"] == "ANSWER_LEAKED"]
    assert len(leaked) == 1
    assert kp in leaked[0]["detail"]


def test_shared_topic_words_alone_do_not_leak():
    # Every question shares vocabulary with its answer -- that is what makes it the same
    # subject. Overlap is measured against the KEY POINT's tokens, and stop words are
    # dropped, precisely so topical overlap stays under the ceiling.
    flags = review.precheck(item(
        "What does MCDP 1 identify as the physical, mental and moral costs of war?",
        ["War is fought by human beings whose limits of endurance shape every plan a "
         "commander can realistically make."]))
    assert "ANSWER_LEAKED" not in codes(flags)


def test_leak_is_measured_per_key_point_not_across_the_set():
    # An item can leak one point of three. Flagging only when the whole answer leaks
    # would miss the common case, where the question quotes the first point verbatim.
    flags = review.precheck(item(
        "Given that friction is the force that makes the easy difficult, how do "
        "commanders reduce it?",
        ["Friction is the force that makes the easy difficult",
         "Simple plans and rehearsed drills reduce the effect of it"]))
    leaked = [f for f in flags if f["code"] == "ANSWER_LEAKED"]
    assert len(leaked) == 1


# ---------------------------------------------------------------------------
# SHAPE
# ---------------------------------------------------------------------------

def test_imperative_prompt_without_a_question_mark_is_not_flagged():
    # "Describe the spectrum of relations between total war and perfect peace" is a
    # perfectly good recall prompt and needs no question mark. Flagging those buried the
    # real signal under 27 false alarms. This item is clean on every offline check, so
    # the assertion is the empty list rather than merely "no SHAPE".
    flags = review.precheck(item(
        "Describe the spectrum of relations between total war and perfect peace",
        ["Escalation is governed by policy and political objectives, not by military "
         "means alone."]))
    assert flags == []


def test_statement_that_is_neither_question_nor_imperative_is_flagged():
    # The check still has to fire on a generator that emitted a declarative sentence
    # instead of a prompt, or it is not earning its false-positive budget.
    flags = review.precheck(item(
        "The commander is responsible for the intent of the operation order.",
        ["Intent expresses the purpose behind the assigned mission and the desired end "
         "state."]))
    assert "SHAPE" in codes(flags)


def test_one_word_key_point_is_flagged_as_too_thin():
    # A single-token key point cannot be graded: the grader is asked which points the
    # student covered, and "Friction." can be satisfied by saying the word.
    flags = review.precheck(item(
        "What is the definition of friction in the conduct of war?",
        ["Friction."]))
    thin = [f for f in flags if f["code"] == "SHAPE" and "too thin" in f["detail"]]
    assert len(thin) == 1
    assert "Friction." in thin[0]["detail"]


def test_very_short_question_is_flagged():
    # Under 25 characters there is not enough prompt to answer against.
    flags = review.precheck(item("What is war?", ["War is an act of policy carried on "
                                                  "by other means."]))
    assert "SHAPE" in codes(flags)


def test_item_with_no_key_points_is_flagged():
    # Nothing to grade against. Without this the item reaches the grader and every
    # answer scores "correct" by vacuous truth.
    flags = review.precheck(item(
        "Describe the relationship between friction and uncertainty in war", []))
    details = [f["detail"] for f in flags if f["code"] == "SHAPE"]
    assert "no key points" in details


def test_a_well_formed_item_produces_no_flags():
    # The volume case: 55 of 74 items had nothing suspicious about them, and
    # bulk_decide_unflagged() clears exactly those. A spurious flag here is not cosmetic
    # -- it forces a human back through items that were fine.
    flags = review.precheck(item(
        "What does MCDP 1 identify as the three elements that make up the nature of war?",
        ["Violence is the essential means by which war is waged",
         "War is an interaction between two opposing independent wills",
         "Uncertainty pervades every level of the conduct of operations"]))
    assert flags == []


# ---------------------------------------------------------------------------
# grounding branch stays offline
# ---------------------------------------------------------------------------

def test_empty_source_text_never_reaches_the_embedding_server():
    # Guard on the guard. If the `if kps and src` condition ever loosened, this whole
    # test module would start opening sockets to 127.0.0.1:8081 and would fail on a
    # machine that is doing its job -- an air-gapped one.
    flags = review.precheck(
        item("Describe the role of the commander in the estimate of the situation",
             ["The commander owns the decision and cannot delegate the judgement."]),
        embed_url="http://127.0.0.1:1")
    assert "UNGROUNDED_KEY_POINT" not in codes(flags)
    assert "PRECHECK_FAILED" not in codes(flags)


def test_tokeniser_drops_stop_words_and_short_tokens():
    # _overlap() is only meaningful because the denominator excludes filler. If stop
    # words counted, a long key point would dilute to below the ceiling and real leaks
    # would stop being flagged.
    assert review._tokens("What is the nature of war?") == ["nature", "war"]
    assert review._tokens("") == []
    assert review._tokens(None) == []


def test_overlap_of_identical_text_is_one_and_disjoint_text_is_zero():
    # The two anchors of the leak scale. LEAK_CEILING is a fraction of the key point's
    # tokens, so these must be exactly 1.0 and 0.0 for the threshold to mean anything.
    assert review._overlap("friction and uncertainty", "friction and uncertainty") == 1.0
    assert review._overlap("friction and uncertainty", "logistics and supply") == 0.0


# ---------------------------------------------------------------------------
# SOURCE_NOT_TEACHABLE
# ---------------------------------------------------------------------------
#
# The source texts below are verbatim corpus paragraphs, lightly de-mojibaked. Using
# invented ones would defeat the purpose: this check exists because a fuzzy version of it
# was refuted against REAL doctrine (D-036), and the cases it must not fire on are real
# paragraphs a Marine is expected to know.

# MCDP 2, ch 3 -- the bibliography that produced "What are the principal sources utilized
# in this case study?", the clearest single item in the whole rejected set.
BIBLIOGRAPHY = (
    "Principal sources used in this case study: Conduct of the Persian Gulf War: Final "
    "Report to Congress (Washington, D.C.: Department of Defense, April 1992); Col "
    "Charles J. Quilter, II, U.S. Marines in the Persian Gulf 1990-1991: With the I "
    "Marine Expeditionary Force in Desert Shield and Desert Storm (Washington, D.C.: "
    "Headquarters, U.S. Marine Corps, History and Museums Division, 1993)")

# MCDP 7, ch 4 -- an endnote the chunker glued onto the head of the quotation it cites.
# This is the shape D-036 blames for 3 of MCDP 7's 13 items.
IBID_ENDNOTE = (
    '7. Ibid., p. 1-6. "All actions in war take place in an atmosphere of uncertainty, '
    'or the fog of war. Uncertainty pervades battle in the form of unknowns about the '
    'enemy, about the environment, and even about the friendly situation."')

# MCDP 7, ch 4 -- same defect, but the only apparatus is the numbered marker and the page
# reference. No Latin shorthand and no imprint, so this is what REF_ENDNOTE is for.
PAGE_CITE_ENDNOTE = (
    '6. MCDP 1, pp. 4-21 to 4-22. "Another important tool for providing unity is the '
    'main effort. Of all the actions going on within our command, we recognize one as '
    'the most critical to success at that moment."')

# MCDP 7, ch 4 -- apparatus carried entirely by the publisher imprint.
IMPRINT_ENDNOTE = (
    "7. Malcolm Shepherd Knowles, Self-Directed Learning (New York: Cambridge Books, "
    "1975). 8. MCDP 1, p. 4-18.")

# MCDP 1, "Friction" -- ordinary doctrine prose, the volume case.
DOCTRINE = (
    "Friction may be mental, as in indecision over a course of action. It may be "
    "physical, as in effective enemy fire or a terrain obstacle that must be overcome. "
    "Friction may be external, imposed by enemy action, the terrain, weather, or mere "
    "chance. Whatever form it takes, because war is a human enterprise, friction will "
    "always have a psychological as well as a physical impact.")

# TC 3-22.9, appendix C -- the ONE false positive D-036 measured. "C-11." is an Army
# paragraph number, not citation 11. TC 3-22.9 opens 486 paragraphs this way and
# MCWP 3-11.3 is numbered the same, so a rule that reads these as citations is not a
# near miss, it is unusable.
ARMY_PARA_C11 = (
    "C-11. Moving targets are those threats that appear to have a consistent pace and "
    "direction. Targets on any battlefield will not remain stationary for long periods "
    "of time, particularly once a firefight begins. Soldiers must have the ability to "
    "deliver lethal fires at a variety of moving target types.")

# TC 3-22.9, ch 3 -- the same numbering, and it also carries a page pointer, which is the
# combination most likely to be misread as "numbered citation followed by a page".
ARMY_PARA_367 = (
    "3-67. The AN/PEQ-15A, DBAL-A2 visible aiming laser provides for active target "
    "acquisition in low light conditions and close-quarters combat situations. The "
    "following information is an extract from the equipment's technical manual for "
    "Soldier reference (see figure 3-17 on page 3-27).")

# TCCC, Analgesia -- the Combat Wound Medication Pack. Digit-dense, abbreviation-dense,
# and exactly the paragraph the refuted fuzzy score flagged. A Marine has to know it
# cold, so a check that touches it is worse than no check.
MEDICATION_LIST = (
    "a. Casualty can stay in the fight/mission capable: 1. Analgesia is "
    "self-administered or co-administered by TCCC Personnel. 2. TCCC Combat Wound "
    "Medication Pack (CWMP) - Acetaminophen 1000mg - 1300mg (e.g., two 650mg "
    "extended-release caplets) PO every 8 hours - Meloxicam 15 mg PO once a day")

# TC 3-22.9, appendix C -- the downrange wind-indicator list, the second thing the
# refuted fuzzy score flagged. Army-numbered AND a table of numbers.
WIND_TABLE = (
    "C-28. Downrange wind indicators include the following: 0 to 3 mph = Hardly felt, "
    "but smoke drifts. 3 to 5 mph = Felt lightly on the face. 5 to 8 mph = Keeps leaves "
    "in constant movement. 8 to 12 mph = Raises dust and loose paper. 12 to 15 mph = "
    "Causes small trees to sway.")

# MCWP 3-11.3 -- a numbered land-navigation procedure. Structurally a run of numbered
# markers, which is why "runs of numbers" alone was not made a signal.
PROCEDURE_LIST = (
    "1. Move the compass so that the desired azimuth on the dial is directly under the "
    "index line on the lower glass. 2. Rotate the upper movable glass so the luminous "
    "line is over the north arrow. 3. Turn the compass until the north arrow is under "
    "the luminous line.")


def source_item(source_text):
    """An item whose SOURCE is what is under test.

    The key point list stays EMPTY on purpose. precheck() guards the embedding call with
    `if kps and src`, so an empty list closes that branch even though source_text is now
    populated -- the same offline guarantee the rest of this module gets from an empty
    source_text (see the module docstring). The source check is pure, so it still runs.

    An empty key-point list also raises the SHAPE "no key points" flag. That is expected
    and irrelevant here: every assertion below is about SOURCE_NOT_TEACHABLE specifically,
    never about the flag list being empty.
    """
    return item("Describe what this paragraph establishes about the conduct of war",
                [], source_text)


def test_bibliography_source_is_flagged():
    # The plainest case in the rejected set, and the one a reviewer can adjudicate in a
    # second: a list of books is not something to be examined on.
    flags = review.precheck(source_item(BIBLIOGRAPHY))
    assert "SOURCE_NOT_TEACHABLE" in codes(flags)


def test_ibid_endnote_source_is_flagged():
    # "Ibid." belongs to an apparatus, never to doctrine prose.
    flags = review.precheck(source_item(IBID_ENDNOTE))
    assert "SOURCE_NOT_TEACHABLE" in codes(flags)


def test_numbered_endnote_with_a_page_reference_is_flagged():
    # No "Ibid.", no imprint -- the only apparatus is "6. MCDP 1, pp. 4-21". Without this
    # pattern three of MCDP 7's endnote-headed chunks go unflagged, and those are the
    # items that cite MCDP 7 for words that live in MCDP 1 (D-036).
    flags = review.precheck(source_item(PAGE_CITE_ENDNOTE))
    assert "SOURCE_NOT_TEACHABLE" in codes(flags)


def test_publisher_imprint_alone_is_enough():
    # "(New York: Cambridge Books, 1975)" -- city, publisher, year inside one bracket. A
    # colon and a four-digit year together is what makes it an imprint rather than an
    # aside; over the whole corpus the pattern matched 168 distinct strings and all 168
    # were real imprints.
    flags = review.precheck(source_item(IMPRINT_ENDNOTE))
    assert "SOURCE_NOT_TEACHABLE" in codes(flags)


def test_detail_names_the_apparatus_that_matched():
    # The reviewer has to see WHY the source was flagged without reopening the paragraph
    # -- the same contract the ANSWER_LEAKED detail keeps.
    flags = review.precheck(source_item(IBID_ENDNOTE))
    hit = [f for f in flags if f["code"] == "SOURCE_NOT_TEACHABLE"]
    assert len(hit) == 1
    assert "Ibid." in hit[0]["detail"]


def test_ordinary_doctrine_paragraph_is_not_flagged():
    # The volume case. 999 of 1,011 sources are like this one and must stay silent, or
    # bulk_decide_unflagged() stops clearing anything and the reviewer reads all of them.
    flags = review.precheck(source_item(DOCTRINE))
    assert "SOURCE_NOT_TEACHABLE" not in codes(flags)


def test_army_numbered_paragraph_is_not_read_as_a_citation():
    # THE regression test for this check. "C-11." is a paragraph number in TC 3-22.9, not
    # citation 11, and it was the single false positive D-036 measured across 1,011 items.
    # TC 3-22.9 opens 486 paragraphs this way and MCWP 3-11.3 numbers the same, so a rule
    # that trips here does not have a false-positive problem, it has no future.
    flags = review.precheck(source_item(ARMY_PARA_C11))
    assert "SOURCE_NOT_TEACHABLE" not in codes(flags)


def test_army_numbered_paragraph_carrying_a_page_pointer_is_not_flagged():
    # The hard half of the same case: "3-67." plus "on page 3-27" is a numbered label
    # followed by a page reference, which is the literal description of the endnote
    # pattern. It stays clean because the marker's digits follow a hyphen, and because
    # the pattern wants "p." or "pp.", not the word "page".
    flags = review.precheck(source_item(ARMY_PARA_367))
    assert "SOURCE_NOT_TEACHABLE" not in codes(flags)


def test_medication_dosing_list_is_not_flagged():
    # The TCCC Combat Wound Medication Pack. The refuted fuzzy score in D-036 flagged
    # this on digit density; it is dense because dosages are dense, and a Marine should
    # be drilled on it. Deleting real doctrine is the failure mode this check must avoid.
    flags = review.precheck(source_item(MEDICATION_LIST))
    assert "SOURCE_NOT_TEACHABLE" not in codes(flags)


def test_wind_speed_table_is_not_flagged():
    # The downrange wind-indicator list, also flagged by the refuted fuzzy score. It is a
    # table of numbers AND an Army-numbered paragraph -- both of the things this check
    # deliberately declines to reason about.
    flags = review.precheck(source_item(WIND_TABLE))
    assert "SOURCE_NOT_TEACHABLE" not in codes(flags)


def test_numbered_procedure_list_is_not_flagged():
    # "1. ... 2. ... 3. ..." is a citation run and a land-navigation drill alike. 17 TCCC
    # chunks and 9 MCWP 3-11.3 chunks open that way, so a bare run-of-numbers signal was
    # left out and the page reference was made mandatory instead.
    flags = review.precheck(source_item(PROCEDURE_LIST))
    assert "SOURCE_NOT_TEACHABLE" not in codes(flags)


def test_source_check_never_reaches_the_embedding_server():
    # This check has to work on an air-gapped device with no model server running, so it
    # is pure text like SHAPE and ANSWER_LEAKED. Pointing at a dead port proves it: if it
    # ever grew an embedding call, PRECHECK_FAILED would appear here.
    flags = review.precheck(source_item(BIBLIOGRAPHY), embed_url="http://127.0.0.1:1")
    assert "SOURCE_NOT_TEACHABLE" in codes(flags)
    assert "PRECHECK_FAILED" not in codes(flags)


def test_reference_apparatus_returns_the_match_and_none_for_clean_text():
    # The helper is what the detail string is built from, so it returns text rather than
    # a bool. Empty and None sources must be silent -- most of this suite passes "".
    assert review._reference_apparatus(IBID_ENDNOTE) == "Ibid."
    assert review._reference_apparatus(DOCTRINE) is None
    assert review._reference_apparatus("") is None
    assert review._reference_apparatus(None) is None
