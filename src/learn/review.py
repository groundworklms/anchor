#!/usr/bin/env python3
"""Instructor review for generated practice items.

An AI-written question that nobody checked is not something a schoolhouse should put in
front of Marines. This is the gate: **no item reaches a learner until a human approves
it.** Review state lives in the records database rather than in the items file, because
the items file is regenerable and approvals are not -- rebuilding the item bank must
never silently un-approve everything a human already checked.

Reviewing 74 items cold is slow and error-prone, so each item is pre-checked
automatically and flagged. The flags do not decide anything; they tell the reviewer where
to look hard, so obvious-good items can be approved quickly and suspicious ones get real
attention. Four checks, chosen because each maps to a way a generated question actually
fails:

  UNGROUNDED_KEY_POINT  a key point is not supported by the source paragraph -- the model
                        invented part of the answer. This is the serious one.
  ANSWER_LEAKED         the question text already contains the answer, so it tests reading
                        rather than recall, which defeats the entire purpose.
  SHAPE                 trivially short question, no question mark, one-word key points --
                        cheap signals of a malformed item.
  SOURCE_NOT_TEACHABLE  the SOURCE PARAGRAPH is reference apparatus -- an endnote, a
                        bibliography -- so no question asked of it can be worth asking.

The fourth was added late, and it closes a structural gap. The first three read the
QUESTION and the KEY POINTS and never ask whether the source was worth generating from,
yet all 12 items a human rejected were rejected for exactly that. The one failure mode a
reviewer actually rejects for was invisible to the checker while 895 items were bulk
cleared on it (D-036).
"""
import json
import re
import time
import urllib.request

