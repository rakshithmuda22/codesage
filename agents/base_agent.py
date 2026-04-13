"""Abstract base class for all CodeSage AI agents.

Provides common functionality: tree-sitter AST parsing, function
extraction, large file chunking, and finding creation. Each agent
subclass implements its own analyze() method.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional

import networkx as nx

from core.llm_client import LLMClient
from models.schemas import AgentResult, ChangedFile, Finding, RepoConventions

logger = logging.getLogger("codesage")

try:
    from tree_sitter_languages import get_parser  # noqa: F401
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
    logger.warning(
        json.dumps({
            "timestamp": 0,
            "level": "WARNING",
            "agent": "BaseAgent",
            "job_id": "",
            "message": "tree-sitter-languages not available, "
                       "falling back to regex parsing",
        })
    )


class BaseAgent(ABC):
    """Abstract base class for code review agents.

    Provides shared utilities for AST parsing, function extraction,
    file chunking, and finding creation. Subclasses must implement
    the analyze() method.

    Attributes:
        llm_client: LLMClient for AI-powered analysis.
        name: Agent name identifier (set by subclass).
    """

    name: str = ""

    def __init__(self, llm_client: LLMClient) -> None:
        """Initialize the base agent.

        Args:
            llm_client: Configured LLMClient instance.
        """
        self.llm_client = llm_client

    @abstractmethod
    async def analyze(
        self,
        files: list[ChangedFile],
        repo_conventions: RepoConventions,
        dependency_graph: nx.DiGraph,
    ) -> AgentResult:
        """Analyze changed files and produce findings.

        Args:
            files: List of changed files with content.
            repo_conventions: Learned repo coding conventions.
            dependency_graph: File dependency graph.

        Returns:
            AgentResult with findings from this agent.
        """
        pass

    def parse_tree_sitter(
        self, content: str, language: str
    ) -> Optional[Any]:
        """Parse file content into an AST using tree-sitter.

        Uses tree_sitter_languages for pre-compiled grammars.
        Falls back to None if tree-sitter is unavailable or
        parsing fails.

        Args:
            content: Source code string.
            language: Language identifier (python, javascript, etc).

        Returns:
            Root tree-sitter Node, or None if parsing fails.
        """
        if not TREE_SITTER_AVAILABLE:
            return None

        lang_map = {
            "python": "python",
            "javascript": "javascript",
            "typescript": "typescript",
        }
        ts_lang = lang_map.get(language)
        if not ts_lang:
            return None

        try:
            parser = get_parser(ts_lang)
            encoded = content.encode("utf-8")
            # tree-sitter 0.21 takes bytes directly;
            # 0.22+ may need a callback
            try:
                tree = parser.parse(encoded)
            except TypeError:
                tree = parser.parse(
                    lambda byte_offset, point: encoded[byte_offset:]
                )
            return tree.root_node
        except Exception as e:
            logger.warning(
                json.dumps({
                    "timestamp": 0,
                    "level": "WARNING",
                    "agent": self.name,
                    "job_id": "",
                    "message": f"Tree-sitter parse failed: {e}",
                })
            )
            return None

    def extract_functions(
        self, tree_node: Optional[Any], content: str,
        language: str = "python"
    ) -> list[dict[str, Any]]:
        """Extract function definitions from an AST or via regex.

        Walks the tree-sitter AST to find function nodes. If
        tree-sitter is unavailable, falls back to regex matching.

        Args:
            tree_node: Root tree-sitter node (or None for regex).
            content: Source code string.
            language: Programming language identifier.

        Returns:
            List of dicts with keys: name, start_line, end_line,
            body, parameters.
        """
        if tree_node is not None:
            return self._extract_functions_ast(
                tree_node, content, language
            )
        return self._extract_functions_regex(content, language)

    def _extract_functions_ast(
        self, node: Any, content: str, language: str
    ) -> list[dict[str, Any]]:
        """Extract functions by walking the tree-sitter AST.

        Args:
            node: Tree-sitter root node.
            content: Source code string.
            language: Programming language identifier.

        Returns:
            List of function info dicts.
        """
        functions: list[dict[str, Any]] = []
        lines = content.split("\n")

        if language == "python":
            target_types = {"function_definition"}
        else:
            target_types = {
                "function_declaration",
                "arrow_function",
                "method_definition",
            }

        self._walk_ast(node, target_types, functions, lines)
        return functions

    def _walk_ast(
        self,
        node: Any,
        target_types: set[str],
        functions: list[dict[str, Any]],
        lines: list[str],
    ) -> None:
        """Recursively walk AST nodes to find function definitions.

        Args:
            node: Current tree-sitter node.
            target_types: Set of node type strings to match.
            functions: Output list to append to.
            lines: Source code lines.
        """
        if node.type in target_types:
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1

            name = ""
            params = ""
            for child in node.children:
                if child.type == "identifier" or child.type == "name":
                    name = child.text.decode("utf-8")
                elif child.type == "parameters":
                    params = child.text.decode("utf-8")

            body_lines = lines[start_line - 1:end_line]
            functions.append({
                "name": name or f"anonymous_{start_line}",
                "start_line": start_line,
                "end_line": end_line,
                "body": "\n".join(body_lines),
                "parameters": params,
            })

        for child in node.children:
            self._walk_ast(child, target_types, functions, lines)

    @staticmethod
    def _extract_functions_regex(
        content: str, language: str
    ) -> list[dict[str, Any]]:
        """Fallback function extraction using regex.

        Used when tree-sitter is not available. Less accurate
        but works for basic cases.

        Args:
            content: Source code string.
            language: Programming language identifier.

        Returns:
            List of function info dicts.
        """
        functions: list[dict[str, Any]] = []
        lines = content.split("\n")

        if language == "python":
            pattern = re.compile(
                r"^\s*(async\s+)?def\s+(\w+)\s*\(([^)]*)\)"
            )
        else:
            pattern = re.compile(
                r"(?:function\s+(\w+)|"
                r"(?:const|let|var)\s+(\w+)\s*=\s*"
                r"(?:async\s+)?(?:function|\([^)]*\)\s*=>))"
            )

        for i, line in enumerate(lines):
            match = pattern.search(line)
            if match:
                if language == "python":
                    name = match.group(2)
                    params = match.group(3)
                else:
                    name = match.group(1) or match.group(2) or ""
                    params = ""

                end = min(i + 50, len(lines))
                for j in range(i + 1, len(lines)):
                    if (
                        language == "python"
                        and j < len(lines)
                        and lines[j].strip()
                        and not lines[j].startswith(" ")
                        and not lines[j].startswith("\t")
                    ):
                        end = j
                        break

                functions.append({
                    "name": name,
                    "start_line": i + 1,
                    "end_line": end,
                    "body": "\n".join(lines[i:end]),
                    "parameters": params,
                })

        return functions

    @staticmethod
    def chunk_large_file(
        content: str, max_lines: int = 300
    ) -> list[str]:
        """Split large files into overlapping chunks.

        Files over 500 lines are split into chunks with 50-line
        overlap to catch issues at boundaries. Attempts to avoid
        splitting mid-function.

        Args:
            content: Full file content string.
            max_lines: Maximum lines per chunk.

        Returns:
            List of chunk strings.
        """
        lines = content.split("\n")
        if len(lines) <= max_lines:
            return [content]

        chunks: list[str] = []
        overlap = 50
        start = 0

        while start < len(lines):
            end = min(start + max_lines, len(lines))

            if end < len(lines):
                search_start = max(end - 20, start)
                for j in range(end, search_start, -1):
                    line = lines[j].strip() if j < len(lines) else ""
                    if (
                        line == ""
                        or line.startswith("def ")
                        or line.startswith("class ")
                        or line.startswith("function ")
                        or line.startswith("async ")
                    ):
                        end = j
                        break

            chunk = "\n".join(lines[start:end])
            chunks.append(chunk)

            if end >= len(lines):
                break
            start = end - overlap

        return chunks

    def create_finding(self, **kwargs: Any) -> Finding:
        """Factory method to create a Finding with auto-generated ID.

        Args:
            **kwargs: Finding field values. 'agent' defaults to
                self.name if not provided.

        Returns:
            New Finding instance.
        """
        if "id" not in kwargs:
            kwargs["id"] = str(uuid.uuid4())
        if "agent" not in kwargs:
            kwargs["agent"] = self.name
        return Finding(**kwargs)
