#!/usr/bin/env python3
"""Tune (or refuse to enable) the BM25 lexical-rescue threshold against device data.

Step 4 of the post-calibration validation chain, and the honest completion of D-054's
stated limit: per-publication offsets cannot rescue TCCC's low reranker tail (scores ~-2.5
at rank 1), so `pipeline.bm25_lexical_rescue` offers an INDEPENDENT lexical signal that can
flip a would-be refusal to an answer. It ships OFF (`bm25_rescue_threshold: null`) precisely
because enabling it trades over-refusal down for false-answer up, and that trade must be
read off real device data -- NOT fitted to eval pass/fail (the D-044 trap).

This produces the trade curve the operator needs, and nothing more. It does not write config.

  What it measures, per eval question (retrieval + rerank only, NO generation -- cheap):
    max_pos_bm25 = max(-chunk.bm25_score) over retrieved chunks   [None -> no lexical match]
  which is exactly the quantity bm25_lexical_rescue thresholds (sqlite bm25() is negated, so
  positive relevance is -bm25_score; the rescue fires when any chunk clears the threshold).

  It joins that with THIS eval run's abstention decisions (last_run.json rows), because the
  rescue is only ever consulted on questions the gate already refused. Then, per candidate T:
    FIXED  = in-corpus  questions currently refused whose max_pos_bm25 >= T  (over-refusal--)
    BROKEN = out-corpus correct-abstentions whose max_pos_bm25 >= T          (false-answer++)
  A usable T fixes real clinical refusals at BROKEN=0. If none does, the honest output is
  "keep it disabled": the corpus and the out-of-corpus traps share too much vocabulary for a
  lexical bar to separate them, and the reranker gate stays the sole authority.

    python3 tune_bm25_rescue.py --eval-json /opt/tutor/eval/last_run.json --out rescue_tune.json
"""
import argparse
import json
import sqlite3
import sys
import time

sys.path.insert(0, "/opt/tutor/retrieval")
import hybrid  # noqa: E402


def load_jsonl(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def max_pos_bm25(chunks):
    best = None
    for c in chunks:
        raw = c.get("bm25_score")
        if raw is None:
            continue
        pos = -raw
        if best is None or pos > best:
            best = pos
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/tutor/doctrine.sqlite")
    ap.add_argument("--in-corpus", default="/opt/tutor/eval/in_corpus.jsonl")
    ap.add_argument("--out-corpus", default="/opt/tutor/eval/out_of_corpus.jsonl")
    ap.add_argument("--eval-json", default="/opt/tutor/eval/last_run.json")
    ap.add_argument("--embed-url", default="http://127.0.0.1:8081")
    ap.add_argument("--rerank-url", default="http://127.0.0.1:8082")
    ap.add_argument("--out", default="/opt/tutor/rescue_tune.json")
    args = ap.parse_args()

    # Abstention decision + trap flags come from the eval run just completed.
    ev = json.load(open(args.eval_json))
    decided = {r["id"]: r for r in ev["rows"]}

    inc = load_jsonl(args.in_corpus)
    ooc = load_jsonl(args.out_corpus)
    TRAPS = {"answer_with_citations_ignoring_override",
             "answer_with_citations_including_edition_date"}

    db = hybrid.connect(args.db)
    probe, errors, t0 = [], 0, time.time()
    for kind, rows in (("in", inc), ("out", ooc)):
        for r in rows:
            row = decided.get(r["id"])
            if row is None:
                continue                      # id not in this eval run -- skip
            try:
                chunks = hybrid.search(db, r["question"], embed_url=args.embed_url,
                                       rerank_url=args.rerank_url)
            except Exception as e:            # noqa: BLE001
                errors += 1
                continue
            # out-corpus rows that EXPECT an answer are traps, not correct-abstentions;
            # exclude them from the protected set (they are not the number rescue endangers).
            expects_answer = (kind == "out" and r.get("correct_behavior") in TRAPS)
            probe.append({
                "id": r["id"], "kind": kind,
                "abstained": bool(row.get("abstained")),
                "expects_answer": expects_answer,
                "max_pos_bm25": max_pos_bm25(chunks),
            })
    dt = time.time() - t0

    # Candidate set for rescue = questions the gate already refused.
    refused = [p for p in probe if p["abstained"]]
    recoverable = [p for p in refused if p["kind"] == "in"]                    # over-refusals
    protected = [p for p in refused if p["kind"] == "out" and not p["expects_answer"]]

    vals = sorted({round(p["max_pos_bm25"], 3) for p in refused
                   if p["max_pos_bm25"] is not None})
    # sweep thresholds at each observed value (a rescue fires at T <= max_pos_bm25)
    curve = []
    for t in vals:
        fixed = sum(1 for p in recoverable
                    if p["max_pos_bm25"] is not None and p["max_pos_bm25"] >= t)
        broken = sum(1 for p in protected
                     if p["max_pos_bm25"] is not None and p["max_pos_bm25"] >= t)
        curve.append({"threshold": t, "fixed": fixed, "broken": broken})

    # Recommendation: the largest FIXED reachable at BROKEN==0 (conservative knee).
    zero_cost = [c for c in curve if c["broken"] == 0 and c["fixed"] > 0]
    best = max(zero_cost, key=lambda c: (c["fixed"], -c["threshold"])) if zero_cost else None

    out = {
        "n_probed": len(probe), "errors": errors, "probe_s": round(dt),
        "refused_total": len(refused),
        "recoverable_over_refusals": len(recoverable),
        "protected_correct_abstentions": len(protected),
        "curve": curve,
        "recommendation": best,
        "verdict": ("enable at threshold %.3f -> fixes %d over-refusals, 0 false-answers"
                    % (best["threshold"], best["fixed"]) if best
                    else "KEEP DISABLED: no threshold fixes any over-refusal at zero "
                         "false-answer cost on this data"),
    }
    json.dump(out, open(args.out, "w"), indent=2)
    print(json.dumps({k: v for k, v in out.items() if k != "curve"}, indent=2))
    print("\ntrade curve (threshold: fixed/broken):")
    for c in curve:
        print(f"  {c['threshold']:7.3f}  fixed={c['fixed']:2d}  broken={c['broken']:2d}")


if __name__ == "__main__":
    main()
