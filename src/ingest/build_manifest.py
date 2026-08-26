#!/usr/bin/env python3
"""Build corpus/manifest.json from the screening results plus curated source metadata.

Provenance is not decoration here. Every document records where it came from, when it
was retrieved, its SHA-256, its releasability basis, and — where the automated screen was
insufficient — what a human checked by eye and when.

    python3 src/ingest/build_manifest.py
"""
import hashlib
import json
from pathlib import Path

import fitz

REPO = Path(__file__).resolve().parents[2]
PDF_DIR = REPO / "corpus" / "pdf"
MARINES = "https://www.marines.mil/Portals/1/Publications/"

# filename -> (pub_id, title, source_url, publication_date, pcn)
SOURCES = {
    "MCDP_1_Warfighting.pdf": (
        "MCDP 1", "Warfighting", MARINES + "MCDP%201%20Warfighting.pdf",
        "20 June 1997", "142 000006 00"),
    "MCDP_1-0_Marine_Corps_Operations.pdf": (
        "MCDP 1-0", "Marine Corps Operations", MARINES + "MCDP%201-0%20w%20Ch%201-3.pdf",
        None, "142 000014 00"),
    "MCDP_1-1_Strategy.pdf": (
        "MCDP 1-1", "Strategy", MARINES + "MCDP%201-1%20Strategy.pdf", None, None),
    "MCDP_1-2_Campaigning.pdf": (
        "MCDP 1-2", "Campaigning", MARINES + "MCDP%201-2%20Campaigning.pdf",
        None, "142 000008 00"),
    "MCDP_1-3_Tactics.pdf": (
        "MCDP 1-3", "Tactics", MARINES + "MCDP%201-3%20Tactics.pdf", "30 July 1997", None),
    "MCDP_2_Intelligence.pdf": (
        "MCDP 2", "Intelligence", MARINES + "MCDP%202%20Intelligence.pdf", None, None),
    "MCDP_3_Expeditionary_Operations.pdf": (
        "MCDP 3", "Expeditionary Operations",
        MARINES + "MCDP%203%20Expeditionary%20Operations.pdf", None, "142 000009 00"),
    "MCDP_5_Planning.pdf": (
        "MCDP 5", "Planning", "https://www.marines.mil/portals/1/publications/mcdp%205%20planning.pdf",
        "21 July 1997", None),
    "MCDP_6_Command_and_Control.pdf": (
        "MCDP 6", "Command and Control",
        MARINES + "MCDP%206%20Command%20and%20Control.pdf", "4 October 1996", None),
    "MCDP_7_Learning.pdf": (
        "MCDP 7", "Learning", MARINES + "MCDP%207.pdf", None, "142 000016 00"),
    "MCWP_3-11.3_Scouting_and_Patrolling.pdf": (
        "MCWP 3-11.3", "Scouting and Patrolling",
        MARINES + "MCWP%203-11.3%20%20Scouting%20and%20Patrolling.pdf", None, "143 000075 00"),
    "TC_3-22.9_Rifle_and_Carbine.pdf": (
        "TC 3-22.9", "Rifle and Carbine",
        "https://armypubs.army.mil/epubs/DR_pubs/DR_a/pdf/web/ARN19927_TC_3-22x9_C3_FINAL_WEB.pdf",
        "May 2016 (Change 3)", None),
    "TCCC_Guidelines_2026-05-01.pdf": (
        "TCCC", "Tactical Combat Casualty Care Guidelines",
        "https://learning-media.allogy.com/api/v1/pdf/18ccfdfc-a076-47e9-8a34-376efdd81b43/contents",
        "01 May 2026", None),
}

HOSTS = {
    "marines.mil": "marines.mil - official USMC (MCPEL: publicly releasable publications)",
    "armypubs.army.mil": "armypubs.army.mil - official Army Publishing Directorate",
    "allogy.com": "deployedmedicine.com CDN - the distribution platform JTS/CoTCCC links to "
                  "from jts.health.mil for the current TCCC Guidelines",
}

