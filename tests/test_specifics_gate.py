"""Unit tests for the verbatim-specific backstop (specifics_gate.py).

The gate refuses ONLY when a fabricated specific is LOAD-BEARING -- the question asked for
a specific of that kind and the answer supplied an unsupported one. Two obligations pull in
opposite directions, and both are guarded here:

  POSITIVE  -- a fabricated year/page/name that the question asked for MUST refuse.
  NEGATIVE  -- a supported specific, or an unsupported one the question never asked for,
               MUST NOT refuse. These are the over-refusal guards, and there are more of
               them than positives on purpose: over-refusal is the failure we most fear.

Plus direct unit tests of extract_specifics and the number-word normalization, since the
whole gate rides on "seven" == 7 and "35-36" ~ "p. 35" folding correctly.

Pure-logic, fully offline: no model server, no sockets, no device. The module has no
cross-module imports; conftest already puts src/generation on the path.
"""
import specifics_gate as sg


# ---------------------------------------------------------------------------
# Realistic doctrine spans (correct, on-topic retrieval -- the fabrication is never here)
# ---------------------------------------------------------------------------
PHASES_SPAN = (
    "Marine Corps planning is built on the six phases of the Marine Corps Planning "
    "Process: problem framing, course of action development, course of action war-gaming, "
    "course of action comparison and decision, orders development, and transition.")

TCCC_SPAN = (
    "Tactical Combat Casualty Care divides care into three phases: Care Under Fire, "
    "Tactical Field Care, and Tactical Evacuation Care. For pain in a casualty still able "
    "to fight, the guidelines recommend meloxicam and acetaminophen from the combat pill "
    "pack.")

WARFIGHTING_SPAN = (
    "Warfighting describes the philosophy of maneuver warfare and the central role of "
    "boldness and the commander's intent in the conduct of war. It emphasizes tempo and "
    "the exploitation of fleeting opportunity.")


# ===========================================================================
# POSITIVE: a load-bearing fabricated specific must refuse
# ===========================================================================
def test_fabricated_year_and_drug_on_false_premise_refuses():
    """'the 2011 TCCC Guidelines ... Suzetrigine, 100 mg PO' -- the year is fabricated.

    Spans are the correct TCCC pain passage; they never say 2011, suzetrigine, or 100.
    The question asserts a 2011 edition, so the unsupported YEAR is load-bearing.
    """
    answer = ("According to the 2011 TCCC Guidelines, the recommended analgesic is "
              "Suzetrigine, 100 mg PO. [1]")
    question = "What does the 2011 edition of the TCCC Guidelines say to give for pain?"
    v = sg.specifics_verdict(answer, question, [TCCC_SPAN])
    assert v["refuse"] is True
    assert any(s["kind"] == "year" and s["raw"] == "2011" for s in v["unsupported"])
    assert "2011" in v["reason"]


def test_fabricated_page_reference_refuses():
    """'on pages 35-36' with spans that carry no such page, and the question asks a page."""
    answer = "The maneuver warfare philosophy is laid out on pages 35-36. [1]"
    question = "On what page does Warfighting define maneuver warfare?"
    v = sg.specifics_verdict(answer, question, [WARFIGHTING_SPAN])
    assert v["refuse"] is True
    assert any(s["kind"] == "page" for s in v["unsupported"])


def test_misattributed_quote_to_a_name_refuses():
    """A quote credited to 'Alfred M. Gray' when no span makes that attribution."""
    answer = ('The dictum "the enemy gets a vote" is credited to Alfred M. Gray. [1]')
    question = "Who is credited with the idea that the enemy gets a vote?"
    v = sg.specifics_verdict(answer, question, [WARFIGHTING_SPAN])
    assert v["refuse"] is True
    assert any(s["kind"] == "proper_noun" for s in v["unsupported"])


def test_false_count_refuses_when_span_has_the_true_count():
    """'seven phases' asserted; the span plainly says six. The question asked how many."""
    answer = "The Marine Corps Planning Process consists of seven phases. [1]"
    question = "How many phases are in the Marine Corps Planning Process?"
    v = sg.specifics_verdict(answer, question, [PHASES_SPAN])
    assert v["refuse"] is True
    assert any(s["kind"] == "cardinal" and s["raw"] == "seven" for s in v["unsupported"])


# ===========================================================================
# NEGATIVE: over-refusal guards -- these must NOT refuse
# ===========================================================================
def test_true_count_present_in_span_does_not_refuse():
    """'six phases' is right there in the span -> supported -> silence."""
    answer = "The Marine Corps Planning Process has six phases. [1]"
    question = "How many phases are in the Marine Corps Planning Process?"
    v = sg.specifics_verdict(answer, question, [PHASES_SPAN])
    assert v["refuse"] is False
    assert v["unsupported"] == []


def test_digit_in_answer_matches_number_word_in_span():
    """Answer says '6', span says 'six'. Normalization must fold them; no refusal."""
    answer = "The process has 6 phases. [1]"
    question = "How many phases does the planning process have?"
    v = sg.specifics_verdict(answer, question, [PHASES_SPAN])
    assert v["refuse"] is False
    # And prove the fold directly: the digit '6' is supported by the word 'six'.
    assert sg.unsupported_specifics("There are 6 phases.", [PHASES_SPAN]) == []


