#!/usr/bin/env python3
"""A verbatim-specific backstop: refuse when a LOAD-BEARING specific was fabricated.

============================================================================
NOT IN THE ANSWER PATH. As of this build, nothing imports specifics_gate: it is
NOT wired into pipeline.ask() and does NOT run on any live query. The false-premise
class it targets is handled in production by the premise pre-gate (Gate 1.5 in
pipeline.py, backed by the HHEM entailment verifier in grounding_verifier.py). This
module is retained as a documented, stdlib-only EXPERIMENT / reference implementation
of a lexical specifics backstop and is exercised only by tests/test_specifics_gate.py.
Do not assume any behaviour here affects a shipped answer; to actually use it, it would
first have to be imported and called from the pipeline.
============================================================================

WHY THIS EXISTS. Gate 2 in pipeline.py checks that every claim SENTENCE is grounded in a
retrieved chunk (D-023). That catches a sentence built entirely from nothing. It does NOT
reliably catch the failure mode this module targets: on a FALSE-PREMISE question the 2B
model retrieves correct, on-topic passages and then paraphrases them faithfully -- while
smuggling in one fabricated SPECIFIC that the premise invited. Observed on this corpus:

    "the SEVEN phases of ..."        a false COUNT   (the doctrine lists six)
    "the 2011 TCCC Guidelines ...    suzetrigine"    a non-existent EDITION/YEAR + drug
    "... described on pages 35-36"    a fabricated PAGE
    "... a principle Patton credits"  a MISATTRIBUTION to a name never in the sources

The surrounding sentence is otherwise well-grounded, so sentence-level grounding lets it
through. But the fabricated token itself -- the number, the year, the page, the name -- is
NOT present in any retrieved passage. That is the signal: an asserted specific that appears
in NONE of the spans (verbatim, after light normalization) is unsupported.

CONSERVATIVE BIAS -- THIS IS THE WHOLE DESIGN. A false positive here is an OVER-REFUSAL, and
over-refusal is the failure we are least willing to add. So the gate leans hard toward
SILENCE:

  * Normalization is AGGRESSIVE on the SUPPORT side, so a specific is easy to call
    "supported": number words == digits ("seven" == 7), ordinals fold ("3rd" == "third"),
    a page range and its endpoints all match each other ("35-36" ~ "p. 35" ~ "35–36"),
    a name is supported if merely its surname shows up. Every one of those choices makes
    the gate MORE forgiving, never less.
  * Extraction is CONSERVATIVE: proper nouns are multi-word only (a lone capitalized word
    is never a name -- too many doctrine terms and sentence starts look like one), and a
    stoplist drops "Marine Corps", "Tactical Combat Casualty Care", and friends.
  * refuse is NOT raised just because SOMETHING is unsupported. It is raised only when an
    unsupported specific is LOAD-BEARING -- i.e. the QUESTION asked for a specific of that
    exact kind ("how many ...", a year/edition, "page", "who/credited/attributed ...") and
    the answer supplied an unsupported one of that kind. A stray unsupported number in an
    answer to a question that never asked for a count is reported, but does not refuse.

The honest cost of the conservative bias is MISSES: a fabricated count that coincidentally
equals some other number sitting in a span is called "supported"; a fabricated single-name
attribution ("Patton") is never extracted at all. We accept those misses to protect against
over-refusal. See the test file for the enumerated blind spots.

Pure stdlib, no cross-module imports: this file is deployed flat to /opt/tutor/generation/
and imported as `import specifics_gate`, so it must stand alone.
"""
import re

# ---------------------------------------------------------------------------
# Number-word vocabulary. Kept small on purpose: doctrine counts are little
# ("six phases", "seven steps"), editions are years, pages are one or two digits.
# ---------------------------------------------------------------------------
_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUM2WORD = {v: k for k, v in _ONES.items()}
_NUM2WORD.update({v: k for k, v in _TENS.items()})

_ORD_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
}
_NUM2ORDWORD = {v: k for k, v in _ORD_WORDS.items()}

# Words that may appear inside a spelled-out number run. "and" is deliberately
# EXCLUDED so "seven and eight" is read as two numbers, not 15.
_NUM_RUN_WORDS = set(_ONES) | set(_TENS) | {"hundred"}

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------
# Citation markers ([1], [2][3]) are pipeline bookkeeping, never asserted specifics.
_CITE_RE = re.compile(r"\[\d+\]")
_YEAR_RE = re.compile(r"\b(1\d{3}|20\d{2})\b")
_PAGE_RE = re.compile(
    r"\b(?:pp?\.?|pages?)\s*(\d+)\s*(?:[-‐-―]\s*(\d+))?", re.IGNORECASE)
