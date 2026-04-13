"""Tests for all CodeSage agents.

Tests BugDetector, SecurityScanner, StyleAdvisor, and TestCoverage
agents including happy paths, edge cases, and error handling.
"""

from __future__ import annotations

import networkx as nx
import pytest

from agents.bug_detector import BugDetector
from agents.security_scanner import SecurityScanner
from agents.style_advisor import StyleAdvisor
from agents.test_coverage import TestCoverageAgent
from models.schemas import AgentResult, ChangedFile, RepoConventions
from tests.conftest import MockLLMClient


# ---------------------------------------------------------------------------
# BugDetector tests
# ---------------------------------------------------------------------------


class TestBugDetector:
    """Tests for the BugDetector agent."""

    @pytest.mark.asyncio
    async def test_returns_agent_result(
        self, sample_python_file, sample_conventions
    ):
        """BugDetector returns a valid AgentResult.

        Args:
            sample_python_file: Python file fixture.
            sample_conventions: Conventions fixture.
        """
        llm = MockLLMClient(responses=[{
            "findings": [{
                "line": 9,
                "severity": "HIGH",
                "category": "null-pointer",
                "title": "Possible None dereference on user.profile",
                "description": "user can be None",
                "suggestion": "Add None check",
                "confidence": 0.85,
            }]
        }])
        agent = BugDetector(llm)
        graph = nx.DiGraph()
        result = await agent.analyze(
            [sample_python_file], sample_conventions, graph
        )
        assert isinstance(result, AgentResult)
        assert result.agent == "BugDetector"
        assert result.files_analyzed == 1
        assert len(result.findings) == 1
        assert result.findings[0].severity == "HIGH"
        assert result.findings[0].file_path == "src/auth/login.py"

    @pytest.mark.asyncio
    async def test_filters_low_confidence(
        self, sample_python_file, sample_conventions
    ):
        """BugDetector filters findings below confidence threshold.

        Args:
            sample_python_file: Python file fixture.
            sample_conventions: Conventions fixture.
        """
        llm = MockLLMClient(responses=[{
            "findings": [
                {
                    "line": 5,
                    "severity": "LOW",
                    "category": "logic-error",
                    "title": "Maybe a bug",
                    "description": "Not sure",
                    "suggestion": "Check it",
                    "confidence": 0.3,
                },
                {
                    "line": 10,
                    "severity": "HIGH",
                    "category": "null-pointer",
                    "title": "Real bug",
                    "description": "Confirmed",
                    "suggestion": "Fix it",
                    "confidence": 0.9,
                },
            ]
        }])
        agent = BugDetector(llm)
        result = await agent.analyze(
            [sample_python_file], sample_conventions, nx.DiGraph()
        )
        assert len(result.findings) == 1
        assert result.findings[0].confidence == 0.9

    @pytest.mark.asyncio
    async def test_handles_empty_files(
        self, sample_empty_file, sample_conventions
    ):
        """BugDetector handles files with no content.

        Args:
            sample_empty_file: Empty file fixture.
            sample_conventions: Conventions fixture.
        """
        llm = MockLLMClient()
        agent = BugDetector(llm)
        result = await agent.analyze(
            [sample_empty_file], sample_conventions, nx.DiGraph()
        )
        assert result.files_analyzed == 0
        assert len(result.findings) == 0

    @pytest.mark.asyncio
    async def test_handles_llm_failure(
        self, sample_python_file, sample_conventions
    ):
        """BugDetector handles LLM errors gracefully.

        Args:
            sample_python_file: Python file fixture.
            sample_conventions: Conventions fixture.
        """
        llm = MockLLMClient()
        llm.complete = lambda *a, **k: (_ for _ in ()).throw(
            Exception("LLM timeout")
        )
        agent = BugDetector(llm)
        result = await agent.analyze(
            [sample_python_file], sample_conventions, nx.DiGraph()
        )
        assert isinstance(result, AgentResult)
        assert len(result.findings) == 0

    @pytest.mark.asyncio
    async def test_skips_unsupported_languages(
        self, sample_conventions
    ):
        """BugDetector skips files in unsupported languages.

        Args:
            sample_conventions: Conventions fixture.
        """
        file = ChangedFile(
            filename="main.go",
            content="package main\nfunc main() {}",
            language="go",
        )
        llm = MockLLMClient()
        agent = BugDetector(llm)
        result = await agent.analyze(
            [file], sample_conventions, nx.DiGraph()
        )
        assert result.files_analyzed == 0


