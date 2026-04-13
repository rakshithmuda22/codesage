"""Finding deduplicator that merges and ranks cross-agent results.

When multiple agents flag overlapping issues (e.g., BugDetector and
SecurityScanner both flag the same line), this module merges them
into a single finding with combined agent tags and the highest
severity.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from models.schemas import AgentResult, Finding

logger = logging.getLogger("codesage")

SEVERITY_ORDER = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "INFO": 4,
}

SEVERITY_EMOJI = {
    "CRITICAL": "\U0001f6a8",
    "HIGH": "\u26a0\ufe0f",
    "MEDIUM": "\U0001f4a1",
    "LOW": "\u2139\ufe0f",
    "INFO": "\U0001f4dd",
}

LINE_PROXIMITY_THRESHOLD = 3


class Deduplicator:
    """Deduplicates, merges, and ranks findings from multiple agents.

    Groups findings that are within 3 lines of each other on the
    same file, merges them using the highest severity, and produces
    combined agent tags (e.g., [SECURITY][BUG]).
    """

    def deduplicate(
        self, agent_results: list[AgentResult]
    ) -> list[Finding]:
        """Deduplicate findings across all agent results.

        Collects all findings, groups nearby ones on the same file,
        merges each group, and sorts by severity.

        Args:
            agent_results: List of AgentResult from each agent.

        Returns:
            Sorted list of deduplicated findings (CRITICAL first).
        """
        start = time.time()
        all_findings = [
            f for r in agent_results for f in r.findings
        ]

        if not all_findings:
            return []

        groups = self._group_nearby_findings(all_findings)
        merged = [self._merge_group(g) for g in groups]

        result = sorted(
            merged,
            key=lambda f: SEVERITY_ORDER.get(f.severity, 5),
        )

        elapsed = time.time() - start
        logger.info(
            json.dumps({
                "timestamp": time.time(),
                "level": "INFO",
                "agent": "Deduplicator",
                "job_id": "",
                "message": "Deduplication complete",
                "input_findings": len(all_findings),
                "output_findings": len(result),
                "groups_merged": len(all_findings) - len(result),
                "elapsed_s": round(elapsed, 3),
            })
        )
        return result

    def _group_nearby_findings(
        self, findings: list[Finding]
    ) -> list[list[Finding]]:
        """Group findings that are on the same file within 3 lines.

        Uses O(n^2) clustering — acceptable for portfolio-scale
        codebases where finding counts are typically < 100.

        Args:
            findings: All findings from all agents.

        Returns:
            List of finding groups (each group is merged later).
        """
        used = set()
        groups: list[list[Finding]] = []

        for i, f1 in enumerate(findings):
            if i in used:
                continue

            group = [f1]
            used.add(i)

            for j, f2 in enumerate(findings):
                if j in used:
                    continue
                if (
                    f1.file_path == f2.file_path
                    and abs(f1.line_number - f2.line_number)
                    <= LINE_PROXIMITY_THRESHOLD
                    and self._is_similar(f1, f2)
                ):
                    group.append(f2)
                    used.add(j)

            groups.append(group)

        return groups

    @staticmethod
    def _is_similar(f1: Finding, f2: Finding) -> bool:
        """Check if two findings describe the same underlying issue.

        Compares titles and categories for overlap. Two findings
        are similar if they share words in their titles or have
        the same category.

        Args:
            f1: First finding.
            f2: Second finding.

        Returns:
            True if the findings appear to describe the same issue.
        """
        if f1.category == f2.category:
            return True

        words1 = set(f1.title.lower().split())
        words2 = set(f2.title.lower().split())
        overlap = words1 & words2
        meaningful = overlap - {
            "the", "a", "an", "in", "on", "at", "to", "for",
            "is", "of", "and", "or", "this", "that",
        }
        return len(meaningful) >= 2

    @staticmethod
    def _merge_group(group: list[Finding]) -> Finding:
        """Merge a group of similar findings into one.

        Takes the highest severity, highest confidence, merges
        descriptions, and creates combined agent tags.

        Args:
            group: List of findings to merge.

        Returns:
            Single merged Finding with combined metadata.
        """
        if len(group) == 1:
            finding = group[0]
            finding.contributing_agents = [finding.agent]
            return finding

        best = min(
            group,
            key=lambda f: SEVERITY_ORDER.get(f.severity, 5),
        )

        agents = list({f.agent for f in group})
        agent_tags = "".join(
            f"[{a.upper().replace('AGENT', '').strip()}]"
            for a in sorted(agents)
        )

        descriptions = []
        for f in group:
            prefix = f"**{f.agent}**: "
            descriptions.append(prefix + f.description)
        merged_description = "\n\n".join(descriptions)

        suggestions = list({f.suggestion for f in group})
        merged_suggestion = " | ".join(suggestions)

        max_confidence = max(f.confidence for f in group)

        fix_example = next(
            (f.fix_example for f in group if f.fix_example), None
        )
        code_snippet = next(
            (f.code_snippet for f in group if f.code_snippet), None
        )

        return Finding(
            id=best.id,
            agent=agent_tags,
            file_path=best.file_path,
            line_number=best.line_number,
            end_line=best.end_line,
            severity=best.severity,
            category=best.category,
            title=best.title,
            description=merged_description,
            suggestion=merged_suggestion,
            code_snippet=code_snippet,
            fix_example=fix_example,
            confidence=max_confidence,
            contributing_agents=agents,
        )

    @staticmethod
    def determine_review_decision(findings: list[Finding]) -> str:
        """Determine the overall review decision based on findings.

        Args:
            findings: Deduplicated findings list.

        Returns:
            One of: APPROVE, REQUEST_CHANGES, COMMENT.
        """
        if not findings:
            return "APPROVE"

        severities = {f.severity for f in findings}
        if "CRITICAL" in severities or "HIGH" in severities:
            return "REQUEST_CHANGES"

        return "COMMENT"

    @staticmethod
    def format_finding_for_github(finding: Finding) -> str:
        """Format a finding as a GitHub-flavored markdown comment.

        Includes severity emoji, title, description, suggestion,
        and fix example if available.

        Args:
            finding: Finding to format.

        Returns:
            Markdown-formatted string for GitHub PR comment.
        """
        emoji = SEVERITY_EMOJI.get(finding.severity, "\U0001f4dd")
        agents = finding.contributing_agents or [finding.agent]
        agent_label = ", ".join(agents)

        lines = [
            f"**{emoji} {finding.severity}: {finding.title}**",
            "",
            finding.description,
            "",
            f"**Suggestion:** {finding.suggestion}",
        ]

        if finding.fix_example:
            lines.extend([
                "",
                "```",
                finding.fix_example,
                "```",
            ])

        lines.extend([
            "",
            f"*Detected by CodeSage {agent_label}*",
        ])

        return "\n".join(lines)

    @staticmethod
    def generate_executive_summary(
        findings: list[Finding],
        agent_results: list[AgentResult],
        job_dict: dict[str, Any],
    ) -> str:
        """Generate a summary review comment for the PR.

        Args:
            findings: Deduplicated findings list.
            agent_results: Results from each agent.
            job_dict: Review job metadata.

        Returns:
            Markdown-formatted executive summary string.
        """
        severity_counts = {}
        for f in findings:
            severity_counts[f.severity] = (
                severity_counts.get(f.severity, 0) + 1
            )

        total = len(findings)
        critical = severity_counts.get("CRITICAL", 0)
        high = severity_counts.get("HIGH", 0)
        medium = severity_counts.get("MEDIUM", 0)
        low = severity_counts.get("LOW", 0)
        info = severity_counts.get("INFO", 0)

        files_reviewed = sum(
            r.files_analyzed for r in agent_results
            if isinstance(r, AgentResult)
        )
        total_time = sum(
            r.processing_time_seconds for r in agent_results
            if isinstance(r, AgentResult)
        )

        lines = [
            "## \U0001f9d9 CodeSage Review Summary",
            "",
            f"**PR:** {job_dict.get('pr_title', 'N/A')}",
            f"**Repository:** {job_dict.get('repo_full_name', 'N/A')}",
            "",
            "### Findings",
            "",
            "| Severity | Count |",
            "|----------|-------|",
            f"| \U0001f6a8 Critical | {critical} |",
            f"| \u26a0\ufe0f High | {high} |",
            f"| \U0001f4a1 Medium | {medium} |",
            f"| \u2139\ufe0f Low | {low} |",
            f"| \U0001f4dd Info | {info} |",
            f"| **Total** | **{total}** |",
            "",
            "### Agent Performance",
            "",
            "| Agent | Findings | Time |",
            "|-------|----------|------|",
        ]

        for r in agent_results:
            if isinstance(r, AgentResult):
                lines.append(
                    f"| {r.agent} | {len(r.findings)} "
                    f"| {r.processing_time_seconds:.1f}s |"
                )

        lines.extend([
            "",
            f"**Files reviewed:** {files_reviewed}",
            f"**Total analysis time:** {total_time:.1f}s",
            "",
            "---",
            "*Powered by CodeSage — Autonomous Code Review Agent*",
        ])

        return "\n".join(lines)
