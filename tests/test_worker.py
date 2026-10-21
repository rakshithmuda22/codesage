"""Tests for the review pipeline (core.worker).

GitHub and the LLM are replaced with in-process fakes; everything else
(RepoAnalyzer, dependency graph, the four real agents, the Deduplicator,
DiffParser) runs for real.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import pytest

import core.worker as worker
from core.github_client import DiffParser
from models.schemas import AgentResult, ChangedFile, Finding, RepoConventions

SOURCE = (
    "import hashlib\n"                                    # 1
    "\n"                                                  # 2
    "\n"                                                  # 3
    "def login(user, password):\n"                        # 4
    "    profile = user.profile\n"                        # 5
    "    return profile.name\n"                           # 6
    "\n"                                                  # 7
    "\n"                                                  # 8
    'API_KEY = "sk-live-1234567890abcdef"\n'              # 9
)

DIFF = """\
diff --git a/lib/auth.py b/lib/auth.py
index 1..2 100644
--- a/lib/auth.py
+++ b/lib/auth.py
@@ -1,3 +1,9 @@
 import hashlib
+
+
+def login(user, password):
+    profile = user.profile
+    return profile.name
+
+
+API_KEY = "sk-live-1234567890abcdef"
"""

JOB = {
    "job_id": "job-1",
    "repo_full_name": "acme/shop",
    "pr_number": 42,
    "pr_title": "Add login",
    "head_sha": "a" * 40,
    "base_sha": "b" * 40,
    "installation_id": 777,
}


class FakeGitHub:
    """Records the review it is asked to post."""

    reviews: list[dict[str, Any]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def get_pr_files(self, repo, pr_number, head_sha=""):
        return [ChangedFile(
            filename="lib/auth.py", status="added", additions=9,
            content=SOURCE, language="python",
        )]

    async def get_pr_diff(self, repo, pr_number):
        return DIFF

    async def create_pr_review(self, **kwargs):
        FakeGitHub.reviews.append(kwargs)
        return {"id": 1}

    async def close(self):
        pass


class ScriptedLLM:
    """Answers each agent's prompt with one fixed finding."""

    def __init__(self, *args, **kwargs):
        pass

    async def complete(self, system_prompt="", user_prompt="", **kwargs):
        if "finding bugs" in system_prompt:
            return {"findings": [{
                "line": 5, "severity": "HIGH", "category": "null-pointer",
                "title": "user.profile may be None",
                "description": "profile is dereferenced unchecked",
                "suggestion": "check for None", "confidence": 0.9,
            }]}
        return {"findings": []}


class NoConventions:
    def __init__(self, *args, **kwargs):
        pass

    async def learn_conventions(self, repo):
        return RepoConventions()


