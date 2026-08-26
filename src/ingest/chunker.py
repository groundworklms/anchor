#!/usr/bin/env python3
"""Paragraph-level chunker for doctrinal PDFs.

The citation is the product. A chunk is only useful if a human can take its metadata,
open the printed publication, and land on the exact paragraph. Everything here serves
that: printed page numbers (not PDF indices), canonical section names, and paragraph
ordinals that count the way a reader would.

Three document families exist in this corpus, discovered by measurement:

  A. Scanned 1996-97 MCDPs (CVISION OCR). No PDF bookmarks. Printed page numbers in the
     footer as plain integers. Structure must come from the printed table of contents.
  B. Native digital publications (MCDP 1-0, MCDP 7, TC 3-22.9). PDF bookmarks present.
     Chapter-relative page numbers ("7-7", "2-13") in header or footer.
  C. Scanned with header page numbers (MCWP 3-11.3).

Rather than branch on a guessed family, the parser measures each document:

* **Furniture** is found by repetition, not by position. Running headers and footers
  repeat across pages; a line whose digit-normalised form appears on many pages is
  furniture. This works whether the furniture sits at the top or the bottom, and avoids
  a fixed y-threshold that would clip body text on one document while missing headers
  on another.
* **Paragraph breaks** are found by vertical gap, verified to work on both families
  (within-paragraph leading ~13pt vs ~26pt between, in scanned and native alike).
  Indentation is a secondary signal, because in the scanned MCDPs an indent starts a
  paragraph while in MCDP 1-0 an indent marks a bullet continuation -- the opposite.
* **Section names** come from PDF bookmarks when present, else from the printed table of
  contents. All-caps display headings OCR badly (FRICTION -> "F1UcTI0N"), so detected
  headings are fuzzy-matched to the authoritative list and the canonical name is stored.
"""
import argparse
import json
import re
import statistics
import sys
from bisect import bisect_right
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import fitz

REPO = Path(__file__).resolve().parents[2]

OCR_FIXES = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B"})

FURNITURE_MIN_FRAC = 0.15   # a line pattern on >=15% of pages is running furniture
PARA_GAP_RATIO = 1.45       # vertical gap this much over median leading starts a para
INDENT_MIN_PT = 6.0
HEADING_SIZE_RATIO = 1.06
MIN_CHUNK_CHARS = 80

PAGENUM_RE = re.compile(r"^(?:[A-Z]{1,2}-\d{1,3}|\d{1,2}-\d{1,3}|\d{1,4})$")


# ---------------------------------------------------------------------------
# Layout primitives
# ---------------------------------------------------------------------------

def page_lines(page):
    out = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            text = "".join(s["text"] for s in line["spans"]).strip()
            if not text:
                continue
            spans = [s for s in line["spans"] if s["text"].strip()]
            sizes = [s["size"] for s in spans]
            fonts = [s.get("font", "") for s in spans]
            out.append({
                "text": text,
                "x": round(line["bbox"][0], 1),
                "x1": round(line["bbox"][2], 1),
                "y": round(line["bbox"][1], 1),
                "size": round(statistics.median(sizes), 1) if sizes else 0.0,
                "bold": bool(fonts) and all("Bold" in f or "bold" in f for f in fonts),
                "font": Counter(fonts).most_common(1)[0][0] if fonts else "",
            })
    out.sort(key=lambda l: (l["y"], l["x"]))
    return out


def order_by_columns(lines, page_width, min_share=0.20, gutter_frac=0.06):
    """Return lines in reading order, handling two-column pages.

    MCWP 3-11.3 is set in two columns. Sorting its lines by y alone interleaves the
    columns and produces scrambled prose: it yielded 757 chunks at a median of 160
    characters with 279 unrecognised headings, because nearly every line break looked
    like a paragraph break. Detection is automatic and a no-op on single-column
    documents, so it costs nothing where it is not needed.

    A page is treated as two-column only when both halves carry a real share of the
    text AND few lines straddle the gutter -- otherwise a centred heading or a wide
    table would be enough to trigger a false split.
    """
    if not lines or page_width <= 0:
        return lines
    mid = page_width / 2.0
    substantive = [l for l in lines if len(l["text"]) > 20]
    if len(substantive) < 8:
        return lines

    left = [l for l in substantive if l["x"] < mid]
    right = [l for l in substantive if l["x"] >= mid]
    share = min(len(left), len(right)) / len(substantive)
    if share < min_share:
        return lines

    # Lines crossing the gutter indicate full-width content, not two columns.
    gutter_band = page_width * gutter_frac
    straddling = sum(1 for l in substantive
                     if l["x"] < mid - gutter_band / 2 and
                     l.get("x1", l["x"] + 1) > mid + gutter_band / 2)
    if straddling > len(substantive) * 0.15:
        return lines

    col0 = sorted([l for l in lines if l["x"] < mid], key=lambda l: (l["y"], l["x"]))
    col1 = sorted([l for l in lines if l["x"] >= mid], key=lambda l: (l["y"], l["x"]))
    return col0 + col1


