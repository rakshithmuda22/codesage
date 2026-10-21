"""Structured JSON logging shared by the API process and the RQ worker."""

from __future__ import annotations

import json
import logging
import os
import sys
import time


class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging.

    CodeSage modules log ``json.dumps({...})`` strings; those are emitted
    as-is (one flat JSON object per line) instead of being nested inside
    a second JSON envelope. Plain messages get a timestamp, level, agent
    and job_id wrapper.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as a single-line JSON string.

        Args:
            record: Log record to format.

        Returns:
            JSON-formatted log string.
        """
        message = record.getMessage()
        try:
            inner = json.loads(message)
        except ValueError:
            inner = None
        if isinstance(inner, dict):
            inner.setdefault("level", record.levelname)
            return json.dumps(inner)
        return json.dumps({
            "timestamp": time.time(),
            "level": record.levelname,
            "agent": getattr(record, "agent", "Main"),
            "job_id": getattr(record, "job_id", ""),
            "message": message,
        })


def configure_logging() -> None:
    """Send JSON logs to stdout. Safe to call more than once."""
    root = logging.getLogger()
    if any(isinstance(h.formatter, JSONFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    # One INFO line per HTTP request drowns out the pipeline logs.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
