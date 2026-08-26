#!/usr/bin/env python3
"""Generate and cache retrieval-practice items from the corpus.

Items are generated ONCE, offline, and cached. Generating on demand would put ~10s of
model time in front of a learner who is waiting to be asked a question, which is exactly
the wrong place to spend it. Caching also makes every item reviewable by an instructor
before a student ever sees it -- an AI-written question that nobody checked is not
something a schoolhouse should put in front of Marines.

Each item carries the paragraph it came from and that paragraph's citation, so grading is
against the source rather than against the model's memory.

    python3 build_items.py --db doctrine.sqlite --out learn_items.json
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

GEN_URL = "http://127.0.0.1:8080/v1/chat/completions"

# Sections whose names came through OCR as noise, plus back-matter that is not teachable.
JUNK = re.compile(r"^(mcd|mcdp|distribution|foreword|preface|notes?|conclusion notes)",
                  re.I)

SYSTEM = """You write recall questions for US Marine Corps professional military education.

You are given ONE paragraph of doctrine. Write a single question that a Marine should be
able to answer from memory after studying it, plus the key points a correct answer must
contain.

Rules:
1. The question must be answerable from the paragraph ALONE. Do not require outside knowledge.
2. Ask for understanding, not trivia. Never ask about page numbers, publication dates, or wording.
3. Open-ended. Never multiple choice, never yes/no.
4. The question must NOT contain its own answer. Never restate a key point in the stem.
   Test it: if a Marine who has never read the paragraph could recite a key point by
   reading the question back, rewrite the question. Name the topic, not the finding --
   ask "what changed and what followed from it", not "how did X strain Y".
5. 3 or fewer key points. Each must carry real content: "four steps: observation,
   orientation, decision, action", never the bare word "observation". A single word is
   never a key point.
