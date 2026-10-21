"""Tests for tree-sitter function extraction and line-numbered prompts."""

from __future__ import annotations

import networkx as nx
import pytest

from agents.bug_detector import BugDetector
from models.schemas import ChangedFile, RepoConventions
from tests.conftest import MockLLMClient


@pytest.fixture
def agent():
    return BugDetector(MockLLMClient())


def functions(agent, src: str, language: str) -> dict[str, tuple[int, int]]:
    tree = agent.parse_tree_sitter(src, language)
    assert tree is not None, f"tree-sitter failed to parse {language}"
    return {
        f["name"]: (f["start_line"], f["end_line"])
        for f in agent.extract_functions(tree, src, language)
    }


def test_python_functions_and_methods(agent):
    src = (
        "class A:\n"
        "    def m(self, x):\n"
        "        return x\n"
        "\n"
        "async def top(a, b):\n"
        "    return a\n"
    )
    assert functions(agent, src, "python") == {"m": (2, 3), "top": (5, 6)}


def test_javascript_named_arrow_and_method_functions(agent):
    src = (
        "function foo(a, b) { return a + b }\n"
        "const bar = (x) => x * 2;\n"
        "const baz = x => x + 1;\n"
        "class K {\n"
        "  method(a) { return a }\n"
        "}\n"
    )
    found = functions(agent, src, "javascript")
    assert set(found) == {"foo", "bar", "baz", "method"}
    assert found["foo"] == (1, 1)
    assert found["method"] == (5, 5)


def test_typescript_functions_and_methods(agent):
    src = (
        "export function foo(a: number): number { return a }\n"
        "export const bar = (x: number): number => x * 2;\n"
        "class K {\n"
        "  private p(): void {}\n"
        "}\n"
    )
    assert set(functions(agent, src, "typescript")) == {"foo", "bar", "p"}


def test_number_lines_uses_absolute_numbers():
    out = BugDetector.number_lines("a\nb", start_line=120)
    assert out.split("\n") == ["  120 | a", "  121 | b"]


def test_chunks_carry_their_absolute_start_line():
    content = "\n".join(f"line{i}" for i in range(1, 701))
    chunks = BugDetector.chunk_with_offsets(content)
    assert len(chunks) > 1
    lines = content.split("\n")
    for first, text in chunks:
        assert text.split("\n")[0] == lines[first - 1]
    assert chunks[0][0] == 1
    # chunk_large_file keeps its old contract
    assert BugDetector.chunk_large_file(content) == [t for _, t in chunks]


@pytest.mark.asyncio
async def test_bug_prompt_shows_the_llm_real_line_numbers_for_every_chunk():
    """Findings in chunk 2+ must be reportable with file-absolute lines."""
    content = "\n".join(f"x{i} = {i}" for i in range(1, 701))
    llm = MockLLMClient()
    agent = BugDetector(llm)
    file = ChangedFile(
        filename="big.py", content=content, language="python", status="added"
    )
    await agent.analyze([file], RepoConventions(), nx.DiGraph())

    assert len(llm.calls) > 1
    second = llm.calls[1]["user_prompt"]
    first_line_of_second_chunk = BugDetector.chunk_with_offsets(content)[1][0]
    assert f"{first_line_of_second_chunk:>5} | " in second
    assert "    1 | x1 = 1" not in second
