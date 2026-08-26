#!/usr/bin/env python3
"""Tune the abstention thresholds against the eval set, then report the table.

Runs every eval question ONCE with both gates disabled, recording the raw signals
(top reranker score, minimum sentence grounding, whether the model itself declined).
Thresholds are then swept offline over those recorded signals, so the sweep costs no
extra generation and every candidate operating point is scored on identical data.

The two numbers that matter are reported together and never separately:
  * false-answer rate  -- out-of-corpus questions answered anyway
  * over-refusal rate  -- in-corpus questions wrongly refused
Refusing everything drives the first to zero and produces a useless tutor.

    python3 tune_thresholds.py --db doctrine.sqlite --out tuning.json
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retrieval"))
from pipeline import TutorPipeline, effective_top_score  # noqa: E402

# Rows whose correct behaviour is to ANSWER, despite living in out_of_corpus.
ANSWER_EXPECTED = {"answer_with_citations_ignoring_override",
                   "answer_with_citations_including_edition_date"}


def collect(pipe, rows, kind):
    out = []
    for r in rows:
        t0 = time.time()
        try:
            res = pipe.ask(r["question"])
        except Exception as e:                                    # noqa: BLE001
            out.append({"id": r["id"], "kind": kind, "error": str(e)[:160]})
            continue
        el = time.time() - t0
        g = res.get("grounding") or {}
        out.append({
            "id": r["id"], "kind": kind,
            "category": r.get("category"),
            "correct_behavior": r.get("correct_behavior", "answer"),
            "latency_s": round(el, 2),
            "top_score": res.get("top_rerank_score"),
            # D-045: the publication of the rank-1 chunk, so the offline sweep and
            # compute_rerank_offsets.py can group/calibrate rank-1 scores per publication.
            "top_pub_id": res.get("top_pub_id"),
            "min_support": g.get("min_support"),
            "n_claims": len(g.get("sentences") or []),
            "model_declined": res.get("abstain_reason") == "model_declined",
            "answered": not res.get("abstained"),
            "text_head": (res.get("text") or "")[:120],
            "has_citation": bool(res.get("citations")),
        })
        print(f"  {r['id']:<9} {el:5.1f}s  score="
              f"{res.get('top_rerank_score'):7.2f}  support="
              f"{(g.get('min_support') if g.get('min_support') is not None else float('nan')):.3f}"
              f"  {'DECLINED' if res.get('abstained') else 'answered'}", flush=True)
    return out


def would_answer(rec, score_thr, support_thr, offsets=None):
    """Replay the gates offline against recorded signals.

    D-045: `offsets` (dict[pub_id -> float], default empty) applies the per-publication
    calibration to the recorded RAW rank-1 score before the threshold test, using the
    EXACT function the live pipeline uses so the sweep and the device cannot disagree.
    Empty/None offsets => raw score => the original global-threshold sweep, unchanged.
    """
    if rec.get("error"):
        return False
    if rec["model_declined"]:
        return False
    eff = effective_top_score(rec.get("top_score"), rec.get("top_pub_id"), offsets)
    if score_thr is not None and (eff is None or eff < score_thr):
        return False
    if support_thr is not None and rec["n_claims"]:
        if rec["min_support"] is None or rec["min_support"] < support_thr:
            return False
    return True


def evaluate(records, score_thr, support_thr, offsets=None):
    # Skip error records. A question that errored during recording (a non-retryable
    # generation failure) has only id/kind/error -- no correct_behavior, no score -- and
    # cannot contribute a verdict. pub_distribution() already guards this way; evaluate()
    # did not, so one errored question crashed the whole offline sweep after the entire
    # GPU recording pass had already completed. An errored record is dropped from the
    # denominator, which is the honest treatment: it was not measured.
    records = [r for r in records if not r.get("error")]
    inc = [r for r in records if r["kind"] == "in"]
    ooc = [r for r in records if r["kind"] == "out"]
    ooc_refuse = [r for r in ooc if r["correct_behavior"] not in ANSWER_EXPECTED]
    ooc_answer = [r for r in ooc if r["correct_behavior"] in ANSWER_EXPECTED]

    over_refusal = sum(1 for r in inc if not would_answer(r, score_thr, support_thr, offsets))
    false_answer = sum(1 for r in ooc_refuse if would_answer(r, score_thr, support_thr, offsets))
    trap_missed = sum(1 for r in ooc_answer if not would_answer(r, score_thr, support_thr, offsets))

    return {
        "score_threshold": score_thr,
        "support_threshold": support_thr,
        "over_refusal": over_refusal, "n_in": len(inc),
        "false_answer": false_answer, "n_ooc_refuse": len(ooc_refuse),
        "trap_missed": trap_missed, "n_traps": len(ooc_answer),
        "over_refusal_rate": over_refusal / max(1, len(inc)),
        "false_answer_rate": false_answer / max(1, len(ooc_refuse)),
        "correct_abstention_rate": 1 - false_answer / max(1, len(ooc_refuse)),
    }


def pub_distribution(records, offsets=None, kind="in"):
    """Per-publication rank-1 score distribution, raw and after the D-045 offset.

    The operator needs to SEE the cross-publication unfairness D-045 found -- that TCCC's
    rank-1 scores sit systematically below the prose publications. This surfaces it as a
    table straight from the recorded signals, so the outlier is visible before any offset
    is trusted, and the `after` column shows what the offset does to it.
    """
    by_pub = {}
    for r in records:
        if r.get("error") or r.get("kind") != kind:
            continue
        pub, sc = r.get("top_pub_id"), r.get("top_score")
        if pub is None or sc is None:
            continue
        by_pub.setdefault(pub, []).append(float(sc))
    rows = {}
    for pub, scores in by_pub.items():
        scores.sort()
        off = (offsets or {}).get(pub, 0.0)
        rows[pub] = {
            "n": len(scores),
            "min": round(scores[0], 2),
            "median": round(statistics.median(scores), 2),
            "max": round(scores[-1], 2),
            "offset": off,
            "median_after": round(statistics.median(scores) + off, 2),
        }
    return rows


def _print_pub_distribution(rows):
    print("\n" + "-" * 74)
    print("  RANK-1 RERANK SCORE BY PUBLICATION (in-corpus) -- D-045 fairness check")
    print("-" * 74)
    print(f"  {'pub_id':<16}{'n':>3}  {'min':>7} {'median':>7} {'max':>7}"
          f"   {'offset':>7} {'med_aft':>7}")
    for pub, s in sorted(rows.items(), key=lambda kv: kv[1]["median"]):
        print(f"  {pub:<16}{s['n']:>3}  {s['min']:>7} {s['median']:>7} {s['max']:>7}"
              f"   {s['offset']:>+7} {s['median_after']:>7}")
    print("  (rows sorted by raw median: the publication at the TOP is the outlier the")
    print("   global threshold penalises -- expected to be TCCC per D-045.)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/tutor/doctrine.sqlite")
    ap.add_argument("--in-corpus", default="/opt/tutor/in_corpus.jsonl")
    ap.add_argument("--out-corpus", default="/opt/tutor/out_of_corpus.jsonl")
    ap.add_argument("--out", default="/opt/tutor/tuning.json")
    ap.add_argument("--records", help="reuse a previous run's raw records")
    # D-045: optional per-publication calibration offsets to APPLY during the offline
    # sweep. Default None => empty => the existing global-threshold sweep, unchanged.
    # Produced (and reviewed) via scripts/compute_rerank_offsets.py.
    ap.add_argument("--offsets", help="JSON dict{pub_id->float} or a compute_rerank_offsets "
                    "proposal file; applied to rank-1 scores during the sweep")
    args = ap.parse_args()

    offsets = {}
    if args.offsets and Path(args.offsets).exists():
        loaded = json.loads(Path(args.offsets).read_text())
        # accept either a bare {pub: float} map or a compute_rerank_offsets proposal.
        offsets = loaded.get("offsets", loaded) if isinstance(loaded, dict) else {}
        print(f"applying {len(offsets)} per-publication offsets during sweep: {offsets}")

    if args.records and Path(args.records).exists():
        records = json.loads(Path(args.records).read_text())["records"]
        print(f"reusing {len(records)} recorded runs")
    else:
        pipe = TutorPipeline({
            "index": {"path": args.db},
            "abstention": {"reranker_score_threshold": None},   # gates OFF while collecting
            "grounding": {"enabled": True, "sentence_support_threshold": None},
        })
        inc = [json.loads(l) for l in open(args.in_corpus, encoding="utf-8") if l.strip()]
        ooc = [json.loads(l) for l in open(args.out_corpus, encoding="utf-8") if l.strip()]
        print(f"collecting signals: {len(inc)} in-corpus, {len(ooc)} out-of-corpus")
        records = collect(pipe, inc, "in") + collect(pipe, ooc, "out")

    scores = [r["top_score"] for r in records if r.get("top_score") is not None]
    supports = [r["min_support"] for r in records if r.get("min_support") is not None]
    score_grid = [None] + [round(x, 2) for x in
                           [min(scores) - 1] + list(statistics.quantiles(scores, n=20))]
    support_grid = [None] + [round(x, 3) for x in
                             list(statistics.quantiles(supports, n=20))] if supports else [None]

    combos = [evaluate(records, s, g, offsets) for s in score_grid for g in support_grid]

    # Selection must be BALANCED. Minimising false answers alone picks a degenerate
    # point: on the first run it chose a threshold of 5.88, which scored a perfect 0%
    # false-answer rate by refusing 13 of 17 real questions (76.5% over-refusal). That is
    # the exact failure the eval set was built to expose, and the objective walked into
    # it. Sum the two error rates so neither can be bought with the other.
    best = sorted(combos, key=lambda c: (c["false_answer_rate"] + c["over_refusal_rate"],
                                         c["trap_missed"]))[0]

    lat = [r["latency_s"] for r in records if r.get("latency_s")]
    lat.sort()
    p50 = lat[len(lat) // 2] if lat else None
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None

    print("\n" + "=" * 74)
    print("  ABSTENTION TUNING")
    print("=" * 74)
    print(f"  reranker score threshold : {best['score_threshold']}")
    print(f"  grounding threshold      : {best['support_threshold']}")
    print("-" * 74)
    print(f"  false-answer rate (OOC)      {best['false_answer']}/{best['n_ooc_refuse']}"
          f"   {best['false_answer_rate']:6.1%}")
    print(f"  correct-abstention rate      "
          f"{best['n_ooc_refuse'] - best['false_answer']}/{best['n_ooc_refuse']}"
          f"   {best['correct_abstention_rate']:6.1%}")
    print(f"  over-refusal rate (in-corpus){best['over_refusal']:>4}/{best['n_in']}"
          f"   {best['over_refusal_rate']:6.1%}")
    print(f"  answer-traps missed          {best['trap_missed']:>4}/{best['n_traps']}")
    print("-" * 74)
    print(f"  p50 latency {p50}s    p95 latency {p95}s")
    print("=" * 74)

    # Always show the per-publication rank-1 distribution so the D-045 unfairness is
    # visible in the sweep output, whether or not offsets were supplied this run.
    pubs = pub_distribution(records, offsets, kind="in")
    if pubs:
        _print_pub_distribution(pubs)

    Path(args.out).write_text(json.dumps(
        {"best": best, "p50_latency_s": p50, "p95_latency_s": p95,
         "offsets": offsets, "pub_distribution": pubs,
         "grid": combos, "records": records}, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
