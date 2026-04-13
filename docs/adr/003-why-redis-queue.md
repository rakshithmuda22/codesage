# ADR 003: Why Redis Queue Instead of Synchronous Processing

## Status
Accepted

## Context
GitHub webhooks have a 10-second timeout. If the webhook endpoint doesn't return within 10 seconds, GitHub marks the delivery as failed and may disable the webhook. A full code review takes 30-90 seconds.

## Options Considered

1. **Synchronous processing** - Do the review in the webhook handler
2. **FastAPI BackgroundTasks** - Use built-in background task support
3. **Redis + RQ** - Enqueue jobs to Redis, process with separate workers
4. **Celery** - Full-featured distributed task queue

## Decision
We chose **Redis + RQ** (Redis Queue).

## Rationale

- **GitHub's 10-second timeout**: The webhook handler must return 200 within 10 seconds. A code review with 4 parallel agents, LLM calls, and GitHub API interactions takes 30-90 seconds. We must decouple receiving the webhook from processing the review.
- **Why not BackgroundTasks**: FastAPI's BackgroundTasks run in the same process. If the server restarts, in-progress reviews are lost. There's no retry mechanism, no job status tracking, and no way to scale workers independently.
- **Why not Celery**: Celery is powerful but heavyweight for this use case. It requires a message broker (RabbitMQ or Redis) plus result backend, adds significant configuration complexity, and is overkill for a single job type.
- **Why RQ fits**: RQ is lightweight (pure Python, Redis-only), provides exactly what we need: job queuing, status tracking, retry on failure, result storage, and timeout handling. It's simple to configure and debug.
- **Worker scaling**: RQ workers can be scaled independently. During high-traffic periods, add more worker containers. The Redis queue distributes jobs automatically.
- **Retry on failure**: If a review fails (LLM timeout, GitHub API error), RQ automatically retries the job. Failed jobs are tracked and can be inspected.
- **Job status API**: RQ stores job status and results in Redis, enabling our `/jobs/{job_id}` status endpoint and dashboard polling.

## Consequences

- Redis is a required dependency (adds operational complexity)
- RQ workers are separate processes (need Docker Compose orchestration)
- Job results expire after 24 hours (configurable via result_ttl)
- The worker bridges async/sync: RQ is synchronous, our agents are async, so we use `asyncio.run()` as the bridge
