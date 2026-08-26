"""Unit tests for the DIRECTIONAL grounding gate (grounding_verifier.py) -- the entailment
check that replaces embedding-cosine grounding as an abstention signal.

The bug this gate exists to fix, guarded directly below: cosine similarity cannot tell a
faithful paraphrase from a confident CONTRADICTION. "six phases" vs "seven phases" is ~0.98
cosine and factually opposite. So the gate must ask DIRECTIONAL SUPPORT (does the source
entail the claim?), not similarity, and must fail BOTH a contradiction and a claim the
source is merely SILENT about (neutral / not-supported) -- silence is not support.

Pure-logic, fully offline: no model server, no sockets, no device, no GPU. The resident 2B
is INJECTED as a callable, so the suite passes a STUB judge -- "SUPPORTED" when the claim's
key term appears verbatim in the spans, "NOT_SUPPORTED" otherwise. That stub stands in for
the one strict grounded-NLI generation the real 2B backend makes; nothing here loads a model
or opens a socket, and the optional HHEM server (torch/transformers) is never touched.

The claims under guard, mirroring the task contract:

  1. verify() maps the three labels correctly: SUPPORTED -> 1.0, NOT_SUPPORTED -> 0.0,
     CONTRADICTED -> 0.0 (both failure modes fail).
  2. gate_answer REFUSES when a factual sentence is unsupported, and PASSES when all are.
  3. The silent-source case (NOT_SUPPORTED) fails the gate exactly like a contradiction.
  4. The sentence splitter and the ROBUST label parse ("not_supported.", "The answer is
     CONTRADICTED", trailing clauses) behave.
"""
import re

import grounding_verifier
from grounding_verifier import GroundingVerifier, gate_answer


# ---------------------------------------------------------------------------
# Stub judge -- stands in for one strict grounded-NLI pass against the resident 2B.
# ---------------------------------------------------------------------------

def _grounded_ask(spans):
    """An ask_model(prompt) stub: SUPPORTED iff the CLAIM shares a key term with the spans.

    This models directional entailment offline: the real 2B is shown the numbered passages
    plus the claim and returns a label; here the stub pulls the CLAIM back out of the prompt
    _build_prompt built and answers SUPPORTED only when a distinctive claim word (>=5 chars)
    actually appears in the spans -- i.e. the source contains the claim's key term. A
    fabricated sentence whose terms are absent from the spans gets NOT_SUPPORTED, which is
    exactly the silent-source failure the gate must catch. Deterministic, no model, no
    socket.
    """
    spans_text = " ".join(spans).lower()

    def ask(prompt):
        m = re.search(r"CLAIM:\s*(.+)", prompt)          # claim is a single line
        claim = m.group(1).strip() if m else ""
        words = [w for w in re.findall(r"[a-z]+", claim.lower()) if len(w) >= 5]
        return "SUPPORTED" if any(w in spans_text for w in words) else "NOT_SUPPORTED"

    return ask


# ---------------------------------------------------------------------------
# 1. verify() maps the three labels correctly
# ---------------------------------------------------------------------------

def test_verify_supported_maps_to_one():
    v = GroundingVerifier(ask_model=lambda p: "SUPPORTED")
    assert v.verify("any claim", ["some passage"]) == 1.0


def test_verify_not_supported_maps_to_zero():
    v = GroundingVerifier(ask_model=lambda p: "NOT_SUPPORTED")
    assert v.verify("any claim", ["some passage"]) == 0.0


def test_verify_contradicted_maps_to_zero():
    # CONTRADICTED must fail just as hard as NOT_SUPPORTED -- both are 0.0.
    v = GroundingVerifier(ask_model=lambda p: "CONTRADICTED")
    assert v.verify("any claim", ["some passage"]) == 0.0


def test_verify_unparseable_reply_fails_closed():
    # A judge reply the parser cannot recognise is treated as NOT_SUPPORTED (0.0):
    # an answer that could not be clearly grounded is not one to trust.
    v = GroundingVerifier(ask_model=lambda p: "hmm, maybe?")
    assert v.verify("any claim", ["some passage"]) == 0.0


def test_verify_stub_keys_off_the_spans_in_the_prompt():
    v = GroundingVerifier(ask_model=_grounded_ask(["Surgical cricothyroidotomy cannula: 6.0 mm cuffed tube."]))
    assert v.verify("A cricothyroidotomy uses a 6.0 mm tube.",
                    ["Surgical cricothyroidotomy cannula: 6.0 mm cuffed tube."]) == 1.0
    assert v.verify("The tourniquet goes high and tight.",
                    ["Surgical cricothyroidotomy cannula: 6.0 mm cuffed tube."]) == 0.0


def test_verify_requires_a_backend():
    # No hhem_url and no ask_model is a misconfiguration, not a silent pass.
    import pytest
    v = GroundingVerifier()
    with pytest.raises(RuntimeError):
        v.verify("claim", ["span"])


# ---------------------------------------------------------------------------
# 2 & 3. gate_answer: refuse on unsupported, pass when all supported, and the
#         SILENT-source (not-supported) case fails the gate like a contradiction.
# ---------------------------------------------------------------------------

_SPANS = ["The Marine Corps Planning Process has six phases.",
          "Tactical Combat Casualty Care divides care into three phases."]


def test_gate_passes_when_every_sentence_is_supported():
    # Both sentences' key terms are present -> all SUPPORTED -> no refusal.
    v = GroundingVerifier(ask_model=_grounded_ask(_SPANS))
    sentences = ["The Marine Corps Planning Process has six phases.",
                 "Tactical Combat Casualty Care divides care into three phases."]
    r = gate_answer(sentences, _SPANS, v, tau=0.5)
    assert r["refuse"] is False
    assert r["min_support"] == 1.0
    assert r["unsupported"] == []


