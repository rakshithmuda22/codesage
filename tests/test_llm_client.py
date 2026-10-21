"""Tests for LLMClient: model selection, reasoning-model params, JSON parsing.

The Groq SDK client is replaced with a recording fake, so no network or
API key is needed.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.llm_client import (
    DEFAULT_GROQ_MODEL,
    REASONING_TOKEN_HEADROOM,
    LLMClient,
)


def reply(content: str | None, finish_reason: str = "stop"):
    """Shape of a Groq chat completion with one choice."""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason=finish_reason,
        )],
        usage=SimpleNamespace(total_tokens=7),
    )


class FakeGroq:
    """Records create() kwargs and returns queued replies."""

    def __init__(self, replies: list[Any]):
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def make_client(monkeypatch, replies, model=None, **env) -> tuple[LLMClient, FakeGroq]:
    for key in ("GROQ_MODEL", "GROQ_REASONING_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    client = LLMClient(api_key="test-key", model=model)
    fake = FakeGroq(replies)
    client.client = fake
    return client, fake


def test_default_model_is_a_currently_served_one():
    # llama-3.1-8b-instant is scheduled for shutdown on Groq (2026-08-16).
    assert DEFAULT_GROQ_MODEL == "openai/gpt-oss-20b"


def test_model_comes_from_env_then_default(monkeypatch):
    client, _ = make_client(monkeypatch, [])
    assert client.model == DEFAULT_GROQ_MODEL
    client, _ = make_client(monkeypatch, [], GROQ_MODEL="openai/gpt-oss-120b")
    assert client.model == "openai/gpt-oss-120b"
    client, _ = make_client(
        monkeypatch, [], model="explicit/model", GROQ_MODEL="from-env"
    )
    assert client.model == "explicit/model"


@pytest.mark.asyncio
async def test_reasoning_model_gets_low_effort_no_reasoning_and_headroom(monkeypatch):
    client, fake = make_client(monkeypatch, [reply('{"findings": []}')])
    out = await client.complete("sys", "user", max_tokens=2000)

    assert out == {"findings": []}
    call = fake.calls[0]
    assert call["model"] == "openai/gpt-oss-20b"
    assert call["max_tokens"] == 2000 + REASONING_TOKEN_HEADROOM
    assert call["extra_body"] == {
        "reasoning_effort": "low", "include_reasoning": False,
    }
    assert call["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_reasoning_effort_is_configurable(monkeypatch):
    client, fake = make_client(
        monkeypatch, [reply("{}")], GROQ_REASONING_EFFORT="medium"
    )
    await client.complete("s", "u")
    assert fake.calls[0]["extra_body"]["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_non_reasoning_model_gets_plain_parameters(monkeypatch):
    client, fake = make_client(
        monkeypatch, [reply("{}")], model="llama-3.3-70b-versatile"
    )
    await client.complete("s", "u", max_tokens=1500)
    call = fake.calls[0]
    assert call["max_tokens"] == 1500
    assert "extra_body" not in call


@pytest.mark.asyncio
async def test_fenced_and_prose_wrapped_json_is_accepted(monkeypatch):
    payload = {"findings": [{"line": 3}]}
    client, _ = make_client(monkeypatch, [
        reply("```json\n" + json.dumps(payload) + "\n```"),
        reply("Here is the review:\n" + json.dumps(payload) + "\nHope it helps."),
    ])
    assert await client.complete("s", "u") == payload
    assert await client.complete("s", "u") == payload


@pytest.mark.asyncio
async def test_empty_answer_from_exhausted_budget_is_retried(monkeypatch):
    """A reasoning model that ran out of tokens returns empty content."""
    client, fake = make_client(monkeypatch, [
        reply("", finish_reason="length"),
        reply(None, finish_reason="length"),
        reply('{"findings": []}'),
    ])
    assert await client.complete("s", "u") == {"findings": []}
    assert len(fake.calls) == 3
    assert "ONLY valid JSON" in fake.calls[2]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_persistently_empty_answer_raises_instead_of_reporting_nothing(
    monkeypatch,
):
    client, _ = make_client(monkeypatch, [reply("", "length")] * 3)
    with pytest.raises(ValueError):
        await client.complete("s", "u")


@pytest.mark.asyncio
async def test_non_object_json_is_rejected(monkeypatch):
    client, _ = make_client(monkeypatch, [reply("[1, 2]")] * 3)
    with pytest.raises(ValueError):
        await client.complete("s", "u")
