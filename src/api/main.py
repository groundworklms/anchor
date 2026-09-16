#!/usr/bin/env python3
"""FastAPI service for the trust dashboard. Loopback only, air-gapped by design.

Serves the kiosk UI, streams answers over SSE, and exposes the three trust signals the
brief asks a judge to be able to see at a glance: what the device is doing (telemetry),
whether it is connected (a real interface check), and how well it behaves (the eval
scoreboard).

    python3 main.py --db /opt/tutor/doctrine.sqlite --config /opt/tutor/config/default.yaml
"""
import argparse
import asyncio
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from math import isfinite
from pathlib import Path
from typing import Annotated, Literal

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "generation"))
sys.path.insert(0, str(HERE.parent / "retrieval"))

from fastapi import FastAPI, HTTPException, Query              # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, StringConstraints  # noqa: E402

import network_status                                          # noqa: E402
import telemetry                                               # noqa: E402

sys.path.insert(0, str(HERE.parent / "records"))
sys.path.insert(0, str(HERE.parent / "learn"))
from store import RecordStore                                  # noqa: E402
from review import precheck                                    # noqa: E402
from session import CONFIDENCE, LearnStore                     # noqa: E402
from hybrid import rerank                                     # noqa: E402

app = FastAPI(title="Offline Doctrine-Grounded Tutor")
STATE = {"pipeline": None, "config": {}, "ui_dir": None, "eval_path": None,
         # main() overwrites this at start-up. It is seeded here so /api/health can never
         # be the thing that raises a KeyError -- it is the endpoint everything else is
         # polled against when something has already gone wrong.
         "history": [], "records": None, "learn": None, "started": time.time()}


def _log(question, res):
    """Persist the interaction. Never let record-keeping break answering."""
    try:
        if STATE["records"]:
            STATE["records"].record(question, res)
    except Exception:                                          # noqa: BLE001
        pass


# STATE["history"] is the in-memory "recent activity" ring the trust panel reads back as
# its last 20 entries. It is never persisted, but it was appended to on EVERY /api/ask and
# /api/ask/stream with no upper bound -- docs/ROBUSTNESS.md flags this as the leak an
# 8-hour soak would find, and it matters here specifically because this unit runs under
# MemoryMax=800M on a 7.4 GiB board shared with three llama.cpp servers, and a wedged demo
# loop can fire thousands of asks. The bound goes at the append site: keep the most recent
# entries and drop the oldest, so the list is capped no matter how long the box stays up.
_HISTORY_CAP = 200


def _remember(entry):
    """Append to the recent-activity ring, bounded to the last _HISTORY_CAP entries.

    del h[:-cap] trims in place, so STATE keeps pointing at the same list object (nothing
    else holds a separate reference, but replacing it would be a foot-gun waiting for one)
    and the cost is O(cap), paid only on the appends past the cap.
    """
    h = STATE["history"]
    h.append(entry)
    if len(h) > _HISTORY_CAP:
        del h[:-_HISTORY_CAP]


# G6 / D-040: pydantic's max_length bounds fire only AFTER Starlette has buffered the whole
# request body into memory, so a multi-megabyte POST is read in full before the 2,000-char
# limit rejects it. On a board under MemoryMax=800M shared with three llama.cpp servers
# that is the cheap exhaustion the request models do NOT close. This guard reads the
# declared Content-Length off the raw ASGI scope and short-circuits with a 413 BEFORE the
# app -- and therefore before the body -- is ever touched.
#
# It is a PURE ASGI middleware, deliberately NOT a BaseHTTPMiddleware. BaseHTTPMiddleware
# buffers the response to re-wrap it, which would break /api/ask/stream: that endpoint's
# whole point is emitting 'retrieving -> scoring -> gate' SSE events as they happen, and a
# buffering middleware would hold them until the stream closed, turning a live demo of the
# gate into a spinner. Operating on scope/receive/send and only ever short-circuiting the
# REQUEST leaves the streaming RESPONSE path completely untouched.
#
# 256 KB is ~30x the largest legitimate body (an 8,000-char answer plus JSON framing); it
# exists to stop the absurd, not to second-guess the pydantic field bounds. The header
# check covers the honest case -- browsers, curl and httpx all send Content-Length; a
# chunked body with no declared length still reaches pydantic, which rejects it after
# buffering as it did before, so this never makes anything worse and closes the common case.
_MAX_BODY_BYTES = 256 * 1024


