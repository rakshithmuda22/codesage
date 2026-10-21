"""Fake GitHub + Groq HTTP APIs so the whole stack runs with no accounts.

Serves just enough of both APIs for one sample pull request
(``acme/shop#42``) to flow through CodeSage's real webhook -> Redis ->
RQ worker -> agents -> review pipeline:

* GitHub: installation token, PR files, file contents, PR diff, merged
  PR history and ``POST .../reviews``. The review endpoint behaves like
  GitHub: it answers 422 for a comment whose ``line`` is not part of the
  diff, so a wrong line mapping shows up as a failure here too.
* Groq: ``/openai/v1/chat/completions``. The "model" is a few regexes
  over the prompt, NOT a real LLM. It reads the line numbers CodeSage
  puts in the prompt, so the line numbers it returns are the ones the
  pipeline sent.

Run it (from the repo root)::

    uvicorn devtools.fake_services:app --port 9000

then point CodeSage at it with ``GITHUB_API_URL=http://localhost:9000``
and ``GROQ_BASE_URL=http://localhost:9000``. Nothing here is imported by
the application itself.
"""

from __future__ import annotations

import base64
import difflib
import json
import re
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

REPO = "acme/shop"
PR_NUMBER = 42
HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40

_FIND_USER = '''def find_user(db, username: str):
    """Look a user up by name."""
    if not username:
        return None
    key = username.strip().lower()
    return db.get(key)
'''

BASE_FILES: dict[str, str] = {
    "lib/auth.py": '''"""Authentication helpers."""
import hashlib


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


''' + _FIND_USER,
}

HEAD_FILES: dict[str, str] = {
    "lib/auth.py": '''"""Authentication helpers."""
import hashlib

API_KEY = "sk-live-9f8e7d6c5b4a3210"


def hash_password(password: str) -> str:
    return hashlib.md5(password.encode()).hexdigest()


''' + _FIND_USER + '''

def getUserProfile(db, username):
    query = f"SELECT * FROM users WHERE name = '{username}'"
    user = db.execute(query).fetchone()
    return user.profile.name
''',
    "web/cart.js": '''const TAX = 0.08;

function totalPrice(items) {
  let total = 0;
  for (let i = 0; i <= items.length; i++) {
    total += items[i].price;
  }
  return total * (1 + TAX);
}

function renderCart(el, html) {
  el.innerHTML = html;
}

module.exports = { totalPrice, renderCart };
''',
}

# Three earlier merged PRs: what the convention learner studies.
HISTORY_DIFF = '''diff --git a/lib/orders.py b/lib/orders.py
--- a/lib/orders.py
+++ b/lib/orders.py
@@ -0,0 +1,11 @@
+def load_order_items(order_id: int) -> list[dict]:
+    """Load the line items of an order.
+
+    Args:
+        order_id: Primary key of the order.
+
+    Returns:
+        One dict per line item.
+    """
+    return []
'''


def _lines(text: str) -> list[str]:
    return text.splitlines()


def build_pr_diff() -> str:
    """Render BASE_FILES -> HEAD_FILES as a GitHub-style unified diff."""
    out: list[str] = []
    for path in sorted(HEAD_FILES):
        old = BASE_FILES.get(path)
        new = HEAD_FILES[path]
        out.append(f"diff --git a/{path} b/{path}")
        if old is None:
            out.append("new file mode 100644")
        out.append("index 1111111..2222222" + ("" if old is None else " 100644"))
        out.append("--- /dev/null" if old is None else f"--- a/{path}")
        out.append(f"+++ b/{path}")
        hunks = difflib.unified_diff(
            _lines(old or ""), _lines(new), n=3, lineterm=""
        )
        out.extend(list(hunks)[2:])  # drop difflib's own ---/+++ lines
    return "\n".join(out) + "\n"


def patch_for(path: str) -> str:
    """The per-file ``patch`` string (starts at the first @@)."""
    diff = build_pr_diff()
    marker = f"+++ b/{path}\n"
    body = diff.split(marker, 1)[1]
    return body.split("\ndiff --git ", 1)[0].rstrip("\n")


