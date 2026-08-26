"""Unit tests for the BM25 lexical-match RESCUE -- the secondary abstention signal
that addresses the D-054 remaining limit (terse clinical TCCC text the reranker
underscores below the gate even after per-publication calibration).

Pure-logic, fully offline: no model server, no sockets, no device, no GPU, and no real
vector index (conftest stubs sqlite_vec, whose load() raises if a test opens one). The
network halves of the pipeline and of hybrid.search are monkeypatched, so nothing here
touches a socket.

The four claims under guard, mirroring the task contract:

  1. DISABLED IS A PERFECT NO-OP. threshold null => rescue never fires => the gate is
     byte-for-byte the reranker-only behaviour, whatever the BM25 signal says.
  2. A below-gate reranker score WITH a strong lexical match is RESCUED (answers).
  3. A below-gate score with only a WEAK lexical match is NOT rescued (still refuses).
  4. The rescue ONLY flips refuse -> answer; it can never turn an answer into a refusal.

Plus: hybrid.search SURFACES the BM25 signal without changing result order (so the D-054
calibration offsets, fit against the current retrieval, stay valid).
"""
import pipeline
import hybrid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chunk(rerank_score, bm25_score, pub_id="TCCC", bm25_rank=1,
           citation="TCCC | Tactical Combat Casualty Care | p.14", page="14",
           text="Surgical cricothyroidotomy cannula: 6.0 mm cuffed tube."):
    """A retrieved chunk shaped exactly as hybrid.search now emits it.

    bm25_score is the RAW sqlite bm25() value (lower-is-better; the positive relevance the
    rescue tests is -bm25_score). A strong lexical match is a large-magnitude NEGATIVE
    score, e.g. -12.0 -> relevance 12.0.
    """
    return {"rerank_score": rerank_score, "bm25_score": bm25_score, "bm25_rank": bm25_rank,
            "pub_id": pub_id, "citation": citation, "page_printed": page, "text": text}


def _make_pipeline(monkeypatch, threshold=None, chunks=None,
                   answer="A surgical cricothyroidotomy uses a 6.0 mm cuffed tube. [1]"):
    """A TutorPipeline whose network + index dependencies are stubbed.

    connect() is patched because the real one calls sqlite_vec.load(), which the unit
    suite stubs to raise. retrieve() and generate() are replaced so no socket is opened.
    Grounding is disabled so Gate 2 (which would embed) is never reached -- this suite is
    about Gate 1, where the rescue lives.
    """
    monkeypatch.setattr(hybrid, "connect", lambda path: object())
    cfg = {
        "index": {"path": ":memory:"},
        "abstention": {
            "reranker_score_threshold": 3.0,        # the live gate (D-045)
            "bm25_rescue_threshold": threshold,     # feature under test
            "reranker_score_offsets": {},           # no-op calibration: raw == effective
            "refusal_text": "REFUSED",
        },
        "grounding": {"enabled": False, "sentence_support_threshold": None},
    }
    p = pipeline.TutorPipeline(cfg)
    p.retrieve = lambda q: chunks
    p.generate = lambda q, c: answer
    return p


# ---------------------------------------------------------------------------
# 1. DISABLED IS A PERFECT NO-OP
# ---------------------------------------------------------------------------

def test_disabled_below_gate_still_refuses_even_with_strong_bm25(monkeypatch):
    # The shipped default (threshold None). A below-gate reranker score with a very strong
    # lexical match MUST still refuse -- the feature is off, so nothing about the decision
    # may change from the pre-feature gate.
    chunks = [_chunk(rerank_score=-2.5, bm25_score=-40.0)]   # crushingly strong lexical hit
    r = _make_pipeline(monkeypatch, threshold=None, chunks=chunks).ask("q")
    assert r["abstained"] is True
    assert r["abstain_reason"] == "low_retrieval_score"
    assert r["text"] == "REFUSED"
    assert "bm25_rescued" not in r          # the rescue path was never entered


def test_disabled_above_gate_still_answers(monkeypatch):
    # The other half of the no-op: an above-gate question answers exactly as before, and
    # the disabled rescue leaves no trace on the result.
    chunks = [_chunk(rerank_score=5.0, bm25_score=-1.0)]
    r = _make_pipeline(monkeypatch, threshold=None, chunks=chunks).ask("q")
    assert r["abstained"] is False
    assert "bm25_rescued" not in r


