"""TestCoverage agent for identifying untested code paths.

Analyzes changed files to identify functions and methods that
lack corresponding test cases. Suggests specific test cases
for untested code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

import networkx as nx

from agents.base_agent import BaseAgent
from core.llm_client import LLMClient
from models.schemas import AgentResult, ChangedFile, Finding, RepoConventions
from prompts import test_prompts

logger = logging.getLogger("codesage")

SUPPORTED_LANGUAGES = {"python", "javascript", "typescript"}

TEST_FILE_PATTERNS: dict[str, list[str]] = {
    "python": [
        "tests/test_{name}.py",
        "test/test_{name}.py",
        "tests/{name}_test.py",
        "{dir}/tests/test_{name}.py",
        "{dir}/test_{name}.py",
    ],
    "javascript": [
        "__tests__/{name}.test.js",
        "{name}.test.js",
        "{name}.spec.js",
        "tests/{name}.test.js",
        "{dir}/__tests__/{name}.test.js",
    ],
    "typescript": [
        "__tests__/{name}.test.ts",
        "{name}.test.ts",
        "{name}.spec.ts",
        "tests/{name}.test.ts",
        "{dir}/__tests__/{name}.test.ts",
    ],
}


class TestCoverageAgent(BaseAgent):
    """Agent specialized in identifying untested code paths.

    For each changed file, finds corresponding test files and
    checks which functions have test coverage. Suggests specific
    test cases for untested functions.

    Attributes:
        name: Agent identifier string.
    """

    name: str = "TestCoverage"

    def __init__(self, llm_client: LLMClient) -> None:
        """Initialize the TestCoverage agent.

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
        """Analyze test coverage for all changed files.

        Skips test files themselves — only analyzes source files.

        Args:
            files: List of changed files with content.
            repo_conventions: Learned repo conventions (unused).
            dependency_graph: File dependency graph (unused).

        Returns:
            AgentResult with test coverage findings.
        """
        start = time.time()
        all_findings: list[Finding] = []

        source_files = [
            f for f in files
            if (
                f.content
                and f.language in SUPPORTED_LANGUAGES
                and not self._is_test_file(f.filename)
            )
        ]

        tasks = [
            self._analyze_file(f, files)
            for f in source_files
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
                            f"Coverage analysis failed for "
                            f"{source_files[i].filename}: {result}"
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
                "message": "Test coverage analysis complete",
                "files_analyzed": len(source_files),
                "findings": len(all_findings),
                "elapsed_s": round(elapsed, 2),
            })
        )

        return AgentResult(
            agent=self.name,
            findings=all_findings,
            processing_time_seconds=elapsed,
            files_analyzed=len(source_files),
        )

    async def _analyze_file(
        self,
        file: ChangedFile,
        all_files: list[ChangedFile],
    ) -> list[Finding]:
        """Analyze test coverage for a single source file.

        1. Extract functions using tree-sitter
        2. Find corresponding test file
        3. Identify untested functions
        4. Use LLM to suggest test cases

        Args:
            file: Source ChangedFile with content.
            all_files: All changed files (to find test files).

        Returns:
            List of test coverage findings.
        """
        if not file.content:
            return []

        tree = self.parse_tree_sitter(
            file.content, file.language or "python"
        )
        functions = self.extract_functions(
            tree, file.content, file.language or "python"
        )

        if not functions:
            return []

        test_file = self._find_test_file(
            file.filename, all_files, file.language or "python"
        )

        if test_file and test_file.content:
            return await self._analyze_with_test_file(
                file, functions, test_file
            )
        else:
            return await self._analyze_without_test_file(
                file, functions
            )

    async def _analyze_with_test_file(
        self,
        source_file: ChangedFile,
        functions: list[dict[str, Any]],
        test_file: ChangedFile,
    ) -> list[Finding]:
        """Analyze when a corresponding test file exists.

        Checks which functions have test cases and uses LLM
        for deeper analysis.

        Args:
            source_file: Source file being analyzed.
            functions: Extracted function definitions.
            test_file: Corresponding test file.

        Returns:
            List of findings about missing test coverage.
        """
        untested = []
        for func in functions:
            if not self._has_test_case(
                func["name"], test_file.content or ""
            ):
                untested.append(func)

        if not untested:
            return []

        prompt = test_prompts.ANALYZE_COVERAGE_PROMPT.format(
            language=source_file.language or "python",
            filename=source_file.filename,
            functions=json.dumps(
                [f["name"] for f in functions]
            ),
            content=source_file.content[:6000] if source_file.content else "",
            test_filename=test_file.filename,
            test_content=(
                test_file.content[:4000] if test_file.content else ""
            ),
        )

        try:
            response = await self.llm_client.complete(
                system_prompt=test_prompts.SYSTEM_PROMPT,
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
                        f"LLM analysis failed for "
                        f"{source_file.filename}: {e}"
                    ),
                })
            )
            return self._create_basic_findings(
                source_file, untested
            )

        findings: list[Finding] = []
        for item in response.get("findings", []):
            findings.append(self.create_finding(
                file_path=source_file.filename,
                line_number=item.get("line", 1),
                severity=item.get("severity", "MEDIUM"),
                category=item.get("category", "missing-test"),
                title=item.get(
                    "title", "Missing test coverage"
                ),
                description=item.get(
                    "description", "Function lacks test"
                ),
                suggestion=item.get(
                    "suggestion", "Add test cases"
                ),
                fix_example=item.get("fix_example"),
                confidence=item.get("confidence", 0.7),
            ))

        return findings

    async def _analyze_without_test_file(
        self,
        source_file: ChangedFile,
        functions: list[dict[str, Any]],
    ) -> list[Finding]:
        """Analyze when no corresponding test file exists.

        Flags the entire file as lacking coverage and suggests
        specific test cases via LLM.

        Args:
            source_file: Source file being analyzed.
            functions: Extracted function definitions.

        Returns:
            List of findings about missing test file/coverage.
        """
        prompt = test_prompts.ANALYZE_NO_TEST_FILE_PROMPT.format(
            language=source_file.language or "python",
            filename=source_file.filename,
            functions=json.dumps(
                [f["name"] for f in functions]
            ),
            content=source_file.content[:6000] if source_file.content else "",
        )

        try:
            response = await self.llm_client.complete(
                system_prompt=test_prompts.SYSTEM_PROMPT,
                user_prompt=prompt,
            )
        except Exception:
            return self._create_basic_findings(
                source_file, functions
            )

        findings: list[Finding] = []
        for item in response.get("findings", []):
            findings.append(self.create_finding(
                file_path=source_file.filename,
                line_number=item.get("line", 1),
                severity=item.get("severity", "MEDIUM"),
                category=item.get("category", "missing-test"),
                title=item.get(
                    "title",
                    f"No test file for {source_file.filename}",
                ),
                description=item.get(
                    "description", "No test coverage"
                ),
                suggestion=item.get(
                    "suggestion", "Create test file"
                ),
                fix_example=item.get("fix_example"),
                confidence=item.get("confidence", 0.8),
            ))

        return findings

    def _create_basic_findings(
        self,
        source_file: ChangedFile,
        untested_functions: list[dict[str, Any]],
    ) -> list[Finding]:
        """Create basic findings when LLM analysis is unavailable.

        Args:
            source_file: Source file being analyzed.
            untested_functions: Functions without test cases.

        Returns:
            List of basic coverage findings.
        """
        findings: list[Finding] = []
        for func in untested_functions[:5]:
            findings.append(self.create_finding(
                file_path=source_file.filename,
                line_number=func.get("start_line", 1),
                severity="MEDIUM",
                category="missing-test",
                title=f"No test for {func['name']}()",
                description=(
                    f"Function {func['name']} defined at line "
                    f"{func.get('start_line', '?')} has no "
                    f"corresponding test case."
                ),
                suggestion=(
                    f"Add test cases covering happy path, edge "
                    f"cases, and error conditions for "
                    f"{func['name']}."
                ),
                confidence=0.8,
            ))
        return findings

    @staticmethod
    def _find_test_file(
        source_filename: str,
        all_files: list[ChangedFile],
        language: str,
    ) -> Optional[ChangedFile]:
        """Find the corresponding test file for a source file.

        Checks the changed files list for common test file naming
        patterns.

        Args:
            source_filename: Source file path.
            all_files: All changed files in the PR.
            language: Programming language identifier.

        Returns:
            ChangedFile for the test file, or None if not found.
        """
        basename = os.path.basename(source_filename)
        name = os.path.splitext(basename)[0]
        file_dir = os.path.dirname(source_filename)

        patterns = TEST_FILE_PATTERNS.get(language, [])
        candidates: set[str] = set()

        for pattern in patterns:
            candidate = pattern.format(name=name, dir=file_dir)
            candidates.add(candidate)
            candidates.add(os.path.normpath(candidate))

        for f in all_files:
            if f.filename in candidates or os.path.normpath(
                f.filename
            ) in candidates:
                return f

        for f in all_files:
            if f"test_{name}" in f.filename or f"{name}_test" in f.filename:
                return f

        return None

    @staticmethod
    def _has_test_case(
        function_name: str, test_content: str
    ) -> bool:
        """Check if a function has a corresponding test case.

        Looks for test function names that reference the target
        function.

        Args:
            function_name: Name of the function to check.
            test_content: Content of the test file.

        Returns:
            True if a test case exists for this function.
        """
        if not test_content or not function_name:
            return False

        patterns = [
            f"test_{function_name}",
            f"test{function_name.capitalize()}",
            f"Test{function_name.capitalize()}",
            function_name,
        ]

        for pattern in patterns:
            if pattern in test_content:
                return True

        return False

    @staticmethod
    def _is_test_file(filename: str) -> bool:
        """Check if a file is itself a test file.

        Args:
            filename: File path to check.

        Returns:
            True if the file is a test file.
        """
        basename = os.path.basename(filename).lower()
        return (
            basename.startswith("test_")
            or basename.endswith("_test.py")
            or basename.endswith(".test.js")
            or basename.endswith(".test.ts")
            or basename.endswith(".spec.js")
            or basename.endswith(".spec.ts")
            or "conftest" in basename
            or "__tests__" in filename
        )