def right_side_lines(path: str) -> set[int]:
    """New-file lines GitHub would accept a comment on.

    Computed from difflib's own hunk grouping, independently of
    CodeSage's DiffParser, so the two can disagree.
    """
    old = _lines(BASE_FILES.get(path, ""))
    new = _lines(HEAD_FILES[path])
    ok: set[int] = set()
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for group in matcher.get_grouped_opcodes(3):
        for tag, _i1, _i2, j1, j2 in group:
            if tag in ("equal", "insert", "replace"):
                ok.update(range(j1 + 1, j2 + 1))
    return ok


app = FastAPI(title="CodeSage fake GitHub + Groq")
reviews: list[dict[str, Any]] = []


def _log(message: str, **extra: Any) -> None:
    print(json.dumps({"agent": "FakeServices", "message": message, **extra}),
          flush=True)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


@app.post("/app/installations/{installation_id}/access_tokens")
async def access_token(installation_id: int, request: Request):
    auth = request.headers.get("authorization", "")
    ok = auth.startswith("Bearer ") and auth.count(".") == 2
    if not ok:
        return JSONResponse({"message": "A JSON web token is required"}, 401)
    _log("issued installation token (JWT looked well-formed)",
         installation_id=installation_id)
    return JSONResponse({"token": "ghs_fake", "expires_at": "2099-01-01"}, 201)


@app.get("/repos/{owner}/{repo}/pulls/{number}/files")
async def pr_files(owner: str, repo: str, number: int, page: int = 1):
    if page > 1:
        return []
    return [
        {
            "filename": path,
            "status": "modified" if path in BASE_FILES else "added",
            "additions": len(_lines(HEAD_FILES[path])),
            "deletions": 0,
            "patch": patch_for(path),
        }
        for path in sorted(HEAD_FILES)
    ]


@app.get("/repos/{owner}/{repo}/contents/{path:path}")
async def contents(owner: str, repo: str, path: str, ref: str = ""):
    if path not in HEAD_FILES:
        return JSONResponse({"message": "Not Found"}, 404)
    raw = HEAD_FILES[path].encode()
    return {
        "size": len(raw),
        "encoding": "base64",
        "content": base64.b64encode(raw).decode(),
    }


@app.get("/repos/{owner}/{repo}/pulls")
async def list_pulls(owner: str, repo: str, state: str = "closed"):
    return [
        {"number": n, "title": f"Merged change {n}", "merged_at": "2026-01-01"}
        for n in (41, 40, 39)
    ]


@app.get("/repos/{owner}/{repo}/pulls/{number}")
async def pull_diff(owner: str, repo: str, number: int):
    # CodeSage only asks this endpoint for the diff media type.
    if number == PR_NUMBER:
        return PlainTextResponse(build_pr_diff())
    return PlainTextResponse(HISTORY_DIFF)


@app.post("/repos/{owner}/{repo}/pulls/{number}/reviews")
async def create_review(owner: str, repo: str, number: int, request: Request):
    payload = await request.json()
    problems = []
    for c in payload.get("comments", []):
        path, line = c.get("path"), c.get("line")
        if "position" in c:
            problems.append(f"{path}: use line/side, not position")
        elif path not in HEAD_FILES or c.get("side") != "RIGHT":
            problems.append(f"{path}: not a RIGHT-side comment on a PR file")
        elif line not in right_side_lines(path):
            problems.append(f"{path}:{line} is not part of the diff")
    record = {
        "pr": number,
        "event": payload.get("event"),
        "commit_id": payload.get("commit_id"),
        "body": payload.get("body", ""),
        "comments": [
            {
                **c,
                "source": _lines(HEAD_FILES[c["path"]])[c["line"] - 1].strip()
                if c.get("path") in HEAD_FILES and c.get("line") else None,
            }
            for c in payload.get("comments", [])
        ],
        "rejected": problems,
    }
    if problems:
        _log("review REJECTED (422)", problems=problems)
        return JSONResponse(
            {"message": "Unprocessable Entity", "errors": problems}, 422
        )
    reviews.append(record)
    _log("review accepted", event=record["event"],
         inline_comments=len(record["comments"]))
    return {"id": len(reviews), "state": "COMMENTED"}