def test_bm25_lexical_rescue_helper_is_noop_when_threshold_none():
    # The helper is the whole no-op guarantee in one line: None short-circuits to False
    # before any chunk is inspected, so a would-be refusal stays a refusal.
    assert pipeline.bm25_lexical_rescue([_chunk(-2.5, -99.0)], None) is False
    assert pipeline.bm25_lexical_rescue([], None) is False


# ---------------------------------------------------------------------------
# 2. STRONG lexical match RESCUES a below-gate reranker score
# ---------------------------------------------------------------------------

def test_below_gate_with_strong_bm25_is_rescued(monkeypatch):
    # The D-054 case: TCCC clinical question, reranker -2.5 (below the 3.0 gate even after
    # a +1.389 offset would apply), but the query terms appear verbatim so BM25 is strong.
    # relevance = -(-12.0) = 12.0 >= 10.0 threshold -> rescue.
    chunks = [_chunk(rerank_score=-2.5, bm25_score=-12.0)]
    r = _make_pipeline(monkeypatch, threshold=10.0, chunks=chunks).ask("q")
    assert r["abstained"] is False
    assert r.get("bm25_rescued") is True
    assert r["text"] != "REFUSED"           # the generated answer, not the refusal


def test_rescue_fires_on_any_retrieved_chunk_not_just_the_top(monkeypatch):
    # The generator sees every retrieved chunk, so a strong lexical hit anywhere in the set
    # is enough -- the strongly-matched chunk is in the sources the answer is built from.
    # Top chunk (which the reranker score is read from) has only a weak lexical match; a
    # LATER chunk carries the strong one.
    chunks = [_chunk(rerank_score=-2.5, bm25_score=-2.0),
              _chunk(rerank_score=-3.0, bm25_score=-15.0, bm25_rank=1)]
    r = _make_pipeline(monkeypatch, threshold=10.0, chunks=chunks).ask("q")
    assert r["abstained"] is False
    assert r.get("bm25_rescued") is True


def test_rescue_does_not_bypass_the_models_own_refusal(monkeypatch):
    # A rescued question still passes through generation, and the model's INSUFFICIENT_
    # SOURCES remains authoritative (D-024/D-045: perfect precision). Rescuing past the
    # reranker gate must NOT silence that second, independent safeguard.
    chunks = [_chunk(rerank_score=-2.5, bm25_score=-30.0)]
    p = _make_pipeline(monkeypatch, threshold=10.0, chunks=chunks,
                       answer="INSUFFICIENT_SOURCES")
    r = p.ask("q")
    assert r["abstained"] is True
    assert r["abstain_reason"] == "model_declined"


# ---------------------------------------------------------------------------
# 3. WEAK lexical match does NOT rescue -- the bar is high on purpose
# ---------------------------------------------------------------------------

def test_below_gate_with_weak_bm25_is_not_rescued(monkeypatch):
    # Incidental term overlap: relevance = 2.0, far below the 10.0 bar. This is the
    # out-of-corpus-vocabulary-overlap case the high bar exists to refuse; if it rescued,
    # the false-answer rate would climb. It must still refuse.
    chunks = [_chunk(rerank_score=-2.5, bm25_score=-2.0)]
    r = _make_pipeline(monkeypatch, threshold=10.0, chunks=chunks).ask("q")
    assert r["abstained"] is True
    assert r["abstain_reason"] == "low_retrieval_score"


def test_dense_only_chunk_never_rescues(monkeypatch):
    # A chunk retrieved by the dense half alone has bm25_score None (no lexical match at
    # all). "No lexical evidence" can never clear the bar, however low the bar is set.
    chunks = [_chunk(rerank_score=-2.5, bm25_score=None, bm25_rank=None)]
    r = _make_pipeline(monkeypatch, threshold=0.0, chunks=chunks).ask("q")
    assert r["abstained"] is True
    assert r["abstain_reason"] == "low_retrieval_score"


def test_helper_threshold_is_a_high_water_mark_not_a_floor():
    # Monotonicity the operator relies on: a stronger required bar rejects a match a weaker
    # bar would accept. relevance here is 8.0.
    weak_hit = [_chunk(rerank_score=-2.5, bm25_score=-8.0)]
    assert pipeline.bm25_lexical_rescue(weak_hit, 5.0) is True    # 8.0 >= 5.0
    assert pipeline.bm25_lexical_rescue(weak_hit, 10.0) is False  # 8.0 <  10.0


# ---------------------------------------------------------------------------
# 4. The rescue only ever flips refuse -> answer, never answer -> refuse
# ---------------------------------------------------------------------------

