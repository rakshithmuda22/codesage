# ADR 002: Why 4 Parallel Agents Instead of 1 Large Prompt

## Status
Accepted

## Context
We need to analyze code for bugs, security issues, style violations, and test coverage. The question is whether to use one large LLM prompt that covers everything, or specialized agents that run in parallel.

## Options Considered

1. **Single mega-prompt** - One LLM call that checks for all issue types
2. **Sequential agents** - Run each agent one after another
3. **Parallel specialized agents** - 4 focused agents running concurrently via asyncio.gather()

## Decision
We chose **4 parallel specialized agents**: BugDetector, SecurityScanner, StyleAdvisor, and TestCoverage.

## Rationale

- **Specialization improves accuracy**: Each agent has a focused system prompt optimized for its domain. The SecurityScanner knows OWASP Top 10 patterns. The BugDetector focuses on null dereferences and off-by-one errors. A single prompt trying to do everything produces lower-quality results because the LLM's attention is split.
- **Latency is the slowest agent, not the sum**: With `asyncio.gather()`, all 4 agents analyze files simultaneously. `tests/test_worker.py` checks this with four stub agents that each sleep 0.2 s: they all start before the first finishes and the pipeline takes about 0.2 s instead of 0.8 s.
- **Independent failure handling**: If the SecurityScanner hits a rate limit, the other 3 agents still complete. With a single prompt, one failure means zero results.
- **Easier debugging**: When a finding is incorrect, we can trace it to a specific agent and adjust that agent's prompt without affecting the others.
- **Different thresholds**: Security issues warrant a lower confidence cut-off (0.5 for LLM findings; regex hits are 0.9) because a missed secret is worse than a false alarm; BugDetector and StyleAdvisor drop findings under 0.6, and StyleAdvisor caps severity at MEDIUM. A single prompt can't easily apply different filters per category.
- **Different techniques**: SecurityScanner uses fast regex matching before LLM analysis. TestCoverage cross-references test files. These specialized approaches aren't possible in a single prompt.

## Consequences

- Higher total token usage (4 separate LLM calls per file vs 1)
- Requires deduplication when multiple agents flag the same issue
- More complex orchestration code in the worker
- Each agent can be developed, tested, and tuned independently
