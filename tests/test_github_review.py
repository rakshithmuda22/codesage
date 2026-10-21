"""Tests for posting a review: what goes inline and what goes in the summary.

Uses httpx.MockTransport so the exact JSON that would be sent to
GitHub can be inspected without any network access.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from core.github_client import MAX_REVIEW_BODY, GitHubClient

DIFF = """\
diff --git a/lib/auth.py b/lib/auth.py
index 1..2 100644
--- a/lib/auth.py
+++ b/lib/auth.py
@@ -10,2 +10,3 @@ def login(user):
     query = build(user)
+    run(query)
     return query
@@ -40,2 +41,2 @@ def logout():
-    old()
+    new()
     done()
"""


def make_client(handler) -> GitHubClient:
    """GitHubClient wired to a fake transport with a cached token."""
    client = GitHubClient(app_id="1", private_key="unused", installation_id=7)
    client._token = "test-token"
    client._token_expires = time.time() + 3600
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def comment(path: str, line: int, body: str = "finding") -> dict:
    return {"path": path, "line": line, "side": "RIGHT", "body": body}


@pytest.mark.asyncio
async def test_inline_comment_uses_exact_line_and_right_side():
    """A finding on an added line is posted at that line, not nearby."""
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": 1})

    client = make_client(handler)
    await client.create_pr_review(
        "o/r", 5, "headsha", "summary",
        [comment("lib/auth.py", 11, "added line"),
         comment("lib/auth.py", 41, "context-adjacent new line")],
        diff_text=DIFF,
    )
    payload = sent[0]
    assert payload["commit_id"] == "headsha"
    assert payload["event"] == "COMMENT"
    assert payload["comments"] == [
        {"path": "lib/auth.py", "line": 11, "side": "RIGHT",
         "body": "added line"},
        {"path": "lib/auth.py", "line": 41, "side": "RIGHT",
         "body": "context-adjacent new line"},
    ]
    assert all("position" not in c for c in payload["comments"])


@pytest.mark.asyncio
async def test_context_lines_inside_a_hunk_are_commentable():
    """Unchanged lines shown in the diff are valid targets too."""
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = make_client(handler)
    await client.create_pr_review(
        "o/r", 5, "h", "s", [comment("lib/auth.py", 10)], diff_text=DIFF
    )
    assert [c["line"] for c in sent[0]["comments"]] == [10]


@pytest.mark.asyncio
async def test_line_outside_every_hunk_goes_to_summary_not_a_nearby_line():
    """Findings GitHub would reject are listed in the body, unmoved."""
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = make_client(handler)
    await client.create_pr_review(
        "o/r", 5, "h", "SUMMARY",
        [comment("lib/auth.py", 25, "far from any hunk"),
         comment("other.py", 3, "file not in diff")],
        diff_text=DIFF,
    )
    payload = sent[0]
    assert payload["comments"] == []
    assert "SUMMARY" in payload["body"]
    assert "lib/auth.py:25" in payload["body"]
    assert "far from any hunk" in payload["body"]
    assert "other.py:3" in payload["body"]


@pytest.mark.asyncio
async def test_422_falls_back_to_summary_only_review():
    """If GitHub rejects the inline comments the feedback is not lost."""
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if body["comments"]:
            return httpx.Response(
                422, json={"message": "Unprocessable Entity"}
            )
        return httpx.Response(200, json={"id": 9})

    client = make_client(handler)
    result = await client.create_pr_review(
        "o/r", 5, "h", "SUMMARY", [comment("lib/auth.py", 11, "bug here")],
        diff_text=DIFF,
    )
    assert result == {"id": 9}
    assert len(sent) == 2
    assert sent[1]["comments"] == []
    assert "lib/auth.py:11" in sent[1]["body"]
    assert "bug here" in sent[1]["body"]


@pytest.mark.asyncio
async def test_other_http_errors_still_raise():
    """A 500 is not swallowed by the 422 fallback."""
    def handler(request):
        return httpx.Response(500)

    client = make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.create_pr_review(
            "o/r", 5, "h", "s", [comment("lib/auth.py", 11)],
            diff_text=DIFF,
        )


@pytest.mark.asyncio
async def test_review_body_is_capped_to_githubs_limit():
    """A huge pile of unmapped findings cannot exceed the body limit."""
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = make_client(handler)
    many = [comment("nope.py", i, "x" * 500) for i in range(400)]
    await client.create_pr_review("o/r", 5, "h", "s", many, diff_text=DIFF)
    assert len(sent[0]["body"]) <= MAX_REVIEW_BODY + 50
