"""Unit tests for the premise pre-gate -- Part C, the third gate that judges the QUESTION.

Gates 1 (reranker) and 2 (sentence grounding) in pipeline.py both inspect the ANSWER, so
neither can see a false premise smuggled into the question ("What are the SEVEN phases of
the intelligence cycle?" -- it has six). The pre-gate extracts each factual presupposition
as an isolated declarative and verifies THAT against the passages, the regime (QA)^2 (Kim
et al., 2021) found a small model can actually get right.

Pure-logic, fully offline: no model server, no sockets, no device, no GPU, no vector
index. The grounded verifier is INJECTED (verify_fn), so every test here stubs it -- the
module owns the linguistics of "what did the question presuppose", nothing else.

The two properties under guard, mirroring the task contract:

  1. HIGH-PRECISION EXTRACTION. The three real failures each yield exactly their
     load-bearing presupposition (kind + specific), while ordinary answerable questions
     ("What is friction according to MCDP 1?", "Define maneuver warfare.") yield NOTHING.
     Over-refusal is the risk the whole gate is designed against, so the empty case is as
     important as the positive one.
  2. VERDICT. With a stub verifier that scores a false claim low and a true one high,
     premise_verdict refuses the false-premise question and passes the supported one.
"""
import premise_gate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _only(presups):
    """Assert exactly one presupposition was extracted and return it."""
    assert len(presups) == 1, presups
    return presups[0]


def _verifier(supported_specifics, high=0.92, low=0.04):
    """A stub grounded verifier keyed on the load-bearing token.

    Returns `high` when the presupposition's `specific` token is in the supported set (the
    passages attest it) and `low` otherwise. The real device verifier reads the passages;
    here the test states directly which premises the corpus supports.
    """
    def verify_fn(claim, spans):
        return high if any(s in claim for s in supported_specifics) else low
    return verify_fn


# ---------------------------------------------------------------------------
# 1. EXTRACTION -- the three real failures each yield their presupposition
# ---------------------------------------------------------------------------

def test_count_extracts_the_asserted_number():
    # The intelligence cycle has six phases; the question asserts seven. The number is the
    # load-bearing token -- if it is wrong the whole question is a false premise.
    p = _only(premise_gate.extract_presuppositions(
        "What are the SEVEN phases of the intelligence cycle?"))
    assert p["kind"] == "count"
    assert p["specific"] == "seven"
    assert "seven phases" in p["claim"]


def test_year_extracts_the_anachronistic_edition():
    # suzetrigine could not be in a 2011 document. The year is the load-bearing token, and
    # the recovered topic makes the claim about THIS drug, not merely that a 2011 edition
    # exists in the abstract.
    p = _only(premise_gate.extract_presuppositions(
        "According to the 2011 TCCC Guidelines, what is the suzetrigine dose?"))
    assert p["kind"] == "year"
    assert p["specific"] == "2011"
    assert "2011" in p["claim"] and "suzetrigine" in p["claim"]


def test_attribution_extracts_the_credited_person():
    # MCDP 7 credits no such thing to Patton. The proper name is the load-bearing token.
    p = _only(premise_gate.extract_presuppositions(
        "Quote where MCDP 7 credits Patton with the definition of tempo."))
    assert p["kind"] == "attribution"
    assert p["specific"] == "Patton"
    assert "Patton" in p["claim"]


# ---------------------------------------------------------------------------
# 2. OVER-TRIGGERING GUARD -- ordinary answerable questions yield NOTHING
# ---------------------------------------------------------------------------

def test_plain_definition_question_extracts_nothing():
    # The common case. "What is friction according to MCDP 1?" asserts no specific token --
    # MCDP 1 is a document, not a person -- so there is nothing to pre-verify. Firing here
    # would refuse a perfectly answerable question, which is strictly worse than no gate.
    assert premise_gate.extract_presuppositions(
        "What is friction according to MCDP 1?") == []


def test_bare_define_extracts_nothing():
    assert premise_gate.extract_presuppositions("Define maneuver warfare.") == []


def test_open_how_many_is_not_a_false_premise():
    # "How many principles of war are there?" asserts no count -- it ASKS for one -- so it
    # carries no false premise and must not be gated. Only an ASSERTED number ("the seven
    # principles") is load-bearing.
    assert premise_gate.extract_presuppositions(
        "How many principles of war are there?") == []


# ---------------------------------------------------------------------------
# 3. VERIFY -- annotation against the injected verifier
# ---------------------------------------------------------------------------

def test_verify_annotates_support_and_supported():
    presups = premise_gate.extract_presuppositions(
        "What are the six warfighting functions?")
    scored = premise_gate.verify_presuppositions(
        presups, ["... the six warfighting functions ..."],
        _verifier({"six"}), tau=0.5)
    assert scored[0]["support"] == 0.92
    assert scored[0]["supported"] is True
    # The originals are not mutated -- verify returns copies.
    assert "support" not in presups[0]


# ---------------------------------------------------------------------------
# 4. VERDICT -- refuse iff a load-bearing presupposition is unsupported
# ---------------------------------------------------------------------------

def test_verdict_refuses_the_false_premise():
    # Verifier scores "seven" LOW (the passages describe six phases) -> refuse.
    v = premise_gate.premise_verdict(
        "What are the SEVEN phases of the intelligence cycle?",
        ["The intelligence cycle has six phases: planning and direction, collection, ..."],
        _verifier(supported_specifics=set()))   # nothing supported
    assert v["refuse"] is True
    assert len(v["unsupported"]) == 1
    assert v["unsupported"][0]["specific"] == "seven"
    # The hint speaks to UNVERIFIABILITY, never asserts the premise is false.
    assert v["correction_hint"] == \
        "The sources do not support that the intelligence cycle has seven phases."


def test_verdict_passes_a_supported_presupposition():
    # Same shape of question, but now the asserted count IS the doctrinal one. Verifier
    # scores it high -> the premise gate must NOT refuse.
    v = premise_gate.premise_verdict(
        "What are the six warfighting functions?",
        ["The six warfighting functions are command and control, fires, ..."],
        _verifier(supported_specifics={"six"}))
    assert v["refuse"] is False
    assert v["unsupported"] == []
    assert v["correction_hint"] == ""


def test_verdict_never_refuses_a_question_with_no_presupposition():
    # No presupposition extracted => nothing to verify => never refuse, whatever the
    # verifier would have said. This is the guarantee that the gate cannot cause an
    # over-refusal on the common, ordinary question.
    v = premise_gate.premise_verdict(
        "What is friction according to MCDP 1?",
        ["Friction is the force that makes the apparently easy difficult."],
        _verifier(supported_specifics=set()))
    assert v["refuse"] is False
    assert v["unsupported"] == []


def test_verdict_refuses_when_any_one_premise_is_unsupported():
    # A question can carry more than one presupposition; refuse if ANY load-bearing one is
    # unsupported. Here the page reference is attested but the asserted count is not.
    q = "On page 42, what are the seven principles of war?"
    v = premise_gate.premise_verdict(
        q, ["Page 42 lists the nine principles of war."],
        _verifier(supported_specifics={"42"}))   # page supported, count not
    kinds = {p["kind"] for p in premise_gate.extract_presuppositions(q)}
    assert kinds == {"page", "count"}
    assert v["refuse"] is True
    assert {p["kind"] for p in v["unsupported"]} == {"count"}
