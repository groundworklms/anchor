# ROBUSTNESS — what happens when something goes wrong at runtime

**Scope:** the FastAPI backend, the three llama.cpp dependencies, the two SQLite files,
the systemd units, and what `ui/index.html` actually renders when a request fails.
**Method:** static analysis and local reasoning only. No device access, no request to
8080/8081/8082, no service restarted, nothing outside this file written.
**Date:** 24 Aug 2026. Closes TRACKER **B2** and **B3** (documented, not yet fixed).

Every finding below carries a `file:line`. Where a claim depends on library behaviour
rather than this repo's code, the verification is recorded inline so nobody has to take
it on faith. Three such checks were run locally against the same library versions the
device uses:

| Claim | Verified how | Result |
|---|---|---|
| A FastAPI 500 body is **not JSON**, so `response.json()` throws in the browser | `inspect.getsource(starlette.middleware.errors.ServerErrorMiddleware.error_response)`, starlette 0.49.3 | `PlainTextResponse("Internal Server Error", status_code=500)` |
| `sqlite3.connect()` with no `timeout=` still sets a busy timeout | `PRAGMA busy_timeout` on a fresh connection | **5000 ms**, and `isolation_level=''` (implicit deferred transactions) |
| `StreamingResponse` sends `200` before the first `yield` | `inspect.getsource(StreamingResponse.stream_response)` | `send({"type": "http.response.start", ...})` precedes the iteration |

---

## The three most likely to be seen by a user

1. **`reset_demo.sh` archives `records.sqlite`, which is where instructor approvals
   live — so the one-key reset silently deletes every approval and kills the entire
   tutor half of the deployment.** The reset is told to run "between sessions".
   → *One-line fix: preserve and restore the `item_review` table across the archive
   (F-01).*
2. **Every failure of the generator or the reranker renders as the single word
   "failed", or as a panel that stays blank forever, because no `fetch()` in the UI
   checks `response.ok` and a 500 body is plain text that `.json()` rejects.**
   → *One-line fix: route every call through a `getJSON()` helper that throws a
   readable message and paints it into the panel (F-05).*
3. **Nothing anywhere tells anyone a model server is down.** `/api/health` returns
   `{"ok": true}` without probing 8080/8081/8082, the UI has no model-server
   indicator, and the app-level timeouts are 120–300 s — so a slow generator shows an
   indefinite "asking…" rather than an error.
   → *One-line fix: make `/api/health` probe the three ports and surface it in the
   header chip beside Connection (F-04).*

---

## Failure-mode table, ranked by likelihood in normal use

Severity is **for a live session**, not in the abstract. "Breaks the session" means the
tutor stops being usable; "visible blemish" means it keeps working but something on screen
is wrong.

| # | Trigger | Current behaviour | What the user sees | Likelihood | Field severity |
|---|---|---|---|---|---|
| **F-01** | `sudo sh reset_demo.sh` between sessions | `records.sqlite` moved to `archive/`; `item_review` goes with it | "No questions have been approved for learners yet · 1,050 awaiting review". Start button disabled. The practice and calibration views are dead | **Certain** — the reset instructs it | **Breaks the session** |
| **F-02** | The ~1,050-item bank replaces the 74-item one (TRACKER A1→A2) | Item ids are positional, so the 74 old approvals re-attach to different questions | Unreviewed, AI-written questions served to a Marine while the UI claims a human approved them | **Certain** on the critical path | **Breaks the session** (it is the central integrity claim) |
| **F-04** | Generator down, or merely slow (contended GPU, battery downclock) | No app timeout under 120 s; no health probe anywhere | Drawer Ask box: "asking…" indefinitely, then `failed`. No indication which of the three servers is at fault | **High** | **Breaks the session** |
| **F-05** | Any endpoint returns 500 | `.json()` rejects on a plain-text body; 6 call sites have no `catch`, 6 have an empty `catch` | `#qtext` stuck on "Loading…" forever, or a blank drawer panel with no explanation | **High** (it is the render path for every other failure here) | **Breaks the session** |
| **F-03** | Reviewer presses "Clear unflagged" without `/api/review/precheck` having run | `flags_json` is NULL → treated as "no flags" → **everything** approved | "Cleared 1,050 unflagged questions. 0 still need a human" | **Medium** — it is the obvious recovery from F-01, and precheck is unreachable from the kiosk | **Breaks the session** |
| **F-06** | Generator unreachable during grading | The (correct) fallback verdict `partial` is written to the DB with `score = 0.5` | The Calibration chart — the 4:00 segment — is silently contaminated by grader outages | Medium | Visible blemish, quiet dishonesty |
| **F-13** | Cold boot | `Type=simple` + `After=` means the API binds :8000 while llama.cpp is still loading weights; `kiosk.sh` only waits for the static page | Kiosk lights up looking ready; first question fails | Medium (any reboot) | Visible blemish |
| **F-12** | A persistent failure with `Restart=always` | `StartLimitIntervalSec`/`StartLimitBurst` are in `[Service]`, not `[Unit]` | Possible unbounded restart loop — the exact incident (counter 386) the comment says this prevents | Low, but catastrophic | Breaks the session |
| **F-08** | Two writers to `records.sqlite` (two tabs; or reviewing while a learner answers) | Two separate connections, 5 s default busy timeout, WAL only by accident of construction order; `seq` allocation is a non-atomic read-then-write | `/api/learn/answer` → 500 → Submit does nothing at all. Duplicate `seq` in the signed export | Low–medium (needs two actors) | Visible blemish; integrity issue in the export |
| **F-10** | `tutor-api` restarts while `build_items.py` is mid-write | `write_text()` truncates in place; partial JSON → `LearnStore = None`; **no reload endpoint exists**, only a restart fixes it | The whole learning half returns `available: false` | Low today (different filename), high after the swap | Breaks the session |
| **F-07** | Item missing a key (schema drift) | `item["source_text"]` is read *before* `grade()`'s `try` | 500 → Submit silently does nothing | Low | Visible blemish |
| **F-11** | Anything raises inside `/api/ask/stream` | 200 already sent; no `done` event; connection dropped | Client waiting for `done` hangs forever — **but nothing in the UI calls this endpoint** | Very low today | Low now, high if the UI is wired to it |
| **F-09** | Hostile or accidental huge POST body | No `max_length` on any field; Starlette buffers the whole body; `MemoryMax=800M` | Unit OOM-killed, restarts in 3 s | Very low (loopback-only kiosk) | Low |

---

# 1. Dependency failure — every call to a llama.cpp server

## 1.1 The call sites and their timeouts

| Call site | Server | Timeout | Exception caught? | Result on failure |
|---|---|---|---|---|
| `src/generation/pipeline.py:52` `_post` → `:121` `generate()` | gen :8080 | **300 s** | No | 500 |
| `src/generation/pipeline.py:52` `_post` → `:134` `embed_many()` | embed :8081 | **300 s** | No | 500 |
| `src/retrieval/hybrid.py:53` `embed()` | embed :8081 | **120 s** | No | 500 |
| `src/retrieval/hybrid.py:81` `rerank()` | rerank :8082 | **300 s** | Only `HTTPError` 400/500, and only to shrink the doc budget (`:83-86`) | 500 |
| `src/learn/session.py:89` `_post` → `:329` `grade()` | gen :8080 | **300 s** | **Yes — `except Exception` at `:334`** | Graceful fallback |
| `src/learn/review.py:72` `_embed()` → `:129` | embed :8081 | **180 s** | **Yes — `except Exception` at `:137`** | `PRECHECK_FAILED` flag |
| `src/learn/build_items.py:51` `post()` | gen :8080 | 300 s x 4 attempts | Yes, with backoff | Retries (D-032) |

**So: timeouts exist, but not one of them is a field-survivable budget.** A p95 answer is
11.0 s (`demo/script.md`). The tightest ceiling in the answer path is 120 s — eleven
times the p95 — and the widest is 300 s. If the generator hangs, the browser waits five
minutes.

## 1.2 Behaviour by failure kind

**Server down (connection refused).** `urllib.request.urlopen` raises
`URLError(ConnectionRefusedError)` *immediately* — it does not wait out the timeout. This
is the one piece of good luck in the dependency story: an outright-dead server fails fast.

- `/api/ask` (`src/api/main.py:361-373`) → `pipeline.ask` → `retrieve` →
  `hybrid.embed` → `URLError` propagates out of `run_in_executor` → **500**.
- `/api/learn/answer` (`src/api/main.py:242-259`) → `L.grade` → caught at
  `src/learn/session.py:334` → fallback returned, HTTP **200**. Correct.

**Server hung / slow past the timeout.** `socket.timeout` (an `OSError`) after 120–300 s.
Same disposition as above: 500 everywhere except `grade()`.

**Malformed JSON / unexpected shape.** Every response is indexed without a guard:

- `src/generation/pipeline.py:129` — `data["choices"][0]["message"]["content"]`
- `src/generation/pipeline.py:136` — `data["data"]`
- `src/retrieval/hybrid.py:54` — `json.loads(r.read())["data"][0]["embedding"]`
- `src/retrieval/hybrid.py:87` — `data["results"]`, and each `d["index"]`,
  `d["relevance_score"]`

