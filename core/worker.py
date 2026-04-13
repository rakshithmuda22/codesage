"""Redis queue worker for asynchronous code review processing.

Handles the full review pipeline: fetch files, build dependency graph,
learn conventions, run 4 agents in parallel, deduplicate findings,
and post the review to GitHub.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import redis
from rq import Queue

from agents.bug_detector import BugDetector
from agents.security_scanner import SecurityScanner
from agents.style_advisor import StyleAdvisor
from agents.test_coverage import TestCoverageAgent
from core.convention_learner import ConventionLearner
from core.deduplicator import Deduplicator
from core.dependency_graph import DependencyGraphBuilder
from core.github_client import GitHubClient
from core.llm_client import LLMClient
from core.repo_analyzer import RepoAnalyzer
from models.schemas import AgentResult, ReviewJob, ReviewSummary

logger = logging.getLogger("codesage")

QUEUE_NAME = "codesage"


class ReviewWorker:
    """Manages the Redis job queue for code reviews.

    Enqueues review jobs and provides status checking.

    Attributes:
        redis_conn: Redis connection instance.
        queue: RQ Queue for job processing.
    """

    def __init__(
        self, redis_url: str | None = None
    ) -> None:
        """Initialize the review worker.

        Args:
            redis_url: Redis connection URL. Falls back to
                REDIS_URL env var.
        """
        url = redis_url or os.environ.get(
            "REDIS_URL", "redis://localhost:6379"
        )
        self.redis_conn = redis.from_url(url)
        self.queue = Queue(QUEUE_NAME, connection=self.redis_conn)

    def enqueue_review(self, job: ReviewJob) -> str:
        """Add a review job to the Redis queue.

        Args:
            job: ReviewJob to process asynchronously.

        Returns:
            RQ job ID string.
        """
        rq_job = self.queue.enqueue(
            process_review_sync,
            job.model_dump(mode="json"),
            job_timeout=300,
            result_ttl=86400,
        )
        logger.info(
            json.dumps({
                "timestamp": time.time(),
                "level": "INFO",
                "agent": "ReviewWorker",
                "job_id": job.job_id,
                "message": "Review job enqueued",
                "rq_job_id": rq_job.id,
                "repo": job.repo_full_name,
                "pr": job.pr_number,
            })
        )
        return rq_job.id

    def get_job_status(self, rq_job_id: str) -> dict[str, Any]:
        """Check the status of an enqueued job.

        Args:
            rq_job_id: RQ job identifier.

        Returns:
            Dict with status, result (if complete), and metadata.
        """
        from rq.job import Job as RQJob
        try:
            rq_job = RQJob.fetch(rq_job_id, connection=self.redis_conn)
            return {
                "rq_job_id": rq_job_id,
                "status": rq_job.get_status(),
                "result": rq_job.result,
                "enqueued_at": str(rq_job.enqueued_at),
                "started_at": str(rq_job.started_at),
                "ended_at": str(rq_job.ended_at),
            }
        except Exception as e:
            return {
                "rq_job_id": rq_job_id,
                "status": "unknown",
                "error": str(e),
            }


def process_review_sync(job_dict: dict[str, Any]) -> dict[str, Any]:
    """Sync wrapper for the async review pipeline.

    RQ workers are synchronous, so this function bridges to the
    async process_review coroutine using asyncio.run().

    Args:
        job_dict: Serialized ReviewJob dictionary.

    Returns:
        Serialized ReviewSummary dictionary.
    """
    return asyncio.run(process_review(job_dict))


async def process_review(
    job_dict: dict[str, Any],
) -> dict[str, Any]:
    """Main async orchestration function for code review.

    Executes the full pipeline:
    1. Initialize clients
    2. Fetch changed files with content
    3. Build dependency graph and impact scores
    4. Learn repo conventions (cached)
    5. Run 4 agents in parallel
    6. Deduplicate and rank findings
    7. Post review to GitHub

    Args:
        job_dict: Serialized ReviewJob dictionary.

    Returns:
        Serialized ReviewSummary dictionary.

    Raises:
        Exception: If any critical step fails (logged and re-raised).
    """
    job = ReviewJob(**job_dict)
    pipeline_start = time.time()

    logger.info(
        json.dumps({
            "timestamp": time.time(),
            "level": "INFO",
            "agent": "Worker",
            "job_id": job.job_id,
            "message": "Starting review pipeline",
            "repo": job.repo_full_name,
            "pr": job.pr_number,
        })
    )

    github = GitHubClient(
        app_id=os.environ.get("GITHUB_APP_ID"),
        installation_id=job.installation_id,
    )
    llm = LLMClient()

    try:
        # Step 1: Fetch changed files
        analyzer = RepoAnalyzer(github)
        changed_files = await analyzer.analyze_pr(
            job.repo_full_name,
            job.pr_number,
            job.head_sha,
            job.base_sha,
        )

        if not changed_files:
            logger.info(
                json.dumps({
                    "timestamp": time.time(),
                    "level": "INFO",
                    "agent": "Worker",
                    "job_id": job.job_id,
                    "message": "No reviewable files found",
                })
            )
            return ReviewSummary(
                job_id=job.job_id,
                repo=job.repo_full_name,
                pr_number=job.pr_number,
                review_decision="APPROVE",
                executive_summary="No reviewable files in this PR.",
                processing_time_seconds=time.time() - pipeline_start,
            ).model_dump(mode="json")

        # Step 2: Build dependency graph
        graph_builder = DependencyGraphBuilder()
        dep_graph = graph_builder.build_graph("", changed_files)
        impact_scores = graph_builder.calculate_impact_scores(
            dep_graph,
            [f.filename for f in changed_files],
        )
        changed_files = graph_builder.get_review_priority_order(
            changed_files, impact_scores
        )

        # Step 3: Learn repo conventions (cached in Redis)
        learner = ConventionLearner(github, llm)
        conventions = await learner.learn_conventions(
            job.repo_full_name
        )

        # Step 4: Run all 4 agents IN PARALLEL
        agents = [
            BugDetector(llm),
            SecurityScanner(llm),
            StyleAdvisor(llm),
            TestCoverageAgent(llm),
        ]

        agent_tasks = [
            agent.analyze(changed_files, conventions, dep_graph)
            for agent in agents
        ]
        raw_results = await asyncio.gather(
            *agent_tasks, return_exceptions=True
        )

        agent_results: list[AgentResult] = []
        for i, result in enumerate(raw_results):
            if isinstance(result, AgentResult):
                agent_results.append(result)
            elif isinstance(result, Exception):
                logger.error(
                    json.dumps({
                        "timestamp": time.time(),
                        "level": "ERROR",
                        "agent": agents[i].name,
                        "job_id": job.job_id,
                        "message": f"Agent failed: {result}",
                    })
                )
                agent_results.append(AgentResult(
                    agent=agents[i].name,
                    error=str(result),
                ))

        # Step 5: Deduplicate and rank findings
        deduplicator = Deduplicator()
        final_findings = deduplicator.deduplicate(agent_results)
        review_decision = deduplicator.determine_review_decision(
            final_findings
        )

        # Step 6: Build review comments
        comments = []
        for finding in final_findings:
            comments.append({
                "path": finding.file_path,
                "line": finding.line_number,
                "side": "RIGHT",
                "body": deduplicator.format_finding_for_github(finding),
            })

        summary = Deduplicator.generate_executive_summary(
            final_findings, agent_results, job.model_dump()
        )

        # Step 7: Post review to GitHub
        diff_text = await github.get_pr_diff(
            job.repo_full_name, job.pr_number
        )
        await github.create_pr_review(
            repo=job.repo_full_name,
            pr_number=job.pr_number,
            head_sha=job.head_sha,
            summary_body=summary,
            comments=comments,
            event=review_decision,
            diff_text=diff_text,
        )

        elapsed = time.time() - pipeline_start

        review_summary = ReviewSummary(
            job_id=job.job_id,
            repo=job.repo_full_name,
            pr_number=job.pr_number,
            total_findings=len(final_findings),
            critical_count=sum(
                1 for f in final_findings if f.severity == "CRITICAL"
            ),
            high_count=sum(
                1 for f in final_findings if f.severity == "HIGH"
            ),
            medium_count=sum(
                1 for f in final_findings if f.severity == "MEDIUM"
            ),
            low_count=sum(
                1 for f in final_findings if f.severity == "LOW"
            ),
            files_reviewed=len(changed_files),
            agent_results=agent_results,
            review_decision=review_decision,
            executive_summary=summary,
            processing_time_seconds=elapsed,
        )

        logger.info(
            json.dumps({
                "timestamp": time.time(),
                "level": "INFO",
                "agent": "Worker",
                "job_id": job.job_id,
                "message": "Review pipeline complete",
                "total_findings": len(final_findings),
                "decision": review_decision,
                "elapsed_s": round(elapsed, 2),
            })
        )

        return review_summary.model_dump(mode="json")

    except Exception as e:
        logger.error(
            json.dumps({
                "timestamp": time.time(),
                "level": "ERROR",
                "agent": "Worker",
                "job_id": job.job_id,
                "message": f"Pipeline failed: {e}",
            })
        )
        raise
    finally:
        await github.close()
