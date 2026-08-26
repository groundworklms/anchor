#!/usr/bin/env python3
"""Measure the reranker's per-publication score scale from a large probe set.

Why this exists (D-052): the abstention gate thresholds the top chunk's ABSOLUTE reranker
score, but bge-reranker-base is not calibrated across publications -- terse clinical prose
(TCCC) scores systematically lower than doctrine prose, so one global threshold penalises a
whole publication for its genre (D-045). Computing a per-publication offset needs a
distribution of scores, and the 129-question eval is far too small: TCCC has n=2 there,
which is noise, not a distribution (D-052).

The approved item bank IS a large, labelled probe set: ~1,004 real doctrine questions, each
tied to a known publication. Running each through retrieval and recording the top chunk's
reranker score gives, per publication, the distribution of "when a question is answerable
from this publication, what score does the top chunk earn." That is exactly the genre-scale
signal the gate needs, at 40-190 samples for most publications instead of a handful.

Crucially this is a clean train/test split: the probe questions are model-generated items,
the eval questions (inc-*/ooc-*) are hand-written and disjoint. Offsets are estimated here
and validated on the eval, so the eval never sees its own training data.

    python3 probe_rerank_by_pub.py --db doctrine.sqlite --items learn_items.json \
        --records records.sqlite --out rerank_probe.json

Output is a records file in the shape compute_rerank_offsets.py already consumes
(kind="in", top_pub_id, top_score), so the reviewed offset computation is reused unchanged.
No generation -- retrieval + rerank only, so it is far cheaper than a full eval.
"""
import argparse
import json
import sqlite3
import sys
import time

sys.path.insert(0, "/opt/tutor/retrieval")
import hybrid  # noqa: E402


def approved_ids(records_path):
    db = sqlite3.connect(records_path)
    rows = db.execute("SELECT item_id FROM item_review WHERE status='approved'").fetchall()
    db.close()
    return {r[0] for r in rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/tutor/doctrine.sqlite")
    ap.add_argument("--items", default="/opt/tutor/learn_items.json")
    ap.add_argument("--records", default="/opt/tutor/records.sqlite")
    ap.add_argument("--out", default="/opt/tutor/rerank_probe.json")
    ap.add_argument("--embed-url", default="http://127.0.0.1:8081")
    ap.add_argument("--rerank-url", default="http://127.0.0.1:8082")
    args = ap.parse_args()

    items = {i["id"]: i for i in json.load(open(args.items))["items"]}
    approved = approved_ids(args.records)
    probe = [items[i] for i in approved if i in items]
    print(f"{len(probe)} approved items to probe", flush=True)

    db = hybrid.connect(args.db)
    records, errors, t0 = [], 0, time.time()
    for n, it in enumerate(probe, 1):
        try:
            chunks = hybrid.search(db, it["question"], embed_url=args.embed_url,
                                   rerank_url=args.rerank_url)
        except Exception as e:                                     # noqa: BLE001
            errors += 1
            records.append({"id": it["id"], "kind": "in", "error": str(e)[:120]})
            continue
        top = chunks[0].get("rerank_score") if chunks else None
        # top_pub_id is the publication of the top-ranked chunk -- the one the gate would
        # score. Usually it matches the item's own publication; when it does not, the item
        # is one where retrieval surfaced another pub first, which is itself part of the
        # genre-scale picture and correctly left in the distribution.
        records.append({
            "id": it["id"], "kind": "in",
            "item_pub_id": it["pub_id"],
            "top_pub_id": chunks[0].get("pub_id") if chunks else None,
            "top_score": top,
            # whether the item's own source publication produced the top chunk, for a
            # correctness view alongside the raw score distribution
            "top_is_item_pub": bool(chunks and chunks[0].get("pub_id") == it["pub_id"]),
        })
        if n % 100 == 0:
            print(f"  {n}/{len(probe)}  ({time.time()-t0:.0f}s, {errors} errors)", flush=True)

    json.dump({"records": records, "n": len(records), "errors": errors,
               "generated_s": round(time.time() - t0)}, open(args.out, "w"), indent=1)
    print(f"\nwrote {args.out}: {len(records)} records, {errors} errors, "
          f"{time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