# Human adjudication of anything the automated screen could not settle by itself.
HUMAN_REVIEW = {
    "MCDP_1_Warfighting.pdf":
        "Automated screen reported no distribution statement. Cover page is an unOCR'd "
        "image; rendered page 1 and read it visually 2026-08-13 -- it carries "
        "'DISTRIBUTION STATEMENT A: Approved for public release; distribution is "
        "unlimited' and 'PCN 142 000006 00'. CLEARED.",
    "MCDP_1-0_Marine_Corps_Operations.pdf":
        "Automated screen flagged upper-case SECRET x2. Reviewed 2026-08-13: both are on "
        "page 274, in a reference list describing the classification of OTHER documents "
        "('Defense Planning and Programming Guidance (Publication is classified "
        "SECRET/NOFORN)'). Not a marking on this document, which carries Distribution "
        "Statement A. CLEARED.",
    "MCWP_3-11.3_Scouting_and_Patrolling.pdf":
        "Automated screen flagged upper-case SECRET x1. Reviewed 2026-08-13: page 36 is an "
        "OCR'd image of a blank message form whose printed classification checkbox row "
        "reads 'TOPSEC SECRET CONF'. A form template, not a marking. Document carries "
        "Distribution Statement A. CLEARED.",
    "TCCC_Guidelines_2026-05-01.pdf":
        "No distribution statement appears anywhere in the document, so the automated "
        "screen could not clear it. CLEARED BY JUDGEMENT 2026-08-13, reasoning recorded: "
        "absence of a distribution statement is normal formatting for clinical practice "
        "guidelines and is not a restriction signal. Published by the Committee on TCCC "
        "via jts.health.mil (.mil, no CAC), mirrored openly on deployedmedicine with no "
        "login, and printed in the Journal of Special Operations Medicine. The document's "
        "purpose is the widest possible dissemination to point-of-injury caregivers. "
        "Edition pinned at 01 May 2026 -- TCCC content changes materially between "
        "editions, so the edition is part of the citation, not a footnote.",
}

# Content-type caveats that survive releasability clearance and belong in the UI, not
# just the manifest.
CONTENT_NOTES = {
    "TCCC_Guidelines_2026-05-01.pdf":
        "CLINICAL CONTENT. Answers drawn from this document are medical guidance. The "
        "citation must always carry the edition date (01 May 2026), because TCCC "
        "recommendations change between editions and a stale answer here is worse than "
        "an abstention.",
}


# Screener findings a human has examined and cleared, keyed narrowly by
# (filename, marking, count-at-adjudication). Narrow on purpose: if the count changes,
# the document changed, and the finding goes back to a human instead of riding on a
# judgement made about different text.
#
# A blanket per-file override was the original approach and it is the wrong shape -- it
# clears findings nobody has ever looked at, which is precisely the failure the
# "stop and ask before ingesting anything ambiguous" guardrail exists to prevent.
ADJUDICATED = {
    ("MCDP_1-0_Marine_Corps_Operations.pdf", "NOFORN", 1): (
        "2026-08-24: References section, PDF p.274. MCDP 1-0 is describing the "
        "classification of OTHER publications it cites -- 'Defense Planning and "
        "Programming Guidance (Publication is classified SECRET/NOFORN)'. The marking "
        "refers to a different document; MCDP 1-0 itself carries Distribution "
        "Statement A. No classified content present."),
    ("MCDP_1-0_Marine_Corps_Operations.pdf", "classification marking", 2): (
        "2026-08-24: Same References list, PDF p.274 -- SECRET/NOFORN and SECRET/LIMDIS, "
        "both naming the classification of cited publications rather than marking this "
        "one. Verified the text does not appear in any chunk."),
    ("MCWP_3-11.3_Scouting_and_Patrolling.pdf", "classification marking", 1): (
        "2026-08-24: OCR of a BLANK message-form graphic, PDF p.36. 'TOPSEC SECRET CONF' "
        "is the pre-printed classification checkbox row on an empty patrol report "
        "template, surrounded by OCR noise ('RAi N CO', '762.3 Vjg 5i 4L'). The form is "
        "unfilled and carries no content. Verified the text does not appear in any "
        "chunk."),
}


