#!/usr/bin/env python3
"""Propose per-publication reranker-score calibration offsets (D-045). OPERATOR runs this.

WHAT THIS SOLVES
----------------
D-045: the abstention gate compares an ABSOLUTE bge-reranker-base score to one global
threshold, but bge-reranker-base is trained to RANK, not to emit a calibrated value, so
its scores are not comparable across publications. All three TCCC over-refusals scored
NEGATIVE at rank 1 -- the correct chunk was found and then scored away -- because terse
clinical lists (TCCC) score systematically lower than MCDP prose. One global cut penalises
a whole publication for its genre. An additive per-publication offset shifts each
publication onto a common reference so "3.0" means the same relevance across genres.

THE ANTI-OVERFITTING RATIONALE (read before trusting a number this prints)
--------------------------------------------------------------------------
This project's culture (D-044, D-025, D-024) is deeply sceptical of improvements that come
from fitting the eval labels. D-044 refused a two-question grounding "improvement" because
it was the best of 420 candidate operating points scored on the same 121 questions -- that
is how you manufacture an improvement that does not exist.

So this offset is derived PURELY FROM THE LOCATION OF EACH PUBLICATION'S SCORE
DISTRIBUTION, never from which specific questions passed or failed:

  * For each publication, take the RAW rank-1 rerank scores it produced.
  * Summarise each publication by a percentile of that distribution (default: the MEDIAN).
  * Choose one global reference R (default: the MEDIAN of the per-publication medians).
  * offset_p = R - location_p.

No answer key enters this computation. No "correct chunk", no in/out label at the question
level, no pass/fail outcome. The offset is a statement about the reranker's per-GENRE score
scale ("TCCC lands ~N points below the middle publication"), which is a property of the
text, not of the eval. Two different question sets drawn from the same publications would
yield nearly the same median shift; a metric fit to labels would not be stable that way --
that stability IS the test that we calibrated genre scale rather than fit noise.

Aligning to the MEDIAN publication (not the max) is deliberate: it re-distributes fairness
across publications while leaving the aggregate operating point of the global threshold
roughly fixed. Shifting everything UP instead would just be a disguised lowering of the
threshold -- buying over-refusal with false answers, the exact trade D-044 rejected.

Publications with too few rank-1 samples get NO offset (they fall through to +0.0 at
runtime) -- estimating a shift from one or two points is precisely the noise-fitting the
project distrusts.

WHAT IT DOES AND DOES NOT DO
----------------------------
It PRINTS the proposed offsets and the before/after per-publication distribution so a human
can judge them (you should be able to SEE that TCCC's distribution was the outlier). It
does NOT write config. Review the table, and if the offsets are defensible, paste them by
hand into config/default.yaml under abstention.reranker_score_offsets.

    # on device, after a tuning run has recorded raw signals:
    python3 compute_rerank_offsets.py --records /opt/tutor/tuning.json

Input format is exactly what tune_thresholds.py writes: a JSON object with a "records"
list, each record carrying "top_pub_id", "top_score" (the RAW rank-1 score) and "kind".
"""
import argparse
import json
import statistics
import sys
from pathlib import Path


def _percentile(sorted_vals, pct):
    """Linear-interpolated percentile of a NON-empty sorted list. pct in [0, 100].

    Hand-rolled rather than statistics.quantiles because we want an exact chosen
    percentile (default the median) of small per-publication samples, evaluated the same
    way regardless of sample size.
    """
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def per_pub_scores(records, kinds=("in",)):
    """Group RAW rank-1 scores by publication.

    Defaults to in-corpus records only: the genre score-scale is expressed where a
    publication legitimately supplies the top chunk. This uses the in/out label to select
    the POPULATION, never the per-question pass/fail outcome -- the offset still knows
    nothing about which questions were answered correctly.
    """
    by_pub = {}
    for r in records:
        if r.get("error"):
            continue
        if kinds and r.get("kind") not in kinds:
            continue
        pub = r.get("top_pub_id")
        score = r.get("top_score")
        if pub is None or score is None:
            continue
        by_pub.setdefault(pub, []).append(float(score))
    return by_pub