6. Reply with ONLY a JSON object, no prose, no code fence:
{"question": "...", "key_points": ["...", "..."]}"""


def post(payload, timeout=300, attempts=4):
    """Retry transient failures.

    llama-server occasionally closes a keep-alive connection between requests, which
    surfaces as RemoteDisconnected. Without a retry, a single one of those ended a run
    that had 521 of 539 sections left to generate.
    """
    body = json.dumps(payload).encode()
    for n in range(attempts):
        req = urllib.request.Request(
            GEN_URL, data=body, headers={"Content-Type": "application/json",
                                         "Connection": "close"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:                                       # noqa: BLE001
            if n == attempts - 1:
                raise
            wait = 2 ** n
            print(f"      ! {type(e).__name__}: retry {n + 1}/{attempts - 1} "
                  f"in {wait}s", flush=True)
            time.sleep(wait)


def gen_item(text):
    d = post({"model": "gemma-4-E2B",
              "messages": [{"role": "system", "content": SYSTEM},
                           {"role": "user", "content": "PARAGRAPH:\n" + text[:1600]}],
              "max_tokens": 320, "temperature": 0.2})
    raw = d["choices"][0]["message"]["content"].strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    q = (obj.get("question") or "").strip()
    kp = [str(k).strip() for k in (obj.get("key_points") or []) if str(k).strip()]
    if len(q) < 15 or not kp:
        return None
    return {"question": q, "key_points": kp[:3]}


def item_id(pub_id, chapter, source_text, ordinal):
    """A stable id derived from the SOURCE PARAGRAPH, not from position in the run.

    Ids used to be `{pub}-c{chapter}-{len(items):04d}` -- a counter. Approvals live in a
    separate table keyed by item id, so regenerating the bank reassigned every existing
    approval to whatever question happened to land in that slot: 74 human-approved items
    would have silently authorised 74 DIFFERENT questions for issue to Marines. The
    review gate would have reported 100% reviewed and been wrong about all of it.

    Hashing the paragraph makes the id a property of the doctrine. Regenerating with a
    better prompt produces the same id for the same paragraph, so an approval either
    still applies or is explicitly invalidated -- never silently transferred. Two items
    from one paragraph are separated by `ordinal`.
    """
    h = hashlib.sha256(" ".join((source_text or "").split()).encode("utf-8")).hexdigest()
    slug = re.sub(r"[^A-Za-z0-9]", "", pub_id)
    return f"{slug}-c{chapter or 0}-{h[:10]}-{ordinal}"


def teachable_sections(db, pubs, min_chars):
    """Every (pub, chapter, section) worth asking about, across the whole corpus.

    Three things this has to get right that the single-publication version did not:

    * **All chapters.** The first build was capped at MCDP 1 chapters 1-4, which is 74
      items -- about forty minutes of practice before a Marine has seen everything.
    * **Publications with no chapter numbering.** TCCC's 43 chunks carry section labels
      but no chapter, so a `chapter IS NOT NULL` filter excluded the single most
      operationally important document in the corpus entirely.
    * **Front matter.** Section detection on natively typeset publications picks up
      signature blocks -- "Robert B. Neller", "By Direction Of The Commandant" -- which
      are real headings and not teachable. In a publication that *does* number its
      chapters, an unnumbered section is front or back matter.
    """
    rows = db.execute(
        "SELECT pub_id, pub_title, chapter, chapter_title, section, COUNT(*) n, "
        "  SUM(CASE WHEN n_chars >= ? THEN 1 ELSE 0 END) big, MIN(id) ord "
        "FROM chunks WHERE section IS NOT NULL "
        "GROUP BY pub_id, chapter, section HAVING n >= 2 AND big >= 1 "
        "ORDER BY pub_id, chapter, ord", (min_chars,)).fetchall()

    numbered = {r["pub_id"] for r in rows if r["chapter"] is not None}
    out = []
    for r in rows:
        if pubs and r["pub_id"] not in pubs:
            continue
        if JUNK.match(r["section"] or ""):
            continue
        if r["chapter"] is None and r["pub_id"] in numbered:
            continue                      # front/back matter of a chaptered publication
        out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/tutor/doctrine.sqlite")
    ap.add_argument("--pubs", default="all",
                    help='comma-separated pub_ids, or "all"')
    ap.add_argument("--out", default="/opt/tutor/learn_items.json")
    ap.add_argument("--per-section", type=int, default=2)
    ap.add_argument("--min-chars", type=int, default=320)
    ap.add_argument("--resume", action="store_true",
                    help="keep items already in --out and only generate what is missing")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    pubs = None if args.pubs.strip().lower() == "all" else         {x.strip() for x in args.pubs.split(",") if x.strip()}
    secs = teachable_sections(db, pubs, args.min_chars)

    out_path_pre = Path(args.out)
    track, items = [], []
    if args.resume and out_path_pre.exists():
        prev = json.loads(out_path_pre.read_text(encoding="utf-8"))
        track, items = prev.get("track", []), prev.get("items", [])
        done = {(t["pub_id"], t["chapter"], t["section"]) for t in track}
        before = len(secs)
        secs = [r for r in secs
                if (r["pub_id"], r["chapter"], r["section"]) not in done]
        print(f"resuming: {len(items)} items already generated, "
              f"{before - len(secs)} sections skipped", flush=True)
    npubs = len({r["pub_id"] for r in secs})
    print(f"{len(secs)} teachable sections across {npubs} publications", flush=True)

    out_path = Path(args.out)
    t0 = time.time()
    for n, s in enumerate(secs, 1):
        chunks = db.execute(
            "SELECT id, text, citation, page_printed, n_chars FROM chunks "
            "WHERE pub_id=? AND chapter IS ? AND section=? AND n_chars >= ? "
            "ORDER BY n_chars DESC LIMIT ?",
            (s["pub_id"], s["chapter"], s["section"], args.min_chars,
             args.per_section)).fetchall()
        made = []
        for ordinal, c in enumerate(chunks):
            try:
                it = gen_item(c["text"])
            except Exception as e:                                   # noqa: BLE001
                print(f"      ! {s['pub_id']} {s['section'][:30]}: "
                      f"{type(e).__name__}, skipping paragraph", flush=True)
                it = None
            if not it:
                continue
            iid = item_id(s["pub_id"], s["chapter"], c["text"], ordinal)
            items.append({
                "id": iid, "pub_id": s["pub_id"],
                "pub_title": s["pub_title"],
                "chapter": s["chapter"], "chapter_title": s["chapter_title"],
                "section": s["section"],
                "question": it["question"], "key_points": it["key_points"],
                "source_chunk_id": c["id"], "source_text": c["text"],
                "citation": c["citation"], "page_printed": c["page_printed"],
            })
            made.append(iid)
        if made:
            track.append({"pub_id": s["pub_id"], "pub_title": s["pub_title"],
                          "chapter": s["chapter"], "chapter_title": s["chapter_title"],
                          "section": s["section"], "item_ids": made})
        print(f"  [{n:>3}/{len(secs)}] {s['pub_id']:<12} ch{str(s['chapter'] or '-'):<3} "
              f"{s['section'][:34]:<36} {len(made)} items ({time.time()-t0:.0f}s)",
              flush=True)

        # Written every section, not only at the end: an hour-long generation run that
        # loses everything to a crash at minute 58 is not a pipeline.
        if n % 5 == 0 or n == len(secs):
            out_path.write_text(json.dumps(
                {"pubs": sorted({t["pub_id"] for t in track}),
                 "generated_s": round(time.time() - t0),
                 "complete": n == len(secs),
                 "track": track, "items": items}, indent=2))

    print(f"\nwrote {args.out}: {len(items)} items across {len(track)} sections "
          f"in {round(time.time()-t0)}s")


if __name__ == "__main__":
    main()