def test_rescue_enabled_leaves_an_above_gate_answer_untouched(monkeypatch):
    # An above-gate question answers whether or not the feature is on. The rescue lives
    # INSIDE the refuse branch, so an answering question never reaches it and cannot be
    # turned into a refusal. A weak BM25 signal here would refuse IF it were ever consulted
    # on the answer path -- proving it is not.
    chunks = [_chunk(rerank_score=5.0, bm25_score=-1.0)]
    r = _make_pipeline(monkeypatch, threshold=10.0, chunks=chunks).ask("q")
    assert r["abstained"] is False
    assert "bm25_rescued" not in r          # never entered the rescue path


def test_no_chunks_still_refuses_with_rescue_enabled(monkeypatch):
    # The empty-retrieval gate is upstream of the rescue and there is no BM25 signal to
    # rescue on. It must refuse with its own reason regardless of the threshold.
    r = _make_pipeline(monkeypatch, threshold=0.0, chunks=[]).ask("q")
    assert r["abstained"] is True
    assert r["abstain_reason"] == "no_results"


# ---------------------------------------------------------------------------
# hybrid.search surfaces the BM25 signal WITHOUT changing result order
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeDB:
    """Stands in for the sqlite connection: only the `SELECT * FROM chunks` reaches it."""
    def __init__(self, rows_by_id):
        self.rows_by_id = rows_by_id

    def execute(self, sql, params=None):
        return _FakeCursor([dict(self.rows_by_id[i]) for i in list(params)])


def test_search_surfaces_bm25_signal_without_changing_order(monkeypatch):
    # bm25_search returns (chunk_id, raw_score) best-first; chunk 10 is a strong lexical
    # hit, 20 a weak one, 30 is dense-only (never a BM25 hit).
    bm = [(10, -12.0), (20, -2.0)]
    dn = [(20, 0.10), (30, 0.20)]
    monkeypatch.setattr(hybrid, "bm25_search", lambda db, q, limit: bm)
    monkeypatch.setattr(hybrid, "dense_search", lambda db, vec, limit: dn)
    monkeypatch.setattr(hybrid, "embed", lambda q, url: [0.0])
    monkeypatch.setattr(hybrid, "retrieval_text", lambda c: c["text"])
    # Identity rerank: keep candidate order so we are asserting on the FUSED order, i.e.
    # exactly what retrieval produced -- the order the D-054 offsets were fit against.
    monkeypatch.setattr(hybrid, "rerank",
                        lambda q, docs, url: [(i, 1.0) for i in range(len(docs))])

    rows_by_id = {10: {"id": 10, "text": "crico"},
                  20: {"id": 20, "text": "tourniquet"},
                  30: {"id": 30, "text": "commander intent"}}
    out = hybrid.search(_FakeDB(rows_by_id), "cricothyroidotomy cannula",
                        cfg={"top_n_rerank": 8}, do_rerank=True)

    # Order is the independent RRF order -- attaching the BM25 keys changed nothing.
    expected_order = [cid for cid, _, _ in hybrid.rrf_fuse({"bm25": bm, "dense": dn}, k=60)]
    assert [c["id"] for c in out] == expected_order

    by_id = {c["id"]: c for c in out}
    assert by_id[10]["bm25_score"] == -12.0 and by_id[10]["bm25_rank"] == 1
    assert by_id[20]["bm25_score"] == -2.0 and by_id[20]["bm25_rank"] == 2
    # Dense-only chunk: no lexical signal, distinguishable from a weak one.
    assert by_id[30]["bm25_score"] is None and by_id[30]["bm25_rank"] is None


def test_search_bm25_keys_present_on_no_rerank_path(monkeypatch):
    # The do_rerank=False path (used by gap_report/measure_recall) must carry the keys too,
    # so any consumer of search() sees a consistent schema.
    bm = [(10, -9.0)]
    dn = [(10, 0.1), (30, 0.2)]
    monkeypatch.setattr(hybrid, "bm25_search", lambda db, q, limit: bm)
    monkeypatch.setattr(hybrid, "dense_search", lambda db, vec, limit: dn)
    monkeypatch.setattr(hybrid, "embed", lambda q, url: [0.0])
    rows_by_id = {10: {"id": 10, "text": "x"}, 30: {"id": 30, "text": "y"}}
    out = hybrid.search(_FakeDB(rows_by_id), "q", cfg={"top_n_rerank": 8}, do_rerank=False)
    by_id = {c["id"]: c for c in out}
    assert by_id[10]["bm25_score"] == -9.0 and by_id[10]["bm25_rank"] == 1
    assert by_id[30]["bm25_score"] is None