EMBED_URL = "http://127.0.0.1:8081"

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_review (
    item_id        TEXT PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    reviewer       TEXT,
    reviewed_utc   TEXT,
    edited_question TEXT,
    edited_points  TEXT,
    note           TEXT,
    flags_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_status ON item_review(status);
"""

IMPERATIVE = re.compile(
    r"^\s*(describe|explain|list|name|identify|state|define|compare|contrast|summari[sz]e"
    r"|outline|discuss|give|provide|distinguish)", re.I)

STOP = set("""a an and are as at be by for from has have how in is it its of on or that the
their they this to was were what when where which who why will with you your does do""".split())

# -- reference apparatus, for SOURCE_NOT_TEACHABLE -------------------------------------
#
# D-036 built and REFUTED a fuzzy "looks like furniture" score over alphabetic ratio,
# digit density, short-word share, mean word length and sentence density. Calibrated
# against the 7 known-bad sources it caught 2 of them, while flagging the TCCC Combat
# Wound Medication Pack, a ballistics wind-drift table and the downrange wind-indicator
# list -- dense, checkable content a Marine should be drilled on. Same lesson as the
# figure-label filter recorded WONTFIX in TRACKER A10: a loose rule deletes real
# doctrine. Do not rebuild it here.
#
# What survived is the narrow half. Reference apparatus is unambiguous -- an endnote or a
# bibliography is not doctrine under any reading, so a question generated from one cannot
# be worth asking. It reaches the generator at all because the chunker glues endnote
# fragments onto the head of real paragraphs: `7. Ibid., p. 1-6. "All actions in war take
# place in an atmosphere of uncertainty..."` arrives as one chunk (D-036).

# Citation shorthand. Across the 4,230-chunk corpus these occur only inside Notes
# sections; doctrine prose never says "Ibid."
REF_LATIN = re.compile(r"\b(?:ibid\.|op\.\s*cit\.|loc\.\s*cit\.)", re.I)

# The heading MCDP case studies put above their bibliography. MCDP 2 chapter 3 is why
# this is spelled out: the question generated from that block was literally "What are the
# principal sources utilized in this case study?"
REF_SOURCES = re.compile(r"\bprincipal sources?\s+(?:used|utilized|consulted)", re.I)

# A publisher imprint: "(Washington, D.C.: Department of Defense, April 1992)". Both the
# colon and a four-digit year must fall inside ONE set of parentheses, which is what
# separates a book imprint from an ordinary parenthetical aside. Measured over the whole
# corpus this matched 168 distinct strings, every one of them a real imprint.
REF_IMPRINT = re.compile(r"\([^()]{0,120}:\s*[^()]{0,120}\b(?:1[6-9]|20)\d{2}\s*\)")

# A numbered endnote marker followed by a page reference: "7. Ibid., p. 1-6.",
# "6. MCDP 1, pp. 4-21", "17. Clausewitz, p. 194."
#
# The lookbehind is the point of this pattern, not a detail. Army numbered paragraphs --
# "3-67.", "C-11." -- are the ONE false positive D-036 measured across 1,011 items, and
# TC 3-22.9 alone opens 486 paragraphs that way. `(?<![\w-])` refuses a marker whose
# digits follow a hyphen or a word character, so the "11." inside "C-11." is not read as
# an endnote number while a bare "11." still is. Checked against the corpus: 0 of those
# 486 paragraphs match.
#
# Requiring the page reference is what keeps this off numbered PROCEDURE lists, which
# fill the operational publications -- 17 TCCC chunks and 9 MCWP 3-11.3 chunks open with
# three or more "1. ... 2. ... 3. ..." markers. A bare run-of-numbers rule would flag the
# Care Under Fire steps and the land-navigation drills, which is precisely the content
# D-036 refused to lose.
REF_ENDNOTE = re.compile(r"(?<![\w-])\d{1,3}\.\s+[^.;]{0,60}?\bpp?\.\s*\d")

# Below this cosine similarity to its best-matching sentence in the source, a key point is
# treated as unsupported. Tuned by hand against this corpus: genuine paraphrases of a
# source sentence land well above it, invented points land well below.
GROUNDING_FLOOR = 0.62
# Above this overlap between the question and a key point, the question is giving the
# answer away.
LEAK_CEILING = 0.60


def _tokens(s):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in STOP and len(w) > 2]


def _overlap(a, b):
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(tb)


def _reference_apparatus(text):
    """Return the apparatus that was matched, or None if this is not reference apparatus.

    Pure text on purpose. SHAPE and ANSWER_LEAKED are pure and the grounding check is the
    only one that calls out; keeping this one pure is what lets precheck() screen sources
    on an air-gapped device with no embedding server, and what lets it be unit-tested.

    The matched text is returned rather than a bool so the flag detail can name what was
    matched -- a reviewer should be able to judge the flag without reopening the source.
    """
    for rx in (REF_LATIN, REF_SOURCES, REF_IMPRINT, REF_ENDNOTE):
        m = rx.search(text or "")
        if m:
            return " ".join(m.group(0).split())
    return None


def _embed(texts, url=EMBED_URL, timeout=180):
    payload = json.dumps({"input": texts, "model": "bge-small-en-v1.5"}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/v1/embeddings", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return [row["embedding"] for row in sorted(d["data"], key=lambda x: x.get("index", 0))]


def _cos(a, b):
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return num / (na * nb) if na and nb else 0.0


def _sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", text or "")
    return [p.strip() for p in parts if len(p.strip()) > 25] or [text or ""]


def precheck(item, embed_url=EMBED_URL):
    """Return a list of flags. Empty list means nothing suspicious was found."""
    flags = []
    q = item.get("question", "")
    kps = item.get("key_points", []) or []
    src = item.get("source_text", "")

    # -- shape ------------------------------------------------------------
    if len(q) < 25:
        flags.append({"code": "SHAPE", "detail": "question is very short"})
    # An imperative prompt is a perfectly good recall question -- "Describe the spectrum
    # of relations between total war and perfect peace" needs no question mark. Flagging
    # those buried the real signal under 27 false alarms, and a checker that cries wolf
    # gets ignored, which is worse than not having it.
    if "?" not in q and not IMPERATIVE.match(q):
        flags.append({"code": "SHAPE",
                      "detail": "neither a question nor an imperative prompt"})
    if not kps:
        flags.append({"code": "SHAPE", "detail": "no key points"})
    for k in kps:
        if len(_tokens(k)) < 2:
            flags.append({"code": "SHAPE", "detail": f"key point too thin: {k!r}"})

    # -- answer leakage ---------------------------------------------------
    for k in kps:
        ov = _overlap(q, k)
        if ov >= LEAK_CEILING:
            flags.append({"code": "ANSWER_LEAKED",
                          "detail": f"question already states {ov:.0%} of: {k!r}"})

    # -- source teachability ----------------------------------------------
    # Advisory, like every other flag here. Measured flag precision is about two thirds
    # (D-035: 36 of 116 flagged items were fine as written), and D-027 and D-036 both
    # record that a gate at that precision throws away good questions -- so this raises a
    # hand, it does not reject. It runs whether or not there are key points, because a
    # source with nothing to teach is wrong regardless of what the model made of it.
    apparatus = _reference_apparatus(src)
    if apparatus:
        flags.append({"code": "SOURCE_NOT_TEACHABLE",
                      "detail": f"source is reference apparatus, not doctrine: "
                                f"{apparatus!r}"})

    # -- grounding --------------------------------------------------------
    # The important check. Compare each key point against the source sentences and take
    # the best match; a key point the model invented will not resemble any of them.
    if kps and src:
        try:
            sents = _sentences(src)
            vecs = _embed(kps + sents, embed_url)
            kv, sv = vecs[:len(kps)], vecs[len(kps):]
            for k, v in zip(kps, kv):
                best = max((_cos(v, s) for s in sv), default=0.0)
                if best < GROUNDING_FLOOR:
                    flags.append({"code": "UNGROUNDED_KEY_POINT",
                                  "detail": f"{best:.2f} similarity to any source "
                                            f"sentence: {k!r}"})
        except Exception as e:                                    # noqa: BLE001
            flags.append({"code": "PRECHECK_FAILED", "detail": str(e)[:120]})
    return flags


class ReviewStore:
    def __init__(self, db):
        self.db = db
        self.db.executescript(SCHEMA)
        self.db.commit()

    def ensure_rows(self, item_ids):
        for i in item_ids:
            self.db.execute(
                "INSERT OR IGNORE INTO item_review (item_id, status) VALUES (?, 'pending')",
                (i,))
        self.db.commit()

    def set_flags(self, item_id, flags):
        self.db.execute("UPDATE item_review SET flags_json=? WHERE item_id=?",
                        (json.dumps(flags), item_id))
        self.db.commit()

    def get(self, item_id):
        r = self.db.execute("SELECT * FROM item_review WHERE item_id=?",
                            (item_id,)).fetchone()
        return dict(r) if r else None

    def decide(self, item_id, status, reviewer="instructor", note=None,
               edited_question=None, edited_points=None):
        if status not in ("approved", "rejected", "pending"):
            raise ValueError("bad status")
        self.db.execute(
            "INSERT INTO item_review (item_id, status, reviewer, reviewed_utc, note,"
            " edited_question, edited_points) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET status=excluded.status,"
            " reviewer=excluded.reviewer, reviewed_utc=excluded.reviewed_utc,"
            " note=excluded.note,"
            " edited_question=COALESCE(excluded.edited_question, item_review.edited_question),"
            " edited_points=COALESCE(excluded.edited_points, item_review.edited_points)",
            (item_id, status, reviewer,
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), note,
             edited_question,
             json.dumps(edited_points) if edited_points is not None else None))
        self.db.commit()

    def bulk_decide_unflagged(self, item_ids, status="approved",
                              reviewer="bulk:unflagged"):
        """Decide every pending item that carries no automatic flag.

        This is a real convenience -- 55 of 74 items had nothing suspicious about them,
        and forcing individual clicks through those is how a reviewer stops reading the
        ones that matter.

        It is recorded with a distinct reviewer id precisely so the audit trail does not
        claim more than happened: these were cleared as a batch on the strength of the
        automatic checks, not individually read. An instructor asking "which questions did
        a human actually read?" can still get a truthful answer.
        """
        rows = self.db.execute(
            "SELECT item_id, flags_json FROM item_review WHERE status='pending'").fetchall()
        touched, unchecked = [], []
        for r in rows:
            if r["item_id"] not in item_ids:
                continue
            # NULL means precheck has NEVER RUN on this item; '[]' means it ran and found
            # nothing. Conflating them let one click approve a whole freshly generated
            # bank on the strength of checks that had not happened -- the precise failure
            # this gate exists to prevent. Bulk clearance is a shortcut past checks that
            # PASSED, never past checks that were skipped.
            if r["flags_json"] is None:
                unchecked.append(r["item_id"])
                continue
            if json.loads(r["flags_json"]):
                continue                      # flagged: a human must look at it
            self.decide(r["item_id"], status, reviewer=reviewer,
                        note="cleared in bulk: no automatic flags")
            touched.append(r["item_id"])
        self.last_bulk_unchecked = unchecked
        return touched

    def approved_ids(self):
        return {r["item_id"] for r in self.db.execute(
            "SELECT item_id FROM item_review WHERE status='approved'")}

    def stats(self):
        rows = self.db.execute(
            "SELECT status, COUNT(*) n FROM item_review GROUP BY status").fetchall()
        d = {r["status"]: r["n"] for r in rows}
        total = sum(d.values())
        return {"pending": d.get("pending", 0), "approved": d.get("approved", 0),
                "rejected": d.get("rejected", 0), "total": total,
                "reviewed_pct": round(100 * (total - d.get("pending", 0)) / total)
                if total else 0}

    def queue(self, items, limit=200):
        """Pending items, most-suspicious first, so attention goes where it is needed."""
        rows = self.db.execute(
            "SELECT item_id, flags_json FROM item_review WHERE status='pending'").fetchall()
        out = []
        for r in rows:
            it = items.get(r["item_id"])
            if not it:
                continue
            flags = json.loads(r["flags_json"] or "[]")
            severity = (2 if any(f["code"] == "UNGROUNDED_KEY_POINT" for f in flags)
                        else 1 if flags else 0)
            out.append((severity, len(flags), it, flags))
        out.sort(key=lambda t: (-t[0], -t[1]))
        return [{"item": it, "flags": fl} for _, _, it, fl in out[:limit]]

    def apply_edits(self, item):
        """Serve the reviewer's edited wording if there is one."""
        r = self.get(item["id"])
        if not r:
            return item
        out = dict(item)
        if r.get("edited_question"):
            out["question"] = r["edited_question"]
        if r.get("edited_points"):
            try:
                pts = json.loads(r["edited_points"])
                if pts:
                    out["key_points"] = pts
            except json.JSONDecodeError:
                pass
        return out
