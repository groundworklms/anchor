#!/usr/bin/env python3
"""Local learning-record store: interactions + xAPI statements + signed export.

Kept in its OWN SQLite file, deliberately. `doctrine.sqlite` is the portable, hashable,
read-only corpus artifact that gets copied between units; mixing a growing write-heavy
record log into it would make the corpus un-hashable and its checksum meaningless.

## The clock problem, handled rather than ignored

This device's RTC is dead (DECISIONS.md D-010) and it is air-gapped by design, so NTP will
never fix it. A signed learning record carrying a 1970 timestamp is worse than no record:
it looks authoritative and is wrong.

So every record carries BOTH a wall-clock time and a monotonic sequence, plus an explicit
`clock_trusted` flag. Ordering is always correct because it comes from the sequence.
Wall-clock is recorded as a claim, not as a fact, and the export says so. A consumer can
re-base the whole bundle onto real time using the export moment as an anchor.
"""
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

XAPI_VERB_ASKED = "http://adlnet.gov/expapi/verbs/asked"
XAPI_VERB_ANSWERED = "http://adlnet.gov/expapi/verbs/answered"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS interactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    seq             INTEGER NOT NULL,
    wall_utc        TEXT,
    clock_trusted   INTEGER NOT NULL DEFAULT 0,
    actor           TEXT,
    question        TEXT NOT NULL,
    answered        INTEGER NOT NULL,
    abstain_reason  TEXT,
    top_score       REAL,
    latency_s       REAL,
    answer_text     TEXT,
    citations_json  TEXT,
    sources_json    TEXT
);