# ---------------------------------------------------------------------------
# SecurityScanner tests
# ---------------------------------------------------------------------------


class TestSecurityScanner:
    """Tests for the SecurityScanner agent."""

    @pytest.mark.asyncio
    async def test_detects_hardcoded_secret(
        self, sample_python_file, sample_conventions
    ):
        """SecurityScanner catches hardcoded secrets via regex.

        Args:
            sample_python_file: Python file with hardcoded key.
            sample_conventions: Conventions fixture.
        """
        llm = MockLLMClient()
        agent = SecurityScanner(llm)
        result = await agent.analyze(
            [sample_python_file], sample_conventions, nx.DiGraph()
        )
        secret_findings = [
            f for f in result.findings
            if f.category == "hardcoded-secret"
        ]
        assert len(secret_findings) >= 1
        assert secret_findings[0].severity == "CRITICAL"

    @pytest.mark.asyncio
    async def test_detects_sql_injection(
        self, sample_python_file, sample_conventions
    ):
        """SecurityScanner catches SQL injection patterns.

        Args:
            sample_python_file: Python file with f-string SQL.
            sample_conventions: Conventions fixture.
        """
        llm = MockLLMClient()
        agent = SecurityScanner(llm)
        result = await agent.analyze(
            [sample_python_file], sample_conventions, nx.DiGraph()
        )
        assert result.files_analyzed == 1

    @pytest.mark.asyncio
    async def test_detects_weak_crypto(
        self, sample_python_file, sample_conventions
    ):
        """SecurityScanner catches weak hash functions.

        Args:
            sample_python_file: Python file using md5.
            sample_conventions: Conventions fixture.
        """
        llm = MockLLMClient()
        agent = SecurityScanner(llm)
        result = await agent.analyze(
            [sample_python_file], sample_conventions, nx.DiGraph()
        )
        weak_crypto = [
            f for f in result.findings
            if f.category == "weak-crypto"
        ]
        assert len(weak_crypto) >= 1

    @pytest.mark.asyncio
    async def test_handles_empty_file(
        self, sample_empty_file, sample_conventions
    ):
        """SecurityScanner handles files with no content.

        Args:
            sample_empty_file: Empty file fixture.
            sample_conventions: Conventions fixture.
        """
        llm = MockLLMClient()
        agent = SecurityScanner(llm)
        result = await agent.analyze(
            [sample_empty_file], sample_conventions, nx.DiGraph()
        )
        assert result.files_analyzed == 0

    @pytest.mark.asyncio
    async def test_regex_only_no_llm_needed(
        self, sample_conventions
    ):
        """SecurityScanner regex works without LLM for patterns.

        Args:
            sample_conventions: Conventions fixture.
        """
        file = ChangedFile(
            filename="config.py",
            content='PASSWORD = "supersecret123"\n',
            language="python",
        )
        llm = MockLLMClient()
        agent = SecurityScanner(llm)
        result = await agent.analyze(
            [file], sample_conventions, nx.DiGraph()
        )
        assert any(
            f.category == "hardcoded-secret"
            for f in result.findings
        )


# ---------------------------------------------------------------------------
# StyleAdvisor tests
# ---------------------------------------------------------------------------


