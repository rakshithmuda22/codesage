"""Convention learner that analyzes merged PRs to learn repo patterns.

Examines recently merged pull requests to extract coding conventions,
naming styles, and patterns used by the team. Results are cached in
Redis with a 24-hour TTL to avoid redundant analysis.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import time
from typing import Any, Optional

import redis

from core.github_client import GitHubClient
from core.llm_client import LLMClient
from models.schemas import RepoConventions

logger = logging.getLogger("codesage")

CONVENTION_CACHE_TTL = 86400  # 24 hours
MAX_PRS_TO_ANALYZE = 3  # Cold start protection


class ConventionLearner:
    """Learns coding conventions from a repository's merged PRs.

    Combines heuristic Python AST analysis with LLM-powered
    convention detection to build a RepoConventions profile.
    Results are cached in Redis to avoid rate limit issues.

    Attributes:
        github_client: GitHubClient for fetching PR data.
        llm_client: LLMClient for convention analysis.
        redis_client: Redis connection for caching.
    """

    def __init__(
        self,
        github_client: GitHubClient,
        llm_client: LLMClient,
        redis_url: Optional[str] = None,
    ) -> None:
        """Initialize the convention learner.

        Args:
            github_client: Authenticated GitHubClient instance.
            llm_client: Configured LLMClient instance.
            redis_url: Redis connection URL. Falls back to
                REDIS_URL env var.
        """
        self.github_client = github_client
        self.llm_client = llm_client
        url = redis_url or os.environ.get(
            "REDIS_URL", "redis://localhost:6379"
        )
        try:
            self.redis_client: Optional[redis.Redis] = redis.from_url(url)
            self.redis_client.ping()
        except (redis.ConnectionError, redis.RedisError):
            logger.warning(
                json.dumps({
                    "timestamp": time.time(),
                    "level": "WARNING",
                    "agent": "ConventionLearner",
                    "job_id": "",
                    "message": "Redis unavailable, caching disabled",
                })
            )
            self.redis_client = None

    async def learn_conventions(
        self, repo: str
    ) -> RepoConventions:
        """Learn coding conventions for a repository.

        Checks Redis cache first. On cache miss, fetches the last
        3 merged PRs, extracts code samples, and uses a single
        batched LLM call to analyze conventions.

        Args:
            repo: Repository in owner/repo format.

        Returns:
            RepoConventions object describing the repo's patterns.
        """
        cache_key = f"codesage:conventions:{repo}"

        cached = self._get_cached(cache_key)
        if cached:
            logger.info(
                json.dumps({
                    "timestamp": time.time(),
                    "level": "INFO",
                    "agent": "ConventionLearner",
                    "job_id": "",
                    "message": "Conventions loaded from cache",
                    "repo": repo,
                })
            )
            return cached

        start = time.time()

        merged_prs = await self.github_client.get_recent_merged_prs(
            repo, limit=MAX_PRS_TO_ANALYZE
        )

        if not merged_prs:
            logger.info(
                json.dumps({
                    "timestamp": time.time(),
                    "level": "INFO",
                    "agent": "ConventionLearner",
                    "job_id": "",
                    "message": "No merged PRs found, using defaults",
                    "repo": repo,
                })
            )
            defaults = RepoConventions()
            self._cache_result(cache_key, defaults)
            return defaults

        code_samples = self._extract_code_samples(merged_prs)

        heuristic = self.extract_python_conventions(code_samples)

        llm_conventions = await self._llm_analyze_conventions(
            repo, code_samples
        )

        conventions = self._merge_conventions(heuristic, llm_conventions)

        self._cache_result(cache_key, conventions)

        elapsed = time.time() - start
        logger.info(
            json.dumps({
                "timestamp": time.time(),
                "level": "INFO",
                "agent": "ConventionLearner",
                "job_id": "",
                "message": "Conventions learned",
                "repo": repo,
                "prs_analyzed": len(merged_prs),
                "samples": len(code_samples),
                "elapsed_s": round(elapsed, 2),
            })
        )
        return conventions

    def _extract_code_samples(
        self, merged_prs: list[dict[str, Any]]
    ) -> list[str]:
        """Extract added code lines from merged PR diffs.

        Only collects lines starting with '+' (excluding file headers)
        and groups them into coherent code blocks.

        Args:
            merged_prs: List of merged PR dicts with 'diff' field.

        Returns:
            List of code sample strings from added lines.
        """
        samples: list[str] = []
        for pr in merged_prs:
            diff = pr.get("diff", "")
            current_block: list[str] = []

            for line in diff.split("\n"):
                if line.startswith("+") and not line.startswith("+++"):
                    current_block.append(line[1:])
                elif current_block:
                    block = "\n".join(current_block).strip()
                    if len(block) > 20:
                        samples.append(block[:2000])
                    current_block = []

            if current_block:
                block = "\n".join(current_block).strip()
                if len(block) > 20:
                    samples.append(block[:2000])

        return samples[:20]

    async def _llm_analyze_conventions(
        self, repo: str, code_samples: list[str]
    ) -> dict[str, Any]:
        """Use LLM to analyze coding conventions from code samples.

        Sends all samples in a single batched call to avoid
        rate limit issues during cold start.

        Args:
            repo: Repository name for context.
            code_samples: List of code sample strings.

        Returns:
            Dict of detected conventions from LLM analysis.
        """
        if not code_samples:
            return {}

        combined = "\n\n---\n\n".join(code_samples[:10])

        system_prompt = (
            "You are a code style analyzer. Analyze the provided code "
            "samples from merged PRs and identify the team's coding "
            "conventions. Return ONLY valid JSON."
        )

        user_prompt = (
            f"Analyze these code samples from {repo} merged PRs and "
            f"identify conventions.\n\n"
            f"Code samples:\n```\n{combined[:6000]}\n```\n\n"
            f"Return JSON with these fields:\n"
            f'{{"naming_style": "snake_case|camelCase|PascalCase",'
            f'"uses_type_hints": true|false,'
            f'"uses_docstrings": true|false,'
            f'"docstring_style": "google|numpy|sphinx|none",'
            f'"max_function_length": <number>,'
            f'"uses_black_formatting": true|false,'
            f'"uses_async": true|false,'
            f'"test_naming_pattern": "test_*|Test*",'
            f'"common_patterns": ["list of patterns"],'
            f'"anti_patterns": ["list of anti-patterns"]}}'
        )

        try:
            return await self.llm_client.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,
            )
        except Exception as e:
            logger.warning(
                json.dumps({
                    "timestamp": time.time(),
                    "level": "WARNING",
                    "agent": "ConventionLearner",
                    "job_id": "",
                    "message": f"LLM convention analysis failed: {e}",
                })
            )
            return {}

    @staticmethod
    def extract_python_conventions(
        code_samples: list[str],
    ) -> dict[str, Any]:
        """Use AST to detect Python conventions from code samples.

        Analyzes naming patterns, type hint usage, docstring
        presence/style, and function lengths.

        Args:
            code_samples: List of Python code strings.

        Returns:
            Dict of detected conventions.
        """
        snake_count = 0
        camel_count = 0
        type_hint_count = 0
        no_hint_count = 0
        docstring_count = 0
        no_docstring_count = 0
        func_lengths: list[int] = []
        uses_async = False
        docstring_styles: dict[str, int] = {
            "google": 0, "numpy": 0, "sphinx": 0,
        }

        for sample in code_samples:
            try:
                tree = ast.parse(sample)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if isinstance(node, ast.AsyncFunctionDef):
                        uses_async = True

                    name = node.name
                    if "_" in name and name.islower():
                        snake_count += 1
                    elif (
                        name[0].islower()
                        and any(c.isupper() for c in name[1:])
                    ):
                        camel_count += 1

                    if node.returns:
                        type_hint_count += 1
                    else:
                        no_hint_count += 1

                    body = node.body
                    if (
                        body
                        and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)
                    ):
                        docstring_count += 1
                        ds = body[0].value.value
                        if "Args:" in ds or "Returns:" in ds:
                            docstring_styles["google"] += 1
                        elif "Parameters" in ds and "---" in ds:
                            docstring_styles["numpy"] += 1
                        elif ":param" in ds or ":type" in ds:
                            docstring_styles["sphinx"] += 1
                    else:
                        no_docstring_count += 1

                    length = (
                        node.end_lineno - node.lineno + 1
                        if node.end_lineno
                        else 10
                    )
                    func_lengths.append(length)

        naming = (
            "snake_case" if snake_count >= camel_count else "camelCase"
        )
        has_hints = type_hint_count > no_hint_count
        has_docs = docstring_count > no_docstring_count
        avg_len = (
            int(sum(func_lengths) / len(func_lengths))
            if func_lengths
            else 50
        )
        ds_style = max(
            docstring_styles, key=docstring_styles.get  # type: ignore
        ) if any(docstring_styles.values()) else "google"

        return {
            "naming_style": naming,
            "uses_type_hints": has_hints,
            "uses_docstrings": has_docs,
            "docstring_style": ds_style,
            "max_function_length": min(avg_len * 2, 200),
            "uses_async": uses_async,
        }

    @staticmethod
    def _merge_conventions(
        heuristic: dict[str, Any],
        llm_result: dict[str, Any],
    ) -> RepoConventions:
        """Merge heuristic and LLM convention results.

        Heuristic results take precedence for measurable fields.
        LLM results fill in pattern/anti-pattern lists.

        Args:
            heuristic: Conventions detected via AST analysis.
            llm_result: Conventions detected via LLM.

        Returns:
            Merged RepoConventions object.
        """
        merged = {**llm_result, **heuristic}

        common = llm_result.get("common_patterns", [])
        if isinstance(common, list):
            merged["common_patterns"] = common[:10]
        else:
            merged["common_patterns"] = []

        anti = llm_result.get("anti_patterns", [])
        if isinstance(anti, list):
            merged["anti_patterns"] = anti[:10]
        else:
            merged["anti_patterns"] = []

        safe_fields = {}
        for field in RepoConventions.model_fields:
            if field in merged:
                safe_fields[field] = merged[field]

        return RepoConventions(**safe_fields)

    def _get_cached(self, key: str) -> Optional[RepoConventions]:
        """Retrieve cached conventions from Redis.

        Args:
            key: Redis cache key.

        Returns:
            RepoConventions if found in cache, None otherwise.
        """
        if not self.redis_client:
            return None
        try:
            data = self.redis_client.get(key)
            if data:
                return RepoConventions(**json.loads(data))
        except (redis.RedisError, json.JSONDecodeError, Exception):
            pass
        return None

    def _cache_result(
        self, key: str, conventions: RepoConventions
    ) -> None:
        """Store conventions in Redis cache.

        Args:
            key: Redis cache key.
            conventions: RepoConventions object to cache.
        """
        if not self.redis_client:
            return
        try:
            self.redis_client.setex(
                key,
                CONVENTION_CACHE_TTL,
                conventions.model_dump_json(),
            )
        except redis.RedisError:
            pass
