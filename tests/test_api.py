"""Unit tests for the four hardening changes in src/api/main.py.

Everything here runs against FastAPI's TestClient in-process: no model server, no device,
no socket that leaves the box. The two places that would otherwise reach the network --
`_probe` and `subprocess.run` -- are substituted, except for one test that deliberately
probes a port it has just proved is closed, because "a refused connection returns
immediately" is the assumption the whole health budget rests on and it is worth measuring
rather than believing.

What is being protected, in the order the fixes were made:

  * `/api/health` used to return {"ok": true} without probing anything, so a dead
    generator first showed up as a judge's question failing. It is also polled by
    scripts/boot_test.py with `curl -m 2`, so both the two-second ceiling and the truthy
    `ok` are contracts, not preferences.
  * The request models had no bounds at all; a 100 KB answer body was accepted.
  * `POST /api/gaps/run` spawns a subprocess that runs for tens of minutes. Two of them
    on a 7.4 GiB board is the failure that got this unit to restart counter 386.
  * `learner` is client-supplied and unauthenticated. That is not fixed here and cannot
    be -- see the comment above `Learner` in main.py. What is tested is that the
    identifier is bounded and normalised.
"""
import socket
import time

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture(scope="module")
def client():
    # The CLI paths main() would normally set. /api/gaps/run reads them while building the
    # subprocess argv, before anything is spawned.
    main.STATE.update(db_path="/nonexistent/doctrine.sqlite",
                      records_path="/nonexistent/records.sqlite",
                      config_path="/nonexistent/default.yaml",
                      gap_path="/nonexistent/gap_report.json",
                      started=time.time())
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _no_health_cache():
    """Each test starts cold; the cache itself is tested explicitly below."""
    main._health_cache.update(at=0.0, models=None)


def stub_probes(monkeypatch, states, calls=None):
    """Replace _probe with a lookup keyed on which port the URL names."""
    ports = {"8080": "generator", "8081": "embeddings", "8082": "reranker"}

    def fake(url, timeout=0.8):
        which = next(v for k, v in ports.items() if k in url)
        if calls is not None:
            calls.append(which)
        return states[which]

    monkeypatch.setattr(main, "_probe", fake)


ALL_UP = {"generator": True, "embeddings": True, "reranker": True}


# --------------------------------------------------------------------------
# 1. /api/health
# --------------------------------------------------------------------------

def test_health_reports_each_server_separately(client, monkeypatch):
    stub_probes(monkeypatch, ALL_UP)
    d = client.get("/api/health").json()
    assert d["models"] == ALL_UP
    assert d["ok"] is True and d["api"] is True
    assert d["uptime_s"] >= 0


def test_health_names_the_server_that_is_down(client, monkeypatch):
    stub_probes(monkeypatch, dict(ALL_UP, generator=False))
    r = client.get("/api/health")
    # Still a 200 with a JSON body: a down dependency is a reported state, not a server
    # error, and the UI has to be able to read WHICH one rather than get plain text.
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert d["models"]["generator"] is False
    assert d["models"]["embeddings"] is True and d["models"]["reranker"] is True


def test_health_keeps_the_boot_test_contract(client, monkeypatch):
    """scripts/boot_test.py gates cold-boot readiness on 200 + a truthy `ok`."""
    stub_probes(monkeypatch, ALL_UP)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["ok"]


def test_health_does_not_stampede_the_model_servers(client, monkeypatch):
    """Twenty polls inside the TTL must cost one round of probes, not twenty."""
    calls = []
    stub_probes(monkeypatch, ALL_UP, calls=calls)
    for _ in range(20):
        assert client.get("/api/health").json()["ok"] is True
    assert len(calls) == 3


def test_health_probes_again_once_the_cache_expires(client, monkeypatch):
    calls = []
    stub_probes(monkeypatch, ALL_UP, calls=calls)
    client.get("/api/health")
    main._health_cache["at"] -= main._HEALTH_TTL_S + 0.01
    client.get("/api/health")
    assert len(calls) == 6


