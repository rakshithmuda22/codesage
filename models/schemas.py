"""Pydantic models for all CodeSage data structures.

Defines the canonical schemas used across the entire codebase:
webhook payloads, agent findings, review summaries, and repo metadata.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """A single code review finding from an agent.

    Attributes:
        id: Unique identifier for the finding.
        agent: Name of the agent that produced this finding.
        file_path: Relative path from repo root.
        line_number: Exact line number for inline comment.
        end_line: End line for multi-line findings.
        severity: One of CRITICAL, HIGH, MEDIUM, LOW, INFO.
        category: Classification tag e.g. null-pointer, sql-injection.
        title: Short one-line description.
        description: Detailed explanation of the problem.
        suggestion: Concrete fix recommendation.
        code_snippet: The problematic code.
        fix_example: What the fixed code should look like.
        confidence: Confidence score from 0.0 to 1.0.
        contributing_agents: List of agents that flagged this
            (populated after deduplication merges cross-agent findings).
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent: str
    file_path: str
    line_number: int
    end_line: Optional[int] = None
    severity: str
    category: str
    title: str
    description: str
    suggestion: str
    code_snippet: Optional[str] = None
    fix_example: Optional[str] = None
    confidence: float = 0.7
    contributing_agents: list[str] = Field(default_factory=list)


class ReviewJob(BaseModel):
    """Represents a queued code review job.

    Attributes:
        job_id: Unique job identifier.
        repo_full_name: Repository in owner/repo format.
        pr_number: Pull request number.
        pr_title: Pull request title.
        head_sha: SHA of the PR head commit.
        base_sha: SHA of the PR base commit.
        installation_id: GitHub App installation ID.
        created_at: Timestamp when the job was created.
        status: Current job status.
    """

    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    repo_full_name: str
    pr_number: int
    pr_title: str
    head_sha: str
    base_sha: str
    installation_id: int
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "queued"


class AgentResult(BaseModel):
    """Result from a single agent's analysis run.

    Attributes:
        agent: Name of the agent.
        findings: List of findings produced.
        processing_time_seconds: Wall-clock time for analysis.
        files_analyzed: Number of files the agent processed.
        error: Error message if the agent failed.
    """

    agent: str
    findings: list[Finding] = Field(default_factory=list)
    processing_time_seconds: float = 0.0
    files_analyzed: int = 0
    error: Optional[str] = None


class ReviewSummary(BaseModel):
    """Summary of a complete code review.

    Attributes:
        job_id: The review job identifier.
        repo: Repository in owner/repo format.
        pr_number: Pull request number.
        total_findings: Total number of deduplicated findings.
        critical_count: Number of CRITICAL findings.
        high_count: Number of HIGH findings.
        medium_count: Number of MEDIUM findings.
        low_count: Number of LOW findings.
        files_reviewed: Number of files analyzed.
        agent_results: Results from each agent.
        review_decision: APPROVE, REQUEST_CHANGES, or COMMENT.
        executive_summary: Human-readable summary paragraph.
        processing_time_seconds: Total wall-clock time.
    """

    job_id: str
    repo: str
    pr_number: int
    total_findings: int = 0
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    files_reviewed: int = 0
    agent_results: list[AgentResult] = Field(default_factory=list)
    review_decision: str = "COMMENT"
    executive_summary: str = ""
    processing_time_seconds: float = 0.0


class RepoConventions(BaseModel):
    """Coding conventions learned from a repository's merged PRs.

    Attributes:
        naming_style: Variable/function naming convention.
        uses_type_hints: Whether the repo uses type annotations.
        uses_docstrings: Whether the repo uses docstrings.
        docstring_style: Docstring format (google, numpy, sphinx).
        max_function_length: Typical maximum function length in lines.
        uses_black_formatting: Whether code is Black-formatted.
        uses_async: Whether the codebase uses async/await patterns.
        test_naming_pattern: Test function naming pattern.
        common_patterns: Observed patterns from past PRs.
        anti_patterns: Things this repo explicitly avoids.
    """

    naming_style: str = "snake_case"
    uses_type_hints: bool = True
    uses_docstrings: bool = True
    docstring_style: str = "google"
    max_function_length: int = 50
    uses_black_formatting: bool = False
    uses_async: bool = False
    test_naming_pattern: str = "test_*"
    common_patterns: list[str] = Field(default_factory=list)
    anti_patterns: list[str] = Field(default_factory=list)


class ChangedFile(BaseModel):
    """A file changed in a pull request.

    Attributes:
        filename: Relative path from repo root.
        status: One of added, modified, removed.
        additions: Number of lines added.
        deletions: Number of lines deleted.
        patch: Unified diff patch for this file.
        content: Full file content (fetched separately).
        language: Detected programming language.
        impact_score: 0-1 score based on dependency graph centrality.
    """

    filename: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0
    patch: Optional[str] = None
    content: Optional[str] = None
    language: Optional[str] = None
    impact_score: float = 0.0
