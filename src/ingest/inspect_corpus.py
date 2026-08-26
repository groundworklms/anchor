#!/usr/bin/env python3
"""Releasability screening for corpus PDFs. Run BEFORE ingest, always.

The guardrail is: public-domain / publicly releasable only. No CUI, FOUO,
distribution-restricted, PII, or classified material. If releasability is ambiguous,
skip it and log it.

This script does not decide releasability. It surfaces every marking it can find so a
human can decide, and it fails loudly rather than quietly passing something through.

    python3 src/ingest/inspect_corpus.py corpus/pdf/*.pdf
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF

# Markings that must stop ingest until a human clears them.
#
# Matched CASE-SENSITIVELY against upper-case forms only. Real classification and
# handling markings are always upper case, usually isolated in a header, footer, or
# cover block. Matching case-insensitively produces false positives from ordinary
# prose -- verified 2026-08-13, when "there was nothing secret about the German attack"
# and Nathan Bedford Forrest's "told the secret of his many victories" both tripped a
# case-insensitive \bSECRET\b in MCDP 1-3 and flagged a Distribution Statement A
# document as BLOCKED.
#
# Lower-case occurrences are still surfaced separately as informational, so a genuine
# marking rendered oddly by OCR is not silently dropped.
BLOCKING = [
    (r"\bCUI\b", "CUI marking"),
    (r"CONTROLLED\s+UNCLASSIFIED", "Controlled Unclassified Information"),
    (r"\bFOUO\b", "FOUO"),
    (r"FOR\s+OFFICIAL\s+USE\s+ONLY", "For Official Use Only"),
    (r"\bNOFORN\b", "NOFORN"),
    (r"\bSECRET\b", "classification marking"),
    (r"\bCONFIDENTIAL\b", "classification marking"),
    (r"\bTOP\s+SECRET\b", "classification marking"),
    (r"LAW\s+ENFORCEMENT\s+SENSITIVE", "LES"),
    (r"EXPORT\s+CONTROL", "export control"),
    (r"\bITAR\b", "ITAR"),
]

# Distribution statements B through F are all restricted. A is the one we want.
DIST_RESTRICTED = re.compile(
    r"DISTRIBUTION\s+STATEMENT\s+([B-F])\b", re.I)
DIST_A = re.compile(
    r"DISTRIBUTION\s+STATEMENT\s+A\b|approved\s+for\s+public\s+release", re.I)

PCN = re.compile(r"PCN\s*[:\s]\s*([\d\s]{6,})", re.I)


def screen(path, scan_pages=8):
    doc = fitz.open(path)
    data = path.read_bytes()

    # Front matter carries the markings; also scan the last pages where
    # distribution statements are sometimes repeated.
    idx = list(range(min(scan_pages, doc.page_count)))
    idx += [p for p in range(max(0, doc.page_count - 3), doc.page_count) if p not in idx]
    front = "\n".join(doc[p].get_text() for p in idx)

    full = "\n".join(doc[p].get_text() for p in range(doc.page_count))

    findings, informational = [], []
    for pattern, label in BLOCKING:
        upper = [m.group(0) for m in re.finditer(pattern, full)]          # case-sensitive
        any_case = [m.group(0) for m in re.finditer(pattern, full, re.I)]
        if upper:
            findings.append({"marking": label, "count": len(upper), "sample": upper[0]})
        elif any_case:
            informational.append({"marking": label, "count": len(any_case),
                                  "sample": any_case[0],
                                  "note": "lower-case only; likely prose, not a marking"})

    # Pages with no text layer cannot be screened at all. This is not a clean bill of
    # health -- it is a blind spot, and it must be reported as one.
    #
    # Found the hard way 2026-08-13: MCDP 1's cover page is an unOCR'd image, and its
    # DISTRIBUTION STATEMENT A lives on exactly that page. The screener saw no statement
    # and reported REVIEW. Had the marking been restrictive instead, this tool would have
    # reported "no blocking markings found" on a document that was plainly marked.
    unscreenable = [p + 1 for p in range(doc.page_count)
                    if not doc[p].get_text().strip() and doc[p].get_images()]

    restricted = DIST_RESTRICTED.search(full)
    dist_a = bool(DIST_A.search(full))
    pcn = PCN.search(front)

    return {
        "unscreenable_image_pages": unscreenable,
        "informational_findings": informational,
        "file": path.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "pages": doc.page_count,
        "pdf_metadata": {k: v for k, v in doc.metadata.items() if v},
        "toc_entries": len(doc.get_toc()),
        "extractable_chars": len(full),
        "text_layer_ok": len(full) > 2000,
        "pcn": pcn.group(1).strip() if pcn else None,
        "distribution_statement_a": dist_a,
        "distribution_restricted": restricted.group(1).upper() if restricted else None,
        "blocking_findings": findings,
        "front_matter_excerpt": re.sub(r"\n{2,}", "\n", front[:1200]).strip(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--json-out")
    args = ap.parse_args()

    results, blocked = [], []
    for p in args.pdfs:
        path = Path(p)
        if not path.exists():
            print(f"MISSING: {p}", file=sys.stderr)
            continue
        r = screen(path)
        results.append(r)

        flag = "CLEAR"
        if r["blocking_findings"] or r["distribution_restricted"]:
            flag = "BLOCKED"
            blocked.append(r["file"])
        elif not r["distribution_statement_a"]:
            # Absence of an explicit statement is not proof of restriction, but it is
            # exactly the ambiguity the guardrail says to escalate rather than assume.
            flag = "REVIEW"
        elif r["unscreenable_image_pages"]:
            # Statement found, but part of the document could not be read.
            flag = "CLEAR (with blind spots)"

        print(f"\n{'=' * 70}")
        print(f"  {r['file']}   [{flag}]")
        print(f"{'=' * 70}")
        print(f"  pages              : {r['pages']}")
        print(f"  bytes              : {r['bytes']:,}")
        print(f"  sha256             : {r['sha256'][:32]}...")
        print(f"  text layer         : {'ok' if r['text_layer_ok'] else 'MISSING - would need OCR'}"
              f"  ({r['extractable_chars']:,} chars)")
        print(f"  bookmarks/TOC      : {r['toc_entries']} entries")
        print(f"  PCN                : {r['pcn']}")
        print(f"  dist statement A   : {r['distribution_statement_a']}")
        print(f"  dist restricted    : {r['distribution_restricted'] or 'none found'}")
        if r["pdf_metadata"]:
            print(f"  pdf metadata       : {r['pdf_metadata']}")
        if r["blocking_findings"]:
            print("  !! BLOCKING MARKINGS:")
            for f in r["blocking_findings"]:
                print(f"       {f['marking']}  x{f['count']}  e.g. {f['sample']!r}")
        if r["informational_findings"]:
            print("  -- lower-case matches (likely prose, review if unexpected):")
            for f in r["informational_findings"]:
                print(f"       {f['marking']}  x{f['count']}  e.g. {f['sample']!r}")
        if r["unscreenable_image_pages"]:
            pages = r["unscreenable_image_pages"]
            shown = pages[:12]
            print(f"  !! {len(pages)} page(s) have NO text layer and could not be screened: "
                  f"{shown}{' ...' if len(pages) > len(shown) else ''}")
            print("     Render and read these by eye before trusting this result.")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json_out}")

    print()
    if blocked:
        print(f"BLOCKED ({len(blocked)}): {', '.join(blocked)}")
        print("Do not ingest these until a human clears them.")
        return 1
    print(f"No blocking markings found in {len(results)} document(s).")
    print("Note: absence of a marking is not a releasability determination.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
