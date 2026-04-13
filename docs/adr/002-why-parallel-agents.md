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
- **4x faster execution**: With `asyncio.gather()`, all 4 agents analyze files simultaneously. A review that takes 30 seconds sequentially completes in ~8 seconds with parallel execution.
- **Independent failure handling**: If the SecurityScanner hits a rate limit, the other 3 agents still complete. With a single prompt, one failure means zero results.
- **Easier debugging**: When a finding is incorrect, we can trace it to a specific agent and adjust that agent's prompt without affecting the others.
- **Different thresholds**: Security issues warrant lower confidence thresholds (we'd rather false-positive on a secret leak than miss it). Style issues use higher thresholds. A single prompt can't easily apply different confidence filters.
- **Different techniques**: SecurityScanner uses fast regex matching before LLM analysis. TestCoverage cross-references test files. These specialized approaches aren't possible in a single prompt.

## Consequences

- Higher total token usage (4 separate LLM calls per file vs 1)
- Requires deduplication when multiple agents flag the same issue
- More complex orchestration code in the worker
- Each agent can be developed, tested, and tuned independently