A `KeyError` / `IndexError` here becomes a 500. Note `hybrid.rerank`'s `except` at
`:83` catches **only** `HTTPError` — a `URLError`, a timeout, or a malformed body escapes
the retry loop entirely, so the budget-halving retry that the comment at `:57-66`
describes does not protect against the two most likely runtime failures.

## 1.3 The SSE stream at `/api/ask/stream`

`src/api/main.py:376-431`. Verified against starlette 0.49.3:
`StreamingResponse.stream_response` sends `http.response.start` with status 200 **before**
iterating the generator. Therefore:

- The first `yield _sse("stage", ...)` at `:388` commits a 200 to the wire.
- If `STATE["pipeline"].retrieve` raises at `:389`, or `pipeline.ask` raises at `:416`,
  or `json.dumps` fails inside `_sse` at `:434`, the exception propagates after the
  response has started. Starlette's error middleware **cannot** convert it to a 500.
  Uvicorn logs it and drops the connection without the terminating zero-length chunk.
- A client waiting for `event: done` waits **forever**. There is no heartbeat and no
  `error` event in the protocol.

**Mitigating fact, stated plainly: nothing in `ui/index.html` calls this endpoint.**
There is no `EventSource`, no `ReadableStream`/`getReader`, and no reference to
`ask/stream` anywhere in the UI. The "While it streams" line in `demo/script.md` is
aspirational — the drawer's Ask box posts to non-streaming `/api/ask`
(`ui/index.html:907`). So the severity today is low; it becomes the worst failure in the
codebase the moment anyone wires the UI to it, which the script implies is the intent.

## 1.4 Is `grade()`'s fallback correct, and is it applied elsewhere?

**The fallback is right in shape and wrong in two details.**

`src/learn/session.py:326-335`:

```python
fallback = {"verdict": "partial", "covered": [],
            "missed": item["key_points"],
            "feedback": "Could not grade automatically; compare with the source."}
try:
    d = _post({...})
    raw = d["choices"][0]["message"]["content"].strip()
except Exception:                                          # noqa: BLE001
    return fallback
```

Right: the `try` covers both the HTTP call **and** the response indexing at `:333`, so a
malformed body degrades the same way a dead server does. The learner still gets the source
paragraph and the citation back from `src/api/main.py:257-258`, so the screen is useful
rather than empty. This is exactly the discipline the rest of the codebase lacks.

Wrong, detail 1 — **the fallback is recorded as a real score (F-06).**
`src/api/main.py:253` calls `L.record(...)` unconditionally.
`src/learn/session.py:358` writes `VERDICT_SCORE[grade["verdict"]]`, and
`VERDICT_SCORE["partial"] == 0.5` (`:80`). That 0.5 then flows into
`AVG(score)` at `src/learn/session.py:373` — the Calibration chart, the calibration
view. A grader outage silently manufactures calibration data. It also reschedules the
item: `_due_after` (`:154-169`) treats `partial` as a 1-day interval.

Wrong, detail 2 — **the fallback does not cover the item access (F-07).**
`src/learn/session.py:321-324` reads `item["source_text"]` and `item["key_points"]`
*before* the `try` at `:328`, and `src/api/main.py:257-258` reads
`item["source_text"]`, `item["citation"]`, `item["key_points"]` *after* the row has
already been written. Either raises `KeyError` on schema drift → 500 → and per section 2
the UI shows nothing at all.

**The discipline is applied nowhere else in the answer path.** `pipeline.generate`,
`pipeline.embed_many`, `hybrid.embed`, and `hybrid.rerank` all let the exception out.

### Fix D-1 — a field-survivable timeout budget

`src/generation/pipeline.py:52`:
```python
# 300s is a batch-job budget, not a field one: p95 is 11.0s, so anything past ~45s is
# already a failed session and the user should be told so rather than watching a spinner.
def _post(url, payload, timeout=45):
```
`src/retrieval/hybrid.py:53` to `timeout=15` (embedding is a single short string).
`src/retrieval/hybrid.py:81` to `timeout=30`.
`src/learn/session.py:89` to `timeout=45`.
Leave `src/learn/build_items.py:51` at 300 — that one *is* a batch job.

### Fix D-2 — one narrow error type instead of a bare 500

Add to `src/retrieval/hybrid.py`, just below `DEFAULTS` (`:30`):

```python
class ModelUnavailable(RuntimeError):
    """A llama.cpp server is down, hung, or answered with something unusable."""
    def __init__(self, which, cause):
        self.which, self.cause = which, cause
        super().__init__(f"{which} server did not answer: "
                         f"{type(cause).__name__}: {str(cause)[:120]}")
```

Wrap each call site. `src/generation/pipeline.py:120-129`:

```python
    def generate(self, question, chunks):
        try:
            data = _post(self.gen_url + "/chat/completions", {...})   # unchanged payload
            return data["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, OSError, ValueError,
                KeyError, IndexError, TypeError) as e:
            raise ModelUnavailable("generator", e) from e
```

Same pattern for `embed_many` (`:133-137`, `which="embeddings"`), `hybrid.embed`
(`:49-54`, `which="embeddings"`) and `hybrid.rerank` (`:70-90`, `which="reranker"`).
`pipeline` already imports `hybrid` at `:32`, so add
`from hybrid import ModelUnavailable  # noqa: F401` there to re-export it.

Then add one exception handler in `src/api/main.py`, after `app = FastAPI(...)` at `:36`:

```python
from fastapi import Request                                    # noqa: E402
from fastapi.responses import JSONResponse                     # noqa: E402
from hybrid import ModelUnavailable                            # noqa: E402

@app.exception_handler(ModelUnavailable)
def _model_unavailable(request: Request, exc: ModelUnavailable):
    # 503, not 500: this is a dependency being absent, and the UI must be able to say
    # WHICH one. "The model is not responding" is a recoverable-looking message;
    # a stack trace is not.
    return JSONResponse(status_code=503, content={
        "ok": False, "error": "model_unavailable", "which": exc.which,
        "detail": str(exc),
        "message": f"The {exc.which} model is not responding on this device."})
```

### Fix D-3 — the SSE stream must always terminate

`src/api/main.py:384-427`, wrap the whole body of `gen()`:

```python
    async def gen():
        loop = asyncio.get_running_loop()
        t0 = time.time()
        try:
            ...                                  # everything currently in gen(), unchanged
        except Exception as e:                                     # noqa: BLE001
            # The 200 is already on the wire (StreamingResponse sends http.response.start
            # before the first yield), so this can never become a 500. The only way to
            # tell the client anything is an event -- and the only way to stop it waiting
            # forever is to always send 'done'.
            yield _sse("error", {"message": str(e)[:200],
                                 "which": getattr(e, "which", "pipeline")})
            yield _sse("done", {"latency_s": round(time.time() - t0, 2),
                                "failed": True})
```

---

# 2. Unhandled exceptions becoming 500s — and what the UI renders

## 2.1 The rendering problem, first

**A FastAPI 500 body is `Internal Server Error` as `text/plain`** (verified above against
starlette 0.49.3). `fetch()` does *not* reject on a 500 — it resolves — so
`(await fetch(url)).json()` reaches `.json()`, which then **throws a SyntaxError**.
Every failure therefore surfaces as a rejected promise, never as a status code the code
can read. No `fetch()` in `ui/index.html` checks `response.ok`.

**Unguarded — the promise rejects and the function simply stops mid-render:**

| UI function | Line | What is left on screen |
|---|---|---|
| `loadTrack()` | `ui/index.html:495` | Home screen never populates: `#track` empty, `#railcap` stale, `#homehint` blank. Runs at page load and after every review decision |
| `nextQuestion()` | `ui/index.html:626` | **`#qtext` stays on "Loading…" forever** (set at `:622`). This is the worst one: the answer box is visible, the question never arrives |
| `loadHistory()` | `ui/index.html:723` | "Recent answers" view opens empty |
| `loadCalibration()` | `ui/index.html:737` | `show("cal")` at `:738` runs only *after* the fetch, so the view never even switches — clicking Calibration appears to do nothing |
| `openReview()` | `ui/index.html:783` | `#rvq` stays on "Loading…" |
| `decide()` | `ui/index.html:830` | **The response is never read.** `rv.i++` at `:833` advances regardless, so a failed approval is indistinguishable from a successful one |

**Guarded but silent — `catch (e) {}` with an empty body:**
`pollNet` (`:490`, at least sets `data-net="UNKNOWN"`), `loadReviewStats` (`:776`),
`pollScore` (`:866`), `pollTelem` (`:878`), `loadCorpus` (`:887`), `loadGaps` (`:901`).
Each leaves its drawer block exactly as it was — usually empty. The See-how-it-works
panel ("If a user pushes on the numbers, open **See how it works** and show them live")
runs straight into `pollScore`.

**Guarded but says nothing useful:**
`submitAnswer` (`ui/index.html:673`) — `catch (e) { state.busy = false; updateSubmit(); return; }`.
The learner presses Submit, "Grading against the source…" appears, then the hint clears
and the button re-enables with **no message at all**.
`adminAsk` (`ui/index.html:913`) — the single word `failed`.

## 2.2 Endpoint walk — what can raise

