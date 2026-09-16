#!/usr/bin/env python3
"""Hybrid retrieval: BM25 (FTS5) + dense (sqlite-vec), fused by RRF, then reranked.

Runs against the single portable index. Both halves return chunk ids, so fusion is a
rank merge with no score-scale reconciliation -- which is the point of reciprocal rank
fusion: BM25 scores and cosine distances are not commensurable, but ranks are.

    python3 hybrid.py --db doctrine.sqlite --query "What is friction in war?"
"""
import argparse
import json
import re
import pathlib
import sqlite3
import struct
import sys
import urllib.error
import urllib.request

from http_retry import post_json

import sqlite_vec

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from chunk_text import retrieval_text  # noqa: E402  (shared with the index builder)

DEFAULTS = {
    "top_k_dense": 50,
    "top_k_bm25": 50,
    "rrf_k": 60,
    "top_n_rerank": 8,
}


def connect(path):
    # check_same_thread=False because FastAPI dispatches handlers onto a threadpool, so
    # the connection is opened on one thread and used on others. Safe here: every access
    # is read-only, and Python's sqlite3 serialises calls on a single connection.
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


# ---------------------------------------------------------------------------
# Model calls (OpenAI-compatible; swappable by URL)
# ---------------------------------------------------------------------------

def embed(text, url="http://127.0.0.1:8081"):
    data = post_json(url.rstrip("/") + "/v1/embeddings",
                     {"input": [text], "model": "bge-small-en-v1.5"}, timeout=120)
    return data["data"][0]["embedding"]


# bge-reranker-base is trained to 512 tokens, and llama.cpp caps the slot context at the
# model's training context no matter what -c is set to:
#     "the slot context (2048) exceeds the training context of the model (512) - capping"
# So query + document must fit 512 tokens, full stop.
#
# Characters per token is NOT uniform across this corpus. Flowing doctrinal prose runs
# about 2.8 chars/token, but dense medical text and OCR noise run closer to 1.75 -- a
# 1,200-character document produced 686 tokens and was rejected. The budget is therefore
# set against the dense case rather than the average, and a retry halves it if the server
# still objects, so one unusual chunk cannot fail an entire query.
RERANK_DOC_CHARS = 700