def norm_pattern(text):
    """Digit-normalised form, so 'MCDP 1  page 47' and '... 48' collapse together."""
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text)).strip()


def detect_furniture(doc):
    """Find running header/footer patterns by repetition within the margin bands.

    Counted across EVERY page, not a sample, and keyed on position. Running heads in
    these publications often alternate (verso carries the book title, recto the chapter
    title), so a chapter-specific head may appear on only ~10% of pages -- below any
    sensible document-wide percentage threshold. Requiring a small absolute count within
    the top/bottom band is both stricter about position and more forgiving about
    frequency, which is the right trade: a repeated line in the margin band is furniture
    even if it belongs to only one chapter.

    Getting this wrong is not cosmetic. A missed running head is appended into the body
    of whichever paragraph is being carried across the page break, so the chunk text ends
    with a stray chapter title and any quote-matching against it fails.
    """
    counts = Counter()
    for pno in range(doc.page_count):
        page = doc[pno]
        lines = page_lines(page)
        if not lines:
            continue
        h = page.rect.height
        for l in lines:
            band = "top" if l["y"] < h * 0.15 else ("bot" if l["y"] > h * 0.85 else None)
            if band:
                counts[norm_pattern(l["text"])] += 1
    # A bare page number normalises to "#" and must not be discarded -- it is extracted
    # separately as the citation anchor.
    return {p for p, c in counts.items() if c >= 3 and p not in {"#", ""}}


def strip_furniture(lines, furniture):
    """Remove running heads/feet; return body lines plus any printed page number."""
    body, printed = [], None
    for l in lines:
        t = l["text"].strip()
        if PAGENUM_RE.fullmatch(t):
            if printed is None:
                printed = t
            continue
        if norm_pattern(t) in furniture:
            continue
        body.append(l)
    return body, printed


def median_leading(lines):
    gaps = [b["y"] - a["y"] for a, b in zip(lines, lines[1:])
            if 0 < b["y"] - a["y"] < 60]
    return statistics.median(gaps) if gaps else 12.0


# ---------------------------------------------------------------------------
# Structure sources
# ---------------------------------------------------------------------------

def parse_printed_toc(doc, max_scan=25):
    """Recover chapters and section lists from the printed contents pages."""
    chapters = {}
    for pno in range(min(max_scan, doc.page_count)):
        text = doc[pno].get_text()
        if "Chapter" not in text:
            continue
        for m in re.finditer(
                r"Chapter\s+(\d+)\.?\s*\n\s*([^\n]{3,80})\n(.*?)(?=Chapter\s+\d+\.|\Z)",
                text, re.S):
            num = int(m.group(1))
            title = m.group(2).strip()
            flat = re.sub(r"\s*\n\s*", " ", m.group(3)).strip()
            parts = re.split(r"\s*[—–]+\s*|\s*-{2,}\s*", flat)
            sections = [re.sub(r"\s+", " ", p).strip(" .-—") for p in parts]
            sections = [s for s in sections if 2 < len(s) < 70]
            if num not in chapters or len(sections) > len(chapters[num]["sections"]):
                chapters[num] = {"title": title, "sections": sections}
    return chapters


def bookmark_index(doc):
    """Map each PDF page to the innermost bookmark covering it.

    Returns (starts, entries) where entries[i] = {"level","title"} applies from
    starts[i] until starts[i+1].
    """
    toc = doc.get_toc()
    if not toc:
        return None
    flat = []
    for level, title, page in toc:
        if page and page >= 1:
            flat.append((page - 1, level, re.sub(r"\s+", " ", title).strip()))
    flat.sort(key=lambda t: (t[0], t[1]))
    starts = [f[0] for f in flat]
    entries = [{"level": f[1], "title": f[2]} for f in flat]
    return starts, entries