def test_word_in_answer_matches_digit_in_span():
    """The mirror case: answer spells 'three', span uses the digit 3."""
    span = "The manual lists 3 phases of care."
    answer = "There are three phases of care. [1]"
    assert sg.unsupported_specifics(answer, [span]) == []


def test_page_range_and_single_page_fold_together():
    """A span citing 'p. 35' supports an answer citing 'pages 35-36' and vice-versa."""
    span = "Maneuver warfare is defined on p. 35 of the publication."
    answer = "See pages 35-36 for the definition. [1]"
    v = sg.specifics_verdict(answer, "On what page is it defined?", [span])
    assert v["refuse"] is False
    # dash variants and the 'p.35' family all fold
    assert sg.unsupported_specifics("See page 35.", ["... pages 35–36 ..."]) == []


def test_unsupported_specific_not_asked_for_does_not_refuse():
    """An answer carries an unsupported name, but the question never asks who -- silence.

    The name IS reported in `unsupported` (transparency) but is not load-bearing, so the
    gate does not refuse. This is the core over-refusal guard.
    """
    answer = ("Maneuver warfare stresses tempo and boldness, a point echoed by "
              "Alfred M. Gray. [1]")
    question = "What does Warfighting say maneuver warfare stresses?"
    v = sg.specifics_verdict(answer, question, [WARFIGHTING_SPAN])
    assert v["refuse"] is False
    assert any(s["kind"] == "proper_noun" for s in v["unsupported"])  # reported...
    assert v["unsupported"]                                            # ...but not fatal


def test_supported_name_attribution_does_not_refuse():
    """When the span DOES make the attribution, a who-question must not refuse."""
    span = ("General Alfred M. Gray, as Commandant, signed Warfighting and championed "
            "maneuver warfare doctrine.")
    answer = "Warfighting was signed by Alfred M. Gray. [1]"
    v = sg.specifics_verdict(answer, "Who signed Warfighting?", [span])
    assert v["refuse"] is False


def test_answer_with_no_specifics_does_not_refuse():
    answer = "Maneuver warfare emphasizes tempo and the exploitation of opportunity. [1]"
    v = sg.specifics_verdict(answer, "What does maneuver warfare emphasize?",
                             [WARFIGHTING_SPAN])
    assert v["refuse"] is False
    assert v["unsupported"] == []


# ===========================================================================
# UNIT: extract_specifics and normalization
# ===========================================================================
def test_extract_kinds_are_classified():
    specs = sg.extract_specifics(
        "The 2011 edition lists the third of seven steps on pages 35-36, per J. Gray.")
    kinds = {s["kind"] for s in specs}
    assert "year" in kinds
    assert "ordinal" in kinds
    assert "cardinal" in kinds
    assert "page" in kinds
    # A page's digits are NOT re-extracted as bare cardinals.
    cardinals = {s["raw"] for s in specs if s["kind"] == "cardinal"}
    assert "35" not in cardinals and "36" not in cardinals
    # 2011 is a year, not a cardinal.
    assert "2011" not in cardinals


def test_number_word_normalization_folds_to_digits():
    def norms(text):
        s = sg.extract_specifics(text)
        return s[0]["norms"] if s else []

    assert "7" in norms("seven")
    assert "seven" in norms("7")
    assert "21" in norms("twenty-one")
    assert "100" in norms("one hundred")
    # 'seven and eight' must read as two numbers, not fifteen.
    seven_eight = sg.extract_specifics("seven and eight")
    values = {n for s in seven_eight for n in s["norms"] if n.isdigit()}
    assert values == {"7", "8"}


def test_ordinal_digit_and_word_fold():
    span = ["The third phase begins evacuation."]
    # answer uses the digit ordinal; span uses the word ordinal
    assert sg.unsupported_specifics("Evacuation is the 3rd phase.", span) == []


def test_proper_noun_is_conservative_multiword_only():
    # A lone capitalized word (even sentence-initial) is never a name.
    assert not [s for s in sg.extract_specifics("Warfighting is a doctrine publication.")
                if s["kind"] == "proper_noun"]
    # Common org phrases are stoplisted out.
    assert not [s for s in sg.extract_specifics("The Marine Corps issued the manual.")
                if s["kind"] == "proper_noun"]
    # A real multi-word name survives, and exposes its surname as a norm.
    names = [s for s in sg.extract_specifics("This was written by Alfred M. Gray today.")
             if s["kind"] == "proper_noun"]
    assert len(names) == 1
    assert "gray" in names[0]["norms"]


def test_surname_alone_in_span_supports_full_name():
    """Aggressive support: a bare surname in a span suffices (fewer refusals, by design)."""
    span = ["Gray, as Commandant, signed the publication."]
    assert sg.unsupported_specifics("Signed by Alfred M. Gray.", span) == []


def test_unsupported_specifics_isolates_only_the_missing_one():
    """Mixed answer: the true count is supported, the fabricated year is not."""
    answer = "The six phases were codified in the 1999 edition."
    unsup = sg.unsupported_specifics(answer, [PHASES_SPAN])
    kinds = {s["kind"] for s in unsup}
    assert kinds == {"year"}          # six is supported; 1999 is not
    assert unsup[0]["raw"] == "1999"
