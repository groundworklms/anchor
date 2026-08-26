#!/usr/bin/env python3
"""Premise pre-gate: verify a question's PRESUPPOSITIONS before the 2B answers.

WHY THIS EXISTS. Gate 1 (reranker) and Gate 2 (sentence grounding) in pipeline.py both
judge the ANSWER. Neither looks at the false premise smuggled INTO the question. A 2B
generator handed "What are the SEVEN phases of the intelligence cycle?" will dutifully
manufacture a seventh phase to be helpful, and because every sentence it writes can be
loosely grounded in the (real, six-phase) passages, Gate 2 waves it through. The lie is
in the question, not the answer, so a gate on the answer cannot see it.

THE INSIGHT WE BUILD ON. (QA)^2 (Kim et al., 2021) showed a model is far better at
verifying an ISOLATED presupposition than at catching a false premise embedded in a
question it is busy trying to answer. So we do exactly that: extract each factual
presupposition as a standalone declarative ("The intelligence cycle has seven phases."),
check THAT one claim against the retrieved passages, and if a load-bearing presupposition
is unsupported we abstain/correct instead of letting the generator fabricate around it.

    "What are the SEVEN phases of the intelligence cycle?"  -> it has six      (count)
    "According to the 2011 TCCC Guidelines, the suzetrigine dose?" -> anachronism (year)
    "Quote where MCDP 7 credits Patton with X"              -> no such credit  (attribution)

OVER-REFUSAL IS THE RISK, and it governs every rule below. A premise gate that fires on
"What is friction according to MCDP 1?" would refuse an ordinary answerable question and
is strictly worse than no gate. So extraction is deliberately HIGH-PRECISION, LOW-RECALL:
a presupposition is emitted ONLY when a specific, load-bearing token is asserted in the
question (a number, a year, an edition, a page, a named person). Ordinary "what is X" /
"define X" questions assert no such token and correctly yield NOTHING -- that is the
common case and must never be over-triggered. When in doubt, emit nothing: a missed false
premise degrades to the pre-gate behaviour (Gates 1/2 still run), whereas a spurious one
refuses a good answer.

PURE STDLIB, INJECTED VERIFIER. Extraction is rule/trigger based (re only). Verification
is NOT done here -- verify_fn(claim, spans) -> [0,1] is passed in, so the unit suite
stubs it and the device wires a grounded verifier. This module owns the linguistics of
"what did the question presuppose"; it owns none of the retrieval or model machinery.
"""
import re

# Cardinal number words we treat as an ASSERTED count. Kept small and doctrinal on
# purpose -- "one"/"two" rarely head a false enumeration, but they are cheap to carry and
# the count noun (below) is what actually makes the trigger fire.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}

# Nouns that name an ENUMERATED doctrinal set -- the place a wrong count hides. The false
# premise is almost always "the <N> <one-of-these> of <thing>". A bare plural noun after a
# number is NOT enough (see extract_presuppositions): we want the count noun to signal an
# actual doctrinal enumeration, not any counted object.
_COUNT_NOUNS = {
    "principles", "phases", "steps", "forms", "pillars", "functions", "tenets",
    "elements", "stages", "categories", "types", "levels", "characteristics",
    "fundamentals", "imperatives", "domains", "lines", "tasks", "rules", "laws",
    "orders", "echelons", "components", "factors", "considerations",
    "attributes", "traits", "properties", "dimensions",
}

_NUM_WORD_RE = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))

# ---- count / cardinal ------------------------------------------------------
# "the SEVEN phases of the intelligence cycle", "seven principles of war",
# "the 6 warfighting functions". A number (word or digits, but NOT a 4-digit year --
# that is the year trigger) immediately heading a count noun.
_COUNT_RE = re.compile(
    r"\b(?:the\s+)?(?P<num>%s|\d{1,3})\s+"
    # Head noun, optionally preceded by one modifier word ("warfighting functions"), but
    # the modifier may not be a preposition/copula -- that is where the "of <subject>"
    # clause starts, e.g. "phases of the intelligence cycle" keeps noun="phases".
    r"(?P<noun>[a-z]+(?:\s+(?!of|in|for|to|are|is)[a-z]+)?)"
    r"(?:\s+(?:of|in|for|to)\s+(?P<subj>(?:the\s+)?[a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*){0,4}))?"
    % _NUM_WORD_RE,
    re.IGNORECASE,
)

