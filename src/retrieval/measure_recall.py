#!/usr/bin/env python3
"""Measure retrieval quality against the labelled eval set. P2 acceptance criterion.

Ground truth is expressed as (pub_id, section) pairs, every one of which was checked to
exist in the built index before scoring -- a label that matches nothing would silently
depress recall and look like a retrieval failure.

Reports recall@1/3/8 and MRR, and separates the two halves of the pipeline: recall
BEFORE reranking (what RRF fusion surfaced) and AFTER. If reranking lowers recall, that
is worth seeing rather than averaging away.

    python3 measure_recall.py --db doctrine.sqlite --eval in_corpus.jsonl
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hybrid  # noqa: E402


def is_relevant(chunk, labels):
    for l in labels:
        if chunk.get("pub_id") == l["pub_id"] and \
           (chunk.get("section") or "").strip().lower() == l["section"].strip().lower():
            return True
    return False


def rank_of_first_relevant(hits, labels):
    for i, h in enumerate(hits, 1):
        if is_relevant(h, labels):
            return i
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/tutor/doctrine.sqlite")
    ap.add_argument("--eval", default="/opt/tutor/in_corpus.jsonl")
    ap.add_argument("--top-n", type=int, default=8)
    ap.add_argument("--out", default="/opt/tutor/recall_report.json")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.eval, encoding="utf-8") if l.strip()]
    scored = [r for r in rows if r.get("score_retrieval") and r.get("relevant_sections")]
    skipped = [r["id"] for r in rows if not r.get("score_retrieval")]

    db = hybrid.connect(args.db)
    results = []
    for r in scored:
        labels = r["relevant_sections"]
        pre = hybrid.search(db, r["question"],
                            cfg={"top_n_rerank": args.top_n}, do_rerank=False)
        post = hybrid.search(db, r["question"],
                             cfg={"top_n_rerank": args.top_n}, do_rerank=True)
        results.append({
            "id": r["id"],
            "question": r["question"][:70],
            "rank_pre": rank_of_first_relevant(pre, labels),
            "rank_post": rank_of_first_relevant(post, labels),
            "top1_post": post[0]["citation"][:72] if post else None,
            "top1_score": post[0].get("rerank_score") if post else None,
        })

    def recall_at(k, key):
        hit = sum(1 for x in results if x[key] is not None and x[key] <= k)
        return hit / len(results) if results else 0.0

    def mrr(key):
        return sum(1.0 / x[key] for x in results if x[key]) / len(results) if results else 0.0

    print(f"\n{'=' * 72}")
    print(f"  RETRIEVAL QUALITY  --  {len(results)} labelled questions"
          f"{f' ({len(skipped)} unscored: ' + ','.join(skipped) + ')' if skipped else ''}")
    print("=" * 72)
    print(f"  {'metric':<28}{'fused only':>14}{'+ reranked':>14}")
    print("-" * 72)
    for k in (1, 3, args.top_n):
        print(f"  {'recall@' + str(k):<28}{recall_at(k, 'rank_pre'):>13.1%}"
              f"{recall_at(k, 'rank_post'):>14.1%}")
    print(f"  {'MRR':<28}{mrr('rank_pre'):>13.3f}{mrr('rank_post'):>14.3f}")
    print("=" * 72)

    misses = [x for x in results if x["rank_post"] is None]
    if misses:
        print(f"\n  misses at top-{args.top_n} after reranking ({len(misses)}):")
        for m in misses:
            print(f"    {m['id']}  {m['question']}")
            print(f"        fused rank: {m['rank_pre']}   top-1 returned: {m['top1_post']}")

    hurt = [x for x in results if x["rank_pre"] and x["rank_post"]
            and x["rank_post"] > x["rank_pre"]]
    if hurt:
        print(f"\n  reranking pushed the relevant chunk DOWN in {len(hurt)} case(s):")
        for h in hurt:
            print(f"    {h['id']}  rank {h['rank_pre']} -> {h['rank_post']}")

    Path(args.out).write_text(json.dumps({
        "n_scored": len(results), "skipped": skipped,
        "recall_at_1": recall_at(1, "rank_post"),
        "recall_at_3": recall_at(3, "rank_post"),
        f"recall_at_{args.top_n}": recall_at(args.top_n, "rank_post"),
        "recall_at_8_fused_only": recall_at(args.top_n, "rank_pre"),
        "mrr": mrr("rank_post"), "results": results}, indent=2))
    print(f"\nwrote {args.out}\n")


if __name__ == "__main__":
    main()
