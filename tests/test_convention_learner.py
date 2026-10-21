"""Tests for ConventionLearner: what it fetches and how it merges results."""

from __future__ import annotations

from typing import Any

import pytest

from core.convention_learner import MAX_PRS_TO_ANALYZE, ConventionLearner
from models.schemas import RepoConventions

PY_DIFF = (
    "diff --git a/m.py b/m.py\n"
    "--- a/m.py\n"
    "+++ b/m.py\n"
    "@@ -0,0 +1,12 @@\n"
    "+def load_user_data(user_id: int) -> dict:\n"
    '+    """Load a user.\n'
    "+\n"
    "+    Args:\n"
    "+        user_id: Primary key.\n"
    "+\n"
    "+    Returns:\n"
    "+        The user record.\n"
    '+    """\n'
    "+    return {}\n"
)

JS_DIFF = (
    "diff --git a/m.js b/m.js\n"
    "--- a/m.js\n"
    "+++ b/m.js\n"
    "@@ -0,0 +1,3 @@\n"
    "+const loadUserData = async (userId) => {\n"
    "+  return fetchUser(userId);\n"
    "+};\n"
)

NO_REDIS = "redis://127.0.0.1:1"  # nothing listens here: caching is disabled


class FakeGitHub:
    def __init__(self, prs: list[dict[str, Any]] | None = None,
                 error: Exception | None = None):
        self.prs = prs or []
        self.error = error
        self.limit_seen: int | None = None

    async def get_recent_merged_prs(self, repo, limit=5):
        self.limit_seen = limit
        if self.error:
            raise self.error
        return self.prs


class FakeLLM:
    def __init__(self, answer: dict[str, Any]):
        self.answer = answer
        self.prompts: list[str] = []

    async def complete(self, system_prompt="", user_prompt="", **kwargs):
        self.prompts.append(user_prompt)
        return self.answer


def pr(diff: str, number: int = 1) -> dict[str, Any]:
    return {"number": number, "title": "t", "merged_at": "x", "diff": diff}


def test_learns_from_at_most_three_merged_prs():
    assert MAX_PRS_TO_ANALYZE == 3


@pytest.mark.asyncio
async def test_asks_github_for_three_prs_and_feeds_their_code_to_the_llm():
    gh = FakeGitHub([pr(PY_DIFF, 1), pr(PY_DIFF, 2), pr(PY_DIFF, 3)])
    llm = FakeLLM({"common_patterns": ["dataclasses"]})
    learner = ConventionLearner(gh, llm, redis_url=NO_REDIS)

    conventions = await learner.learn_conventions("acme/shop")

    assert gh.limit_seen == 3
    assert "load_user_data" in llm.prompts[0]
    assert conventions.common_patterns == ["dataclasses"]
    # Measured from the AST of the added code, not guessed by the LLM.
    assert conventions.naming_style == "snake_case"
    assert conventions.uses_type_hints is True
    assert conventions.uses_docstrings is True
    assert conventions.docstring_style == "google"


@pytest.mark.asyncio
async def test_no_merged_prs_gives_defaults():
    learner = ConventionLearner(
        FakeGitHub([]), FakeLLM({}), redis_url=NO_REDIS
    )
    assert await learner.learn_conventions("a/b") == RepoConventions()


@pytest.mark.asyncio
async def test_js_only_history_keeps_the_llms_camelcase_answer():
    """Python AST heuristics must not overwrite JS conventions."""
    llm = FakeLLM({"naming_style": "camelCase", "uses_type_hints": False})
    learner = ConventionLearner(
        FakeGitHub([pr(JS_DIFF)]), llm, redis_url=NO_REDIS
    )
    conventions = await learner.learn_conventions("acme/web")
    assert conventions.naming_style == "camelCase"


@pytest.mark.asyncio
async def test_malformed_llm_output_does_not_crash_the_review():
    llm = FakeLLM({"uses_black_formatting": "maybe-ish"})
    learner = ConventionLearner(
        FakeGitHub([pr(PY_DIFF)]), llm, redis_url=NO_REDIS
    )
    conventions = await learner.learn_conventions("acme/shop")
    assert isinstance(conventions, RepoConventions)
    assert conventions.naming_style == "snake_case"


@pytest.mark.asyncio
async def test_github_failure_falls_back_to_defaults():
    learner = ConventionLearner(
        FakeGitHub(error=RuntimeError("403")), FakeLLM({}),
        redis_url=NO_REDIS,
    )
    assert await learner.learn_conventions("a/b") == RepoConventions()