# ---- edition ---------------------------------------------------------------
# "the 3rd edition", "the third edition". The ordinal is the load-bearing token.
_EDITION_RE = re.compile(
    r"\bthe\s+(?P<ord>\d{1,2}(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|"
    r"seventh|eighth|ninth|tenth)\s+edition\b",
    re.IGNORECASE,
)

# ---- year tied to a named document -----------------------------------------
# "According to the 2011 TCCC Guidelines, ...", "the 1989 MCDP 1". A 4-digit year (only
# 19xx/20xx) followed by a NAMED doc -- i.e. at least one Capitalised/all-caps token, so a
# lowercase "the 2011 report" does NOT match. Case-SENSITIVE on the doc tokens by design.
_YEAR_DOC_RE = re.compile(
    r"\b(?:according to\s+)?the\s+(?P<year>(?:19|20)\d{2})\s+"
    r"(?P<doc>[A-Z][A-Za-z0-9.\-]*(?:\s+[A-Za-z0-9.\-]+){0,4})"
)

# ---- page ------------------------------------------------------------------
# "on page 42", "page 42", "pp. 42", "p. 42".
_PAGE_RE = re.compile(
    r"\b(?:on\s+)?(?:pages?|pp?\.)\s*(?P<page>\d{1,4})\b",
    re.IGNORECASE,
)

# ---- attribution: a document CREDITS a named person ------------------------
# "MCDP 7 credits Patton with X", "FM 3-0 attributes the phrase to Clausewitz". The doc is
# an optional leading token(group), the verb is a crediting verb, the target is a
# Title-case PERSON name (all-caps acronyms and "the ..." docs deliberately excluded).
_ATTR_DOC_RE = re.compile(
    r"\b(?P<doc>[A-Z][A-Za-z.\-]*(?:\s+[0-9][\w.\-]*)?)\s+"
    r"(?P<verb>credits|attributes|ascribes|quotes|cites)\s+"
    r"(?:(?P<name1>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"          # "credits Patton"
    r"|(?:the\s+)?[a-z][^,.?]*?\s+to\s+(?P<name2>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?))"  # "... to Clausewitz"
    r"(?:\s+with\s+(?P<obj>[^,.?]+))?"
)

# ---- attribution: a named person SPOKE / is quoted -------------------------
# "according to Patton", "Clausewitz wrote", "Sun Tzu said". Title-case person only, so
# "according to MCDP 1" (acronym) and "according to the 2011 TCCC Guidelines" (article +
# acronym) are NOT people and never match here.
_SPEAKER_RE = re.compile(
    r"\b[Aa]ccording to\s+(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"
    r"|\b(?P<name2>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+"
    r"(?:said|wrote|stated|claimed|argued|declared)\b"
)

# ---- existence: a DEFINITE presence framing --------------------------------
# The most conservative trigger by far, because "presupposes term X is in the doc" is the
# easiest to over-fire (every "define X" would qualify and every "define X" is answerable).
# So we require a DEFINITE container framing that only makes sense if the thing exists:
# "MCDP 1's friction section", "the tempo passage in MCDP 6". Bare "define X" / "what is X"
# assert no such thing and are left alone.
_EXIST_POSSESSIVE_RE = re.compile(
    r"\b(?P<doc>[A-Z][A-Za-z0-9.\-]*(?:\s+[0-9][\w.\-]*)?)(?:'s|’s)\s+"
    r"(?P<term>[a-z][a-z'\- ]*?)\s+(?:section|passage|chapter|paragraph|discussion|"
    r"definition|treatment)\b"
)
_EXIST_CONTAINER_RE = re.compile(
    r"\bthe\s+(?P<term>[a-z][a-z'\- ]*?)\s+"
    r"(?:section|passage|chapter|paragraph)\s+(?:of|in)\s+"
    r"(?P<doc>[A-Z][A-Za-z0-9.\-]*(?:\s+[0-9][\w.\-]*)?)"
)


def _clean(s):
    """Trim whitespace and trailing question/terminal punctuation from a captured span."""
    return re.sub(r"[\s.?!,;:]+$", "", (s or "").strip())


def _cap(s):
    """Capitalise the first character only, leaving acronyms mid-sentence intact."""
    s = s.strip()
    return s[:1].upper() + s[1:] if s else s