def outline_has_sections(toc):
    """Does this outline name sections, or only chapters?

    MCDP 1-0 ships 26 bookmarks: 24 chapter and front-matter markers, plus two level-2
    entries -- "APPENDIX B" and "Warfighting Functions" -- that belong to an appendix PDF
    embedded whole, so they point at *its* page 1. Trusting that outline for sections
    labelled all 712 chunks of the publication "Warfighting Functions", which is also the
    header fed to the embedding model, so it degraded retrieval on 17% of the corpus.

    The test is whether section-level entries outnumber chapter-level ones. Measured
    across all 13 publications this separates them cleanly: MCDP 1-0 scores 2 against 24
    and falls back to typography; TC 3-22.9 scores 1815 against 12 and keeps its outline.
    """
    levels = Counter(l for l, _, _ in toc)
    return sum(n for l, n in levels.items() if l >= 2) > levels.get(1, 0)


def bookmark_context(idx, pno):
    """Chapter (level 1) and section (deepest) titles covering a page."""
    if not idx:
        return None, None
    starts, entries = idx
    i = bisect_right(starts, pno) - 1
    if i < 0:
        return None, None
    section = entries[i]["title"]
    chapter = None
    for j in range(i, -1, -1):
        if entries[j]["level"] == 1:
            chapter = entries[j]["title"]
            break
    if chapter == section:
        section = None
    return chapter, section


def normalise_heading(s):
    return re.sub(r"\s+", " ", s.translate(OCR_FIXES)).strip()


DATEISH = re.compile(r"^[\dOoIl]{1,2}\s+\w{3,9}\s+[\dOoIl]{4}$")


def clean_heading(raw):
    """Best-effort readable section name for a heading with no table-of-contents match.

    OCR repair is applied ONLY to headings that are actually all-caps scan artefacts.
    Running it on clean text from a native PDF corrupts it: TCCC's title-page date
    "01 May 2026" became the section name "Oi May 2O26" because 0->O and 1->I fired on
    text that was never damaged, and .title() then mangled "for" into "For".
    """
    t = re.sub(r"\s+", " ", raw).strip()
    letters = [c for c in t if c.isalpha()]
    mostly_caps = letters and sum(c.isupper() for c in letters) / len(letters) >= 0.75
    if mostly_caps:
        t = normalise_heading(t).title()
    return t


def is_heading_noise(t):
    """Headings that are typographically real but useless as citation metadata."""
    t = t.strip()
    if DATEISH.match(t):
        return True
    if len(t) < 4:
        return True
    if not any(c.isalpha() for c in t):
        return True
    return t.lower() in {"and", "or", "note", "notes", "continued"}


def match_section(raw, candidates, threshold=0.62):
    if not candidates:
        return None, 0.0
    probe = normalise_heading(raw).lower()
    best, score = None, 0.0
    for c in candidates:
        r = SequenceMatcher(None, probe, c.lower()).ratio()
        if r > score:
            best, score = c, r
    return (best, round(score, 3)) if score >= threshold else (None, round(score, 3))


def font_family(name):
    """PostScript name to family: 'Arial-BoldMT' and 'Arial-ItalicMT' are one family."""
    return re.split(r"[-,]", (name or "").split("+")[-1], 1)[0].lower()


