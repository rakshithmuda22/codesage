"""Groq API wrapper with rate limiting, retry logic, and structured output.

Provides a resilient LLM client that respects Groq free-tier limits
(30 requests/minute) using a token bucket, retries with exponential
backoff, and validates JSON responses against schemas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

import jsonschema
from groq import AsyncGroq, RateLimitError, APIError

logger = logging.getLogger("codesage")


class LLMClient:
    """Async Groq API client with rate limiting and retry.

    Attributes:
        client: AsyncGroq client instance.
        model: Model identifier for completions.
        semaphore: Limits concurrent API calls.
        max_calls_per_minute: Rate limit ceiling.
        call_timestamps: Rolling window of call times.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "llama-3.1-8b-instant",
        max_concurrent: int = 5,
        max_calls_per_minute: int = 30,
    ) -> None:
        """Initialize the LLM client.

        Args:
            api_key: Groq API key. Falls back to GROQ_API_KEY env var.
            model: Model ID for completions.
            max_concurrent: Maximum concurrent API calls.
            max_calls_per_minute: Rate limit (Groq free tier = 30).

        Raises:
            ValueError: If no API key is provided or found in env.
        """
        resolved_key = api_key or os.environ.get("GROQ_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "GROQ_API_KEY must be set in environment or passed directly"
            )
        self.client = AsyncGroq(api_key=resolved_key)
        self.model = model
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.max_calls_per_minute = max_calls_per_minute
        self.call_timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def _enforce_rate_limit(self) -> None:
        """Token bucket rate limiter: sleep if calls exceed limit.

        Maintains a sliding window of timestamps and blocks if
        the window is full.
        """
        async with self._lock:
            now = time.time()
            cutoff = now - 60.0
            self.call_timestamps = [
                t for t in self.call_timestamps if t > cutoff
            ]
            if len(self.call_timestamps) >= self.max_calls_per_minute:
                oldest = self.call_timestamps[0]
                sleep_time = 60.0 - (now - oldest) + 0.5
                logger.info(
                    json.dumps({
                        "timestamp": now,
                        "level": "INFO",
                        "agent": "LLMClient",
                        "job_id": "",
                        "message": f"Rate limit reached, sleeping {sleep_time:.1f}s",
                    })
                )
                await asyncio.sleep(sleep_time)
            self.call_timestamps.append(time.time())

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2000,
        temperature: float = 0.1,
        response_format: str = "json",
    ) -> dict[str, Any]:
        """Send a completion request to Groq and parse JSON response.

        Args:
            system_prompt: System-level instruction.
            user_prompt: User-level prompt content.
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            response_format: Expected format (always "json").

        Returns:
            Parsed JSON dict from the LLM response.

        Raises:
            ValueError: If JSON parsing fails after retries.
            APIError: If the API call fails after all retries.
        """
        retries = 3
        backoff = 1.0

        for attempt in range(retries):
            try:
                async with self.semaphore:
                    await self._enforce_rate_limit()
                    start = time.time()

                    messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ]

                    response_kwargs: dict[str, Any] = {
                        "model": self.model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    }
                    if response_format == "json":
                        response_kwargs["response_format"] = {
                            "type": "json_object"
                        }

                    response = await self.client.chat.completions.create(
                        **response_kwargs
                    )
                    latency = time.time() - start
                    content = response.choices[0].message.content or "{}"
                    tokens_used = (
                        response.usage.total_tokens
                        if response.usage
                        else 0
                    )

                    logger.info(
                        json.dumps({
                            "timestamp": time.time(),
                            "level": "INFO",
                            "agent": "LLMClient",
                            "job_id": "",
                            "message": "LLM call completed",
                            "tokens": tokens_used,
                            "latency_s": round(latency, 2),
                            "attempt": attempt + 1,
                        })
                    )

                    return self._parse_json(content)

            except RateLimitError:
                logger.warning(
                    json.dumps({
                        "timestamp": time.time(),
                        "level": "WARNING",
                        "agent": "LLMClient",
                        "job_id": "",
                        "message": "Rate limited by Groq, sleeping 60s",
                    })
                )
                await asyncio.sleep(60)

            except APIError as e:
                logger.error(
                    json.dumps({
                        "timestamp": time.time(),
                        "level": "ERROR",
                        "agent": "LLMClient",
                        "job_id": "",
                        "message": f"API error: {e}",
                        "attempt": attempt + 1,
                    })
                )
                if attempt < retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    raise

            except (json.JSONDecodeError, ValueError):
                if attempt < retries - 1:
                    logger.warning(
                        json.dumps({
                            "timestamp": time.time(),
                            "level": "WARNING",
                            "agent": "LLMClient",
                            "job_id": "",
                            "message": "Invalid JSON response, retrying with stricter prompt",
                        })
                    )
                    user_prompt = (
                        user_prompt
                        + "\n\nIMPORTANT: Return ONLY valid JSON. "
                        "No markdown, no explanation, no code fences."
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    raise ValueError(
                        "LLM returned invalid JSON after all retries"
                    )

        return {}

    async def complete_with_structured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        output_schema: dict[str, Any],
        max_tokens: int = 2000,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        """Send a completion request and validate against a JSON schema.

        Args:
            system_prompt: System-level instruction.
            user_prompt: User-level prompt content.
            output_schema: JSON schema to validate the response against.
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            Validated JSON dict matching the provided schema.

        Raises:
            ValueError: If response doesn't match schema after retries.
        """
        schema_instruction = (
            f"\n\nReturn ONLY valid JSON matching this schema:\n"
            f"{json.dumps(output_schema, indent=2)}"
        )
        augmented_system = system_prompt + schema_instruction

        for attempt in range(2):
            result = await self.complete(
                system_prompt=augmented_system,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            try:
                jsonschema.validate(instance=result, schema=output_schema)
                return result
            except jsonschema.ValidationError as e:
                if attempt == 0:
                    logger.warning(
                        json.dumps({
                            "timestamp": time.time(),
                            "level": "WARNING",
                            "agent": "LLMClient",
                            "job_id": "",
                            "message": f"Schema validation failed: {e.message}",
                        })
                    )
                    user_prompt = (
                        user_prompt
                        + f"\n\nYour previous response had a validation error: "
                        f"{e.message}. Please fix and return valid JSON."
                    )
                else:
                    raise ValueError(
                        f"LLM output failed schema validation: {e.message}"
                    )

        return {}

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        """Parse JSON from LLM response, stripping markdown fences.

        Args:
            content: Raw string content from the LLM.

        Returns:
            Parsed dictionary.

        Raises:
            json.JSONDecodeError: If content is not valid JSON.
        """
        cleaned = content.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        return json.loads(cleaned)