| Endpoint | `main.py` | Can raise | Cause |
|---|---|---|---|
| `GET /` | `:70-72` | Yes | `FileResponse` on a missing `--ui` path |
| `GET /api/health` | `:75-77` | No | — but it is a **lie by omission**: `{"ok": true}` regardless of the three model servers |
| `GET /api/network` | `:80-82` | No | `_ip_json` and `can_reach_internet` both catch (`network_status.py:39, 87`) |
| `GET /api/telemetry` | `:85-88` | No | `latest()` copies under a lock; `power_mode()` catches (`telemetry.py:107`) |
| `GET /api/scoreboard` | `:91-109` | **Yes** | `json.loads` at `:98` on a truncated eval file |
| `GET /api/corpus` | `:112-119` | **Yes** | `pipe.db.execute` at `:115-117` — `sqlite3.connect` *creates* a missing DB, so a wrong `--db` yields `no such table: chunks`, not a startup failure |
| `GET /api/records` | `:122-126` | No | guarded at `:124` |
| `POST /api/records/export` | `:129-133` | **Yes** | `STATE["records"]` may be `None` (set only on success at `:485`) → `AttributeError`; also writes to a hardcoded `/opt/tutor/xapi_bundle.json` |
| `GET /api/gaps` | `:136-170` | **Yes** | `json.loads` at `:149`; `r["kind"]` at `:153` and `r["question"]` at `:160,167,168` are hard subscripts on file content |
| `POST /api/gaps/run` | `:173-187` | **Yes** | `subprocess.run(..., timeout=1800)` — a 30-minute blocking call on a threadpool thread. Not reachable from the UI |
| `GET /api/learn/track` | `:195-206` | **Yes** | `L.progress()` → `session.py:238` `sec["chapter"]`, `sec["chapter_title"]`, `:262` `sec["section"]`, `:267` `sec["item_ids"]` |
| `GET /api/learn/next` | `:209-239` | **Yes** | `item["chapter"]`, `item["chapter_title"]`, `item["section"]`, `item["key_points"]` at `:236-239`; `next_item` → `sec["section"]` at `session.py:250` |
| `POST /api/learn/answer` | `:242-259` | **Yes** | `L.grade` pre-`try` item access (`session.py:321-324`); `L.record` DB write (`:253`, **not wrapped**); `item[...]` at `:257-258` |
| `GET /api/learn/history` | `:262-265` | **Yes** | `session.py:198` `LIMIT ?` accepts a negative `limit` (SQLite: `LIMIT -1` = unlimited) |
| `GET /api/learn/calibration` | `:268-273` | **Yes** | DB read; `round(r["acc"], 3)` at `session.py:383` |
| `GET /api/review/stats` | `:280-286` | **Yes** | DB read |
| `GET /api/review/queue` | `:289-308` | **Yes** | `e["item"]["key_points"]`, `["source_text"]` at `:299-302` |
| `POST /api/review/decide` | `:311-322` | **Yes** | `ValueError` is caught at `:320`; `sqlite3.OperationalError` is **not** |
| `POST /api/review/approve_unflagged` | `:325-332` | **Yes** | ~1,050 sequential commits, `review.py:196-207` |
| `POST /api/review/precheck` | `:335-353` | **Yes** | `STATE["pipeline"].embed_url` at `:346` — `AttributeError` if the pipeline failed to build. Not reachable from the UI (see F-03) |
| `GET /api/history` | `:356-358` | No | |
| `POST /api/ask` | `:361-373` | **Yes** | Every dependency failure in section 1. `_log` at `:372` is correctly wrapped (`:41-47`) |
| `POST /api/ask/stream` | `:376-431` | **Yes**, but cannot become a 500 | see 1.3 |

### Fix E-1 — one JSON helper, one visible error strip

Add near the top of the `<script>` block in `ui/index.html`, after `esc` (`:441`):

```js
// A 500 from FastAPI is text/plain "Internal Server Error", so .json() throws and every
// caller's promise rejects silently. Route everything through here so a failure has a
// message and a place to land.
async function getJSON(url, opts) {
  let r;
  try { r = await fetch(url, opts); }
  catch (e) { throw new Error("The tutor service is not responding."); }
  if (!r.ok) {
    let msg = "Something went wrong on the device (HTTP " + r.status + ").";
    try {
      const j = await r.json();
      if (j && j.message) msg = j.message;
      else if (j && j.detail) msg = String(j.detail);
    } catch (e) { /* plain-text 500: keep the generic message */ }
    throw new Error(msg);
  }
  return r.json();
}

function fail(sel, e) {
  // Never leave a panel blank. A user reading "the generator is not responding" is
  // watching a machine that knows what is wrong with it; a blank box is not.
  $(sel).innerHTML = '<p class="muted">' + esc(e.message) + "</p>";
}
```

Then, mechanically, for each site in the table at 2.1:

```js
// ui/index.html:493-495
async function loadTrack() {
  const q = state.pub ? "?pub=" + encodeURIComponent(state.pub) : "";
  let d;
  try { d = await getJSON("/api/learn/track" + q); }
  catch (e) { fail("#track", e); $("#startbtn").disabled = true; return; }
  ...
```

```js
// ui/index.html:625-626
  let d;
  try { d = await getJSON("/api/learn/next" + q); }
  catch (e) { $("#qtext").textContent = e.message;
              $("#answerblock").style.display = "none"; return; }
```

```js
// ui/index.html:668-673
  let d;
  try {
    d = await getJSON("/api/learn/answer", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)});
  } catch (e) {
    state.busy = false;
    $("#submithint").textContent = e.message;   // was: silently nothing
    updateSubmit(); return;
  }
```

```js
// ui/index.html:830-833  -- a failed decision must not look like a successful one
  let ok = false;
  try { await getJSON("/api/review/decide", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body)}); ok = true; }
  catch (e) { $("#rvbulkhint").textContent = e.message; }
  if (!ok) return;                              // stay on this item
  rv.i++; renderReview(); loadReviewStats(); loadTrack();
```

And replace each `catch (e) {}` in the drawer with `catch (e) { fail("#admin-score", e); }`
(`:866`), `fail("#admin-telem", e)` (`:878`), `fail("#admin-corpus", e)` (`:887`),
`fail("#admin-gaps", e)` (`:901`), `fail("#reviewhint", e)` (`:776`).

### Fix E-2 — `adminAsk` should say which model is down

`ui/index.html:903-913`:
```js
async function adminAsk() {
  const q = $("#adminq").value.trim(); if (!q) return;
  $("#adminanswer").textContent = "asking…";
  let d;
  try {
    d = await getJSON("/api/ask", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question: q})});
  } catch (e) { $("#adminanswer").textContent = e.message; return; }   // was: "failed"
  ...
```

### Fix E-3 — bound the wait in the browser as well as the server

Even at 45 s (Fix D-1) a stuck request is a dead session. In `getJSON`:

```js
async function getJSON(url, opts, ms) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), ms || 20000);
  let r;
  try { r = await fetch(url, Object.assign({signal: ac.signal}, opts || {})); }
  catch (e) { throw new Error(e.name === "AbortError"
        ? "The device did not answer in 20 seconds. It may still be loading a model."
        : "The tutor service is not responding."); }
  finally { clearTimeout(t); }
  ...
```
Call the two slow paths with an explicit longer budget: `/api/ask` and
`/api/learn/answer` at `45000`.

---

# 3. SQLite concurrency

## 3.1 What is actually there

There are **two independent connections to the same `records.sqlite` file**, both with
`check_same_thread=False`, both used from FastAPI's threadpool:

| Connection | Opened at | Shared with | Writes |
|---|---|---|---|
| `RecordStore.db` | `src/records/store.py:74` (from `main.py:485`) | — | `record()` `:118-134` — from `/api/ask` and the SSE path via `_log` |
| `LearnStore.db` | `src/learn/session.py:108` (from `main.py:489`) | `ReviewStore` (`session.py:113`) | `record()` `:352-360`; `ensure_rows` `review.py:148-153`; `set_flags` `:155-158`; `decide` `:169-181` |

The read-only corpus connection is separate and genuinely safe:
`src/retrieval/hybrid.py:33-42`, with the reasoning stated in the comment at `:34-36`.

**Is there a busy timeout?** Yes — **5000 ms**, but by CPython default, not by intent.
Neither `sqlite3.connect` call passes `timeout=`, and the default is `5.0`. Verified
locally: a fresh connection reports `PRAGMA busy_timeout = 5000`. Nobody chose that
number and nothing in the repo records it.

**Is there WAL?** Yes — but **only by accident of construction order.**
`PRAGMA journal_mode=WAL` appears exactly once, at `src/records/store.py:32`, inside
`RecordStore`'s `SCHEMA`. Journal mode is a persistent property of the database file, so
`LearnStore` inherits it — *provided* `RecordStore` was constructed first and succeeded.
`src/api/main.py:484-487` constructs it first and **swallows any failure**. If
`RecordStore` ever fails to build (bad path, unwritable `record_hmac.key` at
`store.py:96`), the file stays in `delete` journal mode, where a reader blocks a writer
and a writer blocks a reader — and nothing in the logs connects the two facts.

**Is the connection shared across threads safely?** At the C level, yes: CPython links
SQLite in serialized mode (`sqlite3.threadsafety == 3` locally), so there is no crash. The
hazards are logical, not memory-safety:

