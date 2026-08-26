#!/usr/bin/env python3
"""One retry policy for every call to a local model server.

llama-server occasionally closes a keep-alive connection between requests. It surfaces as
`http.client.RemoteDisconnected` and it is not rare: the full-corpus item generation run
absorbed **24** of them in 104 minutes.

That was learned expensively. D-032 records the first generation run dying after 18 of 539
sections on a single one of these, and the retry was added to `build_items.py` -- only to
`build_items.py`. Everything else kept the bare `urlopen`, so the very next long job to run
died the same way: the expanded 129-question eval failed at its FIRST request with zero
progress.

So the policy lives in one place and every caller uses it. Shared the same way
`chunk_text.py` is -- `scripts/push.sh` copies this file into each directory that needs it,
because the device layout is flat.

Which HTTP errors are retried is a real distinction, learned the hard way. The first
version of this module refused to retry ANY HTTPError, and the very next eval run died on
a **503 Service Unavailable** from llama-server -- which is the server saying "busy, try
again", not "your request is wrong". 503 arrives when every slot is occupied, and the
warm-up query added after the cold-boot work makes that likely for a few seconds after a
restart.

So: 502, 503 and 504 are retried. 4xx is not -- the request is wrong and repeating it
repeats the answer. **500 is deliberately not retried either**, because `rerank()` catches
one and shrinks its document budget, which is a real recovery; retrying would turn a
working adaptation into four identical failures.
"""
import json
import time
import urllib.error
import urllib.request

# Six, not four. The backoff has to outlast a generator RESTART, not just a dropped
# connection: the kernel OOM-killed llama-server during an eval run and systemd brought it
# back in about 8 seconds, while 4 attempts only cover 1+2+4 = 7s of waiting. Six covers
# 31s, which survives a restart plus model load with margin.
DEFAULT_ATTEMPTS = 6
# Transient by definition. 500 is excluded on purpose -- see the module docstring.
RETRY_STATUS = frozenset({502, 503, 504})


def post_json(url, payload, timeout=300, attempts=DEFAULT_ATTEMPTS, on_retry=None):
    """POST json, parse json, retry transient transport failures with backoff.

    `Connection: close` because the failure being handled IS keep-alive reuse: asking for
    a fresh connection each time trades a negligible amount of latency for the thing that
    kept ending long runs.
    """
    body = json.dumps(payload).encode()
    for n in range(attempts):
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json",
                                     "Connection": "close"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            # 502/503/504 mean "busy or momentarily unavailable, try again". Everything
            # else -- including 500, which rerank() recovers from by shrinking its
            # document budget -- is the server answering, so the caller handles it.
            if e.code not in RETRY_STATUS or n == attempts - 1:
                raise
            wait = 2 ** n
            if on_retry:
                on_retry(n + 1, attempts, e, wait)
            time.sleep(wait)
            continue
        except Exception as e:                                     # noqa: BLE001
            if n == attempts - 1:
                raise
            wait = 2 ** n
            if on_retry:
                on_retry(n + 1, attempts, e, wait)
            time.sleep(wait)