@app.get("/__reviews")
async def recorded_reviews():
    return reviews


# ---------------------------------------------------------------------------
# Groq (OpenAI-compatible chat completions)
# ---------------------------------------------------------------------------

NUMBERED = re.compile(r"^\s*(\d+) \| (.*)$", re.M)


def _find(prompt: str, pattern: str) -> int | None:
    """Line number (as printed in the prompt) of the first match."""
    for num, text in NUMBERED.findall(prompt):
        if re.search(pattern, text):
            return int(num)
    return None


def _finding(line: int, severity: str, category: str, title: str,
             description: str, suggestion: str, confidence: float = 0.9,
             ) -> dict[str, Any]:
    return {
        "line": line, "severity": severity, "category": category,
        "title": title, "description": description,
        "suggestion": suggestion, "confidence": confidence,
    }


def scripted_answer(system: str, prompt: str) -> dict[str, Any]:
    """What a competent reviewer would say about the sample PR."""
    findings: list[dict[str, Any]] = []
    if "finding bugs" in system:
        if (n := _find(prompt, r"<= items\.length")):
            findings.append(_finding(
                n, "HIGH", "off-by-one",
                "Loop runs one past the end of items",
                "i <= items.length reads items[items.length], which is "
                "undefined, so the next line throws a TypeError.",
                "Use i < items.length."))
        if (n := _find(prompt, r"f\"SELECT")):
            findings.append(_finding(
                n, "HIGH", "logic-error",
                "Unsafe f-string SQL query",
                "A username containing a quote breaks the query.",
                "Pass the value as a bound parameter."))
        if (n := _find(prompt, r"^\s+if not username:")):
            findings.append(_finding(
                n, "LOW", "error-handling",
                "Empty username silently returns None",
                "Callers cannot tell 'no such user' from 'bad input'.",
                "Raise ValueError for an empty username.", 0.7))
        if (n := _find(prompt, r"return user\.profile")):
            findings.append(_finding(
                n, "HIGH", "null-pointer",
                "fetchone() can return None",
                "user is None when no row matches, so user.profile "
                "raises AttributeError.",
                "Return early when user is None."))
    elif "security auditor" in system:
        if (n := _find(prompt, r"f\"SELECT")):
            findings.append(_finding(
                n, "CRITICAL", "sql-injection",
                "SQL injection through f-string query",
                "username is interpolated into the SQL string; "
                "' OR '1'='1 returns every row.",
                "Use a parameterised query: db.execute(sql, (username,))."))
    elif "code style reviewer" in system:
        if (n := _find(prompt, r"def [a-z]+[A-Z]\w*\(")):
            findings.append(_finding(
                n, "LOW", "naming-convention",
                "Function uses camelCase but repo uses snake_case",
                "Merged PRs in this repo name functions in snake_case "
                "(load_order_items).",
                "Rename getUserProfile to get_user_profile.", 0.85))
    elif "test coverage analyst" in system:
        if (n := _find(prompt, r"^(def|function) \w+")):
            findings.append(_finding(
                n, "MEDIUM", "missing-test",
                "No tests for the functions in this file",
                "This PR adds behaviour but no test file.",
                "Add tests for the happy path and for empty input.", 0.8))
    elif "code style analyzer" in system:
        return {
            "naming_style": "snake_case", "uses_type_hints": True,
            "uses_docstrings": True, "docstring_style": "google",
            "common_patterns": ["Google-style docstrings with Args/Returns"],
            "anti_patterns": ["Building SQL with string formatting"],
        }
    return {"findings": findings}


@app.post("/openai/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    system = next((m["content"] for m in body["messages"]
                   if m["role"] == "system"), "")
    user = next((m["content"] for m in body["messages"]
                 if m["role"] == "user"), "")
    answer = scripted_answer(system, user)
    _log("scripted LLM answered", findings=len(answer.get("findings", [])))
    return {
        "id": "fake", "object": "chat.completion", "created": int(time.time()),
        "model": body.get("model", "fake"),
        "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(answer)},
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                  "total_tokens": 0},
    }
