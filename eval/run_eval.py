#!/usr/bin/env python3
"""The eval harness. Prints the single results table.

Runs ON the device, because it exercises the real pipeline end to end -- retrieval,
the abstention gates, generation, and grounding -- against both eval sets.

    python3 run_eval.py --db doctrine.sqlite --config default.yaml

Reads the tuned thresholds from config. If they are unset it says so in the table rather
than silently reporting numbers produced by an ungated system.
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "generation"))
sys.path.insert(0, str(HERE.parent / "retrieval"))
sys.path.insert(0, str(HERE))

TRAPS = {"answer_with_citations_ignoring_override",
         "answer_with_citations_including_edition_date"}


def load_jsonl(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def cites_relevant(result, labels):
    """Did the answer cite a chunk from a section we labelled relevant?"""
    if not labels:
        return None
    for c in result.get("citations", []):
        for l in labels:
            if c.get("pub_id") == l["pub_id"]:
                return True
    return False


def pct(n, d):
    return f"{100.0 * n / d:.1f}%" if d else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/tutor/doctrine.sqlite")
    ap.add_argument("--config")
    ap.add_argument("--in-corpus", default=str(HERE / "in_corpus.jsonl"))
    ap.add_argument("--out-corpus", default=str(HERE / "out_of_corpus.jsonl"))
    ap.add_argument("--bench", help="bench json for tokens/sec and watts")
    ap.add_argument("--json-out")
    args = ap.parse_args()

    cfg = {}
    if args.config and Path(args.config).exists():
        try:
            import yaml
            cfg = yaml.safe_load(Path(args.config).read_text()) or {}
        except ImportError:
            print("! pyyaml missing; using built-in defaults", file=sys.stderr)
    cfg.setdefault("index", {})["path"] = args.db

    from pipeline import TutorPipeline
    pipe = TutorPipeline(cfg)

    score_thr = pipe.score_threshold
    inc = load_jsonl(args.in_corpus)
    ooc = load_jsonl(args.out_corpus)

    latencies, rows = [], []
    for r in inc:
        t0 = time.time()
        res = pipe.ask(r["question"])
        el = time.time() - t0
        latencies.append(el)
        rows.append({"kind": "in", "id": r["id"], "abstained": res["abstained"],
                     "cited_relevant": cites_relevant(res, r.get("relevant_sections")),
                     "has_citation": bool(res.get("citations")),
                     "latency": el})
    for r in ooc:
        t0 = time.time()
        res = pipe.ask(r["question"])
        el = time.time() - t0
        latencies.append(el)
        rows.append({"kind": "out", "id": r["id"], "abstained": res["abstained"],
                     "expects_answer": r.get("correct_behavior") in TRAPS,
                     "has_citation": bool(res.get("citations")),
                     "latency": el})

    i_rows = [r for r in rows if r["kind"] == "in"]
    o_ref = [r for r in rows if r["kind"] == "out" and not r.get("expects_answer")]
    o_ans = [r for r in rows if r["kind"] == "out" and r.get("expects_answer")]

    over_refusal = sum(1 for r in i_rows if r["abstained"])
    answered_in = [r for r in i_rows if not r["abstained"]]
    cite_scored = [r for r in answered_in if r["cited_relevant"] is not None]
    cite_ok = sum(1 for r in cite_scored if r["cited_relevant"])
    with_citation = sum(1 for r in answered_in if r["has_citation"])
    false_answer = sum(1 for r in o_ref if not r["abstained"])
    traps_kept = sum(1 for r in o_ans if not r["abstained"])

    lat = sorted(latencies)
    p50 = lat[len(lat) // 2] if lat else None
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None

    tps = watts = None
    if args.bench and Path(args.bench).exists():
        b = json.loads(Path(args.bench).read_text())
        tps, watts = b.get("tokens_per_second"), b.get("watts")

    W = 68
    print("\n" + "=" * W)
    print("  OFFLINE DOCTRINE-GROUNDED TUTOR - EVAL")
    print("=" * W)
    print(f"  in-corpus {len(i_rows)}   out-of-corpus {len(o_ref)}"
          f"   answer-traps {len(o_ans)}")
    print(f"  abstention threshold  {score_thr if score_thr is not None else 'UNTUNED'}")
    print("-" * W)
    print(f"  {'metric':<38}{'value':>10}{'basis':>18}")
    print("-" * W)
    print(f"  {'citation-correct rate':<38}{pct(cite_ok, len(cite_scored)):>10}"
          f"{f'{len(cite_scored)} answered':>18}")
    print(f"  {'answers carrying a citation':<38}{pct(with_citation, len(answered_in)):>10}"
          f"{f'{len(answered_in)} answered':>18}")
    print("-" * W)
    print(f"  {'FALSE-ANSWER RATE (out-of-corpus)':<38}{pct(false_answer, len(o_ref)):>10}"
          f"{f'{false_answer}/{len(o_ref)}':>18}")
    print(f"  {'CORRECT-ABSTENTION RATE':<38}"
          f"{pct(len(o_ref) - false_answer, len(o_ref)):>10}"
          f"{f'{len(o_ref)-false_answer}/{len(o_ref)}':>18}")
    print(f"  {'over-refusal rate (in-corpus)':<38}{pct(over_refusal, len(i_rows)):>10}"
          f"{f'{over_refusal}/{len(i_rows)}':>18}")
    print(f"  {'answer-traps correctly answered':<38}{pct(traps_kept, len(o_ans)):>10}"
          f"{f'{traps_kept}/{len(o_ans)}':>18}")
    print("-" * W)
    print(f"  {'p50 latency':<38}{f'{p50:.2f}s' if p50 else 'n/a':>10}")
    print(f"  {'p95 latency':<38}{f'{p95:.2f}s' if p95 else 'n/a':>10}")
    print(f"  {'tokens/sec':<38}{tps or 'see bench/':>10}")
    print(f"  {'watts':<38}{watts or 'see bench/':>10}")
    print("=" * W)
    if score_thr is None:
        print("\n  ! abstention threshold UNTUNED - refusal numbers above are not")
        print("    a property of a gated system. Run `make tune-abstention`.")
    print("\n  Read false-answer and over-refusal together. Refusing everything drives")
    print("  the first to zero and produces a useless tutor.\n")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "threshold": score_thr,
            "citation_correct_rate": cite_ok / len(cite_scored) if cite_scored else None,
            "false_answer_rate": false_answer / len(o_ref) if o_ref else None,
            "correct_abstention_rate": (len(o_ref) - false_answer) / len(o_ref) if o_ref else None,
            "over_refusal_rate": over_refusal / len(i_rows) if i_rows else None,
            "traps_kept": traps_kept, "n_traps": len(o_ans),
            "p50_latency_s": p50, "p95_latency_s": p95,
            "rows": rows}, indent=2))


if __name__ == "__main__":
    main()