def _presup(kind, claim, specific):
    return {"kind": kind, "claim": claim, "specific": specific}


def _topic_after_comma(question, end):
    """Given 'According to the 2011 TCCC Guidelines, what is the suzetrigine dose?' and the
    end offset of the doc phrase, recover the interrogated topic 'the suzetrigine dose'.

    This is what makes the year presupposition LOAD-BEARING rather than trivially true: the
    false premise is not that a 2011 edition exists in the abstract, it is that the 2011
    edition addresses THIS topic. Returns "" when there is no clean ", what is/are ..."
    tail, in which case the caller falls back to a plain existence-of-edition claim.
    """
    rest = question[end:]
    m = re.match(r"\s*,\s*(.*)$", rest)
    if not m:
        return ""
    tail = _clean(m.group(1))
    tail = re.sub(r"^(?:what(?:'s| is| are)?|which|list|name|state|give)\s+", "", tail,
                  flags=re.IGNORECASE)
    return _clean(tail)


def extract_presuppositions(question):
    """Extract the factual presuppositions a question ASSERTS, as standalone declaratives.

    Rule/trigger based and deliberately high-precision (see the module docstring: over-
    refusal is the risk we design against). Each returned dict is
    {"kind", "claim", "specific"} where `claim` is a natural-language declarative the
    passages would need to support and `specific` is the single load-bearing token that,
    if wrong, makes the whole question a false premise.

    Emitting an EMPTY list is the correct, common outcome for ordinary answerable questions
    ("What is friction according to MCDP 1?", "Define maneuver warfare.") -- they assert no
    specific token, so there is nothing to pre-verify.
    """
    if not question:
        return []
    out = []
    seen = set()

    def add(kind, claim, specific):
        key = (kind, claim.lower(), (specific or "").lower())
        if key not in seen:
            seen.add(key)
            out.append(_presup(kind, claim, specific))

    # -- count / cardinal ---------------------------------------------------
    for m in _COUNT_RE.finditer(question):
        num = m.group("num")
        noun = _clean(m.group("noun")).lower()
        # A 4-digit token here is a year, handled by the year trigger, not a count.
        if num.isdigit() and len(num) == 4:
            continue
        # Validate on the HEAD (last) word so "warfighting functions" fires on "functions"
        # while a stray modifier is still carried into the readable claim.
        if noun.split()[-1] not in _COUNT_NOUNS:
            continue
        subj = _clean(m.group("subj"))
        num_l = num.lower()
        if subj:
            claim = _cap("%s has %s %s." % (subj, num_l, noun))
        else:
            claim = _cap("There are %s %s." % (num_l, noun))
        add("count", claim, num_l)

    # -- edition ------------------------------------------------------------
    for m in _EDITION_RE.finditer(question):
        ordv = m.group("ord").lower()
        add("edition", _cap("A %s edition exists." % ordv), ordv)

    # -- year tied to a named document -------------------------------------
    for m in _YEAR_DOC_RE.finditer(question):
        year = m.group("year")
        doc = _clean(m.group("doc"))
        topic = _topic_after_comma(question, m.end())
        docphrase = "the %s %s" % (year, doc)
        if topic:
            claim = _cap("%s address %s." % (docphrase, topic))
        else:
            claim = _cap("%s exists." % docphrase)
        add("year", claim, year)

    # -- page ---------------------------------------------------------------
    for m in _PAGE_RE.finditer(question):
        page = m.group("page")
        add("page", _cap("The relevant content appears on page %s." % page), page)

    # -- attribution: document credits a person ----------------------------
    for m in _ATTR_DOC_RE.finditer(question):
        name = m.group("name1") or m.group("name2")
        if not name:
            continue
        doc = _clean(m.group("doc"))
        verb = m.group("verb").lower()
        # Normalise every crediting verb to "credits" for a stable, readable claim.
        obj = _clean(m.group("obj"))
        if obj:
            claim = _cap("%s credits %s with %s." % (doc, name, obj))
        else:
            claim = _cap("%s credits %s." % (doc, name))
        add("attribution", claim, name)

    # -- attribution: a person is quoted / spoke ---------------------------
    for m in _SPEAKER_RE.finditer(question):
        name = m.group("name") or m.group("name2")
        if not name:
            continue
        add("attribution", _cap("The sources attribute a statement to %s." % name), name)

    # -- existence: definite container framing -----------------------------
    for m in _EXIST_POSSESSIVE_RE.finditer(question):
        term = _clean(m.group("term"))
        doc = _clean(m.group("doc"))
        if term:
            add("existence", _cap("%s discusses %s." % (doc, term)), term)
    for m in _EXIST_CONTAINER_RE.finditer(question):
        term = _clean(m.group("term"))
        doc = _clean(m.group("doc"))
        if term:
            add("existence", _cap("%s discusses %s." % (doc, term)), term)

    return out


