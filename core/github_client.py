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

GITHUB_API = "https://api.github.com"


class DiffParser:
    """Parses unified diffs to map absolute line numbers to diff positions.

    GitHub's REST API requires inline review comments to specify a
    'position' (the line index within the diff hunk), not an absolute
    line number. This class builds that mapping.
    """

    @staticmethod
    def parse(diff_text: str) -> dict[str, dict[int, int]]:
        """Parse a unified diff into a file -> line -> position map.

        Args:
            diff_text: Raw unified diff output (full PR diff).

        Returns:
            Dict mapping file paths to dicts of
            {absolute_line_number: diff_position}.
            Only lines present in the diff (added or context) are mapped.
        """
        result: dict[str, dict[int, int]] = {}
        current_file: Optional[str] = None
        position = 0
        current_line = 0

        for line in diff_text.split("\n"):
            if line.startswith("diff --git"):
                match = re.search(r"b/(.+)$", line)
                if match:
                    current_file = match.group(1)
                    result[current_file] = {}
                    position = 0
                continue

            if line.startswith("@@"):
                match = re.search(r"\+(\d+)", line)
                if match:
                    current_line = int(match.group(1)) - 1
                position += 1
                continue

            if current_file is None:
                continue

            if line.startswith("---") or line.startswith("+++"):
                continue

            if line.startswith("+"):
                current_line += 1
                position += 1
                result[current_file][current_line] = position
            elif line.startswith("-"):
                position += 1
            else:
                current_line += 1
                position += 1
                result[current_file][current_line] = position

        return result

    @staticmethod
    def find_nearest_position(
        line_map: dict[int, int],
        target_line: int,
    ) -> Optional[int]:
        """Find the nearest valid diff position for a given line.

        Args:
            line_map: Mapping of line numbers to diff positions.
            target_line: The absolute line number to locate.

        Returns:
            The diff position of the nearest mapped line, or None
            if the file has no mapped lines.
        """
        if not line_map:
            return None
        if target_line in line_map:
            return line_map[target_line]

        nearest = min(line_map.keys(), key=lambda k: abs(k - target_line))
        return line_map[nearest]


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
        """Post a PR review with inline comments at diff positions.

        Uses DiffParser to convert absolute line numbers to diff
        positions. Comments that can't be mapped to valid positions
        are included as part of the summary body instead.

        Args:
            repo: Repository in owner/repo format.
            pr_number: Pull request number.
            head_sha: Commit SHA the review is for.
            summary_body: Top-level review body (markdown).
            comments: List of dicts with path, line, side, body.
            event: Review event: APPROVE, REQUEST_CHANGES, or COMMENT.
            diff_text: Raw diff text for position mapping. If None,
                fetched automatically.

        Returns:
            GitHub API response dict.

        Raises:
            httpx.HTTPStatusError: If the review posting fails.
        """
        if diff_text is None:
            diff_text = await self.get_pr_diff(repo, pr_number)

        position_map = DiffParser.parse(diff_text)

        mapped_comments = []
        unmapped_comments = []

        for comment in comments:
            file_path = comment["path"]
            line = comment["line"]

            if file_path in position_map:
                position = DiffParser.find_nearest_position(
                    position_map[file_path], line
                )
                if position is not None:
                    mapped_comments.append({
                        "path": file_path,
                        "position": position,
                        "body": comment["body"],
                    })
                    continue

            unmapped_comments.append(comment)

        if unmapped_comments:
            summary_body += "\n\n---\n\n**Additional findings:**\n"
            for c in unmapped_comments:
                summary_body += (
                    f"\n- **{c['path']}:{c['line']}** — {c['body']}\n"
                )

        headers = await self._authed_headers()
        body: dict[str, Any] = {
            "commit_id": head_sha,
            "body": summary_body,
            "event": event,
            "comments": mapped_comments,
        }

        response = await self._http.post(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews",
            headers=headers,
            json=body,
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
        clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"

        git_repo = GitRepo.clone_from(
            clone_url, tmp_dir, depth=1, no_checkout=True
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