def looks_like_heading(line, body_sz, body_font=None):
    """Detect a display heading.

    Font size is deliberately NOT the primary signal. In these OCR'd scans body text
    sizes jitter across roughly 10.9-12.0pt, so a genuine heading can measure smaller
    than the noise band: MCDP 1's "UNCERTAINTY" is 11.90 against a body median of 11.40,
    a ratio of 1.044 that a size threshold of 1.06 rejects. Missing it silently
    reattributed five paragraphs of the Uncertainty section to Friction -- confidently
    wrong metadata, which is worse for a citation than no metadata at all.

    Capitalisation is the reliable signal: these publications set section headings in
    full capitals and body text in sentence case. Size is kept only as a weak guard
    against picking up small-print footnotes.
    """
    t = line["text"].strip()
    if not (3 <= len(t) <= 90) or t.endswith((".", ",", ";", ":")):
        return False
    letters = [c for c in normalise_heading(t) if c.isalpha()]
    if len(letters) < 3:
        return False

    # Native digital publications set headings in bold at a visibly larger size and in
    # title case, so the capitalisation rule alone never fires on them. TCCC is the clear
    # case: 16pt bold "Basic Management Plan for Care Under Fire/Threat" against 12pt
    # body. Without this branch every TCCC chunk carried section=None -- the worst
    # citations in the corpus, on the one document where precision matters most because
    # its content is clinical.
    if body_sz and line.get("bold") and line["size"] >= body_sz * 1.15:
        return True

    # Natively typeset publications set headings in a different font FAMILY from the
    # body, which is a stronger signal than either size or capitalisation. MCDP 1-0 runs
    # Times New Roman body against Arial-BoldMT heads at both 14pt (all caps, section)
    # and 11pt (title case, subsection) -- the 11pt heads match the body size exactly, so
    # every size-based rule misses them. Requiring at least body size rejects the same
    # font at 8-9pt, which is figure captions and table cells.
    if (body_font and line.get("bold") and line.get("font")
            and font_family(line["font"]) != font_family(body_font)
            and body_sz and line["size"] >= body_sz):
        return True

    if body_sz and line["size"] < body_sz * 0.95:
        return False
    return sum(c.isupper() for c in letters) / len(letters) >= 0.75


# ---------------------------------------------------------------------------
# Paragraph assembly
# ---------------------------------------------------------------------------

NAV_TITLES = {"table of contents", "contents", "glossary", "index", "references",
              "bibliography", "list of figures", "list of tables", "abbreviations"}
DOT_LEADER = re.compile(r"\.{4,}|\. \. \. \.")


def is_navigation_chunk(text, chapter_title, section):
    """Front/back matter that can never be a useful answer, only a distractor.

    Tables of contents survive as ordinary paragraphs -- MCDP 1-0's contents pages chunk
    into runs like "Assessment . . . . . . . . . . 4-7" that carry every keyword in the
    publication and none of its meaning. They were crowding real answers out of the
    ranked list, and one eval question missed entirely because of it.
    """
    for t in (chapter_title, section):
        if t and str(t).strip().lower() in NAV_TITLES:
            return True
    if len(DOT_LEADER.findall(text)) >= 2:
        return True
    # Dense runs of page-number references with little prose.
    alpha = sum(c.isalpha() or c.isspace() for c in text)
    if text and alpha / len(text) < 0.75:
        return True
    return False


def dehyphenate(prev, nxt):
    if prev.endswith("-") and not prev.endswith("--") and nxt[:1].islower():
        return prev[:-1] + nxt
    return prev + " " + nxt


# ---------------------------------------------------------------------------
# Leading endnote / reference apparatus  (defect fixed after D-036 / D-045)
# ---------------------------------------------------------------------------
#
# The chunker glued endnote fragments onto the HEAD of the next real paragraph:
# `7. Ibid., p. 1-6. "All actions in war take place in an atmosphere of uncertainty..."`
# arrived as ONE chunk inside MCDP 7's Notes, quoting MCDP 1. Three of MCDP 7's 13 items
# then cited "MCDP 7, Ch 4: Notes" for doctrine that actually lives in MCDP 1 -- the exact
# wrong-publication attribution this tutor exists to prevent (D-036), and the source of the
# mis-attributed citations the reranker was scoring on (D-045). The fix strips that leading
# apparatus so the real paragraph that follows becomes the chunk's text.
#
# The discipline is the one the review agent validated in D-036 / src/learn/review.py, kept
# as a sibling copy here because the device layout is flat and a cross-directory import
# would not resolve there (same reason chunk_text.py is copied, D-041):
#   * `(?<![\w-])` on the endnote number REFUSES Army numbered paragraphs -- in `3-67.` and
#     `C-11.` the digits follow a hyphen, and TC 3-22.9 alone opens 486 paragraphs that
#     way. Measured in D-036: 0 of those 486 match.
#   * a PAGE REFERENCE (or a publisher imprint) is MANDATORY on the leading marker, which is
#     what keeps this off numbered PROCEDURE lists -- the Care Under Fire steps and the
#     land-nav drills open `1. ... 2. ... 3. ...` with no page ref and must survive whole.

