"""Tests for diff -> inline-comment line mapping.

GitHub only accepts an inline review comment on a line that is part of
the PR diff, and ``position`` counts lines of the *diff*, not of the
file. These tests pin down both against hand-computed values and against
real ``git diff`` output.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from core.github_client import DiffParser

_RAW_DIFF = """\
diff --git a/lib/web/app.py b/lib/web/app.py
index 1111111..2222222 100644
--- a/lib/web/app.py
+++ b/lib/web/app.py
@@ -1,4 +1,5 @@
 import os
+import sys
<blank>
 def a():
     return 1
@@ -10,5 +11,5 @@ def c():
     x = 1
     y = 2
-    return x + y
+    return x * y
<blank>
 def d():
diff --git a/new_file.py b/new_file.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new_file.py
@@ -0,0 +1,2 @@
+print("hi")
+print("there")
diff --git a/old.py b/old.py
deleted file mode 100644
index 4444444..0000000
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-gone
-also gone
"""
# A blank context line is a single space; spelled out here so editors
# and linters do not strip it.
MULTI_HUNK_DIFF = "\n".join(
    " " if line == "<blank>" else line for line in _RAW_DIFF.split("\n")
)


class TestHandComputedPositions:
    """Positions checked against values worked out by hand."""

    def test_line_below_first_hunk_header_is_position_one(self):
        """The first @@ is position 0, so the next line is 1 (not 2 or 3)."""
        result = DiffParser.parse(MULTI_HUNK_DIFF)
        # " import os" is the first line under the first "@@".
        assert result["lib/web/app.py"][1] == 1
        # "+import sys" is the second.
        assert result["lib/web/app.py"][2] == 2

    def test_positions_keep_counting_across_hunks(self):
        """A second hunk header takes up a position of its own."""
        m = DiffParser.parse(MULTI_HUNK_DIFF)["lib/web/app.py"]
        # Hunk 1 body is positions 1-5 (" import os", "+import sys",
        # " ", " def a():", "     return 1").
        assert m[5] == 5
        # Position 6 is the second "@@" line itself. New-side line 11
        # ("    x = 1") is the first line under it -> position 7.
        assert m[11] == 7
        assert m[12] == 8  # "    y = 2"
        # "-    return x + y" is position 9 and has no new-side line;
        # "+    return x * y" is new-side line 13 at position 10.
        assert m[13] == 10
        assert m[14] == 11  # blank context line
        assert m[15] == 12  # "def d():"

    def test_removed_lines_are_not_mapped(self):
        """Removed lines have no RIGHT-side line number."""
        m = DiffParser.parse(MULTI_HUNK_DIFF)["lib/web/app.py"]
        assert sorted(m) == [1, 2, 3, 4, 5, 11, 12, 13, 14, 15]

    def test_path_containing_b_slash_is_not_truncated(self):
        """'lib/web/app.py' must be keyed by its full path.

        An earlier version took everything after the first 'b/' in the
        'diff --git' line, which broke every path with a directory whose
        name ends in 'b' (lib, web, sub, ...).
        """
        result = DiffParser.parse(MULTI_HUNK_DIFF)
        assert "lib/web/app.py" in result
        assert not any(k.startswith("b/") or " b/" in k for k in result)

    def test_new_file_positions_ignore_header_lines(self):
        """'new file mode' / 'index' lines do not shift positions."""
        result = DiffParser.parse(MULTI_HUNK_DIFF)
        assert result["new_file.py"] == {1: 1, 2: 2}

    def test_deleted_file_has_nothing_commentable(self):
        """A deleted file has no RIGHT side at all."""
        assert DiffParser.parse(MULTI_HUNK_DIFF)["old.py"] == {}

    def test_removed_line_starting_with_dashes_is_not_a_header(self):
        """'-- sql comment' removed shows up as '--- sql comment'."""
        diff = (
            "diff --git a/q.sql b/q.sql\n"
            "index 1..2 100644\n"
            "--- a/q.sql\n"
            "+++ b/q.sql\n"
            "@@ -1,3 +1,2 @@\n"
            " SELECT 1;\n"
            "--- a comment that was removed\n"
            " SELECT 2;\n"
        )
        m = DiffParser.parse(diff)["q.sql"]
        assert m == {1: 1, 2: 3}

    def test_added_line_starting_with_plus_plus_is_not_a_header(self):
        """'++i' added shows up as '+++i'."""
        diff = (
            "diff --git a/c.js b/c.js\n"
            "--- a/c.js\n"
            "+++ b/c.js\n"
            "@@ -1,1 +1,2 @@\n"
            " for (;;) {\n"
            "+++i;\n"
        )
        assert DiffParser.parse(diff)["c.js"] == {1: 1, 2: 2}

    def test_no_newline_marker_takes_a_position_but_no_line(self):
        """'\\ No newline at end of file' is a diff line without a file line."""
        diff = (
            "diff --git a/x.txt b/x.txt\n"
            "--- a/x.txt\n"
            "+++ b/x.txt\n"
            "@@ -1,2 +1,2 @@\n"
            " keep\n"
            "-old last\n"
            "\\ No newline at end of file\n"
            "+new last\n"
            "\\ No newline at end of file\n"
        )
        assert DiffParser.parse(diff)["x.txt"] == {1: 1, 2: 4}

    def test_rename_uses_the_new_path(self):
        """A renamed + edited file is keyed by its new name."""
        diff = (
            "diff --git a/old_name.py b/pkg/new_name.py\n"
            "similarity index 80%\n"
            "rename from old_name.py\n"
            "rename to pkg/new_name.py\n"
            "index 1..2 100644\n"
            "--- a/old_name.py\n"
            "+++ b/pkg/new_name.py\n"
            "@@ -1,1 +1,2 @@\n"
            " a\n"
            "+b\n"
        )
        assert DiffParser.parse(diff) == {"pkg/new_name.py": {1: 1, 2: 2}}

    def test_hunk_header_without_counts(self):
        """'@@ -1 +1 @@' means one line on each side."""
        diff = (
            "diff --git a/one.txt b/one.txt\n"
            "--- a/one.txt\n"
            "+++ b/one.txt\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
        )
        assert DiffParser.parse(diff)["one.txt"] == {1: 2}

    def test_binary_file_is_present_but_empty(self):
        """Binary files appear in the diff without hunks."""
        diff = (
            "diff --git a/logo.png b/logo.png\n"
            "index 1..2 100644\n"
            "Binary files a/logo.png and b/logo.png differ\n"
        )
        assert DiffParser.parse(diff) == {"logo.png": {}}

    def test_wrong_hunk_counts_do_not_swallow_the_next_hunk(self):
        """A bad '@@ -1,9 +1,9 @@' count must not eat the following header."""
        diff = (
            "diff --git a/f.py b/f.py\n"
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,9 +1,9 @@\n"
            " a\n"
            "+b\n"
            "@@ -20,1 +21,2 @@\n"
            " c\n"
            "+d\n"
        )
        assert DiffParser.parse(diff)["f.py"] == {
            1: 1, 2: 2, 21: 4, 22: 5
        }

    def test_empty_diff(self):
        """No diff, no commentable lines."""
        assert DiffParser.parse("") == {}


# ---------------------------------------------------------------------------
# Ground truth from real git: every mapped line must point at the same text
# ---------------------------------------------------------------------------

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True,
        capture_output=True, text=True,
    ).stdout


def _patch_by_file(diff: str) -> dict[str, list[str]]:
    """Split a diff into {path: lines starting at the first '@@'}.

    This is the string GitHub calls ``patch`` for each file, and the
    string its ``position`` indexes into (first '@@' line = index 0).
    Deliberately implemented without any of DiffParser's logic.
    """
    out: dict[str, list[str]] = {}
    path = None
    body: list[str] | None = None
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            path, body = None, None
        elif body is None and line.startswith("+++ b/"):
            path = line[len("+++ b/"):]
        elif body is None and line.startswith("@@") and path:
            body = out.setdefault(path, [])
            body.append(line)
        elif body is not None:
            body.append(line)
    return out


@needs_git
class TestAgainstRealGitDiffs:
    """Fuzz DiffParser against `git diff` output."""

    @pytest.mark.parametrize("seed", range(25))
    def test_every_mapped_line_points_at_its_own_text(self, tmp_path, seed):
        """position indexes the patch; line indexes the new file.

        For every (line, position) pair: the patch line at ``position``
        must be an added or context line whose text equals line ``line``
        of the new file. Conversely every added/context line in the patch
        must be mapped (nothing commentable is lost).
        """
        rng = random.Random(seed)
        repo = tmp_path
        _git(repo, "init", "-q", "-b", "main")

        names = ["lib/a.py", "web/sub/b.js", "plain.txt", "src/lib/db.ts"]
        for name in names:
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            n = rng.randint(30, 120)
            (repo / name).write_text(
                "".join(f"line {i} of {name}\n" for i in range(n))
            )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")

        for name in names:
            lines = (repo / name).read_text().splitlines(keepends=True)
            for _ in range(rng.randint(1, 8)):
                i = rng.randrange(len(lines))
                action = rng.choice(["edit", "insert", "delete", "dashes"])
                if action == "edit":
                    lines[i] = f"edited {i}\n"
                elif action == "insert":
                    lines.insert(i, f"inserted at {i}\n")
                elif action == "delete" and len(lines) > 10:
                    del lines[i]
                else:
                    lines[i] = f"-- looks like a header {i}\n"
            text = "".join(lines)
            if rng.random() < 0.3:
                text = text.rstrip("\n")  # no newline at EOF
            (repo / name).write_text(text)
        (repo / "added" / "deep").mkdir(parents=True)
        (repo / "added" / "deep" / "new.py").write_text("x = 1\ny = 2\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "change")

        diff = _git(repo, "diff", "HEAD~1", "HEAD", "-U3")
        parsed = DiffParser.parse(diff)
        patches = _patch_by_file(diff)

        assert set(patches) <= set(parsed)
        checked = 0
        for path, patch in patches.items():
            new_lines = (repo / path).read_text().split("\n")
            mapping = parsed[path]
            for line_no, position in mapping.items():
                row = patch[position]
                assert row[:1] in (" ", "+"), (path, line_no, position, row)
                assert row[1:] == new_lines[line_no - 1], (
                    path, line_no, position
                )
                checked += 1
            commentable = [
                p for p, row in enumerate(patch)
                if row[:1] in (" ", "+")
            ]
            assert sorted(mapping.values()) == commentable, path
        assert checked > 0