## 3.2 Failure 1 — non-atomic sequence allocation (silent, and it corrupts the export)

`src/records/store.py:106-108`:
```python
    def _next_seq(self):
        r = self.db.execute("SELECT COALESCE(MAX(seq),0) s FROM interactions").fetchone()
        return (r["s"] or 0) + 1
```
This bare `SELECT` runs in autocommit, *before* the `INSERT` at `:118` opens the implicit
transaction. Two threadpool threads both read `MAX(seq) = N` and both insert `N+1`. There
is **no `UNIQUE` constraint on `interactions.seq`** (`store.py:36`), so the duplicate lands
silently. `src/learn/session.py:349-351` has the identical pattern for
`learn_attempts.seq`, likewise without a constraint (`session.py:62`).

This matters more than it looks. `store.py:197-200` tells a consumer of the signed bundle:
*"Ordering is authoritative via 'sequence'."* With duplicates that statement is false, and
the whole dead-RTC design (D-010) rests on it.

## 3.3 Failure 2 — interleaved implicit transactions on one connection

`isolation_level == ''` (verified), so the first `INSERT`/`UPDATE` on a connection opens a
deferred transaction that is held until `commit()`. Because `LearnStore.db` is shared by
`LearnStore.record` and all three `ReviewStore` writers, two threadpool threads on that
connection join the *same* transaction: whichever commits first commits the other's
partial work. There is no `rollback()` anywhere in this code, so today the consequence is
confined to 3.2 — but it means the transaction boundaries the code appears to have do not
exist.

## 3.4 Failure 3 — the real `database is locked`

WAL permits one writer at a time. A writer-vs-writer conflict *across the two
connections* raises `sqlite3.OperationalError: database is locked` after 5 s.

The realistic trigger is `bulk_decide_unflagged` (`src/learn/review.py:196-207`): it loops
over every pending row and calls `self.decide()`, which **commits once per item**
(`review.py:181`). At ~1,050 pending items that is ~1,050 fsync'd write transactions in a
burst on eMMC, holding and releasing the write lock throughout. If a user asks a question
in a second tab during that burst:

- `/api/ask` → `_log` → **swallowed** at `src/api/main.py:41-47`. The answer still appears.
  The interaction is silently lost from the record — a deliberate and correct trade
  ("Never let record-keeping break answering"), but it means "every interaction is
  recorded" quietly stops being true and nobody is told.
- `/api/learn/answer` → `L.record` at `src/api/main.py:253` is **not wrapped** →
  `OperationalError` → 500 → and per 2.1 `submitAnswer` shows the learner nothing at all.

### Fix S-1 — say what the durability settings are, out loud

`src/records/store.py:74` and `src/learn/session.py:108`, identically:

```python
        # timeout=30: the CPython default is 5s, which nobody chose. Two connections
        # write this file (RecordStore and LearnStore/ReviewStore) and a bulk approval
        # is ~1,050 commits, so 5s is reachable under ordinary use.
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
        self.db.row_factory = sqlite3.Row
        # WAL is set in RecordStore's SCHEMA and is a persistent file property, but that
        # makes it depend on construction order. Assert it on every connection instead.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA synchronous=NORMAL")   # safe under WAL; far fewer fsyncs
```

### Fix S-2 — allocate `seq` in one statement

`src/records/store.py:118-127` — delete `_next_seq` and fold it into the INSERT:

```python
        cur = self.db.execute(
            "INSERT INTO interactions (seq, wall_utc, clock_trusted, actor, question,"
            " answered, abstain_reason, top_score, latency_s, answer_text,"
            " citations_json, sources_json) "
            "SELECT COALESCE(MAX(seq),0)+1,?,?,?,?,?,?,?,?,?,?,? FROM interactions",
            (wall, int(trusted), actor or self.actor, question, answered, ...))
        interaction_id = cur.lastrowid
        seq = self.db.execute("SELECT seq FROM interactions WHERE id=?",
                              (interaction_id,)).fetchone()["seq"]
```
Apply the same shape to `src/learn/session.py:349-359`. Add belt and braces:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_inter_seq ON interactions(seq);
```
in `store.py:31-61`'s `SCHEMA`, and the equivalent on `learn_attempts(seq)` in
`session.py:59-78`. A duplicate should be a loud error, not a quiet one.

### Fix S-3 — one transaction for the bulk approval

`src/learn/review.py:196-207` — pass a flag so `decide` does not commit per row:

```python
    def bulk_decide_unflagged(self, item_ids, status="approved",
                              reviewer="bulk:unflagged"):
        rows = self.db.execute(
            "SELECT item_id, flags_json FROM item_review WHERE status='pending'").fetchall()
        touched = []
        for r in rows:
            if r["item_id"] not in item_ids:
                continue
            if json.loads(r["flags_json"] or "[]"):
                continue
            self.decide(r["item_id"], status, reviewer=reviewer,
                        note="cleared in bulk: no automatic flags", commit=False)
            touched.append(r["item_id"])
        self.db.commit()          # one transaction, one fsync, one lock acquisition
        return touched
```
and `review.py:165-181`: add `commit=True` to the signature, and guard `:181` with
`if commit: self.db.commit()`.

### Fix S-4 — do not let a lost write become a blank screen

`src/api/main.py:242-259`:
```python
    try:
        L.record(body.learner, item, body.answer, conf, grade, el)
        recorded = True
    except Exception:                                          # noqa: BLE001
        # Same rule as _log: never let record-keeping destroy the thing the learner
        # is actually looking at. But say so, rather than losing it silently.
        recorded = False
    return {"ok": True, "recorded": recorded, ...}
```
and in `ui/index.html:676`, after the verdict row, `if (d.recorded === false)` append
"(not saved — the records database was busy)" to `#submithint`.

---

# 4. Resource limits — assessed, and mostly a non-issue

`MemoryMax=800M` on `tutor-api` (`scripts/install_services.sh:88`).

**The item bank is not the problem. Arithmetic, so nobody has to guess:**
`build_items.py` takes the two *largest* chunks per section (`:167-171`,
`ORDER BY n_chars DESC LIMIT 2`) with `min_chars=320` (`:137`). Median chunk size across
the 13 publications is 527 chars (`corpus/chunk_report.json`), so a largest-in-section
chunk is roughly 1,050. Per item: ~1,050 (source) + ~130 (question) + 3 x ~90 (key points)
+ ~180 (citation, ids, titles) = **~1,630 chars**.

| Bank | Text | Resident (incl. Python object overhead) |
|---|---|---|
| 74 items (today) | 118 KB | ~0.2 MB |
| 1,050 items (after A1) | 1.7 MB | **~2.8 MB** |

`LearnStore.reload` (`src/learn/session.py:118`) does `read_text()` then `json.loads`,
peaking at roughly twice the file — **~6 MB**. Against 800 MB that is noise. FastAPI +
uvicorn + pydantic + `sqlite_vec` + pyyaml is the dominant term at ~100–140 MB RSS.

**`/api/review/queue?limit=400` is not a memory risk either.** `ui/index.html:783` requests
400; at ~1,630 chars/item that is ~640 KB of text, ~2–3 MB transient through
`jsonable_encoder` + `json.dumps` (`src/api/main.py:297-308`). It *is* a latency risk — one
large synchronous serialisation on a Jetson — and the reviewer only ever looks at
`rv.queue[rv.i]` one at a time (`ui/index.html:780`), so the other 399 are pure waste.

**`limit` is unvalidated** at `src/api/main.py:263` and `:290`. Neither is truly unbounded
(both are capped by row/item count), but `GET /api/learn/history?limit=-1` becomes SQL
`LIMIT -1`, which SQLite reads as *no limit* — every attempt ever recorded is returned.
Benign today; wrong on principle.

**`STATE["history"]` grows without bound.** Declared `main.py:37`, appended at `:368` and
`:418`, and only the last 20 are ever served (`:358`). ~200 B per question, so ~20 KB per
100 questions. Not a runtime risk; it *is* the leak TRACKER B5 (8-hour soak) will find.

**The one genuine unbounded input: request bodies.** `Ask.question` (`main.py:50-51`) and
`Answer.answer` (`main.py:63-67`) carry no `max_length`, and Starlette buffers the entire
body into memory before pydantic sees it. Nothing in uvicorn caps it. A few-hundred-MB
POST OOM-kills the unit inside its 800 M cgroup. The API binds loopback only
(`main.py:444`) behind a kiosk with no address bar (D-030), so the attacker would have to
be the local operator — but the fix is two lines.

**A 100 KB answer specifically is already handled**: truncated to 1,200 chars for the
grader prompt (`session.py:324`) and 2,000 chars for storage (`session.py:358`).

**Model-server caps are the actually-questionable number.** Three units at
`MemoryMax=3500M` (`install_services.sh:43`) on a 7.4 GiB board totals 10.5 GB of caps.
Caps are not reservations, so this is not over-commit — but it does mean no per-unit cgroup
limit can ever fire before the *kernel's global* OOM killer does, and the global killer
picks by badness score, not by unit. On battery with the kiosk running, the process it
chooses may well be `tutor-api` or WebKit. The comment at `:41-42` says "bound the blast
radius"; at 3500 M each it does not.