def verify_presuppositions(presups, spans, verify_fn, tau=0.5):
    """Score each presupposition against the retrieved passages via the injected verifier.

    verify_fn(claim, spans) -> float in [0,1] is the grounded verifier (stubbed in tests,
    a real entailment/NLI check on device). Each presup is annotated in place-ish (a copy)
    with "support" (the float) and "supported" (support >= tau). Verifying one ISOLATED
    claim at a time is the whole point -- it is the regime (QA)^2 found a small model can
    actually get right, unlike judging a false premise inside a live question.
    """
    scored = []
    for p in presups:
        support = float(verify_fn(p["claim"], spans))
        q = dict(p)
        q["support"] = support
        q["supported"] = support >= tau
        scored.append(q)
    return scored


def _to_clause(claim):
    """Turn a standalone claim sentence into a subordinate clause for the hint template.

    Lowercase only a leading article ("The"/"A"/"An") so "The intelligence cycle has seven
    phases." reads cleanly after "... support that", while a claim opening on a proper noun
    ("MCDP 7 credits Patton ...") keeps its capitalisation. Trailing period stripped.
    """
    c = _clean(claim)
    m = re.match(r"(The|An|A)\b(.*)$", c)
    if m:
        c = m.group(1).lower() + m.group(2)
    return c


def _correction_for(p):
    """Build a clean, user-facing correction for one unsupported presupposition.

    Kept SEPARATE from the `claim` string (which the verifier scores): the count and
    attribution claims read cleanly as a clause, but the year/page/edition claims stitch the
    raw interrogative tail of the question in ("... address was suzetrigine part of ..."),
    which is fine for an entailment check but ugly to show a Marine. So those kinds get a
    purpose-built sentence keyed on the load-bearing specific, never the raw claim. The claim
    the verifier sees is untouched, so measured behaviour does not change -- this is display
    only. Always about UNVERIFIABILITY, never falsehood (see premise_verdict).
    """
    kind = p.get("kind")
    spec = p.get("specific", "")
    if kind == "year":
        m = re.match(r"[Tt]he\s+\d{4}\s+(.+?)\s+(?:address|exists)\b", p.get("claim", ""))
        doc = m.group(1) if m else "publication"
        return "The sources do not confirm the %s %s you referenced." % (spec, doc)
    if kind == "edition":
        return "The sources do not confirm a %s edition of that publication." % spec
    if kind == "page":
        return "The sources do not confirm the page reference you cited (page %s)." % spec
    # count / attribution / existence: the claim already reads cleanly as a clause.
    return "The sources do not support that %s." % _to_clause(p.get("claim", ""))


def premise_verdict(question, spans, verify_fn, tau=0.5):
    """The gate's verdict for one question: refuse iff a presupposition is unsupported.

    Returns {"refuse", "unsupported", "correction_hint"}. `refuse` is True iff at least one
    EXTRACTED presupposition scores below tau -- so a question that asserts nothing (the
    common case) can never be refused here, and a question whose premises are all supported
    passes straight through to the generator.

    The correction_hint is deliberately about UNVERIFIABILITY, never about falsehood: we
    say "The sources do not support that X", not "X is false". On an offline device the
    corpus is finite and the honest claim is only that the sources fail to attest the
    premise -- asserting the negative would be the same over-confidence we are trying to
    stop the 2B from committing.
    """
    scored = verify_presuppositions(extract_presuppositions(question), spans, verify_fn, tau)
    unsupported = [p for p in scored if not p["supported"]]
    hint = _correction_for(unsupported[0]) if unsupported else ""
    return {"refuse": bool(unsupported), "unsupported": unsupported,
            "correction_hint": hint}
