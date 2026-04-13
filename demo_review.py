"""Demo script: Run CodeSage agents against a local codebase.

Simulates the full review pipeline without GitHub webhooks.
Reads files directly from disk and runs all 4 agents in parallel.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from agents.bug_detector import BugDetector
from agents.security_scanner import SecurityScanner
from agents.style_advisor import StyleAdvisor
from agents.test_coverage import TestCoverageAgent
from core.deduplicator import Deduplicator
from core.dependency_graph import DependencyGraphBuilder
from models.schemas import AgentResult, ChangedFile, RepoConventions

# Target repo to analyze
TARGET_REPO = "/Users/sairakshithmuda/Desktop/Claude code/job-assistant"

LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
}


def load_files_from_disk(repo_path: str) -> list[ChangedFile]:
    """Load Python source files from a local repository.

    Args:
        repo_path: Absolute path to the repository.

    Returns:
        List of ChangedFile objects with content populated.
    """
    files: list[ChangedFile] = []

    for root, dirs, filenames in os.walk(repo_path):
        dirs[:] = [
            d for d in dirs
            if d not in {
                "__pycache__", ".git", ".venv", "venv",
                "node_modules", ".pytest_cache",
            }
        ]
        for fname in filenames:
            ext = os.path.splitext(fname)[1]
            lang = LANGUAGE_MAP.get(ext)
            if not lang:
                continue

            filepath = os.path.join(root, fname)
            rel_path = os.path.relpath(filepath, repo_path)

            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
            except (UnicodeDecodeError, PermissionError):
                continue

            if len(content) > 500_000:
                continue

            lines = content.split("\n")
            files.append(ChangedFile(
                filename=rel_path,
                status="modified",
                additions=len(lines),
                deletions=0,
                content=content,
                language=lang,
            ))

    return files


class MockLLMClient:
    """Mock LLM that returns empty findings for demo.

    In production, this would call the real Groq API.
    For demo purposes, we show the regex-based security
    scanner and AST-based analysis working without LLM.
    """

    async def complete(self, **kwargs):
        """Return empty findings (LLM not connected in demo).

        Returns:
            Dict with empty findings list.
        """
        return {"findings": []}

    async def complete_with_structured_output(self, **kwargs):
        """Return empty findings.

        Returns:
            Dict with empty findings list.
        """
        return {"findings": []}


async def run_demo():
    """Run the full CodeSage review pipeline on a local repo."""
    print("=" * 70)
    print("  CodeSage Demo Review")
    print("  Target: job-assistant repository")
    print("=" * 70)
    print()

    # Step 1: Load files
    print("[1/5] Loading files from disk...")
    files = load_files_from_disk(TARGET_REPO)
    print(f"      Found {len(files)} source files:")
    for f in files:
        print(f"        - {f.filename} ({f.language}, {f.additions} lines)")
    print()

    # Step 2: Build dependency graph
    print("[2/5] Building dependency graph...")
    graph_builder = DependencyGraphBuilder()
    dep_graph = graph_builder.build_graph("", files)
    impact_scores = graph_builder.calculate_impact_scores(
        dep_graph, [f.filename for f in files]
    )
    files = graph_builder.get_review_priority_order(files, impact_scores)

    print(f"      Graph: {dep_graph.number_of_nodes()} nodes, "
          f"{dep_graph.number_of_edges()} edges")
    print("      Review priority (by impact):")
    for f in files:
        print(f"        - {f.filename} (impact: {f.impact_score:.2f})")
    print()

    # Step 3: Set conventions (simulated - normally learned from PRs)
    print("[3/5] Using default conventions (no merged PRs to learn from)...")
    conventions = RepoConventions(
        naming_style="snake_case",
        uses_type_hints=True,
        uses_docstrings=True,
        docstring_style="google",
        max_function_length=50,
    )
    print()

    # Step 4: Run all 4 agents IN PARALLEL
    print("[4/5] Running 4 agents in parallel...")
    print("      (Using mock LLM - regex SecurityScanner active)")
    print()

    llm = MockLLMClient()
    agents = [
        BugDetector(llm),
        SecurityScanner(llm),
        StyleAdvisor(llm),
        TestCoverageAgent(llm),
    ]

    start = time.time()
    tasks = [
        agent.analyze(files, conventions, dep_graph)
        for agent in agents
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    agent_results: list[AgentResult] = []
    for i, result in enumerate(raw_results):
        if isinstance(result, AgentResult):
            agent_results.append(result)
            print(f"      {agents[i].name}: "
                  f"{len(result.findings)} findings, "
                  f"{result.processing_time_seconds:.2f}s")
        elif isinstance(result, Exception):
            print(f"      {agents[i].name}: FAILED - {result}")

    elapsed = time.time() - start
    print(f"\n      Total agent time: {elapsed:.2f}s (parallel)")
    print()

    # Step 5: Deduplicate and rank
    print("[5/5] Deduplicating and ranking findings...")
    dedup = Deduplicator()
    final_findings = dedup.deduplicate(agent_results)
    decision = dedup.determine_review_decision(final_findings)

    print(f"      Decision: {decision}")
    print(f"      Total findings: {len(final_findings)}")
    print()

    # Print findings
    if final_findings:
        print("=" * 70)
        print("  FINDINGS")
        print("=" * 70)
        for i, f in enumerate(final_findings, 1):
            agents_label = ", ".join(
                f.contributing_agents
            ) if f.contributing_agents else f.agent
            print(f"\n  [{i}] {f.severity}: {f.title}")
            print(f"      File: {f.file_path}:{f.line_number}")
            print(f"      Category: {f.category}")
            print(f"      Agent(s): {agents_label}")
            print(f"      Confidence: {f.confidence}")
            print(f"      Description: {f.description[:200]}")
            if f.suggestion:
                print(f"      Suggestion: {f.suggestion[:150]}")
    else:
        print("  No findings (LLM-dependent agents need real API key)")

    print()
    print("=" * 70)

    # Print formatted GitHub comment for first finding
    if final_findings:
        print("\n  Example GitHub inline comment:")
        print("  " + "-" * 50)
        formatted = dedup.format_finding_for_github(final_findings[0])
        for line in formatted.split("\n"):
            print(f"  {line}")

    # Summary
    print()
    print("=" * 70)
    print("  REVIEW SUMMARY")
    print("=" * 70)
    summary = Deduplicator.generate_executive_summary(
        final_findings,
        agent_results,
        {
            "pr_title": "Job Assistant Codebase Review",
            "repo_full_name": "sairakshithmuda/job-assistant",
        },
    )
    for line in summary.split("\n"):
        print(f"  {line}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_demo())
