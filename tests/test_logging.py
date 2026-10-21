"""JSON log lines must be flat, one object per line."""

from __future__ import annotations

import json
import logging

from core.logging_setup import JSONFormatter


def make_record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("codesage", logging.INFO, __file__, 1, msg, None, None)


def test_pre_encoded_json_message_is_not_double_wrapped():
    line = JSONFormatter().format(make_record(
        json.dumps({"agent": "Worker", "message": "hi", "job_id": "j1"})
    ))
    out = json.loads(line)
    assert out["message"] == "hi"
    assert out["agent"] == "Worker"
    assert out["level"] == "INFO"


def test_plain_message_gets_an_envelope():
    out = json.loads(JSONFormatter().format(make_record("plain text")))
    assert out["message"] == "plain text"
    assert {"timestamp", "level", "agent", "job_id"} <= set(out)