_ORD_DIGIT_RE = re.compile(r"\b(\d+)(st|nd|rd|th)\b", re.IGNORECASE)
_DIGIT_RE = re.compile(r"\b\d+\b")
_ORD_WORD_RE = re.compile(r"\b(" + "|".join(_ORD_WORDS) + r")\b", re.IGNORECASE)
_NUM_RUN_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_NUM_RUN_WORDS, key=len, reverse=True)) + r")"
    r"(?:[\s-]+(?:" + "|".join(sorted(_NUM_RUN_WORDS, key=len, reverse=True)) + r"))*\b",
    re.IGNORECASE)

# A run of capitalized tokens / initials / lowercase name particles.
_NAME_RUN_RE = re.compile(
    r"\b[A-Z][A-Za-z]*\.?(?:\s+(?:[A-Z][A-Za-z]*\.?|of|the|von|van|de|del|la))*")
_NAME_WORD_RE = re.compile(r"^[A-Z][a-z]+$")   # a real name word: Alfred, Gray
_INITIAL_RE = re.compile(r"^[A-Z]\.?$")        # a middle initial: M or M.
_WORD_TOKEN_RE = re.compile(r"[A-Za-z]+")

# Tokens that, on their own, never make a sequence a person/proper name. If every
# significant token of a candidate is in here, the candidate is dropped. This is the
# conservative heart of proper-noun extraction: doctrine is full of Capitalized Org
# Words and every sentence starts with a Capitalized word.
_NAME_STOP = {
    # function / sentence-initial words
    "the", "this", "that", "these", "those", "there", "here", "it", "in", "on", "at",
    "a", "an", "and", "or", "but", "if", "when", "while", "for", "from", "with", "by",
    "as", "to", "of", "all", "each", "both", "per", "see", "note", "however",
    # doctrine / org / structural terms
    "marine", "corps", "united", "states", "tactical", "combat", "casualty", "care",
    "field", "manual", "department", "defense", "committee", "guidelines", "guideline",
    "army", "navy", "air", "force", "joint", "publication", "headquarters", "section",
    "chapter", "appendix", "table", "figure", "annex", "enclosure", "phase", "phases",
    "step", "steps", "principle", "principles", "doctrine", "edition", "page", "pages",
    "tccc", "mccmos", "usmc", "warfighting", "general", "colonel", "major", "captain",
    "president", "committee's",
}

# Question intent cues.
_Q_CARDINAL_RE = re.compile(
    r"\bhow many\b|\bnumber of\b|\bhow much\b|"
    r"\bthe\s+(?:" + "|".join(_ONES) + "|" + "|".join(_TENS) + r"|\d+)\s+\w+", re.IGNORECASE)
_Q_PAGE_RE = re.compile(r"\bpages?\b|\bpp?\.", re.IGNORECASE)
_Q_YEAR_RE = re.compile(r"\bedition\b|\bwhat year\b|\bwhich year\b", re.IGNORECASE)
_Q_NAME_RE = re.compile(
    r"\bwho\b|\bwhom\b|\bcredit(?:s|ed)?\b|\battribut(?:e|es|ed|ion)\b|"
    r"\bquot(?:e|es|ed|ation)\b|\bwrote\b|\bauthor(?:ed|s)?\b|\bsaid\b|\bnamed?\b",
    re.IGNORECASE)


# ---------------------------------------------------------------------------
# Number-word helpers
# ---------------------------------------------------------------------------
def _strip_cites(text):
    """Blank out [N] citation markers with equal-length whitespace (positions preserved)."""
    return _CITE_RE.sub(lambda m: " " * (m.end() - m.start()), text)


def _words_to_int(phrase):
    """Fold a spelled-out number ('twenty-one', 'one hundred seven') into an int, or None."""
    tokens = re.split(r"[\s-]+", phrase.strip().lower())
    total, current, seen = 0, 0, False
    for tok in tokens:
        if tok in _ONES:
            current += _ONES[tok]
            seen = True
        elif tok in _TENS:
            current += _TENS[tok]
            seen = True
        elif tok == "hundred":
            current = (current or 1) * 100
            seen = True
        else:
            return None
    return total + current if seen else None


def _ordinal_suffix(n):
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _cardinal_norms(value, raw):
    """Surface variants of a count, digit and word, so a reader sees why it matched."""
    norms = {str(value)}
    if raw:
        norms.add(raw.strip().lower())
    if value in _NUM2WORD:
        norms.add(_NUM2WORD[value])
    return sorted(norms)


