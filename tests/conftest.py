"""Shared test fixtures for CodeSage test suite.

Provides mock clients, sample data objects, and reusable
fixtures for all test modules.
"""

from __future__ import annotations

import asyncio
from typing import Any
import pytest

from models.schemas import (
    AgentResult,
    ChangedFile,
    Finding,
    RepoConventions,
)


# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for async tests.

    Returns:
        Asyncio event loop instance.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Mock LLM Client
# ---------------------------------------------------------------------------


class MockLLMClient:
    """Mock LLM client that returns configurable responses.

    Attributes:
        responses: Queue of responses to return on successive calls.
        calls: List of recorded call arguments.
    """

    def __init__(
        self, responses: list[dict[str, Any]] | None = None
    ) -> None:
        """Initialize with optional preset responses.

        Args:
            responses: List of dicts to return from complete().
        """
        self.responses = responses or [{"findings": []}]
        self._call_index = 0
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        system_prompt: str = "",
        user_prompt: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.1,
        response_format: str = "json",
    ) -> dict[str, Any]:
        """Mock completion that returns preset responses.

        Args:
            system_prompt: System prompt (recorded).
            user_prompt: User prompt (recorded).
            max_tokens: Max tokens (ignored).
            temperature: Temperature (ignored).
            response_format: Format (ignored).

        Returns:
            Next preset response dict.
        """
        self.calls.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        })
        if self._call_index < len(self.responses):
            response = self.responses[self._call_index]
            self._call_index += 1
            return response
        return {"findings": []}

    async def complete_with_structured_output(
        self,
        system_prompt: str = "",
        user_prompt: str = "",
        output_schema: dict | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        """Mock structured completion.

        Args:
            system_prompt: System prompt.
            user_prompt: User prompt.
            output_schema: Schema (ignored).
            max_tokens: Max tokens (ignored).
            temperature: Temperature (ignored).

        Returns:
            Next preset response dict.
        """
        return await self.complete(system_prompt, user_prompt)


@pytest.fixture
def mock_llm_client():
    """Fixture for a mock LLM client with default empty responses.

    Returns:
        MockLLMClient instance.
    """
    return MockLLMClient()


@pytest.fixture
def mock_llm_with_findings():
    """Fixture for a mock LLM client with sample findings.

    Returns:
        MockLLMClient with preset findings responses.
    """
    return MockLLMClient(responses=[
        {
            "findings": [
                {
                    "line": 10,
                    "severity": "HIGH",
                    "category": "null-pointer",
                    "title": "Possible None dereference",
                    "description": "Variable may be None",
                    "suggestion": "Add a None check",
                    "fix_example": "if x is not None:",
                    "confidence": 0.85,
                },
                {
                    "line": 25,
                    "severity": "MEDIUM",
                    "category": "logic-error",
                    "title": "Off-by-one in loop",
                    "description": "Loop range is incorrect",
                    "suggestion": "Use range(len(items))",
                    "confidence": 0.7,
                },
            ]
        }
    ])


# ---------------------------------------------------------------------------
# Mock GitHub Client
# ---------------------------------------------------------------------------


class MockGitHubClient:
    """Mock GitHub client for testing without API calls.

    Attributes:
        files: Preset list of ChangedFile objects.
        reviews_posted: List of recorded review post calls.
    """

    def __init__(
        self, files: list[ChangedFile] | None = None
    ) -> None:
        """Initialize with optional preset files.

        Args:
            files: List of ChangedFile to return from get_pr_files.
        """
        self.files = files or []
        self.reviews_posted: list[dict[str, Any]] = []

    async def get_pr_files(
        self, repo: str, pr_number: int, head_sha: str = ""
    ) -> list[ChangedFile]:
        """Return preset files.

        Args:
            repo: Repository name (ignored).
            pr_number: PR number (ignored).
            head_sha: Commit SHA (ignored).

        Returns:
            Preset list of ChangedFile.
        """
        return self.files

    async def get_pr_diff(self, repo: str, pr_number: int) -> str:
        """Return a sample diff.

        Args:
            repo: Repository name (ignored).
            pr_number: PR number (ignored).

        Returns:
            Sample unified diff string.
        """
        return (
            "diff --git a/src/main.py b/src/main.py\n"
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -1,5 +1,10 @@\n"
            " import os\n"
            "+import sys\n"
            " \n"
            " def main():\n"
            "+    x = None\n"
            "+    print(x.value)\n"
            "     pass\n"
        )

    async def get_recent_merged_prs(
        self, repo: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Return empty merged PR list.

        Args:
            repo: Repository name (ignored).
            limit: Max PRs (ignored).

        Returns:
            Empty list.
        """
        return []

    async def create_pr_review(self, **kwargs: Any) -> dict[str, Any]:
        """Record a review post call.

        Args:
            **kwargs: Review parameters.

        Returns:
            Mock success response.
        """
        self.reviews_posted.append(kwargs)
        return {"id": 1, "state": "COMMENTED"}

    async def get_file_content(
        self, repo: str, path: str, ref: str
    ) -> str:
        """Return empty content.

        Args:
            repo: Repository name (ignored).
            path: File path (ignored).
            ref: Git ref (ignored).

        Returns:
            Empty string.
        """
        return ""

    async def clone_repo_to_temp(
        self, repo: str, sha: str
    ) -> str:
        """Return a fake temp directory.

        Args:
            repo: Repository name (ignored).
            sha: Commit SHA (ignored).

        Returns:
            Fake temp directory path.
        """
        return "/tmp/fake-clone"

    async def close(self) -> None:
        """No-op close.

        Does nothing in the mock.
        """
        pass


@pytest.fixture
def mock_github_client():
    """Fixture for a mock GitHub client.

    Returns:
        MockGitHubClient instance.
    """
    return MockGitHubClient()


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_python_file() -> ChangedFile:
    """Fixture for a sample Python changed file.

    Returns:
        ChangedFile with Python code content.
    """
    return ChangedFile(
        filename="src/auth/login.py",
        status="modified",
        additions=15,
        deletions=3,
        patch="@@ -1,10 +1,25 @@\n+def login(user):\n+    pass",
        content=(
            "import os\n"
            "import hashlib\n"
            "\n"
            "\n"
            "def login(username, password):\n"
            '    query = f"SELECT * FROM users '
            'WHERE name=\'{username}\'"\n'
            "    user = db.execute(query)\n"
            "    if user:\n"
            "        return user.profile.name\n"
            "    return None\n"
            "\n"
            "\n"
            "def hash_password(password):\n"
            "    return hashlib.md5(password.encode()).hexdigest()\n"
            "\n"
            "\n"
            'API_KEY = "sk-1234567890abcdef"\n'
            "\n"
            "\n"
            "def get_user(user_id):\n"
            "    user = find_user(user_id)\n"
            "    return user.name\n"
        ),
        language="python",
        impact_score=0.5,
    )


@pytest.fixture
def sample_js_file() -> ChangedFile:
    """Fixture for a sample JavaScript changed file.

    Returns:
        ChangedFile with JavaScript code content.
    """
    return ChangedFile(
        filename="src/utils/helpers.js",
        status="added",
        additions=20,
        deletions=0,
        content=(
            "const axios = require('axios');\n"
            "\n"
            "function fetchData(url) {\n"
            "    return axios.get(url);\n"
            "}\n"
            "\n"
            "function processItems(items) {\n"
            "    for (let i = 0; i <= items.length; i++) {\n"
            "        console.log(items[i].name);\n"
            "    }\n"
            "}\n"
            "\n"
            "module.exports = { fetchData, processItems };\n"
        ),
        language="javascript",
        impact_score=0.3,
    )


@pytest.fixture
def sample_empty_file() -> ChangedFile:
    """Fixture for a file with no content.

    Returns:
        ChangedFile with None content.
    """
    return ChangedFile(
        filename="src/empty.py",
        status="added",
        additions=0,
        deletions=0,
        content=None,
        language="python",
    )


@pytest.fixture
def sample_binary_file() -> ChangedFile:
    """Fixture for a binary file that should be skipped.

    Returns:
        ChangedFile representing a binary file.
    """
    return ChangedFile(
        filename="assets/logo.png",
        status="added",
        additions=0,
        deletions=0,
        content=None,
        language=None,
    )


@pytest.fixture
def sample_conventions() -> RepoConventions:
    """Fixture for sample repository conventions.

    Returns:
        RepoConventions with typical Python project settings.
    """
    return RepoConventions(
        naming_style="snake_case",
        uses_type_hints=True,
        uses_docstrings=True,
        docstring_style="google",
        max_function_length=50,
        uses_black_formatting=True,
        uses_async=False,
        test_naming_pattern="test_*",
        common_patterns=[
            "Use dataclasses for data objects",
            "Type hints on all public functions",
        ],
        anti_patterns=[
            "Global mutable state",
            "Bare except clauses",
        ],
    )


@pytest.fixture
def sample_findings() -> list[Finding]:
    """Fixture for a list of sample findings.

    Returns:
        List of Finding objects with various severities.
    """
    return [
        Finding(
            id="f1",
            agent="BugDetector",
            file_path="src/main.py",
            line_number=10,
            severity="HIGH",
            category="null-pointer",
            title="Possible None dereference",
            description="Variable x may be None at line 10",
            suggestion="Add None check",
            confidence=0.85,
        ),
        Finding(
            id="f2",
            agent="SecurityScanner",
            file_path="src/main.py",
            line_number=11,
            severity="CRITICAL",
            category="hardcoded-secret",
            title="Hardcoded API key",
            description="API key is hardcoded in source",
            suggestion="Use environment variable",
            confidence=0.95,
        ),
        Finding(
            id="f3",
            agent="StyleAdvisor",
            file_path="src/utils.py",
            line_number=5,
            severity="LOW",
            category="naming-convention",
            title="camelCase function name",
            description="Function uses camelCase",
            suggestion="Use snake_case",
            confidence=0.8,
        ),
        Finding(
            id="f4",
            agent="BugDetector",
            file_path="src/main.py",
            line_number=12,
            severity="HIGH",
            category="null-pointer",
            title="Unsafe access on possibly None",
            description="Similar null issue near line 10",
            suggestion="Add validation",
            confidence=0.8,
        ),
    ]


@pytest.fixture
def sample_agent_results(
    sample_findings: list[Finding],
) -> list[AgentResult]:
    """Fixture for sample agent results.

    Args:
        sample_findings: Injected findings fixture.

    Returns:
        List of AgentResult objects.
    """
    return [
        AgentResult(
            agent="BugDetector",
            findings=[sample_findings[0], sample_findings[3]],
            processing_time_seconds=2.5,
            files_analyzed=3,
        ),
        AgentResult(
            agent="SecurityScanner",
            findings=[sample_findings[1]],
            processing_time_seconds=1.8,
            files_analyzed=3,
        ),
        AgentResult(
            agent="StyleAdvisor",
            findings=[sample_findings[2]],
            processing_time_seconds=1.2,
            files_analyzed=3,
        ),
        AgentResult(
            agent="TestCoverage",
            findings=[],
            processing_time_seconds=0.9,
            files_analyzed=3,
        ),
    ]