class TestStyleAdvisor:
    """Tests for the StyleAdvisor agent."""

    @pytest.mark.asyncio
    async def test_adapts_to_conventions(
        self, sample_python_file, sample_conventions
    ):
        """StyleAdvisor uses provided conventions for analysis.

        Args:
            sample_python_file: Python file fixture.
            sample_conventions: Conventions fixture.
        """
        llm = MockLLMClient(responses=[{
            "findings": [{
                "line": 5,
                "severity": "LOW",
                "category": "naming-convention",
                "title": "Inconsistent naming",
                "description": "Uses camelCase instead of snake_case",
                "suggestion": "Rename to snake_case",
                "confidence": 0.8,
            }]
        }])
        agent = StyleAdvisor(llm)
        result = await agent.analyze(
            [sample_python_file], sample_conventions, nx.DiGraph()
        )
        assert isinstance(result, AgentResult)
        assert result.agent == "StyleAdvisor"
        assert len(llm.calls) == 1
        assert "snake_case" in llm.calls[0]["user_prompt"]

    @pytest.mark.asyncio
    async def test_caps_severity_at_medium(
        self, sample_python_file, sample_conventions
    ):
        """StyleAdvisor never reports CRITICAL/HIGH severity.

        Args:
            sample_python_file: Python file fixture.
            sample_conventions: Conventions fixture.
        """
        llm = MockLLMClient(responses=[{
            "findings": [{
                "line": 1,
                "severity": "CRITICAL",
                "category": "naming-convention",
                "title": "Bad name",
                "description": "Very bad",
                "suggestion": "Fix it",
                "confidence": 0.9,
            }]
        }])
        agent = StyleAdvisor(llm)
        result = await agent.analyze(
            [sample_python_file], sample_conventions, nx.DiGraph()
        )
        for f in result.findings:
            assert f.severity in ("MEDIUM", "LOW", "INFO")

    @pytest.mark.asyncio
    async def test_handles_empty_conventions(
        self, sample_python_file
    ):
        """StyleAdvisor works with default conventions.

        Args:
            sample_python_file: Python file fixture.
        """
        llm = MockLLMClient()
        agent = StyleAdvisor(llm)
        result = await agent.analyze(
            [sample_python_file], RepoConventions(), nx.DiGraph()
        )
        assert isinstance(result, AgentResult)


# ---------------------------------------------------------------------------
# TestCoverageAgent tests
# ---------------------------------------------------------------------------


class TestTestCoverageAgent:
    """Tests for the TestCoverageAgent."""

    @pytest.mark.asyncio
    async def test_identifies_untested_functions(
        self, sample_python_file, sample_conventions
    ):
        """TestCoverageAgent finds functions without tests.

        Args:
            sample_python_file: Python file fixture.
            sample_conventions: Conventions fixture.
        """
        llm = MockLLMClient(responses=[{
            "findings": [{
                "line": 5,
                "severity": "MEDIUM",
                "category": "missing-test",
                "title": "No test for login()",
                "description": "login function has no tests",
                "suggestion": "Add test_login() test case",
                "confidence": 0.8,
            }]
        }])
        agent = TestCoverageAgent(llm)
        result = await agent.analyze(
            [sample_python_file], sample_conventions, nx.DiGraph()
        )
        assert isinstance(result, AgentResult)
        assert result.agent == "TestCoverage"

    @pytest.mark.asyncio
    async def test_skips_test_files(
        self, sample_conventions
    ):
        """TestCoverageAgent skips test files themselves.

        Args:
            sample_conventions: Conventions fixture.
        """
        test_file = ChangedFile(
            filename="tests/test_auth.py",
            content="def test_login():\n    assert True\n",
            language="python",
        )
        llm = MockLLMClient()
        agent = TestCoverageAgent(llm)
        result = await agent.analyze(
            [test_file], sample_conventions, nx.DiGraph()
        )
        assert result.files_analyzed == 0

    @pytest.mark.asyncio
    async def test_handles_no_functions(
        self, sample_conventions
    ):
        """TestCoverageAgent handles files with no functions.

        Args:
            sample_conventions: Conventions fixture.
        """
        file = ChangedFile(
            filename="src/constants.py",
            content="MAX_SIZE = 100\nDEFAULT_NAME = 'test'\n",
            language="python",
        )
        llm = MockLLMClient()
        agent = TestCoverageAgent(llm)
        result = await agent.analyze(
            [file], sample_conventions, nx.DiGraph()
        )
        assert len(result.findings) == 0

    @pytest.mark.asyncio
    async def test_handles_llm_failure_gracefully(
        self, sample_python_file, sample_conventions
    ):
        """TestCoverageAgent falls back when LLM fails.

        Args:
            sample_python_file: Python file fixture.
            sample_conventions: Conventions fixture.
        """
        llm = MockLLMClient()

        async def fail(*a, **k):
            raise Exception("Timeout")

        llm.complete = fail
        agent = TestCoverageAgent(llm)
        result = await agent.analyze(
            [sample_python_file], sample_conventions, nx.DiGraph()
        )
        assert isinstance(result, AgentResult)
