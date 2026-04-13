"""Tests for the DependencyGraphBuilder.

Tests Python import parsing, JavaScript import parsing,
impact score calculation, and review priority ordering.
"""

from __future__ import annotations

from core.dependency_graph import DependencyGraphBuilder
from models.schemas import ChangedFile


class TestPythonImportParsing:
    """Tests for Python import statement parsing."""

    def test_parses_simple_import(self):
        """Correctly parses 'import module' statements."""
        builder = DependencyGraphBuilder()
        files = [
            ChangedFile(
                filename="main.py",
                content="import utils\nimport helpers\n",
                language="python",
            ),
            ChangedFile(
                filename="utils.py",
                content="def helper():\n    pass\n",
                language="python",
            ),
        ]
        graph = builder.build_graph("", files)
        assert graph.has_node("main.py")
        assert graph.number_of_nodes() >= 2

    def test_parses_from_import(self):
        """Correctly parses 'from module import X' statements."""
        builder = DependencyGraphBuilder()
        files = [
            ChangedFile(
                filename="app.py",
                content="from utils import helper\n",
                language="python",
            ),
            ChangedFile(
                filename="utils.py",
                content="def helper():\n    pass\n",
                language="python",
            ),
        ]
        graph = builder.build_graph("", files)
        assert graph.has_node("app.py")

    def test_handles_syntax_error(self):
        """Gracefully handles files with syntax errors."""
        builder = DependencyGraphBuilder()
        files = [
            ChangedFile(
                filename="broken.py",
                content="def f(\n    invalid syntax!!!",
                language="python",
            ),
        ]
        graph = builder.build_graph("", files)
        assert graph.has_node("broken.py")
        assert graph.number_of_edges() == 0


class TestJavaScriptImportParsing:
    """Tests for JavaScript/TypeScript import parsing."""

    def test_parses_es6_import(self):
        """Correctly parses ES6 import statements."""
        builder = DependencyGraphBuilder()
        files = [
            ChangedFile(
                filename="app.js",
                content="import { helper } from './utils';\n",
                language="javascript",
            ),
            ChangedFile(
                filename="utils.js",
                content="export function helper() {}\n",
                language="javascript",
            ),
        ]
        graph = builder.build_graph("", files)
        assert graph.has_node("app.js")

    def test_parses_require(self):
        """Correctly parses CommonJS require() calls."""
        builder = DependencyGraphBuilder()
        files = [
            ChangedFile(
                filename="index.js",
                content="const utils = require('./utils');\n",
                language="javascript",
            ),
        ]
        graph = builder.build_graph("", files)
        assert graph.has_node("index.js")


class TestImpactScores:
    """Tests for impact score calculation."""

    def test_high_impact_for_many_dependents(self):
        """Files imported by many others have higher impact."""
        builder = DependencyGraphBuilder()
        files = [
            ChangedFile(
                filename="core/utils.py",
                content="def helper():\n    pass\n",
                language="python",
            ),
            ChangedFile(
                filename="a.py",
                content="from core import utils\n",
                language="python",
            ),
            ChangedFile(
                filename="b.py",
                content="from core import utils\n",
                language="python",
            ),
            ChangedFile(
                filename="c.py",
                content="from core import utils\n",
                language="python",
            ),
        ]
        graph = builder.build_graph("", files)

        for f in files:
            if f.filename != "core/utils.py":
                graph.add_edge(f.filename, "core/utils.py")

        scores = builder.calculate_impact_scores(
            graph, ["core/utils.py", "a.py"]
        )
        assert scores["core/utils.py"] >= scores.get("a.py", 0)

    def test_zero_impact_for_leaf_node(self):
        """Files with no dependents have zero impact."""
        builder = DependencyGraphBuilder()
        files = [
            ChangedFile(
                filename="standalone.py",
                content="print('hello')\n",
                language="python",
            ),
        ]
        graph = builder.build_graph("", files)
        scores = builder.calculate_impact_scores(
            graph, ["standalone.py"]
        )
        assert scores["standalone.py"] == 0.0


class TestReviewPriorityOrder:
    """Tests for review priority ordering."""

    def test_high_impact_files_first(self):
        """Files are sorted by impact score descending."""
        builder = DependencyGraphBuilder()
        files = [
            ChangedFile(
                filename="low.py",
                content="x = 1\n",
                language="python",
                impact_score=0.1,
            ),
            ChangedFile(
                filename="high.py",
                content="y = 2\n",
                language="python",
                impact_score=0.9,
            ),
            ChangedFile(
                filename="mid.py",
                content="z = 3\n",
                language="python",
                impact_score=0.5,
            ),
        ]
        scores = {
            "low.py": 0.1, "high.py": 0.9, "mid.py": 0.5,
        }
        ordered = builder.get_review_priority_order(files, scores)
        assert ordered[0].filename == "high.py"
        assert ordered[-1].filename == "low.py"

    def test_empty_files_list(self):
        """Handles empty file list."""
        builder = DependencyGraphBuilder()
        result = builder.get_review_priority_order([], {})
        assert result == []