def build():
    screening = {r["file"]: r for r in json.loads(
        (REPO / "corpus" / "screening.json").read_text(encoding="utf-8"))}

    docs, unresolved = [], []
    for fname, (pub_id, title, url, date, pcn) in sorted(SOURCES.items()):
        path = PDF_DIR / fname
        if not path.exists():
            continue
        data = path.read_bytes()
        doc = fitz.open(path)
        s = screening.get(fname, {})
        host = next((v for k, v in HOSTS.items() if k in url), "unknown")

        # Clearance needs positive evidence AND the absence of anything disqualifying.
        # It previously needed only the positive half, so a document carrying
        # DISTRIBUTION STATEMENT C would still have cleared as long as the words
        # "approved for public release" appeared anywhere in it -- and the screener's own
        # BLOCKED verdict lived only in its exit code, which nothing consumed.
        blocking = s.get("blocking_findings") or []
        restricted = s.get("distribution_restricted")

        # MCDP 1's statement is on an unOCR'd cover; TCCC has none but was cleared by
        # documented judgement. Both are recorded in HUMAN_REVIEW.
        positive = bool(s.get("distribution_statement_a")) or fname in (
            "MCDP_1_Warfighting.pdf", "TCCC_Guidelines_2026-05-01.pdf")

        # The veto outranks the human allowlist deliberately. Those two were cleared on
        # the ABSENCE of restrictive markings, so a marking found later invalidates that
        # judgement rather than being overridden by it.
        veto, adjudicated = [], []
        if restricted:
            veto.append(f"distribution statement {restricted}")
        for f in blocking:
            key = (fname, f.get("marking"), f.get("count"))
            if key in ADJUDICATED:
                adjudicated.append({"finding": f, "cleared_because": ADJUDICATED[key]})
            else:
                veto.append(str(f)[:160])

        cleared = positive and not veto

        docs.append({
            "pub_id": pub_id,
            "pub_title": title,
            "filename": fname,
            "source_url": url,
            "source_host": host,
            "retrieved_utc": "2026-08-13",
            "retrieval_method": ("direct https" if "armypubs" in url or "allogy" in url
                                 else "browser session (Akamai edge blocks non-browser clients)"),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "pages": doc.page_count,
            "publication_date": date,
            "pcn": pcn or s.get("pcn"),
            "releasability": {
                "cleared_for_ingest": bool(cleared),
                "distribution_statement": "A" if s.get("distribution_statement_a") or
                                          fname == "MCDP_1_Warfighting.pdf" else None,
                "statement_text": ("Approved for public release; distribution is unlimited"
                                   if cleared else None),
                "verified_by": "src/ingest/inspect_corpus.py + human review where noted",
                "verified_utc": "2026-08-13",
                # Recorded so an auditor can see WHY something cleared, and so a veto is
                # visible in the artefact rather than only in a screener exit code.
                "positive_evidence": bool(positive),
                "blocked_by": veto or None,
                "adjudicated_findings": adjudicated or None,
                "human_review": HUMAN_REVIEW.get(fname),
            },
            "content_note": CONTENT_NOTES.get(fname),
            "ingest_notes": {
                "pdf_bookmarks": len(doc.get_toc()),
                "image_only_pages": s.get("unscreenable_image_pages", []),
                # The claim used to be an assertion; it is now a measurement. Every one
                # of the 73 pages with no text layer was rendered at 110dpi and its ink
                # coverage measured -- see corpus/image_page_inspection.json. 72 are
                # blank and cannot carry a marking. The one with content is MCDP 1's
                # cover, inspected visually: it carries DISTRIBUTION STATEMENT A, which
                # is exactly the marking the text screener could not read (D-020).
                "image_only_pages_reviewed":
                    "all 73 rendered and ink-measured 2026-08-25 "
                    "(corpus/image_page_inspection.json); 72 blank, 1 with content "
                    "(MCDP 1 p.1 cover, Distribution Statement A, visually confirmed)",
                "ocr_producer": doc.metadata.get("producer"),
            },
        })

    manifest = {
        "schema_version": 2,
        "generated_utc": "2026-08-13",
        "policy": ("Public-domain / publicly releasable only. No CUI, FOUO, "
                   "distribution-restricted, PII, or classified. Ambiguous releasability is "
                   "skipped and logged, never assumed."),
        "documents": docs,
        "totals": {
            "documents": len(docs),
            "blocked": sum(1 for d in docs
                           if not d["releasability"]["cleared_for_ingest"]),
            "cleared_for_ingest": sum(1 for d in docs
                                      if d["releasability"]["cleared_for_ingest"]),
            "pages": sum(d["pages"] for d in docs),
        },
        "excluded": {
            "Joint Publications": (
                "DELIBERATELY EXCLUDED 2026-08-13. jcs.mil publishes zero PDF links; the "
                "Joint Electronic Library moved behind CAC-gated JEL+ (jdeis.js.mil). GPO "
                "govinfo hosts only the superseded 11 August 2011 JP 3-0 -- the current "
                "publication is retitled 'Joint Campaigns and Operations'. Superseded "
                "doctrine is the one thing that must not enter a corpus whose product is "
                "trustworthy, citable answers. "
                "This exclusion is load-bearing, not a gap: joint-doctrine questions are "
                "now genuine out-of-corpus questions, so the abstention behaviour is "
                "demonstrated against a real corpus boundary rather than a contrived one. "
                "See eval/out_of_corpus.jsonl rows ooc-025 through ooc-028."),
        },
        "known_scope_limits": {
            "MCWP/MCRP/MCTP series (beyond MCWP 3-11.3)": (
                "The MCPEL listing service returned 'ArticleCS is currently unavailable' "
                "during acquisition, so the series could not be enumerated "
                "programmatically. The set here was assembled by probing known publication "
                "paths and is CURATED, NOT EXHAUSTIVE. Re-run enumeration when MCPEL "
                "recovers if fuller series coverage is wanted."),
        },
    }
    out = REPO / "corpus" / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2))
    print(f"wrote {out}")
    print(f"  documents        : {manifest['totals']['documents']}")
    print(f"  cleared          : {manifest['totals']['cleared_for_ingest']}")
    print(f"  total pages      : {manifest['totals']['pages']:,}")
    if unresolved:
        print(f"  NOT cleared      : {', '.join(unresolved)}")
    return manifest


if __name__ == "__main__":
    build()