def compute_offsets(records, percentile=50.0, reference="median", min_samples=3,
                    kinds=("in",)):
    """Return (offsets, report). Pure function; the whole anti-overfitting story lives here.

    offsets: dict[pub_id -> float] for publications with >= min_samples rank-1 scores.
             offset_p = reference_value - percentile_p. Publications below min_samples get
             NO entry (they become +0.0 at runtime) and are reported separately.
    report:  per-publication distribution stats, before and after the offset, for review.
    """
    by_pub = per_pub_scores(records, kinds=kinds)

    locations = {}   # pub -> chosen percentile of its raw rank-1 scores
    skipped = {}     # pub -> sample count, for pubs with too few samples to trust
    for pub, scores in by_pub.items():
        if len(scores) < min_samples:
            skipped[pub] = len(scores)
            continue
        locations[pub] = _percentile(sorted(scores), percentile)

    # Global reference: the central location ACROSS publications. Median-of-medians keeps
    # the middle publication near zero offset, so calibration re-centres genres relative to
    # each other rather than globally loosening the gate.
    ref_value = None
    if locations:
        loc_vals = sorted(locations.values())
        if reference == "median":
            ref_value = statistics.median(loc_vals)
        elif reference == "mean":
            ref_value = statistics.fmean(loc_vals)
        elif reference == "max":
            ref_value = loc_vals[-1]
        else:
            raise ValueError(f"unknown reference {reference!r}")

    offsets = {}
    report = {"reference": reference, "reference_value": ref_value,
              "percentile": percentile, "min_samples": min_samples,
              "pubs": {}, "skipped": skipped}
    for pub, loc in sorted(locations.items()):
        offset = round(ref_value - loc, 3)
        offsets[pub] = offset
        scores = sorted(by_pub[pub])
        report["pubs"][pub] = {
            "n": len(scores),
            "min": round(scores[0], 3),
            "median": round(statistics.median(scores), 3),
            "max": round(scores[-1], 3),
            "location": round(loc, 3),
            "offset": offset,
            # after applying the additive offset, the whole distribution moves by `offset`
            "median_after": round(statistics.median(scores) + offset, 3),
            "min_after": round(scores[0] + offset, 3),
        }
    return offsets, report


def _print_report(offsets, report):
    print("=" * 78)
    print("  PER-PUBLICATION RERANKER-SCORE CALIBRATION (D-045)")
    print("=" * 78)
    print(f"  percentile summarised per pub : p{report['percentile']:g}")
    print(f"  global reference              : {report['reference']} = "
          f"{report['reference_value']}")
    print(f"  min samples to earn an offset : {report['min_samples']}")
    print("-" * 78)
    print(f"  {'pub_id':<16}{'n':>3}  {'min':>7} {'median':>7} {'max':>7}"
          f"   {'offset':>7}  {'min_aft':>7} {'med_aft':>7}")
    print("-" * 78)
    for pub, s in report["pubs"].items():
        print(f"  {pub:<16}{s['n']:>3}  {s['min']:>7} {s['median']:>7} {s['max']:>7}"
              f"   {s['offset']:>+7} {s['min_after']:>7} {s['median_after']:>7}")
    if report["skipped"]:
        print("-" * 78)
        print("  NO OFFSET (too few rank-1 samples -- would be fitting noise; stays +0.0):")
        for pub, n in sorted(report["skipped"].items()):
            print(f"    {pub:<16} n={n}")
    print("=" * 78)
    print("\nProposed abstention.reranker_score_offsets (paste into config after review):\n")
    # YAML-ready block; empty map printed explicitly so the operator sees the no-op case.
    if offsets:
        for pub, off in offsets.items():
            print(f"    {pub}: {off}")
    else:
        print("    {}   # no publication earned an offset")
    print("\nAlso as JSON:")
    print("  " + json.dumps(offsets, sort_keys=True))
    print("\nREVIEW BEFORE PASTING. Confirm the outlier publication (expected: TCCC, which")
    print("D-045 found scoring negative at rank 1) is the one receiving the largest positive")
    print("offset, and that the reference publication sits near zero. These offsets were")
    print("derived from score DISTRIBUTIONS, not from eval pass/fail -- keep it that way.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--records", required=True,
                    help="tuning.json written by tune_thresholds.py (has records[].top_pub_id)")
    ap.add_argument("--percentile", type=float, default=50.0,
                    help="percentile of each pub's rank-1 scores to align (default 50 = median)")
    ap.add_argument("--reference", choices=("median", "mean", "max"), default="median",
                    help="global reference the per-pub locations are aligned to (default median)")
    ap.add_argument("--min-samples", type=int, default=3,
                    help="min rank-1 samples for a pub to earn an offset (default 3)")
    ap.add_argument("--include-ooc", action="store_true",
                    help="also use out-of-corpus rank-1 scores (default: in-corpus only)")
    ap.add_argument("--out", help="optional: write proposed offsets JSON here (NOT config)")
    args = ap.parse_args()

    path = Path(args.records)
    if not path.exists():
        sys.exit(f"records file not found: {path}")
    data = json.loads(path.read_text())
    records = data.get("records", data if isinstance(data, list) else [])
    if not records:
        sys.exit("no records found in input")

    kinds = None if args.include_ooc else ("in",)
    offsets, report = compute_offsets(
        records, percentile=args.percentile, reference=args.reference,
        min_samples=args.min_samples, kinds=kinds)
    _print_report(offsets, report)

    if args.out:
        # A convenience dump of the PROPOSAL only. This never touches config/default.yaml;
        # a human pastes the reviewed values there deliberately.
        Path(args.out).write_text(json.dumps(
            {"offsets": offsets, "report": report}, indent=2))
        print(f"\nwrote proposal to {args.out} (this is NOT config; paste after review)")


if __name__ == "__main__":
    main()
