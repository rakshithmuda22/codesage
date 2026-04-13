"""StyleAdvisor agent for checking code against repo conventions.

Compares code changes against conventions learned from the
repository's merged PRs. Only flags genuine deviations from
the team's established patterns.
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
from prompts import style_prompts

logger = logging.getLogger("codesage")

SUPPORTED_LANGUAGES = {"python", "javascript", "typescript"}


class StyleAdvisor(BaseAgent):
    """Agent specialized in convention-aware style checking.

    Uses repo conventions learned from past merged PRs to check
    that new code follows the team's established patterns. Does
    not impose external style preferences.

    Attributes:
        name: Agent identifier string.
    """

    name: str = "StyleAdvisor"

    def __init__(self, llm_client: LLMClient) -> None:
        """Initialize the StyleAdvisor agent.

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
        """Analyze changed files for style convention violations.

        Args:
            files: List of changed files with content.
            repo_conventions: Learned repo coding conventions.
            dependency_graph: File dependency graph (unused).

        Returns:
            AgentResult with style findings.
        """
        start = time.time()
        all_findings: list[Finding] = []

        analyzable = [
            f for f in files
            if f.content and f.language in SUPPORTED_LANGUAGES
        ]

        tasks = [
            self._analyze_file(f, repo_conventions)
            for f in analyzable
        ]
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
                            f"Style analysis failed for "
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
                "message": "Style analysis complete",
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
        self,
        file: ChangedFile,
        conventions: RepoConventions,
    ) -> list[Finding]:
        """Analyze a single file for style convention deviations.

        Builds a prompt that includes the repo's conventions so
        the LLM only flags genuine deviations, not its own
        style preferences.

        Args:
            file: ChangedFile with content populated.
            conventions: Repository coding conventions.

        Returns:
            List of style findings.
        """
        if not file.content:
            return []

        common_patterns = "\n".join(
            conventions.common_patterns
        ) if conventions.common_patterns else "None documented"

        anti_patterns = "\n".join(
            conventions.anti_patterns
        ) if conventions.anti_patterns else "None documented"

        prompt = style_prompts.ANALYZE_STYLE_PROMPT.format(
            language=file.language or "python",
            filename=file.filename,
            content=file.content[:6000],
            naming_style=conventions.naming_style,
            uses_type_hints=conventions.uses_type_hints,
            uses_docstrings=conventions.uses_docstrings,
            docstring_style=conventions.docstring_style,
            max_function_length=conventions.max_function_length,
            uses_async=conventions.uses_async,
            common_patterns=common_patterns,
            anti_patterns=anti_patterns,
        )

        try:
            response = await self.llm_client.complete(
                system_prompt=style_prompts.SYSTEM_PROMPT,
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
                        f"LLM style analysis failed for "
                        f"{file.filename}: {e}"
                    ),
                })
            )
            return []

        findings: list[Finding] = []
        for item in response.get("findings", []):
            confidence = item.get("confidence", 0)
            if confidence < 0.6:
                continue

            severity = item.get("severity", "LOW")
            if severity in ("CRITICAL", "HIGH"):
                severity = "MEDIUM"

            findings.append(self.create_finding(
                file_path=file.filename,
                line_number=item.get("line", 1),
                severity=severity,
                category=item.get(
                    "category", "style-violation"
                ),
                title=item.get(
                    "title", "Style deviation"
                ),
                description=item.get(
                    "description", "Style issue detected"
                ),
                suggestion=item.get(
                    "suggestion", "Follow repo conventions"
                ),
                code_snippet=item.get("code_snippet"),
                fix_example=item.get("fix_example"),
                confidence=confidence,
            ))

        return findings
