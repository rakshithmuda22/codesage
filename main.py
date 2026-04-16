"""CodeSage — Autonomous Code Review Agent.

FastAPI application that receives GitHub webhooks for pull request
events, enqueues review jobs to Redis, and provides a status API
and dashboard.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.worker import ReviewWorker
from models.schemas import ReviewJob

load_dotenv()

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging.

    Formats log records as JSON objects with timestamp, level,
    and message fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as a JSON string.

        Args:
            record: Log record to format.

        Returns:
            JSON-formatted log string.
        """
        log_data = {
            "timestamp": time.time(),
            "level": record.levelname,
            "agent": getattr(record, "agent", "Main"),
            "job_id": getattr(record, "job_id", ""),
            "message": record.getMessage(),
        }
        return json.dumps(log_data)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    handlers=[handler],
)
logger = logging.getLogger("codesage")

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="CodeSage",
    description="Autonomous Code Review Agent — "
    "4 parallel AI agents that review your PRs",
    version="1.0.0",
)

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# In-memory job tracking (for the dashboard; Redis holds the real state)
recent_jobs: list[dict[str, Any]] = []
MAX_RECENT_JOBS = 50


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------


@app.post("/webhook/github")
async def github_webhook(request: Request) -> JSONResponse:
    """Receive and process GitHub webhook events.

    Verifies the webhook signature, filters for pull_request events
    with action 'opened' or 'synchronize', creates a ReviewJob,
    and enqueues it to Redis. Returns 200 immediately.

    Args:
        request: Incoming FastAPI request.

    Returns:
        JSON response with job status and IDs.

    Raises:
        HTTPException: 401 if signature verification fails.
    """
    # 1. Verify webhook signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()
    webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

    if not webhook_secret:
        # Fail closed. A missing secret is almost always a deploy mistake;
        # silently accepting unauthenticated webhooks would let anyone
        # enqueue review jobs and burn the Groq quota.
        logger.error(
            json.dumps({
                "timestamp": time.time(),
                "level": "ERROR",
                "agent": "Webhook",
                "job_id": "",
                "message": "GITHUB_WEBHOOK_SECRET not configured; refusing request",
            })
        )
        raise HTTPException(
            status_code=503,
            detail="Webhook secret not configured",
        )

    expected = "sha256=" + hmac.new(
        webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        logger.warning(
            json.dumps({
                "timestamp": time.time(),
                "level": "WARNING",
                "agent": "Webhook",
                "job_id": "",
                "message": "Invalid webhook signature rejected",
            })
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature",
        )

    # 2. Parse event
    event_type = request.headers.get("X-GitHub-Event", "")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400, detail="Invalid JSON payload"
        )

    # 3. Filter events
    if event_type == "ping":
        return JSONResponse(
            content={"status": "pong", "zen": payload.get("zen", "")}
        )

    if event_type != "pull_request":
        return JSONResponse(content={
            "status": "ignored",
            "reason": f"Event type '{event_type}' not handled",
        })

    action = payload.get("action", "")
    if action not in ("opened", "synchronize"):
        return JSONResponse(content={
            "status": "ignored",
            "reason": f"Action '{action}' not handled",
        })

    # 4. Create and enqueue job
    pr = payload.get("pull_request", {})
    installation = payload.get("installation", {})

    job = ReviewJob(
        job_id=str(uuid.uuid4()),
        repo_full_name=payload.get("repository", {}).get(
            "full_name", ""
        ),
        pr_number=pr.get("number", 0),
        pr_title=pr.get("title", ""),
        head_sha=pr.get("head", {}).get("sha", ""),
        base_sha=pr.get("base", {}).get("sha", ""),
        installation_id=installation.get("id", 0),
        created_at=datetime.now(tz=None),
        status="queued",
    )

    try:
        worker = ReviewWorker()
        rq_job_id = worker.enqueue_review(job)
    except Exception as e:
        logger.error(
            json.dumps({
                "timestamp": time.time(),
                "level": "ERROR",
                "agent": "Webhook",
                "job_id": job.job_id,
                "message": f"Failed to enqueue job: {e}",
            })
        )
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "message": "Queue unavailable",
                "job_id": job.job_id,
            },
        )

    # Track for dashboard
    job_record = {
        "job_id": job.job_id,
        "rq_job_id": rq_job_id,
        "repo": job.repo_full_name,
        "pr_number": job.pr_number,
        "pr_title": job.pr_title,
        "status": "queued",
        "created_at": job.created_at.isoformat(),
    }
    recent_jobs.insert(0, job_record)
    if len(recent_jobs) > MAX_RECENT_JOBS:
        recent_jobs.pop()

    logger.info(
        json.dumps({
            "timestamp": time.time(),
            "level": "INFO",
            "agent": "Webhook",
            "job_id": job.job_id,
            "message": "Review job queued",
            "repo": job.repo_full_name,
            "pr": job.pr_number,
            "rq_job_id": rq_job_id,
        })
    )

    return JSONResponse(content={
        "status": "queued",
        "job_id": job.job_id,
        "rq_job_id": rq_job_id,
    })


# ---------------------------------------------------------------------------
# Job status endpoint
# ---------------------------------------------------------------------------


@app.get("/jobs/{job_id}")
async def get_job_status(job_id: str) -> JSONResponse:
    """Get the status of a review job.

    Checks in-memory records and Redis for job status.

    Args:
        job_id: CodeSage job ID or RQ job ID.

    Returns:
        JSON response with job status and result if complete.
    """
    # Check in-memory records
    for record in recent_jobs:
        if record["job_id"] == job_id or record.get("rq_job_id") == job_id:
            try:
                worker = ReviewWorker()
                rq_status = worker.get_job_status(
                    record.get("rq_job_id", "")
                )
                record["status"] = rq_status.get("status", "unknown")
                record["result"] = rq_status.get("result")
                return JSONResponse(content=record)
            except Exception:
                return JSONResponse(content=record)

    return JSONResponse(
        status_code=404,
        content={"error": "Job not found", "job_id": job_id},
    )


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@app.get("/jobs")
async def list_jobs() -> JSONResponse:
    """List recent review jobs.

    Returns:
        JSON response with list of recent jobs.
    """
    return JSONResponse(content={"jobs": recent_jobs})


@app.get("/health")
async def health() -> JSONResponse:
    """Health check endpoint.

    Returns:
        JSON response with service status and configuration.
    """
    return JSONResponse(content={
        "status": "ok",
        "agents": [
            "BugDetector",
            "SecurityScanner",
            "StyleAdvisor",
            "TestCoverage",
        ],
        "model": "llama-3.1-8b-instant",
        "queue": "redis",
        "version": "1.0.0",
    })


@app.get("/")
async def dashboard() -> FileResponse:
    """Serve the dashboard HTML page.

    Returns:
        FileResponse serving static/index.html.

    Raises:
        HTTPException: 404 if the dashboard file is missing.
    """
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(
        status_code=404, detail="Dashboard not found"
    )
