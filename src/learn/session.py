#!/usr/bin/env python3
"""Learning session: serve practice items, grade answers, track calibration.

Two design decisions worth stating.

**Grading is against the SOURCE PARAGRAPH, not the model's memory.** The grader is shown
the paragraph the question came from and asked which key points the student's answer
covered. Same discipline as the rest of the system: the model is never the authority,
the document is.

**Confidence is captured BEFORE the answer is revealed.** Asking afterwards measures
hindsight, not calibration. The point is to compare what the learner believed at the
moment of answering against what turned out to be true -- a Marine who is confidently
wrong about commander's intent is the human form of the failure mode this whole project
exists to prevent.
"""
import calendar
import json
import re
import sqlite3
import time
import urllib.request

from http_retry import post_json
from pathlib import Path

from review import ReviewStore

GEN_URL = "http://127.0.0.1:8080/v1/chat/completions"


def _epoch(wall_utc):
    """Stored timestamps are '%Y-%m-%dT%H:%M:%SZ' UTC; missing ones sort as ancient."""
    if not wall_utc:
        return 0.0
    try:
        return calendar.timegm(time.strptime(wall_utc, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return 0.0

GRADE_SYSTEM = """You grade a student's recall answer against a source paragraph of doctrine.

You are given the SOURCE paragraph, the KEY POINTS a correct answer must contain, and the
STUDENT ANSWER. Decide which key points the student actually covered.

Rules:
1. Judge ONLY against the source paragraph. Never use outside knowledge.
2. Accept paraphrase and synonyms. The student does not need the exact words.
3. Do not reward fluency. A confident answer that misses a key point still misses it.
4. A key point counts as covered ONLY if the student expressed that idea. Sharing a
   word or topic with the key point is NOT coverage. If the answer does not actually
   address what was asked, mark every key point missed and the verdict "incorrect",
   however relevant the vocabulary sounds.
5. When genuinely unsure whether a point was covered, mark it MISSED. Telling a Marine
   they got something right when they did not is worse than being harsh.
6. Reply with ONLY a JSON object, no prose, no code fence:
{"covered": ["key point text"], "missed": ["key point text"], "verdict": "correct", "feedback": "one short sentence"}

verdict is "correct" if all key points are covered, "partial" if some are, "incorrect" if none are."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS learn_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    seq          INTEGER NOT NULL,
    wall_utc     TEXT,
    learner      TEXT NOT NULL DEFAULT 'default',
    item_id      TEXT NOT NULL,
    chapter      INTEGER,
    section      TEXT,
    confidence   INTEGER,
    verdict      TEXT,
    score        REAL,
    answer       TEXT,
    covered_json TEXT,
    missed_json  TEXT,
    latency_s    REAL
);
CREATE INDEX IF NOT EXISTS idx_attempts_learner ON learn_attempts(learner);
CREATE INDEX IF NOT EXISTS idx_attempts_item ON learn_attempts(item_id);
"""

VERDICT_SCORE = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}

# Four levels, deliberately, so there is no neutral middle to hide in.
STOP = {"the", "and", "for", "that", "with", "his", "her", "its", "are", "was",
        "not", "but", "can", "our", "war", "from", "this", "into", "than"}

CONFIDENCE = {1: "Guessing", 2: "Unsure", 3: "Fairly sure", 4: "Certain"}


def _post(payload, timeout=300):
    return post_json(GEN_URL, payload, timeout=timeout)


def _parse_json(raw, fallback):
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return fallback
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return fallback


class LearnStore:
    def __init__(self, db_path, items_path):
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        # WAL and a busy timeout set explicitly, not inherited by luck. LearnStore shares
        # records.sqlite with RecordStore, and the robustness audit found WAL held only
        # because RecordStore was constructed first -- a construction-order dependency for
        # a durability property is a bug waiting for a refactor. WAL lets a reader (a
        # learner answering) proceed while a writer (the review UI) commits; the 5s busy
        # timeout turns the two-writer race -- review approving while a learner records an
        # attempt -- from an instant "database is locked" into a short wait.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.items_path = Path(items_path)
        self.review = ReviewStore(self.db)
        self.reload()

    def reload(self):
        if self.items_path.exists():
            d = json.loads(self.items_path.read_text(encoding="utf-8"))
            self.track = d.get("track", [])
            self.items = {i["id"]: i for i in d.get("items", [])}
        else:
            self.track, self.items = [], {}
        # Every generated item gets a review row. Rebuilding the item bank must never
        # silently un-approve what a human already checked, so approvals live in the
        # database and are keyed by item id.
        self.review.ensure_rows(self.items.keys())
        return len(self.items)

    def servable(self):
        """Ids a learner is allowed to see: approved only, no exceptions."""
        return self.review.approved_ids() & set(self.items)

    # -- progress ----------------------------------------------------------

    def _attempts(self, learner):
        return self.db.execute(
            "SELECT item_id, verdict, score, confidence, wall_utc FROM learn_attempts "
            "WHERE learner=? ORDER BY seq", (learner,)).fetchall()

    def _latest(self, learner):
        latest, order = {}, []
        for a in self._attempts(learner):
            latest[a["item_id"]] = a["score"]
            order.append(a["item_id"])
        return latest, (order[-1] if order else None)

    # Days until an item comes back, indexed by how many times in a row it has been
    # recalled correctly. Deliberately short at the front: a Marine who saw something
    # once yesterday has not learned it, and a first interval measured in weeks turns
    # this into a quiz rather than a study tool.
    INTERVALS = [1, 3, 7, 21, 60]

    @staticmethod
    def _due_after(streak, verdict, confidence):
        """When to show this item again, in days.

        Confidence is part of the schedule, not just a statistic. Being *certain* and
        wrong is a different failure from being *unsure* and wrong: the first means a
        Marine is carrying a belief they would act on, so it earns the shortest possible
        interval regardless of how the item scored before.
        """
        if verdict == "incorrect":
            return 0 if (confidence or 0) >= 3 else 1
        if verdict == "partial":
            return 1
        idx = min(max(streak, 1), len(LearnStore.INTERVALS)) - 1
        days = LearnStore.INTERVALS[idx]
        # Correct but guessing is not knowledge; do not push it out a month.
        return min(days, 3) if (confidence or 0) <= 1 else days

    def _schedule(self, learner):
        """item_id -> (due_epoch, streak, last_epoch) from the full attempt history."""
        state = {}
        for a in self._attempts(learner):
            when = _epoch(a["wall_utc"])
            streak, _, _ = state.get(a["item_id"], (0, 0, 0))
            streak = streak + 1 if a["verdict"] == "correct" else 0
            due = when + self._due_after(streak, a["verdict"], a["confidence"]) * 86400
            state[a["item_id"]] = (streak, due, when)
        return {k: (due, streak, last) for k, (streak, due, last) in state.items()}

    def due_counts(self, learner="default"):
        now = time.time()
        sched = self._schedule(learner)
        ok = self.servable()
        due = sum(1 for i in ok if i in sched and sched[i][0] <= now)
        unseen = sum(1 for i in ok if i not in sched)
        later = len(ok) - due - unseen
        nxt = min((sched[i][0] for i in ok if i in sched and sched[i][0] > now),
                  default=None)
        return {"due": due, "unseen": unseen, "scheduled": later,
                "next_due_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(nxt))
                                 if nxt else None)}

    def history(self, learner="default", limit=20):
        rows = self.db.execute(
            "SELECT item_id, section, chapter, confidence, verdict, wall_utc "
            "FROM learn_attempts WHERE learner=? ORDER BY seq DESC LIMIT ?",
            (learner, limit)).fetchall()
        out = []
        for r in rows:
            # apply_edits, not the raw item: a reviewer's rewrite is the question the
            # Marine was actually asked, so it is the one their history must show.
            raw = self.items.get(r["item_id"])
            it = self.review.apply_edits(raw) if raw else {}
            out.append({"item_id": r["item_id"], "section": r["section"],
                        "pub_id": it.get("pub_id"), "chapter": r["chapter"],
                        "question": it.get("question"),
                        "confidence": r["confidence"], "verdict": r["verdict"],
                        "wall_utc": r["wall_utc"]})
        return out

    def progress(self, learner="default", pub=None):
        latest, _ = self._latest(learner)
        ok = self.servable()
        out, attempted_total, item_total = [], 0, 0
        pubs = {}
        for sec in self.track:
            ids = [i for i in sec["item_ids"] if i in ok]
            if ids:
                pid = sec.get("pub_id") or "?"
                pb = pubs.setdefault(pid, {"pub_id": pid,
                                           "pub_title": sec.get("pub_title") or pid,
                                           "n_items": 0, "attempted": 0, "mastered": 0})
                pb["n_items"] += len(ids)
                pb["attempted"] += sum(1 for i in ids if i in latest)
                pb["mastered"] += sum(1 for i in ids if latest.get(i, 0) >= 1.0)
            if pub and sec.get("pub_id") != pub:
                continue
            scores = [latest[i] for i in ids if i in latest]
            mastered = sum(1 for s in scores if s >= 1.0)
            item_total += len(ids)
            attempted_total += len(scores)
            if not ids:
                continue          # section has no approved items yet; do not show it
            out.append({
                "pub_id": sec.get("pub_id"),
                "chapter": sec["chapter"], "chapter_title": sec["chapter_title"],
                "section": sec["section"], "n_items": len(ids),
                "attempted": len(scores), "mastered": mastered,
                "state": ("mastered" if ids and mastered == len(ids)
                          else "shaky" if scores else "new"),
            })
        return {"sections": out, "items_total": item_total,
                "items_attempted": attempted_total,
                "publications": sorted(pubs.values(), key=lambda x: x["pub_id"]),
                "pct": round(100 * attempted_total / item_total) if item_total else 0}

    def class_overview(self):
        """Per-section mastery aggregated across ALL learners, for the instructor view.

        D-050 (closing C5) is the reason this method has the shape it does, and the reason
        the privacy boundary is enforced HERE rather than only in the UI: the instructor
        is entitled to the class aggregate -- where the class is weak -- but NOT to an
        individual Marine's record, except a Marine's own. Those two things are separable,
        so this returns rows keyed by SECTION only, plus a single class-level COUNT of how
        many distinct learners contributed. It never returns a learner identifier and never
        groups by one, so there is no per-Marine drill-down to be had: a method that cannot
        name a learner cannot leak which one is failing. The individual record stays behind
        history()/calibration(), keyed to a learner the client must name.

        The unit of aggregation is the (learner, item) pair, scored by the SAME "last
        attempt wins" rule progress() uses, so a section's class mastery is "of every
        attempt any Marine has made at an approved item in this section, how many now stand
        correct". Only approved (servable) items count, matching what a learner can be
        served.

        Caveat, recorded not fixed: with a single learner the aggregate trivially equals
        that individual. D-050 accepts aggregate exposure to the instructor as the whole
        point; a FIELDED version keyed to an EDIPI would additionally want small-N
        suppression (k-anonymity) before showing a section, which is out of demo scope.
        """
        # Latest score per (learner, item), folded from one ordered pass -- the same
        # approach _latest() takes, extended across every learner instead of one.
        per_item = {}          # item_id -> [attempted_count, mastered_count]
        learners = set()
        for a in self.db.execute(
                "SELECT learner, item_id, score FROM learn_attempts ORDER BY seq"):
            learners.add(a["learner"])
            slot = per_item.setdefault(a["item_id"], [0, 0, {}])
            # dict at [2] dedups repeat attempts by the same learner on the same item to a
            # single latest score before counting, so "attempts" is (learner, item) pairs.
            slot[2][a["learner"]] = a["score"]
        for iid, slot in per_item.items():
            scores = slot[2]
            slot[0] = len(scores)
            slot[1] = sum(1 for s in scores.values() if s >= 1.0)

        ok = self.servable()
        out = []
        items_total = attempts_total = mastered_total = 0
        for sec in self.track:
            ids = [i for i in sec["item_ids"] if i in ok]
            if not ids:
                continue          # no approved items in this section yet; do not show it
            attempts = sum(per_item[i][0] for i in ids if i in per_item)
            mastered = sum(per_item[i][1] for i in ids if i in per_item)
            items_total += len(ids)
            attempts_total += attempts
            mastered_total += mastered
            out.append({
                "pub_id": sec.get("pub_id"),
                "chapter": sec["chapter"], "chapter_title": sec["chapter_title"],
                "section": sec["section"], "n_items": len(ids),
                # class-wide (learner, item) pair counts, deliberately NOT per learner
                "attempts": attempts, "mastered": mastered,
                "mastery_pct": round(100 * mastered / attempts) if attempts else 0,
            })
        return {"sections": out,
                # the ONLY per-learner fact that leaves here is the size of the class,
                # never who is in it -- the denominator an instructor needs to read the
                # aggregate honestly, with no individual attached.
                "n_learners": len(learners),
                "items_total": items_total, "attempts_total": attempts_total,
                "mastered_total": mastered_total,
                "mastery_pct": (round(100 * mastered_total / attempts_total)
                                if attempts_total else 0)}

    def next_item(self, learner="default", section=None, pub=None):
        """Due before new, most overdue first, then anything unseen.

        Ordering due work ahead of new work is the whole point of a schedule: a Marine
        who is fed a fresh question every time never revisits the one they got wrong on
        Tuesday, which is precisely the one that has not stuck.
        """
        _, last = self._latest(learner)
        sched = self._schedule(learner)
        ok = self.servable()
        now = time.time()

        pool = []
        for sec in self.track:
            if section and sec["section"] != section:
                continue
            if pub and sec.get("pub_id") != pub:
                continue
            pool.extend(i for i in sec["item_ids"] if i in ok)

        due = sorted((i for i in pool if i in sched and sched[i][0] <= now and i != last),
                     key=lambda i: sched[i][0])
        if due:
            return self.review.apply_edits(self.items[due[0]])
        unseen = [i for i in pool if i not in sched and i != last]
        if unseen:
            return self.review.apply_edits(self.items[unseen[0]])
        # Nothing due and nothing new: fall back to the weakest so practice never
        # dead-ends, but the caller is told this is ahead of schedule.
        weak = sorted((i for i in pool if i != last),
                      key=lambda i: (sched.get(i, (0, 0, 0))[1], sched.get(i, (0, 0, 0))[0]))
        return self.review.apply_edits(self.items[weak[0]]) if weak else None

    # -- grading -----------------------------------------------------------

    @staticmethod
    def _reconcile(points, covered, missed, verdict):
        """Force covered/missed to partition the item's own key points.

        Measured failure: for an answer that earned all three key points the grader
        returned one merged line -- "covered all three specified types of friction" --
        so the Marine saw a single tick and two points that had simply vanished. What is
        being graded is the item's key points, so those, in their own wording, are what
        gets shown back; the model's phrasing is only used to decide which bucket each
        one lands in.

        `missed` is the authoritative side because the grading prompt already tells the
        grader to resolve genuine doubt against the student. Anything the grader did not
        call missed is credited.
        """
        def toks(t):
            return {w for w in re.findall(r"[a-z]{3,}", t.lower()) if w not in STOP}

        def hit(point, claims):
            pt = toks(point)
            if not pt:
                return False
            for c in claims:
                ct = toks(c)
                if ct and len(pt & ct) / len(pt) >= 0.5:
                    return True
            return False

        miss = [p for p in points if hit(p, missed)]
        # A grader that says "incorrect" and then lists nothing missed is contradicting
        # itself; award no credit rather than silently ticking every point.
        if not miss and verdict != "correct":
            miss = list(points)
        keep = [p for p in points if p not in miss]
        return keep, miss

    def grade(self, item, answer):
        user = ("SOURCE PARAGRAPH:\n" + item["source_text"][:1600] +
                "\n\nKEY POINTS:\n" +
                "\n".join("- " + k for k in item["key_points"]) +
                "\n\nSTUDENT ANSWER:\n" + answer[:1200])
        fallback = {"verdict": "partial", "covered": [],
                    "missed": item["key_points"],
                    "feedback": "Could not grade automatically; compare with the source."}
        try:
            d = _post({"model": "gemma-4-E2B",
                       "messages": [{"role": "system", "content": GRADE_SYSTEM},
                                    {"role": "user", "content": user}],
                       "max_tokens": 320, "temperature": 0.0})
            raw = d["choices"][0]["message"]["content"].strip()
        except Exception:                                          # noqa: BLE001
            return fallback
        g = _parse_json(raw, fallback)
        v = g.get("verdict", "partial")
        if v not in VERDICT_SCORE:
            v = "partial"
        covered, missed = self._reconcile(
            item["key_points"],
            [str(x) for x in (g.get("covered") or [])],
            [str(x) for x in (g.get("missed") or [])],
            v)
        return {"verdict": v, "covered": covered, "missed": missed,
                "feedback": (g.get("feedback") or "").strip()[:300]}

    def record(self, learner, item, answer, confidence, grade, latency_s):
        row = self.db.execute(
            "SELECT COALESCE(MAX(seq),0) s FROM learn_attempts").fetchone()
        seq = (row["s"] or 0) + 1
        self.db.execute(
            "INSERT INTO learn_attempts (seq, wall_utc, learner, item_id, chapter,"
            " section, confidence, verdict, score, answer, covered_json, missed_json,"
            " latency_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (seq, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), learner,
             item["id"], item["chapter"], item["section"], confidence,
             grade["verdict"], VERDICT_SCORE[grade["verdict"]], answer[:2000],
             json.dumps(grade["covered"]), json.dumps(grade["missed"]), latency_s))
        self.db.commit()
        return seq

    # -- calibration -------------------------------------------------------

    def calibration(self, learner="default"):
        """Confidence versus correctness. The gap between them is the whole point.

        Reported per confidence level rather than as one number, because the interesting
        failure is local: a learner can be well calibrated when unsure and badly
        overconfident when certain, and an average hides exactly that.
        """
        rows = self.db.execute(
            "SELECT confidence, AVG(score) acc, COUNT(*) n FROM learn_attempts "
            "WHERE learner=? AND confidence IS NOT NULL GROUP BY confidence "
            "ORDER BY confidence", (learner,)).fetchall()
        buckets = []
        for r in rows:
            stated = (r["confidence"] - 1) / 3.0          # 1..4 -> 0..1
            buckets.append({
                "confidence": r["confidence"],
                "label": CONFIDENCE.get(r["confidence"], "?"),
                "stated": round(stated, 3),
                "actual": round(r["acc"], 3),
                "gap": round(r["acc"] - stated, 3),
                "n": r["n"],
            })
        total = sum(b["n"] for b in buckets)
        overconf = sum(b["n"] for b in buckets if b["gap"] < -0.15)
        worst = min(buckets, key=lambda b: b["gap"]) if buckets else None
        return {"buckets": buckets, "n": total,
                "overconfident_share": round(overconf / total, 3) if total else None,
                "worst_bucket": worst}

    def recent(self, learner="default", limit=12):
        return [dict(r) for r in self.db.execute(
            "SELECT item_id, section, confidence, verdict, wall_utc "
            "FROM learn_attempts WHERE learner=? ORDER BY seq DESC LIMIT ?",
            (learner, limit))]
