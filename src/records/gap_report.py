#!/usr/bin/env python3
"""Turn refusals into curriculum intelligence.

A refusal is usually reported as a limitation of the model. It is more useful read as a
measurement of the *corpus*. When the tutor declines, exactly one of three things is true,
and they have completely different owners:

  DOCTRINAL GAP   nothing in the loaded corpus addresses it at any retrieval depth.
                  -> a question for the doctrine proponent.

  CORPUS GAP      the question names a publication that exists but is not loaded.
                  -> a distribution or releasability problem, fixable by us.

  FINDABILITY GAP the corpus DOES contain an answer, but retrieval never surfaced it.
                  -> the publication failed at the only job it has. This is the one an
                     education institution should care about most: doctrine that cannot
                     be found in practice is doctrine that does not function, however
                     well written it is.

Findability is detected by re-running retrieval with a far deeper candidate pool than
production uses. If a chunk that clears the production threshold exists at depth but not
at production depth, the content was reachable and we failed to reach it. Measured on this
corpus, one eval question ("why does doctrine emphasise tempo") needs a pool of k=800 to
surface an answer that is plainly present in MCDP 1 "Speed and Focus".

    python3 gap_report.py --db doctrine.sqlite --records records.sqlite
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "retrieval"))
import hybrid  # noqa: E402
from store import RecordStore  # noqa: E402

# Publication identifiers a user might name. Deliberately broad: the point is to notice
# "you asked about a pub we do not have", not to parse every doctrinal numbering scheme.
PUB_RE = re.compile(
    r"\b((?:JP|MCDP|MCWP|MCRP|MCTP|ADP|ATP|FM|TC|MCO|NTTP|AFDP)\s?[\d]+(?:[-.][\dA-Za-z]+)*)\b",
    re.I)

DEEP_K = 800          # the depth at which we ask "was this findable at all?"
PROD_K = 50


def normalise_pub(p):
    return re.sub(r"\s+", " ", p.upper().replace(".", "-")).strip()


def loaded_publications(db):
    return {normalise_pub(r["pub_id"]) for r in
            db.execute("SELECT DISTINCT pub_id FROM chunks")}


def classify(db, question, threshold, loaded, embed_url, rerank_url):
    # Keep the user's original spelling for display; normalise only for comparison.
    found = {normalise_pub(m): re.sub(r"\s+", " ", m).strip().upper()
             for m in PUB_RE.findall(question)}
    named = set(found.values())
    missing = sorted(orig for norm, orig in found.items()
                     if not any(norm == l or norm.startswith(l) or l.startswith(norm)
                                for l in loaded))

    # Deep retrieval: does an answer exist anywhere in the corpus?
    deep = hybrid.search(db, question,
                         cfg={"top_k_dense": DEEP_K, "top_k_bm25": DEEP_K,
                              "top_n_rerank": 5},
                         embed_url=embed_url, rerank_url=rerank_url)
    deep_top = deep[0].get("rerank_score") if deep else None
    findable = deep_top is not None and deep_top >= threshold

    if findable:
        kind = "FINDABILITY_GAP"
        detail = (f"An answer scoring {deep_top:.2f} exists at retrieval depth "
                  f"{DEEP_K} but not at production depth {PROD_K}. The doctrine is "
                  f"present and was not reached.")
        evidence = [c["citation"] for c in deep[:3]]
    elif missing:
        kind = "CORPUS_GAP"
        detail = (f"Question names {', '.join(missing)}, which is not loaded on this "
                  f"device.")
        evidence = []
    else:
        kind = "DOCTRINAL_GAP"
        detail = ("No chunk clears the answer threshold at any depth searched. The "
                  "loaded corpus does not address this.")
        evidence = [c["citation"] for c in deep[:2]]

    return {"kind": kind, "detail": detail, "deep_top_score": deep_top,
            "named_publications": sorted(named), "missing_publications": missing,
            "evidence": evidence}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/tutor/doctrine.sqlite")
    ap.add_argument("--records", default="/opt/tutor/records.sqlite")
    ap.add_argument("--config", default="/opt/tutor/config/default.yaml")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--out", default="/opt/tutor/gap_report.json")
    ap.add_argument("--embed-url", default="http://127.0.0.1:8081")
    ap.add_argument("--rerank-url", default="http://127.0.0.1:8082")
    args = ap.parse_args()

    threshold = 3.5
    if Path(args.config).exists():
        try:
            import yaml
            cfg = yaml.safe_load(Path(args.config).read_text()) or {}
            t = (cfg.get("abstention") or {}).get("reranker_score_threshold")
            if t is not None:
                threshold = t
        except ImportError:
            pass

    db = hybrid.connect(args.db)
    loaded = loaded_publications(db)
    store = RecordStore(args.records)
    refusals = store.refusals(args.limit)

    if not refusals:
        print("no refusals recorded yet - ask the tutor some questions first")
        return

    rows = []
    for r in refusals:
        c = classify(db, r["question"], threshold, loaded,
                     args.embed_url, args.rerank_url)
        rows.append({"seq": r["seq"], "question": r["question"],
                     "abstain_reason": r["abstain_reason"],
                     "production_score": r["top_score"], **c})
        print(f"  [{c['kind']:<16}] {r['question'][:60]}")

    counts = {}
    for x in rows:
        counts[x["kind"]] = counts.get(x["kind"], 0) + 1

    W = 74
    print("\n" + "=" * W)
    print("  CURRICULUM GAP REPORT")
    print("=" * W)
    print(f"  refusals analysed : {len(rows)}")
    for k in ("FINDABILITY_GAP", "CORPUS_GAP", "DOCTRINAL_GAP"):
        n = counts.get(k, 0)
        print(f"  {k:<18} {n:>4}   {100*n/len(rows):5.1f}%")
    print("-" * W)

    fg = [x for x in rows if x["kind"] == "FINDABILITY_GAP"]
    if fg:
        print("  FINDABILITY GAPS - doctrine that exists but could not be reached:")
        for x in fg[:10]:
            print(f"    Q: {x['question'][:64]}")
            print(f"       answer exists at: {x['evidence'][0] if x['evidence'] else '?'}")
            print(f"       deep score {x['deep_top_score']:.2f} vs production "
                  f"{x['production_score']:.2f}")
    cg = [x for x in rows if x["kind"] == "CORPUS_GAP"]
    if cg:
        wanted = sorted({p for x in cg for p in x["missing_publications"]})
        print(f"\n  CORPUS GAPS - publications asked for but not loaded: "
              f"{', '.join(wanted)}")
    print("=" * W)

    Path(args.out).write_text(json.dumps(
        {"threshold": threshold, "counts": counts, "rows": rows}, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