# A printed page reference: "p. 1-6", "pp. 4-21-4-22", "p. 194", "p. A-24", plus compound
# forms such as "pp. 485 and 595-596". The `\b` before `pp?\.` is load-bearing, exactly as
# in review.py: without it the trailing "p." of a word like "ma[p]." followed by a numbered
# list item ("...position on the map. 3. Measure...") reads as a page ref "p. 3" and a
# land-nav drill gets split. Unlike review.py's DETECTION regex this must also CONSUME the
# reference, so it swallows the trailing period and any second range.
_PAGE_REF = (r"\bpp?\.\s*[A-Za-z]?-?\d[\w.–—-]*"
             r"(?:\s*(?:and|through|,|&)\s*(?:\bpp?\.\s*)?[A-Za-z]?-?\d[\w.–—-]*)*")

# A publisher imprint: a colon AND a four-digit year inside ONE set of parentheses, the
# shape review.py measured against 168 real imprints. It ends some citations that carry no
# page number, e.g. "7. ... Self-Directed Learning (New York: Cambridge Books, 1975)."
_IMPRINT = r"\([^()]{0,120}:\s*[^()]{0,120}\b(?:1[6-9]|20)\d{2}\s*\)"

# One leading endnote citation. Like review.py it keeps the `[^.;]` class between marker and
# terminator, because a citation that has to CROSS a sentence-ending period is almost always
# a bibliography entry (author initials "Carol S. Dweck..."), which is not glued prose and
# must be left alone. The single exception is a leading Latin abbreviation ("Ibid.",
# "op. cit."), whose own period we allow through so `7. Ibid., p. 1-6.` still strips. The
# 80-char bound and the mandatory page-ref / imprint terminator stop it wandering into the
# following sentence.
_LEADING_CITATION = re.compile(
    r"(?<![\w-])\d{1,3}\.\s+"
    r"(?:(?:ibid\.|op\.\s*cit\.|loc\.\s*cit\.)[,\s]*)?"
    r"[^.;]{0,80}?"
    r"(?:" + _PAGE_REF + r"|" + _IMPRINT + r")"
    r"(?:\s*" + _PAGE_REF + r")?"
    r"\.?(?=\s|$)",
    re.I,
)

# A bare marker ("18. Tempo is often associated...") whose content is prose, not a citation.
# Stripped only AFTER at least one real citation has been removed (so we are demonstrably
# inside an apparatus run, never at the head of a `1. Move up to the obstacle...` drill),
# and only when a real sentence -- a capital letter or an opening quote -- follows.
_BARE_MARKER = re.compile(r"(?<![\w-])\d{1,3}\.\s+(?=[\"'“‘A-Z])")

# review.py's reference-apparatus signals, reused to decide whether what SURVIVES the strip
# is real doctrine or still apparatus (a bibliography block). If apparatus survives, the
# chunk was never glued prose and is left byte-identical -- item ids derive from the chunk
# text (D-035), so a needless edit would invalidate human approvals it should not touch.
_REF_LATIN = re.compile(r"\b(?:ibid\.|op\.\s*cit\.|loc\.\s*cit\.)", re.I)
_REF_SOURCES = re.compile(r"\bprincipal sources?\s+(?:used|utilized|consulted)", re.I)
_REF_IMPRINT = re.compile(_IMPRINT)
_REF_ENDNOTE = re.compile(r"(?<![\w-])\d{1,3}\.\s+[^.;]{0,60}?\bpp?\.\s*\d")

MIN_DOCTRINE_CHARS = 80   # a real paragraph; below this the leftover is bare apparatus


def _has_reference_apparatus(text):
    return any(rx.search(text) for rx in
               (_REF_LATIN, _REF_SOURCES, _REF_IMPRINT, _REF_ENDNOTE))


def strip_leading_endnotes(text):
    """Strip a leading run of endnote/reference citations off a chunk's text.

    A NO-OP unless the text BEGINS with a real citation (a marker followed within a bounded
    run by a page reference or a publisher imprint). Ordinary doctrine prose, numbered
    procedure lists (`1. Orient the map using the compass...`) and Army numbered paragraphs
    (`3-67.`, `C-11.`) therefore all come back byte-identical. Only when a genuine paragraph
    of doctrine remains after the apparatus -- long enough and itself free of reference
    apparatus -- is the stripped text returned; a chunk that was purely a bibliography is
    also returned unchanged, so its id does not change (D-035). See the block comment above.
    """
    if not text or not _LEADING_CITATION.match(text):
        return text
    rest = text
    while True:
        m = _LEADING_CITATION.match(rest)
        if not m:
            break
        rest = rest[m.end():].lstrip()
    m = _BARE_MARKER.match(rest)
    if m:
        rest = rest[m.end():].lstrip()
    if len(rest) < MIN_DOCTRINE_CHARS or _has_reference_apparatus(rest):
        return text
    return rest


