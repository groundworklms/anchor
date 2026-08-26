"""The retry that every long job depends on.

D-032 records a generation run dying after 18 of 539 sections on one RemoteDisconnected.
The fix went into build_items.py only, so the very next long job -- a 129-question eval --
died at its first request with zero progress. These tests pin the policy so the next
module to call a model server inherits it instead of rediscovering it.
"""
import http.client
import json
import urllib.error
import pytest

from http_retry import post_json


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_transient_disconnect_is_retried_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise http.client.RemoteDisconnected("closed")
        return _Resp({"ok": True})

    monkeypatch.setattr("http_retry.urllib.request.urlopen", fake)
    monkeypatch.setattr("http_retry.time.sleep", lambda s: None)
    assert post_json("http://x/y", {"a": 1}) == {"ok": True}
    assert calls["n"] == 3


def test_gives_up_after_the_configured_attempts(monkeypatch):
    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        raise http.client.RemoteDisconnected("closed")

    monkeypatch.setattr("http_retry.urllib.request.urlopen", fake)
    monkeypatch.setattr("http_retry.time.sleep", lambda s: None)
    with pytest.raises(http.client.RemoteDisconnected):
        post_json("http://x/y", {}, attempts=3)
    assert calls["n"] == 3


def test_http_error_is_NOT_retried(monkeypatch):
    """A 400 is the server answering. rerank() shrinks its document budget on one --
    retrying instead would repeat the same answer and defeat that recovery."""
    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://x/y", 400, "bad", {}, None)

    monkeypatch.setattr("http_retry.urllib.request.urlopen", fake)
    monkeypatch.setattr("http_retry.time.sleep", lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        post_json("http://x/y", {})
    assert calls["n"] == 1


def test_connection_is_not_reused(monkeypatch):
    """Keep-alive reuse is the failure being handled, so every attempt asks to close."""
    seen = {}

    def fake(req, timeout=None):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return _Resp({})

    monkeypatch.setattr("http_retry.urllib.request.urlopen", fake)
    post_json("http://x/y", {})
    assert seen.get("connection") == "close"


def test_backoff_grows(monkeypatch):
    waits = []
    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        raise OSError("boom")

    monkeypatch.setattr("http_retry.urllib.request.urlopen", fake)
    monkeypatch.setattr("http_retry.time.sleep", waits.append)
    with pytest.raises(OSError):
        post_json("http://x/y", {}, attempts=4)
    assert waits == [1, 2, 4]


def test_503_IS_retried(monkeypatch):
    """The first version of this module refused to retry any HTTPError, and the next eval
    run died on a 503 from llama-server -- the server saying busy, not wrong."""
    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError("http://x/y", 503, "busy", {}, None)
        return _Resp({"ok": True})

    monkeypatch.setattr("http_retry.urllib.request.urlopen", fake)
    monkeypatch.setattr("http_retry.time.sleep", lambda s: None)
    assert post_json("http://x/y", {}) == {"ok": True}
    assert calls["n"] == 3


@pytest.mark.parametrize("code", [502, 504])
def test_other_transient_statuses_retried(monkeypatch, code):
    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.HTTPError("http://x/y", code, "x", {}, None)
        return _Resp({})

    monkeypatch.setattr("http_retry.urllib.request.urlopen", fake)
    monkeypatch.setattr("http_retry.time.sleep", lambda s: None)
    post_json("http://x/y", {})
    assert calls["n"] == 2


def test_500_is_NOT_retried(monkeypatch):
    """rerank() recovers from a 500 by shrinking its document budget. Retrying would turn
    that working adaptation into four identical failures."""
    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://x/y", 500, "ctx", {}, None)

    monkeypatch.setattr("http_retry.urllib.request.urlopen", fake)
    monkeypatch.setattr("http_retry.time.sleep", lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        post_json("http://x/y", {})
    assert calls["n"] == 1
