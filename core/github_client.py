"""GitHub App client for PR interactions and inline review comments.

Handles GitHub App authentication (JWT -> installation token),
fetching PR data, posting inline review comments with diff position
mapping, and cloning repositories.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import time
import uuid
from typing import Any, Optional

import httpx
import jwt
from git import Repo as GitRepo

from models.schemas import ChangedFile

logger = logging.getLogger("codesage")

GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

# GitHub rejects review bodies above 65,536 characters.
MAX_REVIEW_BODY = 60_000


def _basic_auth(token: str) -> str:
    """Base64-encode 'x-access-token:<token>' for HTTP Basic auth."""
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class DiffParser:
    """Parses a unified diff into the lines GitHub lets you comment on.

    For every file in the diff it records which new-file (RIGHT side)
    line numbers appear in a hunk, together with the line's diff
    position. GitHub only accepts inline review comments on lines that
    are part of the diff, so this map doubles as the "is this line
    commentable?" check.

    Diff position follows GitHub's definition: the first ``@@`` line of
    a file is position 0, the line right below it is position 1, and
    the count keeps increasing across later hunk headers and through
    the end of that file's diff. File headers (``diff --git``, ``index``,
    ``---``/``+++``) are not counted.
    """

    @staticmethod
    def parse(diff_text: str) -> dict[str, dict[int, int]]:
        """Parse a unified diff into a file -> line -> position map.

        Hunk bodies are consumed using the line counts from each
        ``@@ -a,b +c,d @@`` header, so a removed line whose text starts
        with ``--`` (e.g. a deleted SQL comment) is never mistaken for
        a file header.

        Args:
            diff_text: Raw unified diff output (full PR diff).

        Returns:
            Dict mapping file paths to dicts of
            {new_file_line_number: diff_position}. Only added and
            context lines are mapped; removed lines have no RIGHT-side
            line number.
        """
        result: dict[str, dict[int, int]] = {}
        current_file: Optional[str] = None
        seen_hunk = False
        position = 0
        new_line = 0
        old_left = 0
        new_left = 0

        for raw in diff_text.split("\n"):
            if raw.startswith("diff --git ") or raw.startswith("@@ "):
                # Hunk body lines start with ' ', '+', '-' or '\\', so these
                # always begin something new, even if the previous hunk
                # header's counts were wrong or the diff was truncated.
                old_left = new_left = 0

            if old_left > 0 or new_left > 0:
                # Inside a hunk body.
                if raw.startswith("\\"):
                    position += 1
                elif raw.startswith("+"):
                    new_left -= 1
                    new_line += 1
                    position += 1
                    result[current_file][new_line] = position  # type: ignore[index]
                elif raw.startswith("-"):
                    old_left -= 1
                    position += 1
                else:
                    # Context line (a blank one may have lost its space).
                    old_left -= 1
                    new_left -= 1
                    new_line += 1
                    position += 1
                    result[current_file][new_line] = position  # type: ignore[index]
                continue

            if raw.startswith("\\") and seen_hunk:
                # "\ No newline at end of file" after a hunk's last line.
                position += 1
                continue

            if raw.startswith("diff --git "):
                current_file = DiffParser._path_from_git_header(raw)
                result.setdefault(current_file, {})
                seen_hunk = False
                position = 0
                continue

            if current_file is None:
                continue

            if not seen_hunk:
                if raw.startswith("rename to "):
                    current_file = DiffParser._rekey(
                        result, current_file, raw[len("rename to "):]
                    )
                elif raw.startswith("+++ b/"):
                    current_file = DiffParser._rekey(
                        result, current_file, raw[len("+++ b/"):].split("\t")[0]
                    )

            match = _HUNK_RE.match(raw)
            if match:
                if seen_hunk:
                    position += 1  # later "@@" lines count; the first is 0
                seen_hunk = True
                new_line = int(match.group(3)) - 1
                # A missing count means 1; an explicit 0 is an empty side.
                old_left = 1 if match.group(2) is None else int(match.group(2))
                new_left = 1 if match.group(4) is None else int(match.group(4))

        return result

    @staticmethod
    def _path_from_git_header(line: str) -> str:
        """Extract the file path from a ``diff --git a/X b/Y`` line.

        Paths may themselves contain ``b/`` (``lib/x.py``) or spaces, so
        this looks for the split point where both halves name the same
        file and falls back to the last `` b/`` for renames. The
        ``+++``/``rename to`` lines refine the result afterwards.

        Args:
            line: A line starting with ``diff --git ``.

        Returns:
            Best-effort new-side path of the file.
        """
        rest = line[len("diff --git "):]
        if not rest.startswith("a/"):
            return rest
        candidates = [m.start() for m in re.finditer(r" b/", rest)]
        for idx in candidates:
            if rest[2:idx] == rest[idx + 3:]:
                return rest[idx + 3:]
        if candidates:
            return rest[candidates[-1] + 3:]
        return rest[2:]

    @staticmethod
    def _rekey(
        result: dict[str, dict[int, int]], old: str, new: str
    ) -> str:
        """Rename a file key in the result map (rename / +++ header)."""
        if new == old:
            return old
        result[new] = result.pop(old, {})
        return new


class GitHubClient:
    """GitHub App client for all API interactions.

    Authenticates as a GitHub App using JWT, exchanges for
    installation access tokens, and provides methods for PR
    file fetching, diff retrieval, review posting, and repo cloning.

    Attributes:
        app_id: GitHub App ID.
        private_key: RSA private key for JWT signing.
        installation_id: Installation ID for this repo/org.
        _token: Cached installation access token.
        _token_expires: Token expiration timestamp.
        _http: Async HTTP client.
    """

    def __init__(
        self,
        app_id: Optional[str] = None,
        private_key: Optional[str] = None,
        installation_id: Optional[int] = None,
    ) -> None:
        """Initialize the GitHub App client.

        Args:
            app_id: GitHub App ID. Falls back to GITHUB_APP_ID env var.
            private_key: PEM-encoded private key string.
                Falls back to reading GITHUB_PRIVATE_KEY_PATH.
            installation_id: Installation ID for access token exchange.

        Raises:
            ValueError: If required credentials are missing.
        """
        self.app_id = app_id or os.environ.get("GITHUB_APP_ID", "")
        self.installation_id = installation_id or 0

        if private_key:
            self.private_key = private_key
        else:
            key_path = os.environ.get(
                "GITHUB_PRIVATE_KEY_PATH", "./private-key.pem"
            )
            try:
                with open(key_path, "r") as f:
                    self.private_key = f.read()
            except FileNotFoundError:
                self.private_key = ""

        self._token: Optional[str] = None
        self._token_expires: float = 0.0
        self._http = httpx.AsyncClient(
            timeout=30.0,
            headers={"Accept": "application/vnd.github+json"},
        )

    def _generate_jwt(self) -> str:
        """Generate a JWT for GitHub App authentication.

        Returns:
            Signed JWT string valid for 10 minutes.

        Raises:
            ValueError: If app_id or private_key is not configured.
        """
        if not self.app_id or not self.private_key:
            raise ValueError(
                "GitHub App ID and private key are required for JWT"
            )
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + (10 * 60),
            "iss": self.app_id,
        }
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    async def get_installation_token(self) -> str:
        """Get or refresh an installation access token.

        Exchanges a JWT for an installation token valid for 1 hour.
        Caches the token and auto-refreshes when expired.

        Returns:
            Installation access token string.

        Raises:
            httpx.HTTPStatusError: If the token exchange fails.
        """
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        jwt_token = self._generate_jwt()
        response = await self._http.post(
            f"{GITHUB_API}/app/installations/{self.installation_id}"
            "/access_tokens",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        response.raise_for_status()
        data = response.json()
        self._token = data["token"]
        self._token_expires = time.time() + 3600
        logger.info(
            json.dumps({
                "timestamp": time.time(),
                "level": "INFO",
                "agent": "GitHubClient",
                "job_id": "",
                "message": "Installation token refreshed",
                "installation_id": self.installation_id,
            })
        )
        return self._token  # type: ignore[return-value]

    async def _authed_headers(self) -> dict[str, str]:
        """Build headers with a valid installation token.

        Returns:
            Dict of HTTP headers including Authorization.
        """
        token = await self.get_installation_token()
        return {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

    async def get_pr_files(
        self, repo: str, pr_number: int, head_sha: str = ""
    ) -> list[ChangedFile]:
        """Fetch all changed files in a pull request with full content.

        Args:
            repo: Repository in owner/repo format.
            pr_number: Pull request number.
            head_sha: Commit SHA to fetch file contents from.

        Returns:
            List of ChangedFile objects with content populated.

        Raises:
            httpx.HTTPStatusError: If the API call fails.
        """
        headers = await self._authed_headers()
        files: list[ChangedFile] = []
        page = 1

        while True:
            response = await self._http.get(
                f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files",
                headers=headers,
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            page_data = response.json()

            if not page_data:
                break

            for f in page_data:
                content = None
                if (
                    f.get("status") != "removed"
                    and head_sha
                    and f.get("size", 0) < 500_000
                ):
                    try:
                        content = await self.get_file_content(
                            repo, f["filename"], head_sha
                        )
                    except Exception as e:
                        logger.warning(
                            json.dumps({
                                "timestamp": time.time(),
                                "level": "WARNING",
                                "agent": "GitHubClient",
                                "job_id": "",
                                "message": (
                                    f"Failed to fetch content for "
                                    f"{f['filename']}: {e}"
                                ),
                            })
                        )

                ext = f["filename"].rsplit(".", 1)[-1] if "." in f["filename"] else ""
                lang_map = {
                    "py": "python", "js": "javascript",
                    "ts": "typescript", "tsx": "typescript",
                    "jsx": "javascript", "go": "go", "java": "java",
                    "rb": "ruby", "rs": "rust",
                }

                files.append(ChangedFile(
                    filename=f["filename"],
                    status=f.get("status", "modified"),
                    additions=f.get("additions", 0),
                    deletions=f.get("deletions", 0),
                    patch=f.get("patch"),
                    content=content,
                    language=lang_map.get(ext),
                ))

            if len(page_data) < 100:
                break
            page += 1

        logger.info(
            json.dumps({
                "timestamp": time.time(),
                "level": "INFO",
                "agent": "GitHubClient",
                "job_id": "",
                "message": f"Fetched {len(files)} changed files",
                "repo": repo,
                "pr": pr_number,
            })
        )
        return files

    async def get_pr_diff(self, repo: str, pr_number: int) -> str:
        """Fetch raw unified diff for a pull request.

        Args:
            repo: Repository in owner/repo format.
            pr_number: Pull request number.

        Returns:
            Raw unified diff string.

        Raises:
            httpx.HTTPStatusError: If the API call fails.
        """
        headers = await self._authed_headers()
        headers["Accept"] = "application/vnd.github.diff"
        response = await self._http.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
            headers=headers,
        )
        response.raise_for_status()
        return response.text

    async def get_recent_merged_prs(
        self, repo: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Fetch recently merged pull requests with their diffs.

        Args:
            repo: Repository in owner/repo format.
            limit: Maximum number of merged PRs to return.

        Returns:
            List of dicts with PR metadata and diff content.

        Raises:
            httpx.HTTPStatusError: If the API call fails.
        """
        headers = await self._authed_headers()
        response = await self._http.get(
            f"{GITHUB_API}/repos/{repo}/pulls",
            headers=headers,
            params={
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": limit * 2,
            },
        )
        response.raise_for_status()

        merged: list[dict[str, Any]] = []
        for pr in response.json():
            if pr.get("merged_at") and len(merged) < limit:
                try:
                    diff = await self.get_pr_diff(repo, pr["number"])
                    merged.append({
                        "number": pr["number"],
                        "title": pr["title"],
                        "merged_at": pr["merged_at"],
                        "diff": diff,
                    })
                except Exception:
                    continue

        return merged

    async def create_pr_review(
        self,
        repo: str,
        pr_number: int,
        head_sha: str,
        summary_body: str,
        comments: list[dict[str, Any]],
        event: str = "COMMENT",
        diff_text: Optional[str] = None,
    ) -> dict[str, Any]:
        """Post a PR review with inline comments on lines inside the diff.

        GitHub only accepts an inline comment on a line that appears in
        the PR diff. DiffParser tells us which lines those are; each
        comment whose ``line`` is one of them is posted inline with
        ``line``/``side=RIGHT``. Anything else (a finding on an unchanged
        line outside every hunk, a file not in the diff) is listed in the
        review summary instead of being moved to a different line.

        If GitHub still rejects the inline comments with a 422 (for
        example the PR head moved after the webhook fired), the review
        is re-posted once with every finding in the summary so the
        feedback is not lost.

        Args:
            repo: Repository in owner/repo format.
            pr_number: Pull request number.
            head_sha: Commit SHA the review is for.
            summary_body: Top-level review body (markdown).
            comments: List of dicts with path, line, side, body.
            event: Review event: APPROVE, REQUEST_CHANGES, or COMMENT.
            diff_text: Raw diff text for line mapping. If None,
                fetched automatically.

        Returns:
            GitHub API response dict.

        Raises:
            httpx.HTTPStatusError: If the review posting fails.
        """
        if diff_text is None:
            diff_text = await self.get_pr_diff(repo, pr_number)

        line_maps = DiffParser.parse(diff_text)

        mapped_comments: list[dict[str, Any]] = []
        unmapped_comments: list[dict[str, Any]] = []

        for comment in comments:
            file_path = comment["path"]
            line = comment["line"]

            if line in line_maps.get(file_path, {}):
                mapped_comments.append({
                    "path": file_path,
                    "line": line,
                    "side": "RIGHT",
                    "body": comment["body"],
                })
            else:
                unmapped_comments.append(comment)

        headers = await self._authed_headers()
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews"

        def _payload(inline: list[dict[str, Any]],
                     leftover: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "commit_id": head_sha,
                "body": self._with_leftovers(summary_body, leftover),
                "event": event,
                "comments": inline,
            }

        response = await self._http.post(
            url, headers=headers,
            json=_payload(mapped_comments, unmapped_comments),
        )
        if response.status_code == 422 and mapped_comments:
            logger.warning(
                json.dumps({
                    "timestamp": time.time(),
                    "level": "WARNING",
                    "agent": "GitHubClient",
                    "job_id": "",
                    "message": (
                        "GitHub rejected inline comments (422); "
                        "re-posting findings in the review summary"
                    ),
                    "repo": repo,
                    "pr": pr_number,
                })
            )
            unmapped_comments = unmapped_comments + [
                c for c in comments if c not in unmapped_comments
            ]
            mapped_comments = []
            response = await self._http.post(
                url, headers=headers,
                json=_payload([], unmapped_comments),
            )
        response.raise_for_status()

        logger.info(
            json.dumps({
                "timestamp": time.time(),
                "level": "INFO",
                "agent": "GitHubClient",
                "job_id": "",
                "message": "PR review posted",
                "repo": repo,
                "pr": pr_number,
                "event": event,
                "inline_comments": len(mapped_comments),
                "unmapped_comments": len(unmapped_comments),
            })
        )
        return response.json()

    @staticmethod
    def _with_leftovers(
        summary_body: str, leftover: list[dict[str, Any]]
    ) -> str:
        """Append findings that could not be placed inline to the summary.

        Args:
            summary_body: Top-level review body (markdown).
            leftover: Comment dicts (path, line, body) not posted inline.

        Returns:
            The summary body, extended and capped to GitHub's size limit.
        """
        body = summary_body
        if leftover:
            body += (
                "\n\n---\n\n**Findings outside the changed lines "
                "(not attachable inline):**\n"
            )
            for c in leftover:
                body += f"\n- **{c['path']}:{c['line']}** — {c['body']}\n"
        if len(body) > MAX_REVIEW_BODY:
            body = body[:MAX_REVIEW_BODY] + "\n\n*(truncated)*"
        return body

    async def get_file_content(
        self, repo: str, path: str, ref: str
    ) -> str:
        """Fetch and decode file content from a repository.

        Args:
            repo: Repository in owner/repo format.
            path: File path relative to repo root.
            ref: Git ref (branch, tag, or SHA) to fetch from.

        Returns:
            Decoded file content as a string.

        Raises:
            httpx.HTTPStatusError: If the API call fails.
            ValueError: If the file is too large (>1MB).
        """
        headers = await self._authed_headers()
        response = await self._http.get(
            f"{GITHUB_API}/repos/{repo}/contents/{path}",
            headers=headers,
            params={"ref": ref},
        )
        response.raise_for_status()
        data = response.json()

        if data.get("size", 0) > 1_000_000:
            raise ValueError(
                f"File {path} is too large ({data['size']} bytes)"
            )

        if data.get("encoding") == "base64" and data.get("content"):
            return base64.b64decode(data["content"]).decode(
                "utf-8", errors="replace"
            )
        return data.get("content", "")

    async def clone_repo_to_temp(
        self, repo: str, sha: str
    ) -> str:
        """Clone a repository to a temporary directory.

        Args:
            repo: Repository in owner/repo format.
            sha: Commit SHA to checkout.

        Returns:
            Absolute path to the temporary directory.
            Caller is responsible for cleanup.

        Raises:
            git.GitCommandError: If cloning or checkout fails.
        """
        token = await self.get_installation_token()
        tmp_dir = os.path.join(tempfile.gettempdir(), str(uuid.uuid4()))
        # Avoid putting the token in the URL — it would land in shell history,
        # process args, and git config. Use an askpass-style env helper instead.
        clone_url = f"https://github.com/{repo}.git"
        env = {
            **os.environ,
            "GIT_ASKPASS": "/bin/echo",
            "GIT_USERNAME": "x-access-token",
            "GIT_PASSWORD": token,
            # Prevent git from writing credentials to disk
            "GIT_TERMINAL_PROMPT": "0",
        }
        header = f"AUTHORIZATION: Basic {_basic_auth(token)}"

        git_repo = GitRepo.clone_from(
            clone_url, tmp_dir, depth=1, no_checkout=True,
            env=env,
            config=f"http.extraheader={header}",
        )
        git_repo.git.checkout(sha)

        logger.info(
            json.dumps({
                "timestamp": time.time(),
                "level": "INFO",
                "agent": "GitHubClient",
                "job_id": "",
                "message": f"Cloned {repo}@{sha[:8]} to {tmp_dir}",
            })
        )
        return tmp_dir

    async def close(self) -> None:
        """Close the underlying HTTP client.

        Should be called when the client is no longer needed.
        """
        await self._http.aclose()
