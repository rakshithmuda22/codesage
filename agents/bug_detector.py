"""BugDetector agent for finding logic errors and code defects.

Analyzes changed files for null pointer dereferences, off-by-one
errors, infinite loops, resource leaks, incorrect error handling,
race conditions, and other logic bugs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import networkx as nx

from agents.base_agent import BaseAgent
from core.llm_client import LLMClient
from models.schemas import AgentResult, ChangedFile, Finding, RepoConventions
from prompts import bug_prompts

logger = logging.getLogger("codesage")

SUPPORTED_LANGUAGES = {"python", "javascript", "typescript"}
MIN_CONFIDENCE = 0.6


class BugDetector(BaseAgent):
    """Agent specialized in finding logic bugs and code defects.

    Uses tree-sitter for code structure understanding and LLM
    for deep bug analysis. Runs per-file analysis concurrently.

    Attributes:
        name: Agent identifier string.
    """

    name: str = "BugDetector"

    def __init__(self, llm_client: LLMClient) -> None:
        """Initialize the BugDetector agent.

        Args:
            llm_client: Configured LLMClient instance.
        """
        super().__init__(llm_client)

    async def analyze(
        self,
        files: list[ChangedFile],
        repo_conventions: RepoConventions,
        dependency_graph: nx.DiGraph,
    ) -> AgentResult:
        """Analyze all changed files for bugs.

        Runs per-file analysis concurrently. Only processes files
        with content in supported languages.

        Args:
            files: List of changed files with content.
            repo_conventions: Learned repo conventions (unused by
                this agent but required by interface).
            dependency_graph: File dependency graph (unused by
                this agent but required by interface).

        Returns:
            AgentResult with all bug findings.
        """
        start = time.time()
        all_findings: list[Finding] = []

        analyzable = [
            f for f in files
            if f.content and f.language in SUPPORTED_LANGUAGES
        ]

        tasks = [self._analyze_file(f) for f in analyzable]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, list):
                all_findings.extend(result)
            elif isinstance(result, Exception):
                logger.error(
                    json.dumps({
                        "timestamp": time.time(),
                        "level": "ERROR",
                        "agent": self.name,
                        "job_id": "",
                        "message": (
                            f"File analysis failed for "
                            f"{analyzable[i].filename}: {result}"
                        ),
                    })
                )

        elapsed = time.time() - start
        logger.info(
            json.dumps({
                "timestamp": time.time(),
                "level": "INFO",
                "agent": self.name,
                "job_id": "",
                "message": "Bug analysis complete",
                "files_analyzed": len(analyzable),
                "findings": len(all_findings),
                "elapsed_s": round(elapsed, 2),
            })
        )

        return AgentResult(
            agent=self.name,
            findings=all_findings,
            processing_time_seconds=elapsed,
            files_analyzed=len(analyzable),
        )

    async def _analyze_file(
        self, file: ChangedFile
    ) -> list[Finding]:
        """Analyze a single file for bugs using AST + LLM.

        Parses the file with tree-sitter, extracts functions,
        and sends the code to the LLM for bug detection.
        Handles large files by chunking.

        Args:
            file: ChangedFile with content populated.

        Returns:
            List of bug findings for this file.

        Raises:
            Exception: If LLM call fails after retries.
        """
        if not file.content:
            return []

        tree = self.parse_tree_sitter(
            file.content, file.language or "python"
        )
        functions = self.extract_functions(
            tree, file.content, file.language or "python"
        )

        chunks = self.chunk_large_file(file.content)
        all_findings: list[Finding] = []

        for chunk in chunks:
            prompt = bug_prompts.ANALYZE_FILE_PROMPT.format(
                language=file.language or "python",
                filename=file.filename,
                content=chunk[:8000],
                functions=json.dumps(
                    [f["name"] for f in functions]
                ),
            )

            try:
                response = await self.llm_client.complete(
                    system_prompt=bug_prompts.SYSTEM_PROMPT,
                    user_prompt=prompt,
                )
            except Exception as e:
                logger.warning(
                    json.dumps({
                        "timestamp": time.time(),
                        "level": "WARNING",
                        "agent": self.name,
                        "job_id": "",
                        "message": (
                            f"LLM call failed for "
                            f"{file.filename}: {e}"
                        ),
                    })
                )
                continue

            for item in response.get("findings", []):
                confidence = item.get("confidence", 0)
                if confidence < MIN_CONFIDENCE:
                    continue

                all_findings.append(self.create_finding(
                    file_path=file.filename,
                    line_number=item.get("line", 1),
                    severity=item.get("severity", "MEDIUM"),
                    category=item.get("category", "logic-error"),
                    title=item.get("title", "Potential bug"),
                    description=item.get(
                        "description", "Bug detected"
                    ),
                    suggestion=item.get(
                        "suggestion", "Review this code"
                    ),
                    code_snippet=item.get("code_snippet"),
                    fix_example=item.get("fix_example"),
                    confidence=confidence,
                ))

        return all_findings