def _ordinal_norms(value):
    norms = {f"{value}{_ordinal_suffix(value)}"}
    if value in _NUM2ORDWORD:
        norms.add(_NUM2ORDWORD[value])
    return sorted(norms)


def _page_norms(start, end):
    """A page reference and, when a range, its endpoints -- all treated as one family."""
    norms = [f"page:{start}"]
    if end is not None:
        norms.append(f"page:{end}")
        norms.append(f"page:{start}-{end}")
    return norms


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def extract_specifics(text):
    """Return the specifics asserted in `text`.

    Each item is {"kind", "raw", "norms"} where kind is one of
    "cardinal" | "ordinal" | "year" | "page" | "proper_noun". `norms` are the light-
    normalized surface forms used for support comparison (see the module docstring).
    """
    if not text:
        return []
    text = _strip_cites(text)

    specifics = []
    consumed = []  # char spans already claimed by a higher-priority kind

    def _overlaps(a, b):
        return any(not (b <= s or a >= e) for s, e in consumed)

    # 1. Pages (claim their digits so they are not re-read as bare counts).
    for m in _PAGE_RE.finditer(text):
        start, end = m.group(1), m.group(2)
        specifics.append({"kind": "page", "raw": m.group(0).strip(),
                          "norms": _page_norms(start, end)})
        consumed.append((m.start(), m.end()))

    # 2. Years / edition years.
    for m in _YEAR_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        specifics.append({"kind": "year", "raw": m.group(0), "norms": [m.group(0)]})
        consumed.append((m.start(), m.end()))

    # 3. Ordinals: digit form (3rd) then word form (third).
    for m in _ORD_DIGIT_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        value = int(m.group(1))
        specifics.append({"kind": "ordinal", "raw": m.group(0),
                          "norms": _ordinal_norms(value)})
        consumed.append((m.start(), m.end()))
    for m in _ORD_WORD_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        value = _ORD_WORDS[m.group(1).lower()]
        specifics.append({"kind": "ordinal", "raw": m.group(0),
                          "norms": _ordinal_norms(value)})
        consumed.append((m.start(), m.end()))

    # 4. Cardinals: spelled-out runs, then bare digits, minus anything already claimed.
    for m in _NUM_RUN_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        value = _words_to_int(m.group(0))
        if value is None:
            continue
        specifics.append({"kind": "cardinal", "raw": m.group(0),
                          "norms": _cardinal_norms(value, m.group(0))})
        consumed.append((m.start(), m.end()))
    for m in _DIGIT_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        value = int(m.group(0))
        specifics.append({"kind": "cardinal", "raw": m.group(0),
                          "norms": _cardinal_norms(value, m.group(0))})
        consumed.append((m.start(), m.end()))

    # 5. Proper nouns: multi-word person/proper names only.
    for m in _NAME_RUN_RE.finditer(text):
        name = _proper_noun_from_run(m.group(0))
        if name is not None:
            specifics.append(name)

    return specifics


def _proper_noun_from_run(run):
    """Turn a capitalized run into a proper_noun specific, or None if it is not a name.

    Conservative: needs at least two SIGNIFICANT tokens (real name words or initials,
    none of them stoplisted). This rejects lone capitals, all-caps acronyms, and
    sequences built only from doctrine/org/function words.
    """
    tokens = run.split()
    # Trim trailing connective particles ("Gray of" -> "Gray").
    while tokens and tokens[-1].lower() in {"of", "the", "von", "van", "de", "del", "la"}:
        tokens.pop()

    significant = []
    name_words = []
    for tok in tokens:
        bare = tok.rstrip(".")
        low = bare.lower()
        is_name = bool(_NAME_WORD_RE.match(bare))
        is_initial = bool(_INITIAL_RE.match(tok))
        if (is_name or is_initial) and low not in _NAME_STOP:
            significant.append(low)
            if is_name:
                name_words.append(low)

    if len(significant) < 2 or not name_words:
        return None

    full = " ".join(t.rstrip(".").lower() for t in tokens if t.rstrip("."))
    surname = name_words[-1]
    return {"kind": "proper_noun", "raw": run.strip(), "norms": [full, surname]}


# ---------------------------------------------------------------------------
# Span signatures (the aggressive-support side)
# ---------------------------------------------------------------------------
def _numbers_in(text):
    """Every number in `text` as a digit string -- digits and spelled-out runs alike."""
    nums = set()
    for m in _DIGIT_RE.finditer(text):
        nums.add(str(int(m.group(0))))
    for m in _NUM_RUN_RE.finditer(text):
        value = _words_to_int(m.group(0))
        if value is not None:
            nums.add(str(value))
    return nums