### Fix R-1 — cap the inputs
```python
# src/api/main.py:25 and :50
from pydantic import BaseModel, Field                          # noqa: E402

class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

class Answer(BaseModel):
    item_id: str = Field(max_length=128)
    answer: str = Field(max_length=8000)     # the grader sees 1200; storage keeps 2000
    confidence: int = Field(default=3, ge=1, le=4)
    learner: str = Field(default="default", max_length=64)
```
`ge/le` on `confidence` makes the clamp at `main.py:248` redundant but harmless — keep it.
Note this turns an out-of-range confidence from *silently clamped* into a 422; that is the
right trade only once Fix E-1 renders 422s visibly.

### Fix R-2 — bound the query parameters
```python
# src/api/main.py:23 and :262
from fastapi import Query                                      # noqa: E402

@app.get("/api/learn/history")
def learn_history(learner: str = "default", limit: int = Query(20, ge=1, le=200)):

# src/api/main.py:289
@app.get("/api/review/queue")
def review_queue(limit: int = Query(60, ge=1, le=200)):
```
and change `ui/index.html:783` from `?limit=400` to `?limit=60` — the reviewer sees one
item at a time and `d.pending` already carries the true count (`main.py:307`).

### Fix R-3 — bound the in-memory history
```python
# src/api/main.py:368 and :418, after the bare append
        STATE["history"].append({...})
        del STATE["history"][:-200]      # only the last 20 are ever served (:358)
```

---

# 5. Startup and reload

## 5.1 `reload()` is start-only — which is both the mitigation and the trap

Verified by grep across `src/`, `scripts/`, `eval/`: `reload()` is called from exactly one
place, `src/learn/session.py:114`, inside `LearnStore.__init__`. **There is no reload
endpoint.** So a mid-write items file cannot corrupt a running process — but it *can*
poison a restart, and once poisoned, only another restart recovers.

## 5.2 The items file is written non-atomically, every 5 sections

`src/learn/build_items.py:202-207`:
```python
        if n % 5 == 0 or n == len(secs):
            out_path.write_text(json.dumps({...}, indent=2))
```
`Path.write_text` opens with `"w"` — **truncate in place**. For a ~1,050-item bank that is
a ~2.5 MB rewrite. Any reader during that window sees a truncated file. D-032 credits this
flush-every-5-sections decision for making `--resume` possible, and that is right; the
decision is good, the write is not durable.

**The failure chain:** `tutor-api` restarts (it carries `Restart=always`,
`install_services.sh:79`) during a write → `json.loads` at `src/learn/session.py:118`
raises `JSONDecodeError` → caught at `src/api/main.py:491` → **`STATE["learn"] = None`** →
every learning endpoint degrades:

- `/api/learn/track` → `{"available": false, "reason": "no_items_generated"}` (`main.py:198`)
- `/api/learn/next` → same (`main.py:213`)
- `/api/learn/answer` → `{"ok": false, "error": "unknown item"}` (`main.py:246`)
- `/api/review/queue` → `{"queue": []}` (`main.py:293`)

The UI renders that honestly ("Practice items have not been generated yet.",
`ui/index.html:498-502`) — but the tutor stays dead until someone notices and restarts,
and the *only* diagnostic is a line on stderr at `main.py:493`.

**Today's exposure is narrow, and worth stating precisely:** TRACKER A1 says the running
job writes `learn_items_all.json`, while the unit passes
`--items /opt/tutor/learn_items.json` (`install_services.sh:74`). Different files. The
hazard goes live the moment that file is swapped in — which is the next step on the
critical path.

## 5.3 Missing file: handled. Schema drift: not handled.

Missing is correct — `src/learn/session.py:117-121` falls back to empty `track`/`items`
and the API reports it distinctly. Credit that.

Schema drift is a 500 waiting to happen. Hard subscripts on file-derived data:

| Location | Key |
|---|---|
| `src/learn/session.py:238-242` | `sec["chapter"]`, `sec["chapter_title"]`, `sec["section"]` |
| `src/learn/session.py:262` | `sec["section"]` |
| `src/learn/session.py:267` | `sec["item_ids"]` |
| `src/learn/session.py:321-323` | `item["source_text"]`, `item["key_points"]` (before the `try`) |
| `src/learn/session.py:357-358` | `item["id"]`, `item["chapter"]`, `item["section"]` |
| `src/api/main.py:236-239` | `item["chapter"]`, `item["chapter_title"]`, `item["section"]`, `item["key_points"]` |
| `src/api/main.py:257-258` | `item["source_text"]`, `item["citation"]`, `item["key_points"]` |
| `src/api/main.py:299-302` | `e["item"]["key_points"]`, `["chapter_title"]`, `["citation"]`, `["source_text"]` |

Contrast `sec.get("pub_id")` at `session.py:221` and `item.get("pub_id")` at `main.py:235`,
which were correctly made tolerant when `pub_id` was added — the same care was not applied
to the rest.

### Fix ST-1 — atomic write (the important one)

`src/learn/build_items.py`, add near the imports:
```python
import os
import tempfile

def _atomic_write(path, text):
    """Write via a temp file in the same directory, then rename.

    write_text() truncates in place. On a ~2.5MB items file rewritten every five
    sections, any reader that lands in that window gets a JSONDecodeError -- and the API
    responds by dropping the entire learning store for the life of the process.
    os.replace is atomic on the same filesystem, so a reader sees either the old file or
    the new one, never half of either.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".items-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
```
and at `:203`:
```python
            _atomic_write(out_path, json.dumps({...}, indent=2))
```

### Fix ST-2 — a validating loader that keeps the good items

`src/learn/session.py:116-127`:
```python
    ITEM_KEYS = ("id", "question", "key_points", "source_text", "citation",
                 "chapter", "chapter_title", "section")
    TRACK_KEYS = ("chapter", "chapter_title", "section", "item_ids")

    def reload(self):
        """Re-read the items file. Never raise, and never half-load.

        A malformed or half-written file used to take the whole learning store down for
        the life of the process (main.py sets STATE['learn'] = None and there is no
        reload endpoint). Dropping the bad rows and reporting how many is strictly
        better: the tutor keeps working and the problem is visible.
        """
        self.load_error, dropped = None, 0
        track, items = [], {}
        try:
            if self.items_path.exists():
                d = json.loads(self.items_path.read_text(encoding="utf-8"))
                for i in d.get("items", []):
                    if all(k in i for k in self.ITEM_KEYS) and i.get("key_points"):
                        items[i["id"]] = i
                    else:
                        dropped += 1
                for t in d.get("track", []):
                    if all(k in t for k in self.TRACK_KEYS):
                        t = dict(t, item_ids=[x for x in t["item_ids"] if x in items])
                        if t["item_ids"]:
                            track.append(t)
                    else:
                        dropped += 1
        except (OSError, ValueError) as e:
            self.load_error = f"{type(e).__name__}: {str(e)[:160]}"
            return len(getattr(self, "items", {}))        # keep what we already had
        self.track, self.items = track, items
        self.dropped = dropped
        self.review.ensure_rows(self.items)
        return len(self.items)
```
Note the early `return` on a parse error deliberately **keeps the previously loaded bank**
rather than replacing it with nothing. Surface `load_error` and `dropped` in
`/api/learn/track` (`main.py:202`) so a bad file is visible on the dashboard.

### Fix ST-3 — a reload endpoint, so a bad start is recoverable without a restart

`src/api/main.py`, after `:353`:
```python
@app.post("/api/learn/reload")
def learn_reload():
    """Re-read the items file in place.

    Exists because a half-written items file at startup used to leave the tutor dead
    until someone with ssh noticed -- and on a fielded device there is nobody with ssh.
    """
    if not STATE["learn"]:
        try:
            STATE["learn"] = LearnStore(STATE["records_path"], STATE["items_path"])
        except Exception as e:                                 # noqa: BLE001
            return {"ok": False, "error": str(e)[:200]}
    n = STATE["learn"].reload()
    return {"ok": True, "n_items": n,
            "dropped": getattr(STATE["learn"], "dropped", 0),
            "load_error": getattr(STATE["learn"], "load_error", None)}
```
This needs `STATE["items_path"] = args.items` added at `src/api/main.py:465`, which is
currently the only CLI argument not stored in `STATE`.

## 5.4 F-02 — positional item ids let a rebuilt bank inherit stale approvals

This is the most serious finding in the audit, and it is not a crash.

`src/learn/build_items.py:179-181`:
```python
            slug = re.sub(r"[^A-Za-z0-9]", "", s["pub_id"])
            item_id = f"{slug}-c{s['chapter'] or 0}-{len(items):04d}"
```
The id is a **position** in the output list. It is stable within a `--resume` run and
across nothing else.

Approvals are keyed by that id and live in the other database
(`src/learn/review.py:31-40`), which `ensure_rows` only ever adds to
(`review.py:148-153`, `INSERT OR IGNORE`) and `servable()` reads as
`approved_ids() & set(self.items)` (`session.py:131`).