def test_health_probes_run_concurrently(client, monkeypatch):
    """The three probes must overlap, or a hung server blows the `curl -m 2` gate.

    Sequentially this is 3 x 0.4s = 1.2s; concurrently it is ~0.4s. The assertion is
    deliberately loose (< 1.0s) so it measures concurrency rather than the scheduler.
    """
    def slow(url, timeout=0.8):
        time.sleep(0.4)
        return True

    monkeypatch.setattr(main, "_probe", slow)
    t0 = time.time()
    client.get("/api/health")
    assert time.time() - t0 < 1.0


def test_probe_fails_fast_and_false_on_a_refused_port(client):
    """The real _probe, against a port proved closed. No stub, still offline."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    t0 = time.time()
    assert main._probe("http://127.0.0.1:%d" % port) is False
    assert time.time() - t0 < 1.0


def test_health_answers_when_the_pipeline_failed_to_build(client, monkeypatch):
    """The endpoint polled when things break must not need a working pipeline."""
    monkeypatch.setitem(main.STATE, "pipeline", None)
    urls = main._model_urls()
    assert "8080" in urls["generator"] and not urls["generator"].endswith("/v1")
    assert "8081" in urls["embeddings"] and "8082" in urls["reranker"]
    stub_probes(monkeypatch, dict(ALL_UP, embeddings=False))
    assert client.get("/api/health").json()["models"]["embeddings"] is False


# --------------------------------------------------------------------------
# 2. Request bounds. Validation runs before the handler, so none of these needs a
#    pipeline, a learning store or a database.
# --------------------------------------------------------------------------

def test_oversize_question_is_a_422_not_a_500(client):
    r = client.post("/api/ask", json={"question": "x" * 2001})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "string_too_long"


def test_empty_question_is_rejected(client):
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


def test_hundred_kb_answer_is_rejected(client):
    """The body that started this: 100 KB was accepted before the cap."""
    r = client.post("/api/learn/answer",
                    json={"item_id": "MCDP1-c1-0000", "answer": "x" * 100_000})
    assert r.status_code == 422


@pytest.mark.parametrize("conf", [0, 5, -1, 99])
def test_confidence_outside_the_four_labels_is_rejected(client, conf):
    r = client.post("/api/learn/answer",
                    json={"item_id": "i", "answer": "a", "confidence": conf})
    assert r.status_code == 422


@pytest.mark.parametrize("conf", [1, 2, 3, 4])
def test_every_label_session_defines_is_accepted(client, conf):
    """The four buttons the UI renders must all still validate."""
    main.Answer(item_id="i", answer="a", confidence=conf)


def test_oversize_item_id_is_rejected(client):
    r = client.post("/api/learn/answer",
                    json={"item_id": "x" * 129, "answer": "a"})
    assert r.status_code == 422


def test_decision_prose_fields_are_bounded(client):
    for field in ("note", "edited_question"):
        r = client.post("/api/review/decide",
                        json={"item_id": "i", "status": "approved", field: "x" * 2001})
        assert r.status_code == 422, field


def test_decision_edited_points_are_bounded_in_count_and_size(client):
    too_many = {"item_id": "i", "status": "approved",
                "edited_points": ["ok"] * 9}
    assert client.post("/api/review/decide", json=too_many).status_code == 422
    too_long = {"item_id": "i", "status": "approved",
                "edited_points": ["x" * 601]}
    assert client.post("/api/review/decide", json=too_long).status_code == 422


def test_a_bad_status_is_still_a_200_with_an_error(client):
    """Bounding `status` must not move its validation from review.decide to a 422.

    With no learning store loaded the endpoint short-circuits on the unknown item, which
    is the same shape of answer the UI already handles.
    """
    r = client.post("/api/review/decide", json={"item_id": "i", "status": "nonsense"})
    assert r.status_code == 200 and r.json()["ok"] is False


@pytest.mark.parametrize("limit", [-1, 0, 201, 10_000])
def test_history_limit_is_bounded(client, limit):
    """LIMIT -1 is 'no limit' in SQLite -- it returned every attempt ever recorded."""
    assert client.get("/api/learn/history?limit=%d" % limit).status_code == 422


def test_history_limit_the_ui_asks_for_still_works(client):
    assert client.get("/api/learn/history?limit=25").status_code == 200


def test_review_queue_accepts_the_limit_the_ui_hardcodes(client):
    # ui/index.html requests 400 and is not this change's file to touch.
    assert client.get("/api/review/queue?limit=400").status_code == 200
    assert client.get("/api/review/queue?limit=401").status_code == 422
    assert client.get("/api/review/queue?limit=0").status_code == 422


def test_query_string_filters_are_bounded(client):
    assert client.get("/api/learn/track?pub=" + "x" * 65).status_code == 422
    assert client.get("/api/learn/next?section=" + "x" * 201).status_code == 422


# --------------------------------------------------------------------------
# 3. /api/gaps/run concurrency guard
# --------------------------------------------------------------------------

class _Completed:
    def __init__(self, returncode=0, stderr=""):
        self.returncode, self.stderr, self.stdout = returncode, stderr, ""


def test_a_second_gap_run_is_refused_while_one_is_in_flight(client):
    assert main._gaps_lock.acquire(blocking=False)
    try:
        main._gaps_started = time.time()
        r = client.post("/api/gaps/run")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False and d["error"] == "already_running"
        assert d["running_s"] >= 0 and d["message"]
    finally:
        main._gaps_lock.release()


def test_the_guard_is_released_after_a_normal_run(client, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed())
    assert client.post("/api/gaps/run").json()["ok"] is True
    assert main._gaps_lock.acquire(blocking=False), "guard stuck after a successful run"
    main._gaps_lock.release()


def test_the_guard_is_released_when_the_subprocess_dies(client, monkeypatch):
    """A guard that survives its own subprocess is worse than no guard: it disables the
    feature until someone restarts the unit, and on this device nobody has ssh."""
    import subprocess

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gap_report.py", timeout=1800)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(subprocess.TimeoutExpired):
        client.post("/api/gaps/run")
    assert main._gaps_lock.acquire(blocking=False), "guard stuck after a failed run"
    main._gaps_lock.release()


def test_a_run_is_possible_again_after_a_refusal(client, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Completed(returncode=2, stderr="e" * 900))
    assert main._gaps_lock.acquire(blocking=False)
    try:
        assert client.post("/api/gaps/run").json()["error"] == "already_running"
    finally:
        main._gaps_lock.release()
    d = client.post("/api/gaps/run").json()
    assert d["ok"] is False and len(d["stderr"]) == 400


# --------------------------------------------------------------------------
# 4. The learner identifier: bounded and normalised, NOT authenticated.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "x" * 65,                    # unbounded length
    "",                          # empty
    "a b",                       # whitespace
    "'; DROP TABLE learn_attempts--",
    "../../etc/passwd",
    "sgt\x00smith",
    "متدرب",   # non-ASCII: not an identifier this device mints
    "-leading-dash",
])
def test_a_malformed_learner_is_rejected_everywhere(client, bad):
    assert client.get("/api/learn/history", params={"learner": bad}).status_code == 422
    assert client.get("/api/learn/next", params={"learner": bad}).status_code == 422
    assert client.get("/api/learn/calibration",
                      params={"learner": bad}).status_code == 422
    r = client.post("/api/learn/answer",
                    json={"item_id": "i", "answer": "a", "learner": bad})
    assert r.status_code == 422


@pytest.mark.parametrize("raw,normalised", [
    ("default", "default"),
    ("  Default  ", "default"),
    ("SGT.Smith_1", "sgt.smith_1"),
    ("1234567890", "1234567890"),      # the shape an EDIPI would arrive in
])
def test_a_valid_learner_is_normalised_to_one_record(raw, normalised):
    """Case folding is what stops 'Smith' and 'smith' becoming two people."""
    assert main.Answer(item_id="i", answer="a", learner=raw).learner == normalised


def test_the_default_learner_is_unchanged(client):
    assert main.Answer(item_id="i", answer="a").learner == "default"
    assert client.get("/api/learn/history").status_code == 200


# --------------------------------------------------------------------------
# 5. Content-Length size guard (G6). Pydantic's bounds fire only after Starlette has
#    buffered the whole body; this ASGI guard refuses an over-large declared body first.
#    Unit-tested by driving the middleware directly (no live server), plus one round trip.
# --------------------------------------------------------------------------

def _drive_guard(scope_type, headers):
    """Run MaxBodySizeMiddleware over a synthetic ASGI request.

    Returns (was_the_wrapped_app_called, list_of_sent_messages). This is the offline test
    of the header check the guard rests on: it must short-circuit BEFORE delegating to the
    app, and must never touch the response path (which is what keeps SSE intact).
    """
    import asyncio
    called = {"app": False}

    async def fake_app(scope, receive, send):
        called["app"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = main.MaxBodySizeMiddleware(fake_app)
    asyncio.run(mw({"type": scope_type, "headers": headers}, receive, send))
    return called["app"], sent


def test_size_guard_rejects_oversize_declared_body_before_the_app():
    hdr = [(b"content-length", str(main._MAX_BODY_BYTES + 1).encode())]
    called, sent = _drive_guard("http", hdr)
    assert called is False, "the app must never be reached for an over-cap body"
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 413


def test_size_guard_lets_a_normal_body_through_to_the_app():
    called, sent = _drive_guard("http", [(b"content-length", b"500")])
    assert called is True
    assert sent[0]["status"] == 200


def test_size_guard_lets_a_body_at_the_cap_through():
    """The cap is a ceiling, not a wall one byte early -- exactly _MAX_BODY_BYTES is fine."""
    called, _ = _drive_guard("http", [(b"content-length", str(main._MAX_BODY_BYTES).encode())])
    assert called is True


def test_size_guard_ignores_a_malformed_content_length():
    """A non-numeric length is left for the app to 4xx, not turned into a 413 here."""
    called, _ = _drive_guard("http", [(b"content-length", b"not-a-number")])
    assert called is True


def test_size_guard_is_transparent_to_non_http_scopes():
    # Lifespan / websocket scopes carry no request body to bound; pass them straight on.
    called, _ = _drive_guard("lifespan", [])
    assert called is True


def test_oversize_post_is_413_through_the_real_stack(client):
    """End to end: an over-cap POST is refused with 413 before any handler runs, so it
    needs no pipeline and never buffers the body."""
    big = b"x" * (main._MAX_BODY_BYTES + 64)
    r = client.post("/api/ask", content=big,
                    headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_a_normal_post_still_reaches_validation_through_the_guard(client):
    """The guard must not swallow ordinary requests: a 2001-char question is small enough
    to pass the size guard and must still be rejected by the pydantic bound (422)."""
    r = client.post("/api/ask", json={"question": "x" * 2001})
    assert r.status_code == 422


# --------------------------------------------------------------------------
# 6. The recent-activity ring is bounded (B5 / docs/ROBUSTNESS.md).
# --------------------------------------------------------------------------

def test_history_ring_is_capped_and_keeps_the_newest():
    main.STATE["history"].clear()
    n = main._HISTORY_CAP + 350
    for i in range(n):
        main._remember({"question": i})
    h = main.STATE["history"]
    assert len(h) == main._HISTORY_CAP           # bounded no matter how many asks
    assert h[-1]["question"] == n - 1            # newest survives
    assert h[0]["question"] == n - main._HISTORY_CAP   # oldest dropped
    main.STATE["history"].clear()


# --------------------------------------------------------------------------
# 7. Instructor overview: no store -> unavailable, not a 500; no learner drill-down.
#    (The aggregate logic itself is exercised against a real store in test_instructor.py.)
# --------------------------------------------------------------------------

def test_instructor_overview_is_unavailable_without_a_store(client):
    r = client.get("/api/instructor/overview")
    assert r.status_code == 200 and r.json()["available"] is False


def test_instructor_overview_ignores_a_learner_query_param(client):
    """The privacy boundary shows up in the URL shape too: the endpoint declares no
    `learner` parameter, so a smuggled ?learner= is simply ignored -- there is no
    per-Marine view to request."""
    assert client.get("/api/instructor/overview?learner=sgtsmith").status_code == 200