def flush(buf):
    if not buf:
        return None
    text = buf[0]["text"]
    for l in buf[1:]:
        text = dehyphenate(text, l["text"])
    text = re.sub(r"\s+", " ", text).strip()
    # Split off any endnote/reference apparatus glued to the head of the paragraph before it
    # becomes a chunk -- the wrong-publication citation defect from D-036 / D-045.
    return strip_leading_endnotes(text)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_document(path, pub_id, pub_title, edition=None):
    doc = fitz.open(path)
    furniture = detect_furniture(doc)
    bmarks = bookmark_index(doc)
    bmark_sections = bool(bmarks) and outline_has_sections(doc.get_toc())
    printed_toc = parse_printed_toc(doc) if not bmark_sections else {}
    all_sections = [s for c in printed_toc.values() for s in c["sections"]]

    chunks = []
    unmatched = []
    chapter_num = None
    chapter_title = None
    section = None
    section_conf = None
    counters = Counter()          # (chapter, section) -> paragraph ordinal
    section_seq = 0

    # Carry an unterminated paragraph across a page break. Doctrine paragraphs routinely
    # span pages; splitting them would put half a claim in one chunk and half in another,
    # and a citation would then point at a paragraph that does not contain the sentence.
    carry = []
    carry_meta = None
    pending_short = ""
    nav_skipped = []

    for pno in range(doc.page_count):
        page = doc[pno]
        lines = page_lines(page)
        if not lines:
            continue
        lines = order_by_columns(lines, page.rect.width)
        body, printed = strip_furniture(lines, furniture)
        if not body:
            continue

        lead = median_leading(body)
        sizes = [l["size"] for l in body]
        bsz = statistics.median(sizes) if sizes else 0.0
        # Only a clearly dominant font can serve as the baseline. On OCR'd scans every
        # line often reports the same synthesised font, in which case this stays None and
        # the family rule never fires.
        fonts = Counter(l.get("font", "") for l in body if l.get("font"))
        bfont = None
        if fonts:
            top, n = fonts.most_common(1)[0]
            if n >= 0.6 * len(body) and len(fonts) > 1:
                bfont = top
        margin = float(Counter(round(l["x"]) for l in body).most_common(1)[0][0])

        if bmarks:
            ch_title, sec_title = bookmark_context(bmarks, pno)
            if ch_title and ch_title != chapter_title:
                chapter_title = ch_title
                m = re.match(r"(?:Chapter\s+)?(\d+)", ch_title)
                chapter_num = int(m.group(1)) if m else chapter_num
            if not bmark_sections:
                sec_title = None          # chapters from the outline, sections from type
            if sec_title != section and (sec_title or bmark_sections):
                # Clearing matters as much as setting: a section that is never unset
                # follows the reader into every chapter after it.
                section, section_conf = sec_title, 1.0 if sec_title else None
                section_seq += 1
        else:
            joined = " ".join(l["text"] for l in body[:3])
            mch = re.match(r"\s*Chapter\s+(\d+)\b", joined)
            if mch:
                chapter_num = int(mch.group(1))
                chapter_title = (printed_toc.get(chapter_num) or {}).get("title")
                if not chapter_title and len(body) > 1:
                    chapter_title = body[1]["text"].strip()
                section, section_conf, section_seq = None, None, 0

        def emit(text, meta):
            # Short lines are labels, not noise: TCCC's "3. Massive Hemorrhage" is 21
            # characters and introduces the content beneath it. Dropping it removed the
            # only occurrence of that phrase from the whole corpus. Carry it forward so it
            # prefixes the paragraph it heads.
            nonlocal pending_short
            if text and len(text) < MIN_CHUNK_CHARS:
                pending_short = (pending_short + " " + text).strip() if pending_short else text
                return
            if pending_short:
                text = (pending_short + " " + (text or "")).strip()
                pending_short = ""
            if not text or len(text) < MIN_CHUNK_CHARS:
                return
            if is_navigation_chunk(text, meta["chapter_title"], meta["section"]):
                nav_skipped.append(text[:60])
                return
            key = (meta["chapter"], meta["section"])
            counters[key] += 1
            ordinal = counters[key]
            chunks.append({
                "pub_id": pub_id, "pub_title": pub_title, "edition": edition,
                "chapter": meta["chapter"], "chapter_title": meta["chapter_title"],
                "section": meta["section"],
                "section_match_confidence": meta["conf"],
                "para_id": f"{meta['chapter'] or 0}.{meta['seq']}.{ordinal}",
                "page_printed": meta["page_printed"],
                "page_pdf_start": meta["page_pdf"],
                "page_pdf_end": pno + 1,
                "spans_pages": meta["page_pdf"] != pno + 1,
                "text": text, "n_chars": len(text),
                "citation": format_citation(pub_id, edition, meta["chapter_title"],
                                            meta["chapter"], meta["section"],
                                            ordinal, meta["page_printed"]),
            })

        def meta_now():
            return {"chapter": chapter_num, "chapter_title": chapter_title,
                    "section": section, "conf": section_conf, "seq": section_seq,
                    "page_printed": printed, "page_pdf": pno + 1}

        prev_y = None
        for l in body:
            if looks_like_heading(l, bsz, bfont) and not is_heading_noise(l["text"]):
                emit(flush(carry), carry_meta or meta_now())
                carry, carry_meta = [], None
                if not bmark_sections:
                    cand = (printed_toc.get(chapter_num) or {}).get("sections") or all_sections
                    matched, score = match_section(l["text"], cand)
                    if matched:
                        section, section_conf = matched, score
                    else:
                        section = clean_heading(l["text"])
                        section_conf = score
                        unmatched.append({"page_pdf": pno + 1, "raw": l["text"],
                                          "best_score": score})
                    section_seq += 1
                prev_y = l["y"]
                continue

            gap = (l["y"] - prev_y) if prev_y is not None else 0
            indented = l["x"] > margin + INDENT_MIN_PT
            starts_para = (prev_y is not None and gap > lead * PARA_GAP_RATIO) or \
                          (indented and not carry)

            if starts_para and carry:
                emit(flush(carry), carry_meta or meta_now())
                carry, carry_meta = [], None
            if not carry:
                carry_meta = meta_now()
            carry.append(l)
            prev_y = l["y"]

        # Do NOT flush at page end -- the paragraph may continue overleaf. It is flushed
        # when the next page's first line starts a new paragraph, or at document end.
        if carry and carry_meta and carry_meta["page_printed"] is None:
            carry_meta["page_printed"] = printed

    if carry:
        pno = doc.page_count - 1

        def emit_final(text, meta):
            if not text or len(text) < MIN_CHUNK_CHARS:
                return
            if is_navigation_chunk(text, meta["chapter_title"], meta["section"]):
                nav_skipped.append(text[:60])
                return
            key = (meta["chapter"], meta["section"])
            counters[key] += 1
            chunks.append({
                "pub_id": pub_id, "pub_title": pub_title, "edition": edition,
                "chapter": meta["chapter"], "chapter_title": meta["chapter_title"],
                "section": meta["section"], "section_match_confidence": meta["conf"],
                "para_id": f"{meta['chapter'] or 0}.{meta['seq']}.{counters[key]}",
                "page_printed": meta["page_printed"],
                "page_pdf_start": meta["page_pdf"], "page_pdf_end": pno + 1,
                "spans_pages": meta["page_pdf"] != pno + 1,
                "text": text, "n_chars": len(text),
                "citation": format_citation(pub_id, edition, meta["chapter_title"],
                                            meta["chapter"], meta["section"],
                                            counters[key], meta["page_printed"]),
            })
        emit_final(flush(carry), carry_meta)

    return {"pub_id": pub_id, "chunks": chunks, "unmatched_headings": unmatched,
            "nav_chunks_skipped": len(nav_skipped),
            "pages": doc.page_count, "used_bookmarks": bool(bmarks),
            "bookmark_sections": bmark_sections,
            "toc_chapters": len(printed_toc), "toc_sections": len(all_sections),
            "furniture_patterns": sorted(furniture)[:6]}


