#!/usr/bin/env python3
"""Does reranker-scored grounding separate answerable from unanswerable?

Embedding-cosine grounding failed as an abstention gate (DECISIONS.md D-024): in-corpus
sentence support spans 0.642-0.861 while out-of-corpus spans 0.546-0.985 -- wider and
higher. The hypothesis for why: cosine measures whether the answer paraphrases the
retrieved chunks, which it always does, because the model was told to use only those
chunks. That is FIDELITY. Abstention needs RELEVANCE.

The reranker is a relevance model and already separates cleanly at the query level. This
scores each answer sentence against the retrieved chunks with the reranker instead, and
compares the two signals on identical answers.

    python3 grounding_experiment.py --db doctrine.sqlite --out grounding_exp.json
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retrieval"))
import hybrid  # noqa: E402
from pipeline import SENT_SPLIT, TutorPipeline  # noqa: E402

TRAPS = {"answer_with_citations_ignoring_override",
         "answer_with_citations_including_edition_date"}


def rerank_grounding(sentences, chunks, rerank_url):
    """Max reranker score of each sentence against the retrieved chunks."""
    from chunk_text import retrieval_text
    docs = [retrieval_text(c) for c in chunks]
    per_sentence = []
    for s in sentences:
        order = hybrid.rerank(s, docs, rerank_url)
        per_sentence.append(max((sc for _, sc in order), default=-99.0))
    return per_sentence


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/tutor/doctrine.sqlite")
    ap.add_argument("--in-corpus", default="/opt/tutor/in_corpus.jsonl")
    ap.add_argument("--out-corpus", default="/opt/tutor/out_of_corpus.jsonl")
    ap.add_argument("--out", default="/opt/tutor/grounding_exp.json")
    args = ap.parse_args()

    pipe = TutorPipeline({
        "index": {"path": args.db},
        "abstention": {"reranker_score_threshold": None},
        "grounding": {"enabled": True, "sentence_support_threshold": None},
    })

    rows = []
    for path, kind in ((args.in_corpus, "in"), (args.out_corpus, "out")):
        for line in open(path, encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                r["_kind"] = kind
                rows.append(r)

    out = []
    for r in rows:
        if r["_kind"] == "out" and r.get("correct_behavior") in TRAPS:
            continue
        t0 = time.time()
        res = pipe.ask(r["question"])
        if res.get("abstained"):
            out.append({"id": r["id"], "kind": r["_kind"], "abstained": True,
                        "reason": res.get("abstain_reason"),
                        "top_score": res.get("top_rerank_score")})
            print(f"  {r['id']:<9} DECLINED ({res.get('abstain_reason')})", flush=True)
            continue

        answer = res["text"]
        chunks = pipe.retrieve(r["question"])
        sents = [s.strip() for s in SENT_SPLIT.split(answer)
                 if len(s.strip()) > 25]
        if not sents:
            continue
        rr = rerank_grounding(sents, chunks, pipe.rerank_url)
        cos = [o["support"] for o in (res.get("grounding") or {}).get("sentences", [])]
        rec = {
            "id": r["id"], "kind": r["_kind"], "abstained": False,
            "top_score": res.get("top_rerank_score"),
            "n_sentences": len(sents),
            "cos_min": min(cos) if cos else None,
            "rr_min": min(rr), "rr_mean": round(statistics.mean(rr), 3),
            "latency_s": round(time.time() - t0, 2),
        }
        out.append(rec)
        print(f"  {rec['id']:<9} cos_min="
              f"{(rec['cos_min'] if rec['cos_min'] is not None else float('nan')):.3f}"
              f"  rr_min={rec['rr_min']:7.2f}  rr_mean={rec['rr_mean']:7.2f}", flush=True)

    live = [r for r in out if not r["abstained"]]
    inc = [r["rr_min"] for r in live if r["kind"] == "in"]
    ooc = [r["rr_min"] for r in live if r["kind"] == "out"]
    cinc = [r["cos_min"] for r in live if r["kind"] == "in" and r["cos_min"] is not None]
    cooc = [r["cos_min"] for r in live if r["kind"] == "out" and r["cos_min"] is not None]

    def rng(v):
        return f"{min(v):7.3f} .. {max(v):7.3f}  (median {statistics.median(v):7.3f})" if v else "n/a"

    print("\n" + "=" * 76)
    print("  GROUNDING SIGNAL COMPARISON  (answers that were not already declined)")
    print("=" * 76)
    print(f"  embedding cosine   in-corpus   {rng(cinc)}")
    print(f"                     out-corpus  {rng(cooc)}")
    print(f"  reranker score     in-corpus   {rng(inc)}")
    print(f"                     out-corpus  {rng(ooc)}")
    print("-" * 76)

    if inc and ooc:
        best = None
        for thr in [x / 2 for x in range(-16, 17)]:
            fa = sum(1 for v in ooc if v >= thr)
            orf = sum(1 for v in inc if v < thr)
            s = fa / len(ooc) + orf / len(inc)
            if best is None or s < best[0]:
                best = (s, thr, fa, orf)
        _, thr, fa, orf = best
        print(f"  best reranker-grounding threshold: {thr}")
        print(f"    would-be false answers : {fa}/{len(ooc)}  ({fa/len(ooc):.1%})")
        print(f"    would-be over-refusals : {orf}/{len(inc)}  ({orf/len(inc):.1%})")
    print("=" * 76)

    Path(args.out).write_text(json.dumps({"records": out}, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
