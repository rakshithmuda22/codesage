"""Dependency graph builder for understanding file relationships.

Builds a directed graph where nodes are files and edges represent
import relationships. Used to calculate impact scores that prioritize
review of high-dependency files.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import time
from typing import Optional

import networkx as nx

from models.schemas import ChangedFile

logger = logging.getLogger("codesage")


class DependencyGraphBuilder:
    """Builds and analyzes file dependency graphs.

    Parses import statements from Python and JavaScript/TypeScript
    files to construct a directed graph. Calculates impact scores
    based on how many files depend on each changed file.
    """

    def build_graph(
        self,
        repo_path: str,
        changed_files: list[ChangedFile],
    ) -> nx.DiGraph:
        """Build a directed dependency graph from changed files.

        Nodes represent file paths. An edge from A to B means A
        imports/requires B.

        Args:
            repo_path: Absolute path to the repository root.
                Can be empty if working from file content only.
            changed_files: List of changed files with content.

        Returns:
            Directed graph of file dependencies.
        """
        start = time.time()
        graph = nx.DiGraph()

        all_filenames = {f.filename for f in changed_files}

        for f in changed_files:
            graph.add_node(f.filename)

            if not f.content or not f.language:
                continue

            if f.language == "python":
                imports = self._parse_python_imports(
                    f.content, f.filename
                )
            elif f.language in ("javascript", "typescript"):
                imports = self._parse_js_imports(
                    f.content, f.filename
                )
            else:
                imports = []

            for imported_file in imports:
                normalized = self._normalize_import_path(
                    imported_file, f.filename, all_filenames,
                    repo_path
                )
                if normalized and normalized != f.filename:
                    graph.add_node(normalized)
                    graph.add_edge(f.filename, normalized)

        elapsed = time.time() - start
        logger.info(
            json.dumps({
                "timestamp": time.time(),
                "level": "INFO",
                "agent": "DependencyGraph",
                "job_id": "",
                "message": "Dependency graph built",
                "nodes": graph.number_of_nodes(),
                "edges": graph.number_of_edges(),
                "elapsed_s": round(elapsed, 3),
            })
        )
        return graph

    def calculate_impact_scores(
        self,
        graph: nx.DiGraph,
        changed_files: list[str],
    ) -> dict[str, float]:
        """Calculate impact scores for changed files.

        Impact is the fraction of files that transitively depend
        on the changed file. A higher score means the file is
        more critical to review carefully.

        Args:
            graph: Directed dependency graph.
            changed_files: List of changed file paths.

        Returns:
            Dict mapping file paths to impact scores (0.0-1.0).
        """
        total = max(graph.number_of_nodes(), 1)
        scores: dict[str, float] = {}

        reverse_graph = graph.reverse()

        for filepath in changed_files:
            if filepath not in reverse_graph:
                scores[filepath] = 0.0
                continue

            dependents = nx.descendants(reverse_graph, filepath)
            scores[filepath] = len(dependents) / total

        return scores

    def get_review_priority_order(
        self,
        changed_files: list[ChangedFile],
        impact_scores: dict[str, float],
    ) -> list[ChangedFile]:
        """Sort changed files by impact score (highest first).

        Files with higher impact scores are reviewed first because
        they affect the most other files in the codebase.

        Args:
            changed_files: List of changed files.
            impact_scores: Dict of file path to impact score.

        Returns:
            List of ChangedFile sorted by impact score descending.
        """
        for f in changed_files:
            f.impact_score = impact_scores.get(f.filename, 0.0)

        return sorted(
            changed_files,
            key=lambda f: f.impact_score,
            reverse=True,
        )

    @staticmethod
    def _parse_python_imports(
        content: str, filename: str
    ) -> list[str]:
        """Extract import targets from Python source code.

        Uses the ast module for reliable parsing of import
        and from-import statements.

        Args:
            content: Python source code.
            filename: Source file path (for error context).

        Returns:
            List of imported module path strings.
        """
        imports: list[str] = []
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return imports

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)

        return imports

    @staticmethod
    def _parse_js_imports(
        content: str, filename: str
    ) -> list[str]:
        """Extract import targets from JavaScript/TypeScript source.

        Uses regex to match ES6 import statements and CommonJS
        require() calls.

        Args:
            content: JavaScript/TypeScript source code.
            filename: Source file path (for error context).

        Returns:
            List of imported module path strings.
        """
        imports: list[str] = []

        es6_pattern = re.compile(
            r'import\s+(?:.*?\s+from\s+)?["\']([^"\']+)["\']'
        )
        for match in es6_pattern.finditer(content):
            imports.append(match.group(1))

        require_pattern = re.compile(
            r'require\s*\(\s*["\']([^"\']+)["\']\s*\)'
        )
        for match in require_pattern.finditer(content):
            imports.append(match.group(1))

        return imports

    @staticmethod
    def _normalize_import_path(
        import_path: str,
        source_file: str,
        known_files: set[str],
        repo_path: str,
    ) -> Optional[str]:
        """Normalize an import path to a known file path.

        Attempts to resolve relative and absolute imports to
        actual file paths in the repository.

        Args:
            import_path: Raw import path from source code.
            source_file: File that contains the import.
            known_files: Set of all known file paths.
            repo_path: Repository root path.

        Returns:
            Normalized file path if found, None otherwise.
        """
        if import_path in known_files:
            return import_path

        if import_path.startswith("."):
            source_dir = os.path.dirname(source_file)
            resolved = os.path.normpath(
                os.path.join(source_dir, import_path)
            )
        else:
            resolved = import_path.replace(".", "/")

        extensions = [".py", ".js", ".ts", ".tsx", ".jsx"]
        candidates = [resolved]
        for ext in extensions:
            candidates.append(resolved + ext)
            candidates.append(os.path.join(resolved, "__init__" + ext))
            candidates.append(os.path.join(resolved, "index" + ext))

        for candidate in candidates:
            if candidate in known_files:
                return candidate

        return None