def test_gate_refuses_when_a_sentence_is_unsupported():
    # Second sentence fabricates a term absent from the spans -> NOT_SUPPORTED -> refuse.
    v = GroundingVerifier(ask_model=_grounded_ask(_SPANS))
    sentences = ["The Marine Corps Planning Process has six phases.",
                 "The commander personally approves each fires mission."]
    r = gate_answer(sentences, _SPANS, v, tau=0.5)
    assert r["refuse"] is True
    assert r["min_support"] == 0.0
    assert [u["sentence"] for u in r["unsupported"]] == \
        ["The commander personally approves each fires mission."]


def test_silent_source_fails_gate_like_a_contradiction():
    # A fabrication the source is SILENT about (NOT_SUPPORTED) must fail the gate exactly
    # like a contradiction. This is the whole reason for a directional check: the sentence
    # is fluent, plausible, and never stated by the passages.
    silent = GroundingVerifier(ask_model=lambda p: "NOT_SUPPORTED")
    contradicted = GroundingVerifier(ask_model=lambda p: "CONTRADICTED")
    sentence = ["The planning process has seven phases, added in the latest edition."]
    r_silent = gate_answer(sentence, _SPANS, silent, tau=0.5)
    r_contra = gate_answer(sentence, _SPANS, contradicted, tau=0.5)
    assert r_silent["refuse"] is True
    assert r_contra["refuse"] is True
    # Identical treatment: both are min_support 0.0 and both refuse.
    assert r_silent["min_support"] == r_contra["min_support"] == 0.0


def test_gate_ignores_non_factual_sentences():
    # Short fragments that would never carry a citation are not verified, and an answer
    # made only of them is vacuously grounded -- the gate never invents a refusal.
    v = GroundingVerifier(ask_model=lambda p: "NOT_SUPPORTED")
    r = gate_answer(["Yes.", "See below."], _SPANS, v, tau=0.5)
    assert r["refuse"] is False
    assert r["unsupported"] == []


def test_gate_refuses_on_the_lowest_support_sentence():
    # refuse keys off the load-bearing (lowest-support) sentence: one bad claim among good
    # ones still triggers refusal, and it is the one reported unsupported.
    v = GroundingVerifier(ask_model=_grounded_ask(_SPANS))
    sentences = ["The Marine Corps Planning Process has six phases.",
                 "An entirely invented sentence with no matching source term at all."]
    r = gate_answer(sentences, _SPANS, v, tau=0.5)
    assert r["refuse"] is True
    assert r["min_support"] == 0.0
    assert len(r["unsupported"]) == 1


# ---------------------------------------------------------------------------
# 4. The sentence splitter and the robust label parse.
# ---------------------------------------------------------------------------

def test_split_sentences_pipeline_style():
    text = ("The process has six phases. Each phase has a named output! "
            "Is that clear? Yes.")
    parts = grounding_verifier.split_sentences(text)
    assert parts == ["The process has six phases.",
                     "Each phase has a named output!",
                     "Is that clear?",
                     "Yes."]


def test_split_sentences_empty():
    assert grounding_verifier.split_sentences("") == []
    assert grounding_verifier.split_sentences(None) == []


def test_is_factual_length_rule():
    assert grounding_verifier.is_factual(
        "The planning process has six named phases.") is True
    assert grounding_verifier.is_factual("Yes.") is False


def test_parse_label_bare_tokens():
    assert grounding_verifier.parse_label("SUPPORTED") == "SUPPORTED"
    assert grounding_verifier.parse_label("NOT_SUPPORTED") == "NOT_SUPPORTED"
    assert grounding_verifier.parse_label("CONTRADICTED") == "CONTRADICTED"


def test_parse_label_is_robust_to_case_and_punctuation_and_prefixes():
    # Lowercase + trailing punctuation.
    assert grounding_verifier.parse_label("not_supported.") == "NOT_SUPPORTED"
    # A prefixed clause -- first RECOGNISED label wins, not the first token.
    assert grounding_verifier.parse_label("The answer is CONTRADICTED") == "CONTRADICTED"
    # "supported" as a substring of "not supported" must NOT win.
    assert grounding_verifier.parse_label("not supported by the passages") == "NOT_SUPPORTED"
    # "UNSUPPORTED" variant.
    assert grounding_verifier.parse_label("Unsupported.") == "NOT_SUPPORTED"
    # A trailing justification clause after SUPPORTED still parses SUPPORTED.
    assert grounding_verifier.parse_label(
        "SUPPORTED - passage [1] states it verbatim.") == "SUPPORTED"


def test_parse_label_fails_closed_on_garbage():
    assert grounding_verifier.parse_label("banana") == "NOT_SUPPORTED"
    assert grounding_verifier.parse_label("") == "NOT_SUPPORTED"
    assert grounding_verifier.parse_label(None) == "NOT_SUPPORTED"


# ---------------------------------------------------------------------------
# The prompt is one cheap pass: built once per claim, showing numbered passages.
# ---------------------------------------------------------------------------

def test_prompt_numbers_passages_and_includes_claim():
    prompt = GroundingVerifier._build_prompt(
        "The process has six phases.",
        ["First passage text.", "Second passage text."])
    assert "[1] First passage text." in prompt
    assert "[2] Second passage text." in prompt
    assert "The process has six phases." in prompt
    # One decision, forced to a single label -- the mechanism that keeps it one cheap pass.
    assert "EXACTLY ONE" in prompt