def rerank(query, documents, url="http://127.0.0.1:8082", budget=RERANK_DOC_CHARS,
           timeout=300, attempts=None, retry_context_errors=True):
    """Returns [(index, score)] sorted best-first. Scores are raw logits, not 0-1.

    The defaults are Anchor's original full-retrieval behaviour: a 300-second request,
    http_retry's normal retry budget, and a smaller-document retry for context-related
    400/500 responses. Callers with a tighter service-level deadline can explicitly set
    ``timeout``, ``attempts``, and ``retry_context_errors`` without changing that legacy
    path.
    """
    if not documents:
        return []
    for attempt_budget in (budget, budget // 2, budget // 4):
        try:
            # post_json re-raises HTTPError untouched, so the budget-shrinking recovery
            # below still sees it; only transport failures are retried.
            request_args = {"timeout": timeout}
            if attempts is not None:
                request_args["attempts"] = attempts
            data = post_json(
                url.rstrip("/") + "/v1/rerank",
                {"query": query,
                 "documents": [d[:attempt_budget] for d in documents]}, **request_args)
        except urllib.error.HTTPError as e:
            if (retry_context_errors and e.code in (400, 500)
                    and attempt_budget > budget // 4):
                continue
            raise
        out = [(d["index"], d["relevance_score"]) for d in data["results"]]
        out.sort(key=lambda t: -t[1])
        return out
    return []


# ---------------------------------------------------------------------------
# Retrieval halves
# ---------------------------------------------------------------------------

FTS_SAFE = re.compile(r"[^\w\s]")
FTS_DIGIT = re.compile(r"\d")


def fts_query(text):
    """Build a safe FTS5 MATCH expression.

    User text goes straight into a MATCH expression, where bare punctuation is FTS5
    syntax -- a stray quote or hyphen is a syntax error, and a question mark is not the
    literal the user meant. Each token is stripped and quoted so the query is data, never
    grammar.

    Tokens shorter than two characters are dropped: single letters are noise in BM25 and
    blow up the term count on OCR'd text. That filter used to eat every publication
    identifier a Marine typed -- "MCDP 1-0" split to MCDP/1/0 and both digits were
    discarded, so BM25 saw only "MCDP" and matched all 2,892 MCDP chunks equally. The
    dense half was carrying the disambiguation alone (see chunk_text.py: +3.39 with the
    header versus -2.42 without).

    A word containing a digit is therefore emitted as a QUOTED PHRASE with the plain word
    before it, not as loose tokens. Measured against the real 4,230-chunk index:

        "1"            -> 2,278 chunks   (every MCDP 1, 1-0, 1-1, 1-2, 1-3 -- useless)
        "MCDP"         -> 2,892 chunks   (today's behaviour: no disambiguation at all)
        "MCDP 1 0"     ->   712 chunks   (exactly MCDP 1-0)
        "MCWP 3 11 3"  ->   668 chunks   (exactly MCWP 3-11.3)
        "chapter 5"    ->    92 chunks   (exactly the chapter-5 chunks)

    The phrase works because build_index.py indexes retrieval_text() -- header + body --
    under tokenize='porter unicode61', which splits on the hyphen. "MCDP 1-0, Marine
    Corps Operations | ..." lands in the index as adjacent tokens mcdp/1/0, and an FTS5
    phrase spans exactly that run. A phrase query that could not match anything would be
    worse than the old behaviour, so this was verified against the corpus before it was
    written, not after.

    The plain word is still emitted on its own as well, so this is strictly additive:
    every chunk BM25 used to find it still finds, plus a high-IDF phrase that pulls the
    named publication up the ranking. Over the 11 eval questions that name a publication,
    mean top-50 BM25 chunks belonging to the NAMED publication went 23.4 -> 26.1; "MCDP 6
    command and control" 30 -> 42, and "MCDP 5 planning" 17 -> 33 with the first correct
    chunk moving from rank 11 to rank 5.

    Known limit, accepted: FTS5 has no anchor for the end of a token run, so the phrase
    "MCDP 1" is a positional prefix of "MCDP 1-0" and still matches the 1-x family (1,781
    chunks, versus 2,892 for bare "MCDP"). It narrows; it does not pin. The 1-x pubs are
    the only place in this corpus where that bites.
    """
    terms, prev_word = [], None
    for word in text.split():
        # Punctuation is removed rather than escaped -- that is the whole basis of the
        # quoting safety, since a surviving '"' would terminate a term and let the rest
        # parse as grammar. Splitting per whitespace-word first is what preserves the
        # grouping "MCDP 1-0" needs; FTS_SAFE alone would flatten it into the sentence.
        parts = FTS_SAFE.sub(" ", word).split()
        if not parts:
            prev_word = None                 # '--' between words is not an adjacency
            continue
        if FTS_DIGIT.search(word):
            phrase = ([prev_word] if prev_word else []) + parts
            # A lone digit ("5", "p. 5" with nothing before it) is the noise the length
            # filter was built for; only a real multi-token run earns a phrase.
            if len(phrase) > 1 or len(phrase[0]) > 1:
                terms.append(" ".join(phrase))
            prev_word = None
        else:
            terms.extend(p for p in parts if len(p) > 1)
            # Kept even when too short to be a term of its own: the 'p' of "p. 5".
            prev_word = parts[-1]
    return " OR ".join(f'"{t}"' for t in terms) if terms else None


def bm25_search(db, query, limit):
    expr = fts_query(query)
    if not expr:
        return []
    rows = db.execute(
        "SELECT rowid AS chunk_id, bm25(chunks_fts) AS score "
        "FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
        (expr, limit)).fetchall()
    # bm25() returns lower-is-better; rank order is what feeds RRF.
    return [(r["chunk_id"], r["score"]) for r in rows]


def dense_search(db, vector, limit):
    """KNN over windows, collapsed to best window per chunk."""
    blob = struct.pack(f"{len(vector)}f", *vector)
    rows = db.execute(
        "SELECT v.rowid AS vec_rowid, v.distance AS distance, m.chunk_id AS chunk_id "
        "FROM chunks_vec v JOIN vec_map m ON m.vec_rowid = v.rowid "
        "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
        (blob, limit * 3)).fetchall()
    best = {}
    for r in rows:
        cid = r["chunk_id"]
        if cid not in best or r["distance"] < best[cid]:
            best[cid] = r["distance"]
    return sorted(best.items(), key=lambda t: t[1])[:limit]


def rrf_fuse(ranked_lists, k=60):
    """Reciprocal rank fusion. Operates on ranks, so incommensurable scores never meet."""
    scores, seen_in = {}, {}
    for name, lst in ranked_lists.items():
        for rank, (cid, _) in enumerate(lst, 1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            seen_in.setdefault(cid, []).append(name)
    fused = sorted(scores.items(), key=lambda t: -t[1])
    return [(cid, sc, seen_in[cid]) for cid, sc in fused]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def search(db, query, cfg=None, embed_url="http://127.0.0.1:8081",
           rerank_url="http://127.0.0.1:8082", do_rerank=True):
    cfg = {**DEFAULTS, **(cfg or {})}

    bm = bm25_search(db, query, cfg["top_k_bm25"])
    dn = dense_search(db, embed(query, embed_url), cfg["top_k_dense"])
    fused = rrf_fuse({"bm25": bm, "dense": dn}, k=cfg["rrf_k"])

    if not fused:
        return []

    ids = [cid for cid, _, _ in fused]
    placeholders = ",".join("?" * len(ids))
    rows = {r["id"]: dict(r) for r in db.execute(
        f"SELECT * FROM chunks WHERE id IN ({placeholders})", ids).fetchall()}

    # Surface the BM25 lexical signal per candidate, for the pipeline's secondary
    # abstention gate (D-045/D-054). bm25_search already computed this -- we are only
    # exposing it, never consuming it here. Fusion is by RANK (rrf_fuse), so attaching
    # the raw BM25 score/rank as extra dict keys changes NOTHING about which chunks are
    # returned or in what order; the D-054 calibration offsets were fit against the
    # current retrieval and must stay valid, so retrieval behaviour is held byte-identical.
    #   bm25_score: raw sqlite bm25() value, lower-is-better (negated Okapi BM25); the
    #               conventional positive relevance is -bm25_score. None if the chunk was
    #               not a BM25 hit (dense-only), so "no lexical match" is distinguishable
    #               from "a weak one".
    #   bm25_rank:  1-based position in the BM25 result list, or None. A rank-1 lexical
    #               hit is the strong-match case the clinical rescue is built for.
    bm25_score = {cid: sc for cid, sc in bm}
    bm25_rank = {cid: i for i, (cid, _) in enumerate(bm, 1)}

    candidates = [dict(rows[cid], rrf_score=sc, found_by=src,
                       bm25_score=bm25_score.get(cid), bm25_rank=bm25_rank.get(cid))
                  for cid, sc, src in fused if cid in rows]

    if not do_rerank:
        return candidates[:cfg["top_n_rerank"]]

    # Rerank on header+text, matching exactly what was embedded.
    order = rerank(query, [retrieval_text(c) for c in candidates], rerank_url)
    out = []
    for idx, score in order[:cfg["top_n_rerank"]]:
        c = dict(candidates[idx])
        c["rerank_score"] = score
        out.append(c)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/tutor/doctrine.sqlite")
    ap.add_argument("--query", required=True)
    ap.add_argument("--top-n", type=int, default=8)
    ap.add_argument("--no-rerank", action="store_true")
    args = ap.parse_args()

    db = connect(args.db)
    hits = search(db, args.query, cfg={"top_n_rerank": args.top_n},
                  do_rerank=not args.no_rerank)
    print(f"\nquery: {args.query}\n" + "=" * 78)
    for i, h in enumerate(hits, 1):
        rs = h.get("rerank_score")
        print(f"{i:>2}. [{'+'.join(h['found_by']):<11}] "
              f"rrf={h['rrf_score']:.4f}" + (f"  rerank={rs:8.3f}" if rs is not None else ""))
        print(f"    {h['citation']}")
        print(f"    {h['text'][:150]}...")
    print()


if __name__ == "__main__":
    main()
