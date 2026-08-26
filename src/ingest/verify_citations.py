#!/usr/bin/env python3
"""Verify that citations point where they claim to. P1 acceptance criterion.

A citation is only real if a reader can follow it to the page and find the text. This
checks exactly that, mechanically: take the chunk's cited printed page, locate the PDF
page bearing that printed number, and confirm the chunk's own words are on it.

Failure modes this is designed to catch, all of which produce confident-looking but
wrong citations:
  * printed-page offset errors (citing the PDF index instead of the printed number)
  * text assembled from the wrong column on multi-column pages
  * running heads leaking into paragraph text
  * paragraphs merged across a page boundary and then attributed to the wrong page

    python3 src/ingest/verify_citations.py --sample 40 --seed 12
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

import fitz

REPO = Path(__file__).resolve().parents[2]
PAGENUM_RE = re.compile(r"^(?:[A-Z]{1,2}-\d{1,3}|\d{1,2}-\d{1,3}|\d{1,4})$")


def norm(s):
    """Normalise for comparison: OCR spacing and punctuation vary, words do not."""
    return re.sub(r"[^a-z0-9 ]", " ", re.sub(r"\s+", " ", s.lower())).strip()


def compact(s):
    """Collapse to a bare letter/digit stream.

    Chunk text is dehyphenated during ingest ('inani-' + 'mate' -> 'inanimate') while the
    raw page still contains the line-break hyphen. Comparing word sequences therefore
    fails on any probe crossing a hyphenated break -- which is most of them in a
    justified, narrow-column book. Dropping spaces and punctuation entirely makes the
    comparison immune to hyphenation, line wrapping and OCR spacing noise, while
    remaining strict about the actual characters.
    """
    return re.sub(r"[^a-z0-9]", "", s.lower())


def page_text_norm(doc, pno):
    return compact(doc[pno].get_text())


def find_pages_with_printed(doc, printed):
    """PDF page indices whose page carries this printed page number."""
    hits = []
    for pno in range(doc.page_count):
        for line in doc[pno].get_text().splitlines():
            t = line.strip()
            if PAGENUM_RE.fullmatch(t) and t == str(printed):
                hits.append(pno)
                break
    return hits


def probe_terms(text, n=3, span=6):
    """Distinctive word runs from the chunk, avoiding the very edges.

    The first and last few words are where cross-page merges and header leakage do their
    damage, so probes are taken from the interior where a true match must hold.
    """
    words = norm(text).split()
    if len(words) < span * 2:
        return [" ".join(words)] if words else []
    out, step = [], max(1, (len(words) - span) // max(1, n))
    for i in range(n):
        start = min(len(words) - span, 2 + i * step)
        out.append(compact(" ".join(words[start:start + span])))
    return [p for p in out if p]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default="corpus/chunks.jsonl")
    ap.add_argument("--manifest", default="corpus/manifest.json")
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--seed", type=int, default=12)
    ap.add_argument("--out", default="corpus/citation_verification.json")
    args = ap.parse_args()

    chunks = [json.loads(l) for l in
              (REPO / args.chunks).read_text(encoding="utf-8").splitlines() if l.strip()]
    manifest = json.loads((REPO / args.manifest).read_text(encoding="utf-8"))
    files = {d["pub_id"]: d["filename"] for d in manifest["documents"]}

    citable = [c for c in chunks if c.get("page_printed")]
    random.seed(args.seed)
    sample = random.sample(citable, min(args.sample, len(citable)))

    docs, results = {}, []
    for c in sample:
        pub = c["pub_id"]
        if pub not in docs:
            docs[pub] = fitz.open(REPO / "corpus" / "pdf" / files[pub])
        doc = docs[pub]

        pages = find_pages_with_printed(doc, c["page_printed"])
        probes = probe_terms(c["text"])

        verdict, detail = "FAIL", ""
        if not pages:
            verdict, detail = "NO_PAGE", f"no page bears printed number {c['page_printed']}"
        else:
            # A chunk may legitimately span pages, so accept a hit on the cited page or
            # the one following it, and record which.
            # A chunk may legitimately span a page (or column) boundary, so the cited
            # page and its successor are searched as one continuous stream rather than
            # separately -- otherwise a probe straddling the join matches neither half.
            best = None
            for p in pages:
                txt = page_text_norm(doc, p)
                if p + 1 < doc.page_count:
                    txt += page_text_norm(doc, p + 1)
                hits = sum(1 for pr in probes if pr in txt)
                if best is None or hits > best[1]:
                    best = (p, hits)
            if best and best[1] == len(probes):
                verdict = "PASS"
                detail = f"all {len(probes)} probes found on pdf page {best[0] + 1}"
            elif best and best[1] > 0:
                verdict = "PARTIAL"
                detail = (f"{best[1]}/{len(probes)} probes on pdf page {best[0] + 1} "
                          f"(chunk spans pages: {c.get('spans_pages')})")
            else:
                detail = f"0/{len(probes)} probes found near printed page {c['page_printed']}"

        results.append({
            "pub_id": pub, "citation": c["citation"], "verdict": verdict,
            "detail": detail, "page_printed": c["page_printed"],
            "page_pdf_start": c.get("page_pdf_start"),
            "spans_pages": c.get("spans_pages"),
            "text_head": c["text"][:110],
        })

    tally = {}
    for r in results:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1

    print(f"\nverified {len(results)} sampled citations "
          f"(seed={args.seed}, from {len(citable):,} citable chunks)\n")
    for r in results:
        if r["verdict"] != "PASS":
            print(f"  [{r['verdict']}] {r['citation']}")
            print(f"        {r['detail']}")
            print(f"        text: {r['text_head']}...")
    print("\n  " + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    passed = tally.get("PASS", 0)
    print(f"  strict pass rate: {100 * passed / max(1, len(results)):.1f}%")
    soft = passed + tally.get("PARTIAL", 0)
    print(f"  pass or partial : {100 * soft / max(1, len(results)):.1f}%")

    (REPO / args.out).write_text(json.dumps(
        {"seed": args.seed, "sampled": len(results), "tally": tally,
         "results": results}, indent=2))
    print(f"\nwrote {REPO / args.out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