class MaxBodySizeMiddleware:
    """Reject an over-large declared request body with 413 before it is buffered."""

    def __init__(self, app, max_bytes=_MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # ASGI header names are lower-cased bytes.
            for name, value in scope.get("headers", []):
                if name == b"content-length":
                    try:
                        length = int(value)
                    except ValueError:
                        length = None                 # malformed: let the app 4xx it
                    if length is not None and length > self.max_bytes:
                        await self._reject(send)
                        return
                    break
        await self.app(scope, receive, send)

    async def _reject(self, send):
        body = json.dumps({"detail": "request body too large"}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(MaxBodySizeMiddleware)


# --------------------------------------------------------------------------
# `learner` is chosen by the client and nothing authenticates it. That is a deliberate
# position for this device -- the API binds 127.0.0.1 (main() below), the kiosk is WebKit
# with no address bar, and the box is single-user by design -- but state the limit
# plainly: anyone who can reach port 8000 can read anyone's record with
# GET /api/learn/history?learner=X. Nothing below changes that, and no auth system is
# invented here, because a fake one is worse than an honest absence of one.
#
# docs/FIELDING.md ("Privacy") and docs/SECURITY_AUDIT.md M-3 draw the line in the same
# place: THE MOMENT THIS IDENTIFIER IS AN EDIPI this becomes a genuine fielding gate, not
# a note -- the record then holds an individual Marine's demonstrated weaknesses, keyed to
# a name the client picked and carried in a URL. What that needs is a server-side session
# or a kiosk PIN, an HMAC of the identifier at rest, a retention decision, and the
# identifier out of the query string and out of every log line.
#
# What IS enforced here is shape, which is worth having on its own. Every query in
# session.py is parameterised, but "parameterised" is not a reason to accept arbitrary
# bytes into a column that is also an index key, a filename-shaped thing in the xAPI
# export, and eventually a log line. Bounding it also stops a stray client minting
# unbounded distinct learners: each one is a permanent partition of learn_attempts.
# Case is folded so "Smith" and "smith" cannot become two records of the same person --
# an EDIPI is digits, so that costs nothing later.
Learner = Annotated[str, StringConstraints(
    strip_whitespace=True, to_lower=True, min_length=1, max_length=64,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]


class Ask(BaseModel):
    # The longest question in eval/ is 128 characters and the median is 76, so 2,000 is
    # roughly fifteen times the real workload. It needs a bound at all because Starlette
    # buffers the entire body before pydantic sees it and this unit runs under
    # MemoryMax=800M on a 7.4 GiB board shared with three llama.cpp servers.
    question: str = Field(min_length=1, max_length=2000)


# SchoolCircle's caller supplies the approved source set. This is intentionally a
# different operation from /api/ask: it ranks only this request's passages and never opens
# or queries Anchor's doctrine index. The request cap also keeps the cross-encoder request
# comfortably below the API's body-size guard.
_GROUND_CONTRACT = "schoolcircle-grounding-v1"
_GROUND_MAX_PASSAGES = 16
_GROUND_MAX_PASSAGE_CHARS = 8000
_GROUND_MAX_SOURCE_CHARS = 2048
_GROUND_TOP_N = 8
_GROUND_RERANK_TIMEOUT_S = 20
# This is the shipped default.yaml threshold. A normally started service takes its
# configured value from the pipeline; retaining this fallback makes the standalone route
# fail closed if it is mounted before startup configuration has been installed.
_GROUND_DEFAULT_RERANK_THRESHOLD = 3.0


class GroundPassage(BaseModel):
    """An opaque, caller-approved passage whose provenance must survive unchanged."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=_GROUND_MAX_PASSAGE_CHARS)
    source: str = Field(min_length=1, max_length=_GROUND_MAX_SOURCE_CHARS)


class GroundRequest(BaseModel):
    """Source-scoped grounding request; no corpus identifiers are accepted or inferred."""

    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    passages: list[GroundPassage] = Field(max_length=_GROUND_MAX_PASSAGES)


class GroundResponse(BaseModel):
    """The fixed SchoolCircle response contract."""

    model_config = ConfigDict(extra="forbid")
    abstained: bool
    passages: list[GroundPassage]
    contract: Literal["schoolcircle-grounding-v1"] = _GROUND_CONTRACT


class Decision(BaseModel):
    item_id: str = Field(min_length=1, max_length=128)
    status: str = Field(min_length=1, max_length=16)   # approved | rejected | pending
    reviewer: str = Field(default="instructor", max_length=64)
    # These three are optional: the client sends what it edited and omits the rest, and
    # some clients send an explicit null. The annotation must therefore be `... | None`
    # -- a bare `str`/`list` with default=None accepts an ABSENT key but 422s on an
    # explicit `null` (pydantic v2 validates None against the declared type), which is a
    # surprising, client-breaking asymmetry for an optional field.
    note: str | None = Field(default=None, max_length=2000)
    edited_question: str | None = Field(default=None, max_length=2000)
    # build_items.py keeps at most three key points per item (:93), each around 90
    # characters. Eight at 600 leaves a reviewer room to rewrite one properly without
    # leaving the field open -- the reviewer console is the one endpoint that legitimately
    # accepts prose, so it is the one worth bounding explicitly.
    edited_points: list[Annotated[str, StringConstraints(max_length=600)]] | None = Field(
        default=None, max_length=8)


class Answer(BaseModel):
    item_id: str = Field(min_length=1, max_length=128)
    # A 100 KB answer was accepted before this line. It was never going to be read:
    # grade() truncates to 1,200 characters for the prompt and record() stores 2,000
    # (session.py:324, :358), so everything past that cost memory and bought nothing.
    answer: str = Field(max_length=8000)
    # 1-4 are the only labels session.CONFIDENCE defines, and the UI renders exactly those
    # four buttons. Out of range is therefore a client bug; a 422 makes it visible instead
    # of silently recording a clamped number as if the learner had chosen it.
    confidence: int = Field(default=3, ge=1, le=4)
    learner: Learner = "default"


@app.get("/")
def index():
    return FileResponse(STATE["ui_dir"] / "index.html")


# The three llama.cpp servers, probed rather than assumed. Ports are 8080 generator,
# 8081 embeddings, 8082 reranker (scripts/install_services.sh).
_HEALTH_TTL_S = 2.0
_health_cache = {"at": 0.0, "models": None}
_health_lock = threading.Lock()


def _probe(url, timeout=0.8):
    """True if a llama.cpp server answers /health with a 200.

    0.8s, not the 120-300s the answer path uses: this endpoint is polled and its job is to
    report a hung server, not to wait for one. A server that is merely down fails
    immediately anyway -- urlopen raises ConnectionRefusedError without waiting out the
    timeout -- so the budget only ever gets spent on a genuinely wedged process.
    """
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:                                          # noqa: BLE001
        return False


def _model_urls():
    """Where the three servers are, per config, with the documented ports as a fallback.

    Read off the pipeline rather than hardcoded so a config change cannot make health
    report on a port nothing is using -- but it must still answer when the pipeline failed
    to build, which is exactly the moment someone is polling this.
    """
    p = STATE["pipeline"]
    gen = getattr(p, "gen_url", None) or "http://127.0.0.1:8080/v1"
    return {"generator": gen.rsplit("/v1", 1)[0],
            "embeddings": getattr(p, "embed_url", None) or "http://127.0.0.1:8081",
            "reranker": getattr(p, "rerank_url", None) or "http://127.0.0.1:8082"}


@app.get("/api/health")
def health():
    """Report the three dependencies instead of asserting that they are fine.

    This used to return {"ok": true} unconditionally, which meant the first sign of a dead
    generator was a judge's question coming back as the word "failed", with nothing
    anywhere saying which of the three boxes was at fault.

    Two constraints shape the implementation. scripts/boot_test.py gates cold-boot
    readiness on this URL with `curl -m 2`, so the whole probe must finish inside two
    seconds -- hence three concurrent probes at 0.8s rather than three sequential ones at
    2.4s worst case. And it is polled, by boot_test in a loop and by the kiosk header, so
    the result is cached for two seconds: a poll must never turn into a request per poller
    per tick against model servers that are already the scarce resource on this board.

    `ok` stays truthy while the device can answer, because boot_test.py and kiosk.sh read
    it as a readiness gate and that contract predates this change. Note llama.cpp answers
    /health before it has finished loading weights, so `ok` means reachable, not warm.
    """
    now = time.time()
    with _health_lock:
        models = _health_cache["models"]
        if models is None or now - _health_cache["at"] >= _HEALTH_TTL_S:
            urls = _model_urls()
            with ThreadPoolExecutor(max_workers=len(urls)) as ex:
                models = dict(zip(urls, ex.map(_probe, urls.values())))
            _health_cache.update(at=now, models=models)
    return {"ok": all(models.values()), "api": True, "models": models,
            "uptime_s": round(time.time() - STATE["started"], 1)}


@app.get("/api/network")
def network():
    return network_status.status()


@app.get("/api/telemetry")
def telem():
    r = telemetry.get_reader()
    return {"telemetry": r.latest(), "power_mode": telemetry.power_mode()}


@app.get("/api/scoreboard")
def scoreboard():
    """Last committed eval run. The abstention numbers are the product."""
    p = STATE["eval_path"]
    if not p or not Path(p).exists():
        return {"available": False,
                "note": "no eval run on device yet - run `make eval`"}
    d = json.loads(Path(p).read_text())
    return {
        "available": True,
        "threshold": d.get("threshold"),
        "false_answer_rate": d.get("false_answer_rate"),
        "correct_abstention_rate": d.get("correct_abstention_rate"),
        "over_refusal_rate": d.get("over_refusal_rate"),
        "citation_correct_rate": d.get("citation_correct_rate"),
        "p50_latency_s": d.get("p50_latency_s"),
        "p95_latency_s": d.get("p95_latency_s"),
        "traps_kept": d.get("traps_kept"), "n_traps": d.get("n_traps"),
    }


@app.get("/api/corpus")
def corpus():
    pipe = STATE["pipeline"]
    rows = pipe.db.execute(
        "SELECT pub_id, COUNT(*) n FROM chunks GROUP BY pub_id ORDER BY pub_id").fetchall()
    meta = {k: v for k, v in pipe.db.execute("SELECT key, value FROM index_meta")}
    return {"documents": [{"pub_id": r["pub_id"], "chunks": r["n"]} for r in rows],
            "total_chunks": sum(r["n"] for r in rows), "index_meta": meta}


@app.get("/api/records")
def records():
    if not STATE["records"]:
        return {"available": False}
    return {"available": True, **STATE["records"].stats()}


@app.post("/api/records/export")
def export_records():
    """Signed bundle for later sync -- the on-ramp to an existing LMS."""
    out = Path("/opt/tutor/xapi_bundle.json")
    return STATE["records"].export_bundle(out)


@app.get("/api/gaps")
def gaps():
    """Curriculum gap report. Read from disk, not recomputed per request.

    Classifying a refusal costs a deep retrieval sweep (k=800 vs production k=50), which
    is far too slow to run on a dashboard poll. The report is generated on demand and
    served from its last run, with the run time shown so nobody mistakes a stale report
    for a live one.
    """
    p = Path(STATE["gap_path"])
    if not p.exists():
        return {"available": False,
                "note": "no gap report yet - POST /api/gaps/run"}
    d = json.loads(p.read_text())
    rows = d.get("rows", [])
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    return {
        "available": True,
        "generated_mtime": p.stat().st_mtime,
        "counts": d.get("counts", {}),
        "total": len(rows),
        "findability": [
            {"question": r["question"], "evidence": (r.get("evidence") or [None])[0],
             "deep_score": r.get("deep_top_score"),
             "production_score": r.get("production_score")}
            for r in by_kind.get("FINDABILITY_GAP", [])][:12],
        "missing_publications": sorted({
            p for r in by_kind.get("CORPUS_GAP", [])
            for p in r.get("missing_publications", [])}),
        "corpus_gap_questions": [r["question"] for r in by_kind.get("CORPUS_GAP", [])][:12],
        "doctrinal_gap_questions": [r["question"]
                                    for r in by_kind.get("DOCTRINAL_GAP", [])][:12],
    }


# One gap run at a time. gap_report.py costs a k=800 retrieval sweep per recorded refusal
# and runs for tens of minutes; two of them compete with three llama.cpp servers for
# 7.4 GiB, and the subprocess is a child of this unit so it counts against the same
# MemoryMax=800M. Thrashing this board is not hypothetical -- the API unit reached restart
# counter 386 doing exactly that (scripts/install_services.sh:83-85), and the button that
# starts this is one a judge holding the keyboard can double-click.
_gaps_lock = threading.Lock()
_gaps_started = 0.0
# Same shape, same reason: one embedding round trip per item over the whole bank, against
# a single-slot embedding server. Two concurrent runs also race to write the same
# flags_json rows, so the loser's verdicts silently overwrite the winner's.
_precheck_lock = threading.Lock()
_precheck_started = 0.0


@app.post("/api/gaps/run")
async def gaps_run():
    """Regenerate the gap report. Slow by nature; runs off the request thread."""
    import subprocess
    global _gaps_started
    loop = asyncio.get_running_loop()

    # Non-blocking, so a second click is answered rather than silently queued behind half
    # an hour of work. The guard cannot stick: it is released in the finally below, so a
    # subprocess that fails, times out, or raises still clears it, and the only way to
    # lose the release is to lose the process -- which takes the lock with it.
    if not _gaps_lock.acquire(blocking=False):
        return {"ok": False, "error": "already_running",
                "running_s": round(time.time() - _gaps_started, 1),
                "message": "A gap report is already being generated on this device."}
    _gaps_started = time.time()

    def _run():
        return subprocess.run(
            [sys.executable, str(HERE.parent / "records" / "gap_report.py"),
             "--db", STATE["db_path"], "--records", STATE["records_path"],
             "--config", STATE["config_path"], "--out", STATE["gap_path"]],
            capture_output=True, text=True, timeout=1800)

    try:
        r = await loop.run_in_executor(None, _run)
    finally:
        _gaps_lock.release()
    return {"ok": r.returncode == 0, "stderr": r.stderr[-400:] if r.returncode else None}


# --------------------------------------------------------------------------
# Instructor view. Class aggregate only -- see the privacy note on the endpoint.
# --------------------------------------------------------------------------

@app.get("/api/instructor/overview")
def instructor_overview():
    """Class-level mastery across all learners, plus the existing gap report.

    D-050 (closing C5) draws the line this endpoint enforces: the instructor sees the
    AGGREGATE -- per-section mastery summed over the class, and where the corpus falls
    short -- but never an individual Marine's record, except a Marine's own. The privacy
    boundary is a real constraint in the code, not a UI choice:

      * this endpoint takes NO `learner` parameter, so there is nothing to point at a
        named Marine;
      * LearnStore.class_overview() returns rows keyed by SECTION and a single class-size
        COUNT, and never a learner identifier -- there are no per-learner rows to return,
        so a client cannot ask for one by fiddling the URL.

    An individual's history and calibration stay behind /api/learn/history and
    /api/learn/calibration, each keyed to a learner the caller must name -- which on this
    shared appliance is the Marine's own convenience identifier (see the Learner note
    below and D-050). The pedagogically useful thing (where the class is weak) is served
    here; the privacy-sensitive thing (which Marine is weak) is not, because the two are
    separable and only the first belongs to the instructor.
    """
    L = STATE["learn"]
    if not L or not L.items:
        return {"available": False, "reason": "no_items_generated"}
    # gaps() reads the last gap report off disk and already returns a plain dict, so the
    # gap report is reused rather than recomputed or duplicated here.
    return {"available": True, **L.class_overview(), "gaps": gaps()}


# --------------------------------------------------------------------------
# Learning: the front door. Everything above this line is the trust story, which
# belongs behind "see how it works" rather than in front of someone studying.
# --------------------------------------------------------------------------

@app.get("/api/learn/track")
def learn_track(pub: str = Query(None, max_length=64),
                # The home screen's progress and due counts are per learner now that
                # several Marines can share the box (D-050). Same validated, case-folded
                # Learner as everywhere else; defaults to "default" so the single-learner
                # path is unchanged.
                learner: Learner = "default"):
    L = STATE["learn"]
    if not L or not L.items:
        return {"available": False, "reason": "no_items_generated",
                "note": "no practice items yet - run learn/build_items.py"}
    approved = len(L.servable())
    return {"available": True, "n_items": len(L.items),
            "n_approved": approved,
            "n_pending_review": len(L.items) - approved,
            "confidence_labels": CONFIDENCE,
            "due": L.due_counts(learner), **L.progress(learner=learner, pub=pub)}


@app.get("/api/learn/next")
def learn_next(section: str = Query(None, max_length=200),
               pub: str = Query(None, max_length=64),
               learner: Learner = "default"):
    L = STATE["learn"]
    if not L or not L.items:
        return {"available": False, "reason": "no_items_generated"}

    # "Nothing to serve" and "you have finished" are completely different states and
    # must never share a message. Reporting the first as the second told a learner with
    # zero approved questions that they had completed the course.
    servable = L.servable()
    if not servable:
        return {"available": True, "done": False, "blocked": True,
                "reason": "none_approved",
                "n_items": len(L.items), "n_approved": 0}

    item = L.next_item(learner=learner, section=section, pub=pub)
    if not item:
        return {"available": True, "done": True, "reason": "all_attempted",
                "n_approved": len(servable)}
    # The source text and key points are deliberately NOT sent until the learner has
    # answered. Shipping them to the browser first would put the answer one devtools
    # inspection away, and more importantly would tempt an honest learner into
    # recognition instead of recall -- which is the entire thing being trained.
    return {"available": True, "done": False,
            "due": L.due_counts(learner),
            "item": {"id": item["id"], "question": item["question"],
                     "pub_id": item.get("pub_id"),
                     "chapter": item["chapter"],
                     "chapter_title": item["chapter_title"],
                     "section": item["section"],
                     "n_key_points": len(item["key_points"])}}


@app.post("/api/learn/answer")
async def learn_answer(body: Answer):
    L = STATE["learn"]
    item = L.items.get(body.item_id) if L else None
    if not item:
        return {"ok": False, "error": "unknown item"}
    conf = max(1, min(4, int(body.confidence)))
    loop = asyncio.get_running_loop()
    t0 = time.time()
    grade = await loop.run_in_executor(None, L.grade, item, body.answer)
    el = round(time.time() - t0, 2)
    L.record(body.learner, item, body.answer, conf, grade, el)
    return {"ok": True, "verdict": grade["verdict"], "covered": grade["covered"],
            "missed": grade["missed"], "feedback": grade["feedback"],
            "confidence": conf, "latency_s": el,
            "source_text": item["source_text"], "citation": item["citation"],
            "key_points": item["key_points"],
            "calibration": L.calibration(body.learner)}


@app.get("/api/learn/history")
def learn_history(learner: Learner = "default",
                  # SQLite reads LIMIT -1 as "no limit", so an unvalidated negative here
                  # returned every attempt ever recorded. The UI asks for 25.
                  limit: int = Query(20, ge=1, le=200)):
    L = STATE["learn"]
    return {"history": L.history(learner, limit) if L else []}


@app.get("/api/learn/calibration")
def learn_calibration(learner: Learner = "default"):
    L = STATE["learn"]
    if not L:
        return {"n": 0, "buckets": []}
    return {**L.calibration(learner), "recent": L.recent(learner)}


# --------------------------------------------------------------------------
# Item review. No item reaches a learner until a human approves it.
# --------------------------------------------------------------------------

@app.get("/api/review/stats")
def review_stats():
    L = STATE["learn"]
    if not L:
        return {"available": False}
    return {"available": True, "n_items": len(L.items),
            "servable": len(L.servable()), **L.review.stats()}


@app.get("/api/review/queue")
# 400, not the 60 this defaults to, because ui/index.html asks for exactly 400 and that
# file is not this change's to touch. Serialising 400 items is a latency cost worth
# revisiting there; it is not a reason to 422 the reviewer console today.
def review_queue(limit: int = Query(60, ge=1, le=400)):
    L = STATE["learn"]
    if not L:
        return {"queue": []}
    q = L.review.queue(L.items, limit=limit)
    # The full item goes to the reviewer, source paragraph included -- they cannot judge
    # whether a question is grounded without seeing what it was generated from.
    return {"queue": [{
        "id": e["item"]["id"], "question": e["item"]["question"],
        "key_points": e["item"]["key_points"],
        "chapter": e["item"]["chapter"], "chapter_title": e["item"]["chapter_title"],
        "section": e["item"]["section"], "citation": e["item"]["citation"],
        "source_text": e["item"]["source_text"], "flags": e["flags"],
    } for e in q],
        # The queue is truncated; the counts are not. A reviewer who is shown "1 of 60"
        # while 1,000 items sit pending has been told something false about how much work
        # is left.
        "shown": len(q), "pending": L.review.stats().get("pending", 0),
        "flagged_pending": sum(1 for e in q if e["flags"])}


@app.post("/api/review/decide")
def review_decide(body: Decision):
    L = STATE["learn"]
    if not L or body.item_id not in L.items:
        return {"ok": False, "error": "unknown item"}
    try:
        L.review.decide(body.item_id, body.status, reviewer=body.reviewer,
                        note=body.note, edited_question=body.edited_question,
                        edited_points=body.edited_points)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **L.review.stats()}


@app.post("/api/review/approve_unflagged")
def review_approve_unflagged():
    """Clear every pending item that carries no automatic flag."""
    L = STATE["learn"]
    if not L:
        return {"ok": False}
    touched = L.review.bulk_decide_unflagged(set(L.items))
    unchecked = getattr(L.review, "last_bulk_unchecked", [])
    return {"ok": True, "approved": len(touched),
            # Reported so the reviewer is told what was NOT cleared and why, rather than
            # being left to infer it from a count that looks lower than expected.
            "skipped_unchecked": len(unchecked),
            **L.review.stats()}


@app.post("/api/review/precheck")
async def review_precheck():
    """Run the automatic checks over every item so the queue can be triaged."""
    global _precheck_started
    L = STATE["learn"]
    if not L:
        return {"ok": False}
    loop = asyncio.get_running_loop()

    if not _precheck_lock.acquire(blocking=False):
        return {"ok": False, "error": "already_running",
                "running_s": round(time.time() - _precheck_started, 1),
                "message": "The automatic checks are already running over the item bank."}
    _precheck_started = time.time()

    def _run():
        n_flagged = 0
        for item in L.items.values():
            flags = precheck(item, embed_url=STATE["pipeline"].embed_url)
            L.review.set_flags(item["id"], flags)
            if flags:
                n_flagged += 1
        return n_flagged

    try:
        n = await loop.run_in_executor(None, _run)
    finally:
        _precheck_lock.release()
    return {"ok": True, "checked": len(L.items), "flagged": n, **L.review.stats()}


@app.get("/api/history")
def history():
    return {"recent": STATE["history"][-20:]}


def _ground_rerank_url():
    """Return the existing local reranker URL without invoking corpus retrieval."""
    pipe = STATE.get("pipeline")
    if pipe and getattr(pipe, "rerank_url", None):
        return pipe.rerank_url
    return (STATE.get("config", {}).get("reranker", {}).get("base_url")
            or "http://127.0.0.1:8082")


def _ground_score_threshold():
    """Use Anchor's configured score gate, with a fail-closed startup fallback."""
    pipe = STATE.get("pipeline")
    if pipe and hasattr(pipe, "score_threshold"):
        return pipe.score_threshold
    configured = (STATE.get("config", {}).get("abstention", {})
                  .get("reranker_score_threshold", _GROUND_DEFAULT_RERANK_THRESHOLD))
    return configured


def _ground_response(abstained, passages=()):
    """Build the fixed response shape without transforming caller provenance."""
    return GroundResponse(
        abstained=abstained,
        passages=[GroundPassage(id=p.id, text=p.text, source=p.source) for p in passages],
    )


def _ground_reranker_error(status_code, detail):
    """Raise a deliberately generic upstream-service error for /api/ground."""
    raise HTTPException(status_code=status_code, detail=detail)


@app.post("/api/ground", response_model=GroundResponse,
          responses={502: {"description": "reranker returned an invalid or failed response"},
                     503: {"description": "reranker is unavailable"}})
async def ground(body: GroundRequest):
    """Rank only caller-supplied approved passages and return their exact provenance.

    This endpoint deliberately calls the existing reranker directly rather than
    ``pipeline.retrieve`` or ``hybrid.search``. Consequently neither BM25, embeddings,
    nor SQLite's global index can add an unapproved passage to the result. It does not
    generate an answer. Only an empty source set or a valid low relevance score is an
    abstention. A failed, unavailable, or malformed reranker response is an explicit,
    sanitized 502/503 service error rather than a misleading normal abstention.
    """
    if not body.passages:
        return _ground_response(True)

    loop = asyncio.get_running_loop()
    try:
        # The ordinary Anchor query path deliberately tolerates long reranker requests and
        # retries (hybrid.rerank's legacy defaults). SchoolCircle is a request/response
        # evidence API, so its one reranker request is strictly bounded to 20 seconds with
        # neither transport retries nor the context-budget retry.
        rerank_request = partial(
            rerank, body.question, [p.text for p in body.passages], _ground_rerank_url(),
            timeout=_GROUND_RERANK_TIMEOUT_S, attempts=1, retry_context_errors=False)
        ranked = await loop.run_in_executor(None, rerank_request)
    except urllib.error.HTTPError as e:
        # post_json has already retried 502/503/504. Those are still availability errors
        # after the retry budget; other HTTP responses mean the upstream request failed.
        if e.code in (502, 503, 504):
            _ground_reranker_error(503, "reranker unavailable")
        _ground_reranker_error(502, "reranker failed")
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        _ground_reranker_error(503, "reranker unavailable")
    except Exception:                                              # noqa: BLE001
        _ground_reranker_error(502, "reranker failed")

    # Treat results from the model server as untrusted. In particular, never use its
    # returned text or identifier: an index can select only an original request passage.
    if not isinstance(ranked, list) or not ranked:
        _ground_reranker_error(502, "reranker returned an invalid response")
    selected, seen = [], set()
    for item in ranked:
        if not isinstance(item, tuple) or len(item) != 2:
            _ground_reranker_error(502, "reranker returned an invalid response")
        index, score = item
        if (isinstance(index, bool) or not isinstance(index, int)
                or index < 0 or index >= len(body.passages)
                or index in seen
                or isinstance(score, bool) or not isinstance(score, (int, float))
                or not isfinite(score)):
            _ground_reranker_error(502, "reranker returned an invalid response")
        seen.add(index)
        selected.append((index, score))

    threshold = _ground_score_threshold()
    if threshold is not None and selected[0][1] < threshold:
        return _ground_response(True)

    # rerank() is Anchor's existing best-first ordering. Preserve it; only cap the number
    # of evidence passages, and copy each original id/text/source verbatim into the
    # response. No generated text and no global-index metadata can cross this boundary.
    return _ground_response(False, (body.passages[index]
                                    for index, _score in selected[:_GROUND_TOP_N]))


@app.post("/api/ask")
async def ask(body: Ask):
    """Non-streaming answer. Used by the eval path and as an SSE fallback."""
    loop = asyncio.get_running_loop()
    t0 = time.time()
    res = await loop.run_in_executor(None, STATE["pipeline"].ask, body.question)
    res["latency_s"] = round(time.time() - t0, 2)
    _remember({"question": body.question,
               "abstained": res["abstained"],
               "latency_s": res["latency_s"],
               "reason": res.get("abstain_reason")})
    _log(body.question, res)
    return res


@app.post("/api/ask/stream")
async def ask_stream(body: Ask):
    """SSE. Emits stage events so the UI can show the gate working, not just the answer.

    Showing 'retrieving -> scoring -> gate decision' is the point of the demo: the
    abstention is the product, and a spinner followed by a refusal looks like a failure
    rather than a designed behaviour.
    """
    async def gen():
        loop = asyncio.get_running_loop()
        t0 = time.time()

        yield _sse("stage", {"stage": "retrieving"})
        chunks = await loop.run_in_executor(
            None, STATE["pipeline"].retrieve, body.question)
        top = chunks[0].get("rerank_score") if chunks else None
        yield _sse("retrieval", {
            "n": len(chunks), "top_score": top,
            "threshold": STATE["pipeline"].score_threshold,
            "sources": [{"n": i, "citation": c["citation"],
                         "score": c.get("rerank_score")}
                        for i, c in enumerate(chunks[:8], 1)]})

        thr = STATE["pipeline"].score_threshold
        if thr is not None and (top is None or top < thr):
            yield _sse("gate", {"passed": False, "reason": "low_retrieval_score",
                                "top_score": top, "threshold": thr})
            refusal = {"abstained": True, "abstain_reason": "low_retrieval_score",
                       "top_rerank_score": top, "latency_s": round(time.time()-t0, 2),
                       "text": STATE["pipeline"].refusal_text, "citations": [],
                       "sources": [{"citation": c["citation"]} for c in chunks[:8]]}
            _log(body.question, refusal)
            yield _sse("answer", {"text": STATE["pipeline"].refusal_text,
                                  "abstained": True})
            yield _sse("done", {"latency_s": round(time.time() - t0, 2)})
            return

        yield _sse("gate", {"passed": True, "top_score": top, "threshold": thr})
        yield _sse("stage", {"stage": "generating"})

        res = await loop.run_in_executor(None, STATE["pipeline"].ask, body.question)
        res["latency_s"] = round(time.time() - t0, 2)
        _remember({"question": body.question,
                   "abstained": res["abstained"],
                   "latency_s": res["latency_s"],
                   "reason": res.get("abstain_reason")})
        _log(body.question, res)
        yield _sse("answer", {"text": res.get("text"),
                              "abstained": res["abstained"],
                              "abstain_reason": res.get("abstain_reason"),
                              "citations": res.get("citations", [])})
        yield _sse("done", {"latency_s": res["latency_s"]})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/tutor/doctrine.sqlite")
    ap.add_argument("--config", default="/opt/tutor/config/default.yaml")
    ap.add_argument("--ui", default=str(HERE.parent.parent / "ui"))
    ap.add_argument("--eval-json", default="/opt/tutor/eval/last_run.json")
    ap.add_argument("--host", default="127.0.0.1")   # loopback only, never 0.0.0.0
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--records", default="/opt/tutor/records.sqlite")
    ap.add_argument("--gap-json", default="/opt/tutor/gap_report.json")
    ap.add_argument("--items", default="/opt/tutor/learn_items.json")
    args = ap.parse_args()

    cfg = {}
    if Path(args.config).exists():
        import yaml
        cfg = yaml.safe_load(Path(args.config).read_text()) or {}
    cfg.setdefault("index", {})["path"] = args.db

    from pipeline import TutorPipeline
    STATE["pipeline"] = TutorPipeline(cfg)
    STATE["config"] = cfg
    STATE["ui_dir"] = Path(args.ui)
    STATE["eval_path"] = args.eval_json
    STATE["gap_path"] = args.gap_json
    STATE["db_path"] = args.db
    STATE["records_path"] = args.records
    STATE["config_path"] = args.config
    STATE["started"] = time.time()

    def _warm(attempts=40, gap=2.0):
        """Absorb first-request initialisation before a human touches the box.

        The first query through a fresh API costs far more than a steady-state one: it
        opens sqlite, loads the vec extension, and makes the first round trip to each of
        the three model servers, none of which have resident weights yet.

        It RETRIES, and that is the whole point. The API and the model servers are started
        in parallel -- systemd's After= orders start-up, not readiness -- so the first
        attempt reliably lands on a refused connection. The original single attempt
        swallowed that ConnectionRefusedError and returned, so nothing was ever warmed and
        the cost was still paid by whoever asked first. The cold-boot measurement showed
        it plainly: 13.0s between the model servers reporting healthy and a question
        coming back, against a 60s budget the run missed by 0.7s.

        Failure is reported rather than swallowed. A warm-up that silently does nothing is
        worse than none, because the boot budget is planned around it working.
        """
        for n in range(attempts):
            t0 = time.time()
            try:
                STATE["pipeline"].ask("What is maneuver warfare?")
                print(f"warm-up complete after {n + 1} attempt(s), "
                      f"{time.time() - t0:.1f}s", flush=True)
                return
            except Exception as e:                             # noqa: BLE001
                if n == attempts - 1:
                    print(f"! warm-up never succeeded after {attempts} attempts: {e}",
                          file=sys.stderr, flush=True)
                    return
                time.sleep(gap)

    import threading
    threading.Thread(target=_warm, daemon=True).start()
    try:
        STATE["records"] = RecordStore(args.records)
    except Exception as e:                                     # noqa: BLE001
        print(f"! record store unavailable: {e}", file=sys.stderr)
    try:
        STATE["learn"] = LearnStore(args.records, args.items)
        print(f"learning items loaded: {len(STATE['learn'].items)}")
    except Exception as e:                                     # noqa: BLE001
        STATE["learn"] = None
        print(f"! learning store unavailable: {e}", file=sys.stderr)
    telemetry.get_reader()

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