# Some scanned front-matter headings survive OCR as noise ("Distribution: I42 Ooooo7 Oo",
# "Bmps, T-Bos."). A citation carrying one is still LOCATABLE -- pub, page and paragraph
# are all correct -- but it reads as careless to anyone who sees it, and this project is
# judged on looking trustworthy as well as being trustworthy. Suppress the section name in
# that case rather than inventing one; the rest of the citation still resolves.
def _section_is_usable(section):
    if not section:
        return False
    t = str(section)
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 4:
        return False
    # OCR noise is dense in digits and stray capitals mid-word.
    digits = sum(c.isdigit() for c in t)
    if digits / max(1, len(t)) > 0.15:
        return False
    if any(w.lower().startswith("distribut") for w in t.split()):
        return False
    return True


NUMBERED_CHAPTER = re.compile(r"^chap(?:ter)?\s*\d+$", re.I)


def format_citation(pub_id, edition, chapter_title, chapter, section, ordinal, page):
    if not _section_is_usable(section):
        section = None
    # MCDP 1-0's outline titles its chapters "Chapter 5", which rendered as
    # "Ch 5: Chapter 5". A citation a Marine reads aloud should not stutter.
    if chapter_title and NUMBERED_CHAPTER.match(str(chapter_title).strip()):
        chapter_title = None
    bits = [pub_id]
    if edition:
        bits.append(f"({edition})")
    if chapter_title:
        bits.append(f"Ch {chapter}: {chapter_title}" if chapter else str(chapter_title))
    elif chapter:
        bits.append(f"Ch {chapter}")
    if section:
        bits.append(f'"{section}"')
    bits.append(f"para {ordinal}")
    if page:
        bits.append(f"p.{page}")
    return ", ".join(bits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="corpus/manifest.json")
    ap.add_argument("--only")
    ap.add_argument("--out", default="corpus/chunks.jsonl")
    ap.add_argument("--report", default="corpus/chunk_report.json")
    args = ap.parse_args()

    manifest = json.loads((REPO / args.manifest).read_text(encoding="utf-8"))
    docs = [d for d in manifest["documents"] if d["releasability"]["cleared_for_ingest"]]
    if args.only:
        docs = [d for d in docs if args.only.lower() in d["pub_id"].lower()]
    if not docs:
        sys.exit("no cleared documents matched")

    reports, total = [], 0
    with (REPO / args.out).open("w", encoding="utf-8") as fh:
        for d in docs:
            res = chunk_document(REPO / "corpus" / "pdf" / d["filename"],
                                 d["pub_id"], d["pub_title"],
                                 edition=d.get("publication_date"))
            for c in res["chunks"]:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
            total += len(res["chunks"])
            n = max(1, len(res["chunks"]))
            r = {
                "pub_id": d["pub_id"], "pages": res["pages"], "chunks": len(res["chunks"]),
                "structure_source": "bookmarks" if res["used_bookmarks"] else
                                    (f"printed TOC ({res['toc_chapters']}ch/"
                                     f"{res['toc_sections']}sec)"),
                "pct_with_printed_page": round(100 * sum(
                    1 for c in res["chunks"] if c["page_printed"]) / n, 1),
                "pct_with_section": round(100 * sum(
                    1 for c in res["chunks"] if c["section"]) / n, 1),
                "pct_spanning_pages": round(100 * sum(
                    1 for c in res["chunks"] if c["spans_pages"]) / n, 1),
                "median_chunk_chars": int(statistics.median(
                    [c["n_chars"] for c in res["chunks"]])) if res["chunks"] else 0,
                "nav_chunks_skipped": res.get("nav_chunks_skipped", 0),
                "unmatched_heading_count": len(res["unmatched_headings"]),
                "unmatched_headings": res["unmatched_headings"][:8],
                "furniture_patterns": res["furniture_patterns"],
            }
            reports.append(r)
            print(f"{r['pub_id']:<12} pg={r['pages']:>4} chunks={r['chunks']:>5} "
                  f"page%={r['pct_with_printed_page']:>5} sec%={r['pct_with_section']:>5} "
                  f"span%={r['pct_spanning_pages']:>5} med={r['median_chunk_chars']:>4} "
                  f"nav_drop={r['nav_chunks_skipped']:>4} "
                  f"unmatched={r['unmatched_heading_count']:>3}  {r['structure_source']}")

    (REPO / args.report).write_text(json.dumps(reports, indent=2))
    print(f"\nwrote {REPO / args.out}  ({total:,} chunks)")


if __name__ == "__main__":
    main()