def _ordinals_in(text):
    ords_ = set()
    for m in _ORD_DIGIT_RE.finditer(text):
        ords_.add(f"ord:{int(m.group(1))}")
    for m in _ORD_WORD_RE.finditer(text):
        ords_.add(f"ord:{_ORD_WORDS[m.group(1).lower()]}")
    return ords_


def _pages_in(text):
    pages = set()
    for m in _PAGE_RE.finditer(text):
        for n in _page_norms(m.group(1), m.group(2)):
            pages.add(n)
    return pages


def _span_signatures(spans):
    """Fold all spans into the lookup structures support-checking needs."""
    blob = _strip_cites("  ".join(spans or []))
    return {
        "num": _numbers_in(blob),
        "ord": _ordinals_in(blob),
        "page": _pages_in(blob),
        "words": {w.lower() for w in _WORD_TOKEN_RE.findall(blob)},
        "text": re.sub(r"\s+", " ", blob.replace(".", " ")).lower(),
    }


def _ord_key_from_norms(norms):
    for n in norms:
        m = re.match(r"(\d+)", n)
        if m:
            return f"ord:{int(m.group(1))}"
        if n.lower() in _ORD_WORDS:
            return f"ord:{_ORD_WORDS[n.lower()]}"
    return None


def _num_key_from_norms(norms):
    for n in norms:
        if n.isdigit():
            return str(int(n))
    return None


def _is_supported(specific, sig):
    """True if the specific appears, in ANY normalized form, in ANY span."""
    kind, norms = specific["kind"], specific["norms"]

    if kind in ("cardinal", "year"):
        key = _num_key_from_norms(norms)
        return key is not None and key in sig["num"]

    if kind == "ordinal":
        key = _ord_key_from_norms(norms)
        return key is not None and key in sig["ord"]

    if kind == "page":
        return any(n in sig["page"] for n in norms)

    if kind == "proper_noun":
        full, surname = norms[0], norms[-1]
        if full and full in sig["text"]:
            return True
        return surname in sig["words"]

    return True  # unknown kind: never flag


# ---------------------------------------------------------------------------
# Public gate
# ---------------------------------------------------------------------------
def unsupported_specifics(answer, spans):
    """Specifics asserted in `answer` whose norms appear in NONE of `spans`."""
    sig = _span_signatures(spans)
    return [s for s in extract_specifics(answer) if not _is_supported(s, sig)]


def _question_kinds(question):
    """Which specific-kinds the QUESTION asks for -- i.e. which are load-bearing."""
    kinds = set()
    if not question:
        return kinds
    if _Q_CARDINAL_RE.search(question):
        kinds.add("cardinal")
    if _Q_PAGE_RE.search(question):
        kinds.add("page")
    if _Q_YEAR_RE.search(question) or _YEAR_RE.search(question):
        kinds.add("year")
    if _Q_NAME_RE.search(question):
        kinds.add("proper_noun")
    return kinds


_KIND_LABEL = {
    "cardinal": "count", "ordinal": "ordinal", "year": "year/edition",
    "page": "page reference", "proper_noun": "name",
}


def specifics_verdict(answer, question, spans):
    """Decide whether to refuse because a LOAD-BEARING fabricated specific slipped through.

    Returns {"unsupported": [...], "refuse": bool, "reason": str}. `refuse` is True ONLY
    when an unsupported specific's kind is one the QUESTION asked for -- the tie to intent
    is what keeps the conservative bias from becoming over-refusal.
    """
    unsupported = unsupported_specifics(answer, spans)
    asked = _question_kinds(question)
    load_bearing = [s for s in unsupported if s["kind"] in asked]

    if load_bearing:
        first = load_bearing[0]
        label = _KIND_LABEL.get(first["kind"], first["kind"])
        raws = ", ".join(sorted({s["raw"] for s in load_bearing}))
        reason = (
            f"The question asks for a {label}; the answer supplies "
            f"'{raws}', which appears in none of the retrieved sources. "
            f"Refusing rather than emit an unsupported specific.")
        return {"unsupported": unsupported, "refuse": True, "reason": reason}

    if unsupported:
        raws = ", ".join(sorted({s["raw"] for s in unsupported}))
        reason = (
            f"{len(unsupported)} unsupported specific(s) present ('{raws}') but none "
            f"match what the question asked, so the gate stays silent to avoid "
            f"over-refusal.")
    else:
        reason = "Every asserted specific is supported by a retrieved source."
    return {"unsupported": unsupported, "refuse": False, "reason": reason}
