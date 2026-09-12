"""Integration tests for the CITE-OR-REFUSE answer gate (Gate 2b in pipeline.ask()).

SYSTEM_PROMPT rule 2 and the README both promise that every factual sentence carries a
citation. Before this gate that was only an INSTRUCTION to the model: a fluent, plausible,
UNCITED answer was returned with abstained:false. This gate makes the promise structural --
if the generated answer asserts factual sentences but carries ZERO valid in-range [N]
citation markers, the pipeline refuses it as `uncited_answer` instead of shipping it.

Pure-logic, fully offline, in the shape of test_abstention_fallback.py: hybrid.connect is
patched (the real one loads sqlite_vec, which conftest stubs to raise), and retrieve()/
generate() are replaced so no socket is opened. The gate reads only the markers already in
the answer text, so it needs no embedding and no verifier -- which is the whole point: it
is the free floor that holds even with grounding.enabled False.

Claims under guard:
  1. An uncited factual answer is REFUSED (the core fix), with abstained:true.
  2. A properly cited answer is NOT refused and reports its citations.
  3. The gate is INDEPENDENT of grounding.enabled -- it fires with grounding off AND on.
  4. Only VALID IN-RANGE markers count: an out-of-range [9] on a 1-chunk answer is no
     citation, so the answer is still refused.
  5. A non-factual answer (no load-bearing sentence) is NOT refused -- the gate never
     manufactures a refusal out of a fragment.
  6. The model's own INSUFFICIENT_SOURCES still short-circuits earlier and is unaffected.
"""
import pipeline


def _chunk(n=1, citation="MCDP 1 | Warfighting | p.5", pub_id="MCDP 1", page="5",
           text="Friction is the force that makes the easy difficult."):
    return {"rerank_score": 9.0, "bm25_score": -1.0, "bm25_rank": 1,
            "pub_id": pub_id, "citation": citation, "page_printed": page, "text": text}


def _make_pipeline(monkeypatch, answer, chunks=None, grounding_enabled=False):
    """A TutorPipeline with network + index stubbed, retrieval score gate open.

    reranker_score_threshold None keeps Gate 1 from refusing (chunks are present), and the
    premise gate is off, so a query lands squarely on Gate 2b with whatever `answer` the
    stubbed generator returns. When grounding is enabled, embed_many is stubbed too so the
    cosine pass never reaches a socket -- it does not affect the cite-or-refuse decision
    (sentence_support_threshold stays null) but it must not open a connection.
    """
    monkeypatch.setattr(pipeline.hybrid, "connect", lambda path: object())
    cfg = {
        "index": {"path": ":memory:"},
        "abstention": {"reranker_score_threshold": None, "refusal_text": "REFUSED"},
        "grounding": {"enabled": grounding_enabled, "sentence_support_threshold": None},
        "premise_gate": {"enabled": False},
    }
    p = pipeline.TutorPipeline(cfg)
    p.retrieve = lambda q: (chunks if chunks is not None else [_chunk()])
    p.generate = lambda q, c: answer
    p.embed_many = lambda texts: [[1.0, 0.0] for _ in texts]
    return p


# 1. The core fix: an uncited factual answer is refused. ----------------------

def test_uncited_factual_answer_is_refused(monkeypatch):
    # A fluent, plausible, well-formed answer with NO citation marker at all. Pre-fix this
    # shipped with abstained:false; it must now be refused as uncited_answer.
    answer = ("Friction is the force that makes the apparently easy so difficult, "
              "and it resists all action and saps energy.")
    r = _make_pipeline(monkeypatch, answer).ask("What is friction?")
    assert r["abstained"] is True
    assert r["abstain_reason"] == "uncited_answer"
    assert r["text"] == "REFUSED"
    assert r["citations"] == []


def test_uncited_multi_sentence_answer_is_refused(monkeypatch):
    answer = ("Maneuver warfare seeks to shatter the enemy's cohesion. "
              "It targets the enemy system rather than its forces piecemeal.")
    r = _make_pipeline(monkeypatch, answer).ask("What is maneuver warfare?")
    assert r["abstained"] is True
    assert r["abstain_reason"] == "uncited_answer"


# 2. A cited answer is NOT refused. -------------------------------------------

def test_cited_answer_is_not_refused(monkeypatch):
    answer = ("Friction is the force that makes the apparently easy so difficult, "
              "and it resists all action and saps energy. [1]")
    r = _make_pipeline(monkeypatch, answer).ask("What is friction?")
    assert r["abstained"] is False
    assert r["abstain_reason"] is None
    assert [c["n"] for c in r["citations"]] == [1]
    assert r["text"] == answer


# 3. Independent of grounding.enabled. ----------------------------------------

def test_uncited_answer_refused_with_grounding_enabled(monkeypatch):
    # The gate must not depend on the cosine grounding pass being on: it reads markers
    # straight from the text. Same uncited answer, grounding ENABLED -> still refused.
    answer = "Friction resists all action and saps energy across the whole force."
    r = _make_pipeline(monkeypatch, answer, grounding_enabled=True).ask("q")
    assert r["abstained"] is True
    assert r["abstain_reason"] == "uncited_answer"


def test_cited_answer_passes_with_grounding_enabled(monkeypatch):
    answer = "Friction resists all action and saps energy across the whole force. [1]"
    r = _make_pipeline(monkeypatch, answer, grounding_enabled=True).ask("q")
    assert r["abstained"] is False
    assert [c["n"] for c in r["citations"]] == [1]


# 4. Only valid IN-RANGE markers count. ---------------------------------------

def test_out_of_range_marker_only_is_still_refused(monkeypatch):
    # One chunk retrieved, but the answer cites [9]. That marker points at nothing, so it
    # is not a valid citation -- the answer is effectively uncited and must be refused.
    answer = "Friction is the force that makes the apparently easy so difficult. [9]"
    r = _make_pipeline(monkeypatch, answer, chunks=[_chunk()]).ask("q")
    assert r["abstained"] is True
    assert r["abstain_reason"] == "uncited_answer"


def test_mixed_valid_and_invalid_markers_keeps_only_in_range(monkeypatch):
    # Two chunks; answer cites [1] (valid) and [7] (out of range). The valid one saves the
    # answer, and only the in-range citation is reported.
    answer = "The intelligence cycle has phases. [1] It ends with dissemination. [7]"
    r = _make_pipeline(monkeypatch, answer,
                       chunks=[_chunk(), _chunk(citation="MCDP 2 | Intelligence | p.9")]
                       ).ask("q")
    assert r["abstained"] is False
    assert [c["n"] for c in r["citations"]] == [1]


# 5. A non-factual answer is not force-refused. -------------------------------

def test_short_non_factual_answer_is_not_refused(monkeypatch):
    # No sentence long enough to be a load-bearing claim (>25 chars). The gate has no
    # factual sentence to demand a citation for, so it stays silent rather than invent a
    # refusal. (An answer this thin is an upstream problem, not this gate's to flag.)
    r = _make_pipeline(monkeypatch, "Yes.").ask("q")
    assert r["abstained"] is False
    assert r["abstain_reason"] is None


# 6. The model's own refusal is unaffected (it returns earlier). --------------

def test_model_declined_still_takes_precedence(monkeypatch):
    r = _make_pipeline(monkeypatch, "INSUFFICIENT_SOURCES").ask("q")
    assert r["abstained"] is True
    assert r["abstain_reason"] == "model_declined"