CREATE TABLE IF NOT EXISTS xapi_statements (
    id              TEXT PRIMARY KEY,
    interaction_id  INTEGER NOT NULL,
    seq             INTEGER NOT NULL,
    statement_json  TEXT NOT NULL,
    exported_at     TEXT,
    FOREIGN KEY (interaction_id) REFERENCES interactions(id)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_inter_answered ON interactions(answered);
"""


def _clock_trusted():
    """A plausible wall clock, or an epoch-ish fallback we must not pretend about."""
    # Anything before 2020 means the RTC never got set this boot.
    return time.time() > 1577836800


class RecordStore:
    def __init__(self, path, actor="anonymous-device-user", key_path=None):
        self.path = str(path)
        self.actor = actor
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._ensure_meta("device_id", str(uuid.uuid4()))
        self.key_path = Path(key_path or (Path(self.path).parent / "record_hmac.key"))
        self._ensure_key()
        self.db.commit()

    # -- setup -------------------------------------------------------------

    def _ensure_meta(self, k, default):
        cur = self.db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        if cur:
            return cur["value"]
        self.db.execute("INSERT INTO meta(key,value) VALUES (?,?)", (k, default))
        return default

    def _ensure_key(self):
        if self.key_path.exists():
            self.key = self.key_path.read_bytes()
            return
        self.key = os.urandom(32)
        self.key_path.write_bytes(self.key)
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass

    def device_id(self):
        r = self.db.execute("SELECT value FROM meta WHERE key='device_id'").fetchone()
        return r["value"] if r else None

    def _next_seq(self):
        r = self.db.execute("SELECT COALESCE(MAX(seq),0) s FROM interactions").fetchone()
        return (r["s"] or 0) + 1

    # -- write -------------------------------------------------------------

    def record(self, question, result, actor=None):
        seq = self._next_seq()
        trusted = _clock_trusted()
        wall = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        answered = 0 if result.get("abstained") else 1

        cur = self.db.execute(
            "INSERT INTO interactions (seq, wall_utc, clock_trusted, actor, question,"
            " answered, abstain_reason, top_score, latency_s, answer_text,"
            " citations_json, sources_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (seq, wall, int(trusted), actor or self.actor, question, answered,
             result.get("abstain_reason"), result.get("top_rerank_score"),
             result.get("latency_s"), (result.get("text") or "")[:4000],
             json.dumps(result.get("citations", [])),
             json.dumps([s.get("citation") for s in result.get("sources", [])][:8])))
        interaction_id = cur.lastrowid

        stmt = self._xapi(interaction_id, seq, wall, trusted, question, result,
                          actor or self.actor)
        self.db.execute(
            "INSERT INTO xapi_statements (id, interaction_id, seq, statement_json)"
            " VALUES (?,?,?,?)", (stmt["id"], interaction_id, seq, json.dumps(stmt)))
        self.db.commit()
        return interaction_id

    def _xapi(self, interaction_id, seq, wall, trusted, question, result, actor):
        answered = not result.get("abstained")
        cites = result.get("citations", [])
        return {
            "id": str(uuid.uuid4()),
            "actor": {"objectType": "Agent",
                      "account": {"homePage": "urn:doctrine-tutor:device",
                                  "name": actor}},
            "verb": {"id": XAPI_VERB_ANSWERED if answered else XAPI_VERB_ASKED,
                     "display": {"en-US": "answered" if answered else "asked"}},
            "object": {
                "objectType": "Activity",
                "id": f"urn:doctrine-tutor:question:{interaction_id}",
                "definition": {
                    "type": "http://adlnet.gov/expapi/activities/question",
                    "name": {"en-US": question[:250]},
                }},
            "result": {
                "success": bool(answered),
                "response": (result.get("text") or "")[:1000],
                "extensions": {
                    "urn:doctrine-tutor:abstained": bool(result.get("abstained")),
                    "urn:doctrine-tutor:abstain_reason": result.get("abstain_reason"),
                    "urn:doctrine-tutor:retrieval_score": result.get("top_rerank_score"),
                    "urn:doctrine-tutor:citations": [c.get("citation") for c in cites],
                    "urn:doctrine-tutor:latency_s": result.get("latency_s"),
                }},
            "timestamp": wall,
            "context": {"extensions": {
                "urn:doctrine-tutor:device_id": self.device_id(),
                "urn:doctrine-tutor:sequence": seq,
                # The honest part: say whether the timestamp can be believed.
                "urn:doctrine-tutor:clock_trusted": bool(trusted),
            }},
            "version": "1.0.3",
        }

    # -- export ------------------------------------------------------------

    def export_bundle(self, out_path, mark_exported=True):
        """Signed, self-describing bundle for later sync.

        Signed with HMAC-SHA256 over the canonical JSON of the statements. Be precise
        about what that proves: it proves the bundle was produced by a holder of this
        device's key and has not been altered since. It is NOT non-repudiation -- the
        device holds the key, so the device could forge its own records. Upgrading to an
        asymmetric signature is a key-management decision for whoever fields this, not a
        code change, and pretending otherwise would be the kind of overclaim this project
        exists to avoid.
        """
        rows = self.db.execute(
            "SELECT id, seq, statement_json FROM xapi_statements ORDER BY seq").fetchall()
        statements = [json.loads(r["statement_json"]) for r in rows]

        body = {
            "bundle_version": 1,
            "device_id": self.device_id(),
            "statement_count": len(statements),
            "exported_wall_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "exported_clock_trusted": _clock_trusted(),
            "clock_note": (
                "Device RTC is unreliable and the device is air-gapped, so wall-clock "
                "values are CLAIMS. Ordering is authoritative via 'sequence'. To recover "
                "real times, anchor on the moment of import and re-base by sequence."),
            "statements": statements,
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        sig = hmac.new(self.key, canonical, hashlib.sha256).hexdigest()

        bundle = {"signature": {"alg": "HMAC-SHA256", "value": sig,
                                "covers": "canonical JSON of 'payload'",
                                "proves": "integrity and device-key possession; "
                                          "NOT non-repudiation"},
                  "payload": body}
        Path(out_path).write_text(json.dumps(bundle, indent=2))

        if mark_exported:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.db.execute("UPDATE xapi_statements SET exported_at=? "
                            "WHERE exported_at IS NULL", (now,))
            self.db.commit()
        return {"path": str(out_path), "statements": len(statements),
                "signature": sig[:16] + "...", "clock_trusted": _clock_trusted()}

    @staticmethod
    def verify_bundle(path, key):
        b = json.loads(Path(path).read_text())
        canonical = json.dumps(b["payload"], sort_keys=True,
                               separators=(",", ":")).encode()
        expect = hmac.new(key, canonical, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, b["signature"]["value"])

    # -- read --------------------------------------------------------------

    def stats(self):
        r = self.db.execute(
            "SELECT COUNT(*) n, SUM(answered) a, AVG(latency_s) l FROM interactions"
        ).fetchone()
        n = r["n"] or 0
        return {"interactions": n, "answered": r["a"] or 0,
                "abstained": n - (r["a"] or 0),
                "mean_latency_s": round(r["l"], 2) if r["l"] else None,
                "device_id": self.device_id(),
                "clock_trusted": _clock_trusted()}

    def refusals(self, limit=200):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM interactions WHERE answered=0 ORDER BY seq DESC LIMIT ?",
            (limit,))]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/tutor/records.sqlite")
    ap.add_argument("--export", help="write a signed bundle to this path")
    args = ap.parse_args()
    s = RecordStore(args.db)
    print(json.dumps(s.stats(), indent=2))
    if args.export:
        print(json.dumps(s.export_bundle(args.export), indent=2))
