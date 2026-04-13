"""Tests for the Deduplicator module.

Tests finding grouping, cross-agent merging, review decisions,
and GitHub formatting.
"""

from __future__ import annotations

from core.deduplicator import Deduplicator
from models.schemas import AgentResult, Finding


# ---------------------------------------------------------------------------
# Grouping and merging tests
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Tests for finding deduplication logic."""

    def test_nearby_findings_are_merged(
        self, sample_findings
    ):
        """Findings within 3 lines on same file are grouped.

        Args:
            sample_findings: Sample findings fixture.
        """
        dedup = Deduplicator()
        agent_results = [
            AgentResult(
                agent="BugDetector",
                findings=[sample_findings[0]],
                processing_time_seconds=1.0,
                files_analyzed=1,
            ),
            AgentResult(
                agent="SecurityScanner",
                findings=[sample_findings[3]],
                processing_time_seconds=1.0,
                files_analyzed=1,
            ),
        ]

        result = dedup.deduplicate(agent_results)
        assert len(result) < len(sample_findings[:2]) + 1

    def test_distant_findings_not_merged(self):
        """Findings far apart on same file stay separate."""
        dedup = Deduplicator()
        f1 = Finding(
            id="a",
            agent="BugDetector",
            file_path="src/main.py",
            line_number=10,
            severity="HIGH",
            category="null-pointer",
            title="Bug A",
            description="First issue",
            suggestion="Fix A",
            confidence=0.8,
        )
        f2 = Finding(
            id="b",
            agent="BugDetector",
            file_path="src/main.py",
            line_number=100,
            severity="MEDIUM",
            category="logic-error",
            title="Bug B",
            description="Second issue",
            suggestion="Fix B",
            confidence=0.7,
        )
        results = [AgentResult(
            agent="BugDetector",
            findings=[f1, f2],
            processing_time_seconds=1.0,
            files_analyzed=1,
        )]
        output = dedup.deduplicate(results)
        assert len(output) == 2

    def test_different_files_not_merged(self):
        """Findings on different files are never merged."""
        dedup = Deduplicator()
        f1 = Finding(
            id="a",
            agent="BugDetector",
            file_path="src/a.py",
            line_number=10,
            severity="HIGH",
            category="null-pointer",
            title="Null bug",
            description="Issue",
            suggestion="Fix",
            confidence=0.8,
        )
        f2 = Finding(
            id="b",
            agent="BugDetector",
            file_path="src/b.py",
            line_number=10,
            severity="HIGH",
            category="null-pointer",
            title="Null bug",
            description="Issue",
            suggestion="Fix",
            confidence=0.8,
        )
        results = [AgentResult(
            agent="BugDetector",
            findings=[f1, f2],
            processing_time_seconds=1.0,
            files_analyzed=2,
        )]
        output = dedup.deduplicate(results)
        assert len(output) == 2

    def test_cross_agent_merge_uses_highest_severity(self):
        """Merged findings use highest severity from any agent."""
        dedup = Deduplicator()
        f1 = Finding(
            id="a",
            agent="BugDetector",
            file_path="src/main.py",
            line_number=10,
            severity="MEDIUM",
            category="null-pointer",
            title="Possible null pointer access",
            description="Bug perspective",
            suggestion="Fix from bug",
            confidence=0.7,
        )
        f2 = Finding(
            id="b",
            agent="SecurityScanner",
            file_path="src/main.py",
            line_number=11,
            severity="CRITICAL",
            category="null-pointer",
            title="Null pointer security risk",
            description="Security perspective",
            suggestion="Fix from security",
            confidence=0.9,
        )
        results = [
            AgentResult(
                agent="BugDetector", findings=[f1],
                processing_time_seconds=1.0, files_analyzed=1,
            ),
            AgentResult(
                agent="SecurityScanner", findings=[f2],
                processing_time_seconds=1.0, files_analyzed=1,
            ),
        ]
        output = dedup.deduplicate(results)
        assert len(output) == 1
        assert output[0].severity == "CRITICAL"
        assert len(output[0].contributing_agents) == 2

    def test_cross_agent_merge_combines_descriptions(self):
        """Merged findings combine descriptions from all agents."""
        dedup = Deduplicator()
        f1 = Finding(
            id="a",
            agent="BugDetector",
            file_path="src/main.py",
            line_number=10,
            severity="HIGH",
            category="null-pointer",
            title="Null pointer access",
            description="Bug: may be None",
            suggestion="Check for None",
            confidence=0.8,
        )
        f2 = Finding(
            id="b",
            agent="SecurityScanner",
            file_path="src/main.py",
            line_number=11,
            severity="HIGH",
            category="null-pointer",
            title="Null pointer vulnerability",
            description="Security: exploitable crash",
            suggestion="Validate input",
            confidence=0.9,
        )
        results = [
            AgentResult(
                agent="BugDetector", findings=[f1],
                processing_time_seconds=1.0, files_analyzed=1,
            ),
            AgentResult(
                agent="SecurityScanner", findings=[f2],
                processing_time_seconds=1.0, files_analyzed=1,
            ),
        ]
        output = dedup.deduplicate(results)
        assert "BugDetector" in output[0].description
        assert "SecurityScanner" in output[0].description

    def test_empty_findings(self):
        """Deduplicator handles empty results gracefully."""
        dedup = Deduplicator()
        result = dedup.deduplicate([
            AgentResult(
                agent="BugDetector", findings=[],
                processing_time_seconds=0.5, files_analyzed=0,
            ),
        ])
        assert result == []


# ---------------------------------------------------------------------------
# Review decision tests
# ---------------------------------------------------------------------------


class TestReviewDecision:
    """Tests for review decision logic."""

    def test_critical_finding_requests_changes(self):
        """CRITICAL findings trigger REQUEST_CHANGES."""
        dedup = Deduplicator()
        findings = [Finding(
            id="a",
            agent="SecurityScanner",
            file_path="src/main.py",
            line_number=1,
            severity="CRITICAL",
            category="hardcoded-secret",
            title="Secret exposed",
            description="API key in source",
            suggestion="Use env var",
            confidence=0.95,
        )]
        assert dedup.determine_review_decision(findings) == "REQUEST_CHANGES"

    def test_high_finding_requests_changes(self):
        """HIGH findings trigger REQUEST_CHANGES."""
        dedup = Deduplicator()
        findings = [Finding(
            id="a",
            agent="BugDetector",
            file_path="src/main.py",
            line_number=1,
            severity="HIGH",
            category="null-pointer",
            title="Null deref",
            description="Will crash",
            suggestion="Fix",
            confidence=0.8,
        )]
        assert dedup.determine_review_decision(findings) == "REQUEST_CHANGES"

    def test_medium_only_comments(self):
        """Only MEDIUM/LOW/INFO findings trigger COMMENT."""
        dedup = Deduplicator()
        findings = [Finding(
            id="a",
            agent="StyleAdvisor",
            file_path="src/main.py",
            line_number=1,
            severity="MEDIUM",
            category="naming",
            title="Style issue",
            description="Not urgent",
            suggestion="Consider renaming",
            confidence=0.7,
        )]
        assert dedup.determine_review_decision(findings) == "COMMENT"

    def test_no_findings_approves(self):
        """No findings trigger APPROVE."""
        dedup = Deduplicator()
        assert dedup.determine_review_decision([]) == "APPROVE"


# ---------------------------------------------------------------------------
# Formatting tests
# ---------------------------------------------------------------------------


class TestFormatting:
    """Tests for GitHub comment formatting."""

    def test_format_includes_severity_emoji(self):
        """Formatted output includes severity emoji."""
        dedup = Deduplicator()
        finding = Finding(
            id="a",
            agent="BugDetector",
            file_path="src/main.py",
            line_number=10,
            severity="CRITICAL",
            category="null-pointer",
            title="Null bug",
            description="Bad bug",
            suggestion="Fix it",
            confidence=0.9,
            contributing_agents=["BugDetector"],
        )
        formatted = dedup.format_finding_for_github(finding)
        assert "CRITICAL" in formatted
        assert "Null bug" in formatted
        assert "CodeSage" in formatted

    def test_format_includes_fix_example(self):
        """Formatted output includes fix example when available."""
        dedup = Deduplicator()
        finding = Finding(
            id="a",
            agent="BugDetector",
            file_path="src/main.py",
            line_number=10,
            severity="HIGH",
            category="logic-error",
            title="Bug",
            description="Problem",
            suggestion="Fix",
            fix_example="if x is not None:\n    use(x)",
            confidence=0.8,
            contributing_agents=["BugDetector"],
        )
        formatted = dedup.format_finding_for_github(finding)
        assert "if x is not None:" in formatted
        assert "```" in formatted
