"""Tests for SchoolCircle's source-scoped evidence selection endpoint.

The endpoint must be able to rank caller-provided text without touching the doctrine
database or calling the ordinary retrieval pipeline. The reranker is replaced below, so
these are deterministic in-process tests with no model server.
"""
import time
import urllib.error

import pytest
from fastapi.testclient import TestClient

import main


def _passage(identifier, text, source):
    return {"id": identifier, "text": text, "source": source}


@pytest.fixture
def client(monkeypatch):
    class NoGlobalRetrieval:
        rerank_url = "http://reranker.test"
        score_threshold = 3.0

        def retrieve(self, *_args, **_kwargs):
            raise AssertionError("/api/ground must not use global retrieval")

    monkeypatch.setitem(main.STATE, "pipeline", NoGlobalRetrieval())
    monkeypatch.setitem(main.STATE, "config", {})
    monkeypatch.setitem(main.STATE, "started", time.time())
    return TestClient(main.app)


def test_ground_reranks_only_supplied_passages_and_preserves_provenance(client, monkeypatch):
    supplied = [
        _passage("first", " original first text ", " course/one "),
        _passage("second", "original second text", "course/two"),
    ]
    calls = {}

    def fake_rerank(question, documents, url, budget=700):
        calls.update(question=question, documents=documents, url=url)
        return [(1, 8.0), (0, 4.0)]

    monkeypatch.setattr(main, "rerank", fake_rerank)
    response = client.post("/api/ground", json={
        "question": "Which supplied passage is relevant?",
        "passages": supplied,
    })

    assert response.status_code == 200
    assert calls == {
        "question": "Which supplied passage is relevant?",
        "documents": [p["text"] for p in supplied],
        "url": "http://reranker.test",
    }
    assert response.json() == {
        "abstained": False,
        # The best-first reranker order is retained, but every field is the exact supplied
        # value -- including whitespace that a normalisation step would have lost.
        "passages": [supplied[1], supplied[0]],
        "contract": "schoolcircle-grounding-v1",
    }


def test_ground_abstains_without_returning_low_score_passages(client, monkeypatch):
    monkeypatch.setattr(main, "rerank", lambda *_args, **_kwargs: [(0, 2.99)])
    supplied = [_passage("approved-1", "Some approved text.", "lesson 1")]

    response = client.post("/api/ground", json={"question": "A question", "passages": supplied})

    assert response.status_code == 200
    assert response.json() == {
        "abstained": True,
        "passages": [],
        "contract": "schoolcircle-grounding-v1",
    }


def test_ground_reports_unavailable_reranker_as_a_sanitized_503(client, monkeypatch):
    supplied = [_passage("approved-1", "Some approved text.", "lesson 1")]

    def unavailable(*_args, **_kwargs):
        raise urllib.error.URLError("connection details must not reach the client")

    monkeypatch.setattr(main, "rerank", unavailable)
    response = client.post("/api/ground", json={"question": "A question", "passages": supplied})
    assert response.status_code == 503
    assert response.json() == {"detail": "reranker unavailable"}


def test_ground_reports_failed_or_malformed_reranking_as_a_sanitized_502(client, monkeypatch):
    supplied = [_passage("approved-1", "Some approved text.", "lesson 1")]

    def failed(*_args, **_kwargs):
        raise RuntimeError("internal reranker details must not reach the client")

    monkeypatch.setattr(main, "rerank", failed)
    response = client.post("/api/ground", json={"question": "A question", "passages": supplied})
    assert response.status_code == 502
    assert response.json() == {"detail": "reranker failed"}

    monkeypatch.setattr(main, "rerank", lambda *_args, **_kwargs: [(99, 99.0)])
    response = client.post("/api/ground", json={"question": "A question", "passages": supplied})
    assert response.status_code == 502
    assert response.json() == {"detail": "reranker returned an invalid response"}

    monkeypatch.setattr(main, "rerank", lambda *_args, **_kwargs: None)
    response = client.post("/api/ground", json={"question": "A question", "passages": supplied})
    assert response.status_code == 502
    assert response.json() == {"detail": "reranker returned an invalid response"}


def test_ground_empty_source_set_abstains_without_calling_reranker(client, monkeypatch):
    def must_not_run(*_args, **_kwargs):
        raise AssertionError("empty requests do not need reranking")

    monkeypatch.setattr(main, "rerank", must_not_run)
    response = client.post("/api/ground", json={"question": "A question", "passages": []})
    assert response.status_code == 200
    assert response.json()["abstained"] is True
    assert response.json()["passages"] == []


@pytest.mark.parametrize("body", [
    {"question": "x" * 2001, "passages": []},
    {"question": "question", "passages": [
        _passage("id", "x" * 8001, "source"),
    ]},
    {"question": "question", "passages": [
        _passage(str(n), "text", "source") for n in range(17)
    ]},
    {"question": "question", "passages": [
        {**_passage("id", "text", "source"), "unapproved": "metadata"},
    ]},
])
def test_ground_rejects_oversized_or_noncontract_input(client, body):
    assert client.post("/api/ground", json=body).status_code == 422