@pytest.fixture
def pipeline(monkeypatch):
    FakeGitHub.reviews = []
    monkeypatch.setattr(worker, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(worker, "LLMClient", ScriptedLLM)
    monkeypatch.setattr(worker, "ConventionLearner", NoConventions)
    monkeypatch.delenv("CODESAGE_POST_VERDICTS", raising=False)
    return FakeGitHub


@pytest.mark.asyncio
async def test_pipeline_posts_inline_comments_that_land_inside_the_diff(
    pipeline,
):
    result = await worker.process_review(JOB)

    assert len(pipeline.reviews) == 1
    review = pipeline.reviews[0]
    commentable = DiffParser.parse(DIFF)["lib/auth.py"]
    by_line = {c["line"]: c for c in review["comments"]}

    # The LLM's bug (line 5) and the regex-found secret (line 9) both
    # become comments, on those exact lines of the new file.
    assert set(by_line) == {5, 9}
    assert all(c["path"] == "lib/auth.py" for c in review["comments"])
    assert all(line in commentable for line in by_line)
    assert "user.profile may be None" in by_line[5]["body"]
    assert "Hardcoded secret" in by_line[9]["body"]
    assert result["total_findings"] == 2
    assert result["files_reviewed"] == 1


@pytest.mark.asyncio
async def test_default_is_a_plain_comment_review_never_approve_or_block(
    pipeline,
):
    result = await worker.process_review(JOB)
    # A CRITICAL secret was found, so the computed decision is
    # REQUEST_CHANGES ...
    assert result["review_decision"] == "REQUEST_CHANGES"
    # ... but the bot does not post it unless the operator opts in.
    assert pipeline.reviews[0]["event"] == "COMMENT"


@pytest.mark.asyncio
async def test_verdicts_are_posted_when_enabled(pipeline, monkeypatch):
    monkeypatch.setenv("CODESAGE_POST_VERDICTS", "true")
    await worker.process_review(JOB)
    assert pipeline.reviews[0]["event"] == "REQUEST_CHANGES"


def test_review_event_policy(monkeypatch):
    monkeypatch.delenv("CODESAGE_POST_VERDICTS", raising=False)
    assert worker.review_event("APPROVE") == "COMMENT"
    monkeypatch.setenv("CODESAGE_POST_VERDICTS", "1")
    assert worker.review_event("APPROVE") == "APPROVE"


# ---------------------------------------------------------------------------
# Parallelism and failure isolation
# ---------------------------------------------------------------------------


def make_agent(name: str, log: dict[str, list[float]],
               delay: float = 0.2, fail: bool = False):
    class Agent:
        def __init__(self, llm):
            pass

        async def analyze(self, files, conventions, graph):
            log["start"].append(time.perf_counter())
            await asyncio.sleep(delay)
            log["end"].append(time.perf_counter())
            if fail:
                raise RuntimeError(f"{name} blew up")
            return AgentResult(agent=name, findings=[Finding(
                agent=name, file_path="lib/auth.py", line_number=5,
                severity="MEDIUM", category=f"cat-{name}",
                title=f"{name} finding", description="d", suggestion="s",
            )])

    Agent.name = name
    return Agent


def patch_agents(monkeypatch, log, failing: str | None = None):
    for attr, name in [
        ("BugDetector", "BugDetector"),
        ("SecurityScanner", "SecurityScanner"),
        ("StyleAdvisor", "StyleAdvisor"),
        ("TestCoverageAgent", "TestCoverage"),
    ]:
        monkeypatch.setattr(
            worker, attr, make_agent(name, log, fail=(name == failing))
        )


@pytest.mark.asyncio
async def test_four_agents_run_concurrently_not_one_after_another(
    pipeline, monkeypatch
):
    log: dict[str, list[float]] = {"start": [], "end": []}
    patch_agents(monkeypatch, log)

    t0 = time.perf_counter()
    await worker.process_review(JOB)
    elapsed = time.perf_counter() - t0

    assert len(log["start"]) == 4
    # All four were started before the first one finished ...
    assert max(log["start"]) < min(log["end"])
    # ... so four 0.2s agents take ~0.2s, not ~0.8s.
    assert elapsed < 0.6


@pytest.mark.asyncio
async def test_one_failing_agent_does_not_block_the_others(
    pipeline, monkeypatch
):
    log: dict[str, list[float]] = {"start": [], "end": []}
    patch_agents(monkeypatch, log, failing="SecurityScanner")

    result = await worker.process_review(JOB)

    errors = {r["agent"]: r["error"] for r in result["agent_results"]}
    assert "SecurityScanner blew up" in errors["SecurityScanner"]
    assert errors["BugDetector"] is None
    # The three healthy agents still produced a review.
    assert len(pipeline.reviews) == 1
    assert result["total_findings"] >= 1


# ---------------------------------------------------------------------------
# Redis + RQ plumbing (skipped when no Redis is reachable)
# ---------------------------------------------------------------------------


def _redis_or_skip():
    import redis
    url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    conn = redis.from_url(url)
    try:
        conn.ping()
    except redis.RedisError:
        pytest.skip("no Redis reachable at REDIS_URL")
    return url, conn


def test_job_goes_through_redis_and_an_rq_worker(pipeline, monkeypatch):
    """enqueue_review -> Redis -> rq worker -> process_review -> result."""
    from rq import SimpleWorker

    from models.schemas import ReviewJob

    url, conn = _redis_or_skip()
    queue_name = f"codesage-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(worker, "QUEUE_NAME", queue_name)

    rw = worker.ReviewWorker(redis_url=url)
    rq_id = rw.enqueue_review(ReviewJob(**JOB))
    assert rw.get_job_status(rq_id)["status"] == "queued"

    SimpleWorker([rw.queue], connection=conn).work(burst=True)

    status = rw.get_job_status(rq_id)
    try:
        assert status["status"] == "finished"
        assert status["result"]["total_findings"] == 2
        assert len(pipeline.reviews) == 1
    finally:
        rw.queue.empty()
        conn.delete(f"rq:job:{rq_id}")
