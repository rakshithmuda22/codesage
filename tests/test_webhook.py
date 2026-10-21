"""Tests for the GitHub webhook endpoint: auth first, then routing.

The signature check must fail closed in every case: no secret
configured, no signature, wrong signature, tampered body, junk header.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import main

SECRET = "test-webhook-secret"

PR_EVENT: dict[str, Any] = {
    "action": "opened",
    "pull_request": {
        "number": 42,
        "title": "Add login",
        "head": {"sha": "a" * 40},
        "base": {"sha": "b" * 40},
    },
    "repository": {"full_name": "acme/shop"},
    "installation": {"id": 777},
}


def sign(body: bytes, secret: str = SECRET) -> str:
    """Return the X-Hub-Signature-256 header value for ``body``."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class FakeWorker:
    """Stands in for ReviewWorker so no Redis is needed."""

    enqueued: list[Any] = []
    fail = False

    def enqueue_review(self, job):
        if FakeWorker.fail:
            raise ConnectionError("redis down")
        FakeWorker.enqueued.append(job)
        return "rq-123"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(main, "ReviewWorker", FakeWorker)
    FakeWorker.enqueued = []
    FakeWorker.fail = False
    main.recent_jobs.clear()
    return TestClient(main.app)


def post(client, body: bytes, event="pull_request", sig=None, headers=None):
    hdrs = {
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
    }
    if sig is not False:
        hdrs["X-Hub-Signature-256"] = sig if sig is not None else sign(body)
    hdrs.update(headers or {})
    return client.post("/webhook/github", content=body, headers=hdrs)


def test_valid_signature_queues_a_job(client):
    body = json.dumps(PR_EVENT).encode()
    r = post(client, body)
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert r.json()["rq_job_id"] == "rq-123"
    job = FakeWorker.enqueued[0]
    assert (job.repo_full_name, job.pr_number) == ("acme/shop", 42)
    assert job.head_sha == "a" * 40
    assert job.installation_id == 777


def test_missing_secret_fails_closed_with_503(client, monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET")
    body = json.dumps(PR_EVENT).encode()
    # Even a request signed with the "empty" secret must not get through.
    r = post(client, body, sig=sign(body, secret=""))
    assert r.status_code == 503
    assert FakeWorker.enqueued == []


def test_empty_secret_fails_closed_with_503(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "")
    r = post(client, b"{}", sig="sha256=")
    assert r.status_code == 503
    assert FakeWorker.enqueued == []


def test_missing_signature_header_is_rejected(client):
    r = post(client, json.dumps(PR_EVENT).encode(), sig=False)
    assert r.status_code == 401
    assert FakeWorker.enqueued == []


def test_wrong_signature_is_rejected(client):
    body = json.dumps(PR_EVENT).encode()
    r = post(client, body, sig=sign(body, secret="someone-elses-secret"))
    assert r.status_code == 401
    assert FakeWorker.enqueued == []


def test_tampered_body_is_rejected(client):
    body = json.dumps(PR_EVENT).encode()
    good_sig = sign(body)
    tampered = json.dumps({**PR_EVENT, "repository": {"full_name": "evil/x"}})
    r = post(client, tampered.encode(), sig=good_sig)
    assert r.status_code == 401
    assert FakeWorker.enqueued == []


def test_unprefixed_signature_is_rejected(client):
    body = json.dumps(PR_EVENT).encode()
    r = post(client, body, sig=sign(body).removeprefix("sha256="))
    assert r.status_code == 401


def test_legacy_sha1_header_alone_is_not_accepted(client):
    body = json.dumps(PR_EVENT).encode()
    sha1 = hmac.new(SECRET.encode(), body, hashlib.sha1).hexdigest()
    r = post(client, body, sig=False,
             headers={"X-Hub-Signature": f"sha1={sha1}"})
    assert r.status_code == 401


def test_non_ascii_signature_header_is_401_not_500(client):
    body = json.dumps(PR_EVENT).encode()
    r = client.post(
        "/webhook/github", content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": b"sha256=\xe9\xe9",
        },
    )
    assert r.status_code == 401


def test_signature_is_checked_before_json_is_parsed(client):
    r = post(client, b"not json", sig="sha256=00")
    assert r.status_code == 401  # not 400


def test_valid_signature_but_invalid_json_is_400(client):
    r = post(client, b"not json")
    assert r.status_code == 400


def test_valid_signature_but_non_object_json_is_400(client):
    r = post(client, b"[1, 2, 3]")
    assert r.status_code == 400


def test_ping_event_gets_pong(client):
    body = json.dumps({"zen": "Keep it logically awesome."}).encode()
    r = post(client, body, event="ping")
    assert r.status_code == 200
    assert r.json()["status"] == "pong"
    assert FakeWorker.enqueued == []


def test_unhandled_event_type_is_ignored(client):
    r = post(client, json.dumps({"action": "created"}).encode(),
             event="issues")
    assert r.json()["status"] == "ignored"
    assert FakeWorker.enqueued == []


@pytest.mark.parametrize("action", ["closed", "labeled", "edited"])
def test_unhandled_pr_action_is_ignored(client, action):
    r = post(client, json.dumps({**PR_EVENT, "action": action}).encode())
    assert r.json()["status"] == "ignored"
    assert FakeWorker.enqueued == []


def test_synchronize_is_handled(client):
    r = post(client, json.dumps({**PR_EVENT, "action": "synchronize"}).encode())
    assert r.json()["status"] == "queued"


def test_queue_failure_returns_503_not_a_crash(client):
    FakeWorker.fail = True
    r = post(client, json.dumps(PR_EVENT).encode())
    assert r.status_code == 503
    assert r.json()["status"] == "error"


def test_job_shows_up_in_jobs_list(client):
    post(client, json.dumps(PR_EVENT).encode())
    jobs = client.get("/jobs").json()["jobs"]
    assert jobs[0]["repo"] == "acme/shop"
    assert jobs[0]["pr_number"] == 42


def test_health_reports_the_four_agents(client):
    body = client.get("/health").json()
    assert body["agents"] == [
        "BugDetector", "SecurityScanner", "StyleAdvisor", "TestCoverage",
    ]