`review.py`'s own docstring (`:5-8`) states the intent: *"rebuilding the item bank must
never silently un-approve everything a human already checked."* That half is correct. The
inverse was not considered: after the 74-item bank (D-027: "74 approved, 0 rejected, 8
questions rewritten") is replaced by the ~1,050-item bank, `MCDP1-c1-0000` still carries
an `approved` row — but it is now a **different question**, generated by a different
prompt (TRACKER A8) against re-chunked sections (D-028). Worse, `apply_edits`
(`review.py:239-252`) will graft a reviewer's rewrite of the *old* question onto the *new*
item's stem.

The result: unreviewed, AI-written questions reach a Marine while the UI and the
documentation both state that a human approved them. Every approval made before the rebuild
becomes a lie, and there is no signal anywhere that it happened.

### Fix ST-4 — content-address the item id

`src/learn/build_items.py`, add `import hashlib`, then replace `:179-181`:
```python
            slug = re.sub(r"[^A-Za-z0-9]", "", s["pub_id"])
            # Content-addressed, NOT positional. An approval is a human's signature on a
            # specific question; a positional id silently transfers that signature to
            # whatever question lands in that slot on the next rebuild. Hash the source
            # chunk and the question text so a regenerated item gets a NEW id and lands
            # back in the review queue where it belongs.
            fp = hashlib.sha256(
                (str(c["id"]) + "|" + it["question"]).encode("utf-8")).hexdigest()[:10]
            item_id = f"{slug}-c{s['chapter'] or 0}-{fp}"
```

Belt and braces, in case anything else ever mints an id — `src/learn/review.py:31-40`, add
a fingerprint column and check it:
```sql
CREATE TABLE IF NOT EXISTS item_review (
    ...
    fingerprint    TEXT
);
```
```python
    def ensure_rows(self, items):
        """Insert a pending row per item, and DEMOTE any approval whose item changed."""
        for i, it in items.items():
            fp = hashlib.sha256(
                (it.get("question", "") + "|" +
                 it.get("source_text", "")).encode("utf-8")).hexdigest()[:16]
            self.db.execute(
                "INSERT OR IGNORE INTO item_review (item_id, status, fingerprint) "
                "VALUES (?, 'pending', ?)", (i, fp))
            self.db.execute(
                "UPDATE item_review SET status='pending', note='item text changed since "
                "review; approval withdrawn', edited_question=NULL, edited_points=NULL, "
                "flags_json=NULL, fingerprint=? "
                "WHERE item_id=? AND fingerprint IS NOT NULL AND fingerprint<>?",
                (fp, i, fp))
        self.db.commit()
```
and change the call at `src/learn/session.py:126` from `ensure_rows(self.items.keys())` to
`ensure_rows(self.items)`.

Existing rows have `fingerprint IS NULL` and are left alone by the `UPDATE`, so this is
safe to deploy against the current 74 approvals — **but** those 74 must be re-reviewed
after the bank swap regardless, because their ids will change under Fix ST-4. That is the
correct outcome: TRACKER A2 already schedules it.

## 5.5 F-03 — "Clear unflagged" approves everything when precheck was never run

`POST /api/review/approve_unflagged` (`src/api/main.py:325-332`) calls
`bulk_decide_unflagged` (`src/learn/review.py:196-207`), which decides on:

```python
            if json.loads(r["flags_json"] or "[]"):
                continue                      # flagged: a human must look at it
```

`flags_json` is only ever populated by `set_flags` (`review.py:155-158`), called only from
`POST /api/review/precheck` (`main.py:335-353`). **`/api/review/precheck` is not called
anywhere in `ui/index.html`** (verified by grep: no occurrence of the string `precheck`),
and the kiosk is WebKit with no address bar (D-030), so from the fielded device it cannot
be run at all.

On a database where precheck has never run — which is every database after `reset_demo.sh`
— every `flags_json` is `NULL`, `"" or "[]"` yields `[]`, nothing is treated as flagged,
and **the button approves the entire bank**. The UI then reports `d.pending` (0) as
"still need a human — those are the flagged ones" (`ui/index.html:941-943`).

This is the fastest way to undo the project's central claim, and after F-01 it is the
obvious thing for an operator to reach for.

### Fix ST-5 — refuse to bulk-clear items that were never checked

`src/learn/review.py:196`, change the query and add a guard:
```python
        rows = self.db.execute(
            "SELECT item_id, flags_json FROM item_review WHERE status='pending'").fetchall()
        # A NULL flags_json means precheck has never run for this item -- which is not
        # the same as "no flags were found". Clearing those in bulk would approve the
        # whole bank on the strength of checks that were never performed, which is the
        # exact claim this gate exists to make true.
        unchecked = [r["item_id"] for r in rows if r["flags_json"] is None]
        if unchecked:
            raise ValueError(f"{len(unchecked)} pending items have never been "
                             f"prechecked; run POST /api/review/precheck first")
```
and at `src/api/main.py:325-332`:
```python
@app.post("/api/review/approve_unflagged")
def review_approve_unflagged():
    L = STATE["learn"]
    if not L:
        return {"ok": False}
    try:
        touched = L.review.bulk_decide_unflagged(set(L.items))
    except ValueError as e:
        return {"ok": False, "error": str(e), "needs_precheck": True}
    return {"ok": True, "approved": len(touched), **L.review.stats()}
```
and in `ui/index.html:937-946`, on `needs_precheck`, offer the missing button:
```js
    if (d.needs_precheck) {
      $("#rvbulkhint").textContent = d.error + " — running the automatic checks now…";
      await getJSON("/api/review/precheck", {method: "POST"});
      $("#rvbulkhint").textContent = "Checks complete. Press again to clear the unflagged.";
      return;
    }
```
This is the smallest change that makes the precheck reachable from a kiosk with no
address bar, which it currently is not.

---

# 6. Input validation

Traced against pydantic 2.11.7 (`Answer`/`Ask`/`Decision` at `src/api/main.py:50-67`).

| Input | Where | Behaviour | Verdict |
|---|---|---|---|
| 100 KB answer | `/api/learn/answer` | Accepted whole into memory, then truncated: 1,200 chars to the grader (`session.py:324`), 2,000 chars stored (`session.py:358`) | **Handled.** Cap the body anyway (Fix R-1) |
| `confidence: 99` | `main.py:248` | `max(1, min(4, int(...)))` gives 4 | **Handled** |
| `confidence: -1` | `main.py:248` | gives 1 | **Handled** |
| `confidence: 3.5` / `"abc"` / `null` | pydantic | 422 with a **JSON** body, so `d.ok` is undefined and the UI shows "Could not grade that." (`ui:675`). Verified locally: `3.0` coerces to `3`, `3.5` rejects | Acceptable |
| Unknown `item_id` | `main.py:245-247` | `{"ok": false, "error": "unknown item"}`, id not echoed | **Handled** |
| Unknown `item_id` to `/api/review/decide` | `main.py:314` | `{"ok": false}` — **but the UI never reads it** (`ui:830`) and advances `rv.i++` at `:833` | **Gap** — see Fix E-1 |
| Bad `status` | `review.py:166-167` | `ValueError` caught at `main.py:320` | **Handled** |
| `"note": null` sent explicitly | pydantic | `note: str = None` is not `Optional[str]` in v2, so an explicit null is a 422. Verified locally | Cosmetic; the UI never sends it. Change to `str | None = None` |
| FTS5 operators (`AND`, `NEAR/3`, quote, star, hyphen) | `hybrid.py:97-109` | `FTS_SAFE = re.compile(r"[^\w\s]")` strips all punctuation; each surviving token is quoted. `NEAR/3` degrades to the harmless literals `"NEAR"` | **Handled well.** Credited below |
| Null byte in the question | `hybrid.py:97-108` | It is neither a word character nor whitespace, so `FTS_SAFE.sub` replaces it with a space. FTS is safe. It *does* survive into the embedding payload at `hybrid.py:50` (JSON-escaped, so the request is valid); llama.cpp's handling is untested | FTS handled; embedding path unverified, low risk |
| Empty / punctuation-only question | `hybrid.py:109` returns `None`, so `bm25_search` returns `[]` (`:114`); the dense half still runs at `hybrid.py:160` | `embed("")` reaches llama.cpp. If it returns 400, the `HTTPError` is uncaught and becomes a 500. `Ask.question` has no `min_length`; the UI guards at `ui:904` | **Gap** — fixed by `min_length=1` (Fix R-1) |
| `edited_points: [{"a": 1}]` | `Decision.edited_points: list` — untyped | `json.dumps` accepts it (`review.py:180`); `apply_edits` sets `key_points` to dicts (`review.py:250`); `precheck`'s `_tokens` then hits `re.findall` on a non-string and raises `TypeError`, and `ui:813` renders `[object Object]` | Low likelihood (the textarea at `ui:824` sends strings). Type it: `edited_points: list[str] | None = None` |

**Overall: input validation is the strongest area of the codebase.** The FTS5 sanitiser in
particular is exactly right, and the comment at `hybrid.py:101-107` explains *why* rather
than just *what*.

---

# 7. Service supervision

`scripts/install_services.sh`.

## 7.1 F-12 — the start limit may not be in force

`install_services.sh:86-87` (API) and `:119-120` (kiosk) place `StartLimitIntervalSec=`
and `StartLimitBurst=` inside `[Service]`. Since systemd v229 these are documented
`[Unit]` options; only the legacy spellings `StartLimitInterval` / `StartLimitBurst`
retain a `[Service]` compatibility path. `StartLimitIntervalSec` in `[Service]` is not a
documented key there and is expected to be parsed as unknown.

This matters because the comment immediately above (`:81-85`) says a start limit is *"not
optional alongside Restart=always"* and cites the real incident — restart counter 386,
thrashing the board. If the key is being ignored, the protection that incident bought is
not actually installed.

**Verify on the device before changing anything** (read-only, safe to run during the
generation job):
```sh
systemctl show tutor-api -p StartLimitBurst -p StartLimitIntervalUSec
systemd-analyze verify /etc/systemd/system/tutor-api.service
journalctl -u tutor-api | grep -i "unknown lvalue"
```
`StartLimitBurst=0` means the limit is off.

**Fix SV-1** — `install_services.sh:64-92`, move both lines into `[Unit]`:
```ini
[Unit]
Description=Doctrine Tutor - API and kiosk dashboard
After=local-fs.target tutor-gen.service tutor-embed.service tutor-rerank.service
Wants=tutor-gen.service tutor-embed.service tutor-rerank.service
# StartLimit* are [Unit] options, not [Service] ones -- systemd moved them in v229 and
# only the legacy names are still accepted under [Service]. Placed here they are
# actually in force; the 386-restart incident in the comment below is what happens when
# they are not.
StartLimitIntervalSec=60
StartLimitBurst=5
```
and delete `:86-87`. Same move for the kiosk unit (`:119-120` into its `[Unit]` block at
`:95-101`).

## 7.2 F-13 — `After=` does not mean "the model is loaded"

`install_services.sh:67` orders `tutor-api` after the three model units, but all four are
`Type=simple` (`:36`). systemd considers a `Type=simple` unit *started* the instant
`fork()` returns. llama.cpp then spends tens of seconds mmapping and offloading weights
(`-ngl 999`, `:52/55/58`).

So `tutor-api` binds :8000 while :8080 is still refusing connections. And `kiosk.sh:13`
waits only for `curl -fs -m 2 http://127.0.0.1:8000/`, which succeeds as soon as FastAPI
can serve the static page — so the monitor lights up looking ready.

Both D-026 and `reset_demo.sh:71-72` already know this: *"'Healthy' is not 'usable' --
llama.cpp answers /health while weights still load."* The unit files and `kiosk.sh` do not.

Consequence chain at cold boot: kiosk shows the tutor, a user asks a question,
`/api/ask` hits `ConnectionRefusedError`, 500, `adminAsk` prints `failed`.

There is a second casualty. `_warm()` (`src/api/main.py:468-483`) exists specifically to
absorb the 2–3 s first-request cost that the 60-second boot budget depends on. It fires
**once**, at startup, against a generator that is by construction not ready, fails
silently at `:479-480`, and never retries. The warm-up the docstring justifies at length
almost certainly does not happen on a cold boot.

**Fix SV-2** — make `_warm` retry, `src/api/main.py:468-483`:
```python
    def _warm():
        """Absorb first-request initialisation before a human touches the box.

        Retries, because systemd's After= on a Type=simple unit only guarantees the
        model server has been *forked*, not that it has loaded weights -- so the first
        attempt reliably lands on a refused connection and the warm-up never happens.
        """
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                STATE["pipeline"].ask("What is maneuver warfare?")
                STATE["warm"] = True
                return
            except Exception:                                  # noqa: BLE001
                time.sleep(5)
```

**Fix SV-3** — a health endpoint that tells the truth. `src/api/main.py:75-77`
(add `import urllib.request` to the imports at `:11-16`):
```python
def _probe(url, path="/health", timeout=1.5):
    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as r:
            return r.status == 200
    except Exception:                                          # noqa: BLE001
        return False


@app.get("/api/health")
def health():
    """Report the three dependencies, not just that this process is alive.

    Returning {"ok": true} while the generator is dead is the reason a runtime failure
    first shows up as a failed question instead of a warning.
    """
    p = STATE["pipeline"]
    models = {
        "generator": _probe(p.gen_url.rsplit("/v1", 1)[0]),
        "embeddings": _probe(p.embed_url),
        "reranker": _probe(p.rerank_url),
    }
    return {"ok": all(models.values()), "api": True, "models": models,
            "warm": STATE.get("warm", False),
            "uptime_s": round(time.time() - STATE["started"], 1)}
```
Note llama.cpp answers `/health` before weights finish loading, so `warm` is the field
that means *usable* — keep both and label them differently in the UI.

**Fix SV-4** — surface it. In `ui/index.html`, add a chip beside Connection in the header
and poll it on the same 4 s interval as `pollNet` (`:969`):
```js
async function pollHealth() {
  let h;
  try { h = await getJSON("/api/health"); }
  catch (e) { $("#modeltext").textContent = "Models unknown"; return; }
  const down = Object.entries(h.models).filter(([k, v]) => !v).map(([k]) => k);
  document.body.dataset.models = down.length ? "DOWN" : (h.warm ? "READY" : "LOADING");
  $("#modeltext").textContent = down.length ? down.join(" + ") + " down"
    : h.warm ? "Models ready" : "Models loading…";
}
```
This is the single highest-value change in the document: it converts every dependency
failure from *a mysterious blank panel* into *a label that says which box is broken*,
which is a far better thing for a user to be looking at.

**Fix SV-5** — gate the kiosk on usable, not on servable. `scripts/kiosk.sh:8-20`:
```sh
URL=${KIOSK_URL:-http://127.0.0.1:8000/}
HEALTH=${KIOSK_HEALTH:-http://127.0.0.1:8000/api/health}
DEADLINE=$(( $(date +%s) + ${KIOSK_WAIT:-120} ))

# Wait for the API to serve the page AND for the three model servers to answer. Waiting
# only for the page means the monitor lights up looking ready while the generator is
# still loading weights -- and the first question a user asks is the one that fails.
while ! curl -fs -m 2 -o /dev/null "$URL" || \
      ! curl -fs -m 2 "$HEALTH" 2>/dev/null | grep -q '"ok": *true'; do
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        echo "kiosk: API or models never came up" >&2
        break        # show the UI anyway -- a dashboard that says what is wrong beats
    fi               # a dark screen, now that SV-4 makes it say so
    sleep 1
done
```
Note the `break` rather than `exit 1`: with SV-4 in place, a UI that reports "generator
down" is strictly better than the black screen `kiosk.sh:15-17` currently produces.

## 7.3 Restart policy — correct, and the reasoning is sound

`Restart=always` + `RestartSec=3` on the API (`:79-80`) and the kiosk (`:117-118`), with
the reasoning recorded inline at `:75-78`: `on-failure` will not restart after a clean
SIGTERM, which is exactly the case that left the dashboard dead for half an hour. That is
right and was learned the hard way. The model units keep `Restart=on-failure` (`:39`),
which is also right — a model server that exits cleanly was stopped deliberately.

The model units carry no `StartLimit*` at all, so systemd's defaults
(`DefaultStartLimitIntervalSec=10s`, `DefaultStartLimitBurst=5`) apply. That is fine.

## 7.4 The memory caps

See section 4. `MemoryMax=3500M` x 3 (`:43`) on a 7.4 GiB board means the per-unit cgroup
limit can never fire before the global OOM killer.

**Fix SV-6** — measure and cap. On the device, with all three loaded and warm:
```sh
for u in tutor-gen tutor-embed tutor-rerank tutor-api; do
    printf "%-14s %s\n" "$u" "$(systemctl show "$u" -p MemoryCurrent --value)"
done
```
Then set each `MemoryMax` to the measured value + 15%. From D-024's note that
right-sizing buffers reclaimed ~1 GB, the reranker in particular should be nowhere near
3500 M — it is a 304 MB model capped at 512 tokens.

---

# 8. Already handled well

Several of these were hardened after real incidents and the reasoning is recorded in
`DECISIONS.md`. Credit where it is due — a report that only lists faults is not a
trustworthy picture of this codebase.

1. **The systemd restart policy, and the reasoning behind it.**
   `install_services.sh:75-85` does not just set `Restart=always`, it explains why
   `on-failure` was wrong (a SIGTERM is a clean exit) and why a start limit must
   accompany it (counter 386). The only defect is where the two limit lines are placed,
   not whether they were thought about. `:31-33` deliberately omits
   `After=network-online.target` on an air-gapped device — precisely the kind of default
   that quietly costs a boot budget.

2. **The builder's resilience (D-032).** `build_items.py:51-71` — 4 attempts, exponential
   backoff, `Connection: close` for the exact `RemoteDisconnected` observed; `--resume`
   at `:148-157`; per-paragraph exception isolation at `:171-177` so one bad chunk cannot
   end a 100-minute run. The flush-every-5-sections decision at `:202` is what made
   resume possible. Only the *atomicity* of that write is wrong (Fix ST-1); the design is
   right.

3. **The abstention gating, and its honesty about a negative result.**
   `pipeline.py:1-23` documents the measured scores that force a two-stage gate, and
   `config/default.yaml:58-66` keeps `sentence_support_threshold: null` with the reason
   spelled out — the grounding signal ranges overlap and *the wrong side scores higher*
   (D-024/D-025). Keeping a useful-but-non-gating signal on, and writing down why it
   cannot gate, is unusual discipline.

4. **`grade()`'s fallback (`session.py:326-335`).** The single best failure-handling
   decision in the codebase: the `try` covers both the HTTP call and the response
   indexing, and the learner still gets the source paragraph and citation back
   (`main.py:257-258`), so the screen stays useful. Two details need fixing (F-06, F-07)
   but the shape is the model the rest of the code should follow.

5. **`_log` never breaks answering (`main.py:41-47`).** "Never let record-keeping break
   answering" is the right priority and it is enforced, not merely asserted. Same
   discipline at `main.py:484-493`, where a failed `RecordStore` or `LearnStore` degrades
   the feature instead of killing the process.

6. **The FTS5 sanitiser (`hybrid.py:97-109`).** User text is turned into data, never
   grammar — each token stripped of punctuation and quoted. The comment explains the
   attack surface rather than just the mechanism. It also happens to neutralise the null
   byte.

7. **The reranker's document-budget retry (`hybrid.py:57-90`).** The comment records the
   measured 1.75 chars/token worst case that motivated `RERANK_DOC_CHARS = 700`, and the
   halving retry means one unusual chunk cannot fail a whole query. (It should also catch
   `URLError` — Fix D-2.)

8. **The "nothing to serve" vs "you have finished" distinction (`main.py:215-222`,
   `ui/index.html:556-570`, commit `bef7207`).** Two states that must never share a
   message, correctly separated, with the bug that motivated it recorded in the code.
   This is why the F-01 aftermath at least *reads* honestly instead of claiming the
   learner finished.

9. **The queue-truncation honesty (`main.py:304-307`).** `shown` and `pending` are
   reported separately so a reviewer is never told "1 of 60" while 1,000 items wait. The
   UI honours it at `ui/index.html:799-802`.

10. **`bulk_decide_unflagged` records a distinct reviewer id (`review.py:183-207`).** The
    audit trail can still answer "which questions did a human actually read?" — the exact
    question `demo/script.md` promises a truthful answer to. The defect is F-03 (it runs
    on flags that were never computed), not the audit design.

11. **The clock-honesty design (`store.py:8-17, 164-171, 196-201`).** A dead RTC on an
    air-gapped device is handled by recording wall time as a *claim* alongside an
    authoritative sequence and an explicit `clock_trusted` flag, and the export says so in
    prose. The `signature.proves` field (`:207-210`) explicitly disclaims non-repudiation.
    That is the opposite of overclaiming — F-08's duplicate-`seq` bug is worth fixing
    precisely *because* this design is good enough to be relied on.

12. **The network banner (`network_status.py`, D-031).** Three independent signals with
    the strongest one deciding (`:81-89`), the honest middle state
    `LINK UP - NO INTERNET` (`:102-104`), and the USB dev-link correctly excluded from
    what counts as an uplink (`:64-78`) — with the note that this is *safe rather than
    flattering* because reachability is probed first. `_ip_json` catches (`:39`) so the
    endpoint cannot 500.

13. **Telemetry degrades to blanks, not errors (`telemetry.py:6-9, 75-81`).** Every field
    optional by design, `stale` reported explicitly, `tegrastats` absence captured as
    `error` rather than raised, and the UI renders em-dashes
    (`ui/index.html:868-878`). This is the *only* drawer panel that fails gracefully.

14. **Loopback-only binding (`main.py:444`, `config/default.yaml:76`), with a comment on
    both.** For a device whose entire claim is that nothing leaves it, this being
    unmissable in two places is right.

15. **`reset_demo.sh` archives rather than deletes, and refuses to touch
    `doctrine.sqlite` (`:8-10, 22-31`).** The instinct is correct — the reset must not be
    able to invalidate the provenance chain. F-01 is a scope error (it takes the review
    table with it), not a philosophy error. Its port-8000 squatter handling (`:44-56`)
    and its refusal to call the device ready until a real question answers (`:71-81`) are
    both hard-won and right.

---

# 9. Suggested order of work

Ordered by (field risk x cost to fix), not by section number.

| Order | Fix | Files | Rough size |
|---|---|---|---|
| 1 | **F-01** — preserve `item_review` across the reset | `scripts/reset_demo.sh` | ~35 lines |
| 2 | **E-1/E-2** — `getJSON` + `fail` helpers, wire up all 12 call sites | `ui/index.html` | ~40 lines |
| 3 | **SV-3/SV-4** — real `/api/health` + a model chip in the header | `src/api/main.py`, `ui/index.html` | ~35 lines |
| 4 | **ST-4** — content-addressed item ids + fingerprint demotion | `build_items.py`, `review.py`, `session.py` | ~25 lines |
| 5 | **D-1/D-2** — timeout budget + `ModelUnavailable` to 503 | `pipeline.py`, `hybrid.py`, `session.py`, `main.py` | ~40 lines |
| 6 | **ST-5** — refuse bulk approval when nothing was prechecked | `review.py`, `main.py`, `ui/index.html` | ~20 lines |
| 7 | **ST-1** — atomic items write | `build_items.py` | ~20 lines |
| 8 | **S-1/S-2/S-3** — busy timeout, atomic `seq`, single-transaction bulk | `store.py`, `session.py`, `review.py` | ~30 lines |
| 9 | **SV-1** — move `StartLimit*` into `[Unit]` *(verify on device first)* | `install_services.sh` | 4 lines |
| 10 | **F-06/F-07/S-4** — do not score an ungraded attempt; guard item access | `session.py`, `main.py` | ~15 lines |
| 11 | **ST-2/ST-3** — validating loader + reload endpoint | `session.py`, `main.py` | ~45 lines |
| 12 | **R-1/R-2/R-3** — bound bodies, query params, in-memory history | `main.py`, `ui/index.html` | ~15 lines |
| 13 | **D-3** — SSE always terminates | `main.py` | ~10 lines |
| 14 | **SV-2/SV-5/SV-6** — warm retry, kiosk health gate, measured memory caps | `main.py`, `kiosk.sh`, `install_services.sh` | ~25 lines |

Items 1–3 are roughly 110 lines and remove the three failure modes most likely to be seen
by a user. Item 4 is the integrity fix and must land before the ~1,050-item bank is
reviewed (TRACKER A2), or that review will be wasted.

## Fix F-01 in full — the one that matters most

`scripts/reset_demo.sh:22-31`, replace the archive block:

```sh
# Records are ARCHIVED, not deleted. But the review table lives in this same file, and
# an approval is a human's signature -- a reset must clear the LEARNER's history,
# not un-sign every question an instructor read. Preserve item_review across the move.
KEEP=""
if [ -f "$TUTOR/records.sqlite" ]; then
    STAMP=$(date +%Y%m%d-%H%M%S 2>/dev/null || echo manual)
    mkdir -p "$TUTOR/archive"
    KEEP="$TUTOR/archive/item_review-$STAMP.sql"
    python3 - "$TUTOR/records.sqlite" "$KEEP" <<'PY'
import sqlite3, sys
src, out = sys.argv[1], sys.argv[2]
db = sqlite3.connect(src)
try:
    rows = [r for r in db.iterdump() if "item_review" in r]
except sqlite3.DatabaseError:
    rows = []
open(out, "w", encoding="utf-8").write("\n".join(rows))
n = sum(1 for r in rows if r.lstrip().upper().startswith("INSERT"))
print("  preserved %d review rows" % n)
PY
    mv "$TUTOR/records.sqlite" "$TUTOR/archive/records-$STAMP.sqlite"
    rm -f "$TUTOR/records.sqlite-wal" "$TUTOR/records.sqlite-shm"
    say "records archived -> archive/records-$STAMP.sqlite"
fi
```

and restore after the services are back up — insert immediately before the
"verifying with a real question" step at `:70`:

```sh
# Restore the approvals into the freshly created database. The API has already created
# the schema and the pending rows by this point, so INSERT OR REPLACE is what we want.
if [ -n "$KEEP" ] && [ -s "$KEEP" ]; then
    python3 - "$TUTOR/records.sqlite" "$KEEP" <<'PY'
import re, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
n = 0
for stmt in open(sys.argv[2], encoding="utf-8").read().split(";\n"):
    s = stmt.strip()
    if not s.upper().startswith("INSERT"):
        continue
    try:
        db.execute(re.sub(r"^INSERT INTO", "INSERT OR REPLACE INTO", s, flags=re.I))
        n += 1
    except sqlite3.Error:
        pass
db.commit()
print("  restored %d review rows" % n)
PY
    say "instructor approvals restored"
fi
```

Also extend the readiness check at `reset_demo.sh:71-81` so the reset cannot report
`READY` while the tutor half is dead:

```sh
APPROVED=$(curl -s -m 5 http://127.0.0.1:8000/api/learn/track 2>/dev/null \
           | grep -o '"n_approved": *[0-9]*' | grep -o '[0-9]*')
if [ "${APPROVED:-0}" -lt 1 ]; then
    echo "  !! NOT READY - 0 approved practice items. The learning half will not run."
    exit 1
fi
say "READY - ${APPROVED} approved practice items"
```

---

*Nothing in this document was inferred from behaviour observed on the device. Every
finding is traceable to the file and line cited, and the three library-behaviour claims
carry their verification method inline. No source file was modified and no service was
touched in producing it.*
