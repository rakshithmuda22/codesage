# CodeSage — Autonomous Code Review Agent

> 4 parallel AI agents that review your PRs and post real inline GitHub comments

[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3110/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109-009688.svg)](https://fastapi.tiangolo.com)
[![Groq](https://img.shields.io/badge/Groq-LLaMA%203.1-orange.svg)](https://console.groq.com)
[![Redis](https://img.shields.io/badge/Redis-7-red.svg)](https://redis.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://img.shields.io/badge/CI-passing-brightgreen.svg)](.github/workflows/ci.yml)

## Why I Built This

By 2026, 54% of PRs in production codebases receive AI-powered analysis, and engineering teams report 40% faster merge cycles. But most AI code review tools treat every repo the same — generic rules applied generically. CodeSage is different: it **learns your repo's conventions** from past merged PRs and checks new code against your team's actual patterns, not textbook rules.

## Technical Highlights

- **Convention learning:** StyleAdvisor analyzes the last 3 merged PRs to learn your repo's naming conventions, import patterns, and code style — then enforces them
- **True parallel execution:** 4 agents run simultaneously via `asyncio.gather()` with `return_exceptions=True` — one failing agent doesn't block the others
- **Cross-agent deduplication:** When BugDetector and SecurityScanner flag the same line, findings are merged with combined tags `[SECURITY][BUG]` and highest severity wins
- **Diff position mapping:** Inline GitHub comments land at the exact right line using a custom diff-position mapper (GitHub's API requires diff-relative positions, not absolute line numbers)
- **tree-sitter AST parsing:** Language-agnostic code analysis across Python, JavaScript, and TypeScript with a single parsing engine
- **GitHub App auth:** JWT → installation token flow (not PATs), with RQ workers for async processing

---

## Architecture

```
GitHub PR Opened
      |
      v
GitHub Webhook --> FastAPI /webhook --> Redis Queue
                        |                    |
                   Return 200           RQ Worker picks up job
                   immediately               |
                                    +--------+--------+
                              Repo Analyzer      Convention Learner
                              (get files,        (learn patterns
                               build dep graph)   from past PRs)
                                    |
                   +----------+----------+----------+
                   v          v          v          v
              BugDetector SecurityScanner StyleAdvisor TestCoverage
              (asyncio parallel -- all 4 run simultaneously)
                   |          |          |          |
                   +----------+----------+----------+
                                    |
                              Deduplicator
                              (merge + rank)
                                    |
                         GitHub PR Review API
                         (inline comments at
                          exact file:line)
```

## How It Works

1. **Webhook** — When a PR is opened or updated, GitHub sends a webhook to CodeSage
2. **Queue** — The webhook returns 200 immediately and enqueues the review job to Redis
3. **Analyze** — The RQ worker clones the repo, builds a dependency graph, and learns the repo's conventions
4. **Review** — 4 specialized AI agents analyze the code in parallel using asyncio.gather()
5. **Post** — Findings are deduplicated, ranked, and posted as real inline GitHub comments

## Agent Examples

### BugDetector
Finds logic errors, null dereferences, off-by-one errors:

> **HIGH: Possible None dereference on user.profile**
>
> `user.profile` can be None when the user has not completed onboarding, but `.name` is accessed without a null check on line 42.
>
> **Suggestion:** Add a None check before accessing .name

### SecurityScanner
Catches secrets, injection, OWASP Top 10:

> **CRITICAL: Hardcoded API key detected**
>
> API key is hardcoded in source code at line 17. This will be exposed in version control.
>
> **Suggestion:** Use environment variables via `os.environ.get("API_KEY")`

### StyleAdvisor
Learns your repo's conventions and checks for deviations:

> **LOW: Function uses camelCase but repo uses snake_case**
>
> Function `getUserData` on line 28 uses camelCase naming, but this project consistently uses snake_case.
>
> **Suggestion:** Rename to `get_user_data`

### TestCoverage
Identifies untested code paths:

> **MEDIUM: No test for validate_email()**
>
> The `validate_email` function has no corresponding test case. It handles user input validation which is critical for data integrity.
>
> **Suggestion:** Add tests for: valid email, invalid format, empty string, special characters

## Quick Start

### 1. Create a GitHub App

Go to [GitHub Settings > Developer settings > GitHub Apps](https://github.com/settings/apps) and create a new app:

- **Webhook URL:** Your server URL + `/webhook/github`
- **Permissions:** Pull Requests (read & write), Contents (read)
- **Events:** Pull request
- Generate a private key and download it

### 2. Get a Groq API Key

Sign up at [console.groq.com](https://console.groq.com) (free tier includes 30 requests/minute).

### 3. Configure Environment

```bash
cp .env.example .env
# Edit .env with your GitHub App ID, private key path, webhook secret, and Groq API key
```

### 4. Start with Docker

```bash
docker-compose up --build
```

### 5. Connect GitHub App

In your GitHub App settings, set the webhook URL to your server's public URL:
- Local development: use ngrok (see below)
- Production: your server's domain + `/webhook/github`

## Development Setup

```bash
# Clone and install
git clone https://github.com/rakshithmuda22/codesage.git && cd codesage
pip install -r requirements.txt

# Start Redis
docker run -d -p 6379:6379 redis:7-alpine

# Start the API server
uvicorn main:app --reload --port 8000

# Start a worker (separate terminal)
rq worker codesage --url redis://localhost:6379
```

### Local Webhook Testing with ngrok

```bash
# Install ngrok and expose port 8000
ngrok http 8000

# Copy the https URL (e.g., https://abc123.ngrok.io)
# Set it as your GitHub App's webhook URL: https://abc123.ngrok.io/webhook/github
```

### Test the Webhook Locally

```bash
curl -X POST http://localhost:8000/webhook/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: sha256=test" \
  -d '{
    "action": "opened",
    "pull_request": {
      "number": 1,
      "title": "Test PR",
      "head": {"sha": "abc123"},
      "base": {"sha": "def456"}
    },
    "repository": {"full_name": "owner/repo"},
    "installation": {"id": 12345}
  }'
```

> **Note:** For local testing without signature verification, temporarily remove the webhook secret from `.env`.

## Common Setup Mistake

**GitHub App vs. OAuth App confusion.** CodeSage uses a **GitHub App** (not an OAuth App). The key differences:

- **GitHub App** authenticates with a JWT signed by a private key, then exchanges it for an installation token. This is what CodeSage uses.
- **OAuth App** uses client ID/secret for user-level authorization. This does NOT work for posting PR reviews as a bot.

If you see `401 Unauthorized` errors, verify you're using a GitHub App with the correct private key and app ID.

## Project Structure

```
codesage/
├── main.py                    # FastAPI webhook receiver
├── agents/
│   ├── base_agent.py          # Abstract base with tree-sitter parsing
│   ├── bug_detector.py        # Logic errors, null risks, off-by-one
│   ├── security_scanner.py    # Secrets, injection, OWASP Top 10
│   ├── style_advisor.py       # Convention-aware style checking
│   └── test_coverage.py       # Untested code path detection
├── core/
│   ├── github_client.py       # GitHub App auth + PR review posting
│   ├── llm_client.py          # Groq API with rate limiting + retry
│   ├── repo_analyzer.py       # Changed file processing
│   ├── dependency_graph.py    # NetworkX dependency graph
│   ├── convention_learner.py  # Learn patterns from merged PRs
│   ├── deduplicator.py        # Cross-agent finding merge + rank
│   └── worker.py              # Redis queue worker + orchestration
├── models/
│   └── schemas.py             # Pydantic models
├── prompts/                   # LLM prompt templates
├── static/
│   └── index.html             # Dashboard
├── tests/                     # Pytest suite
├── docs/adr/                  # Architecture Decision Records
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Technical Deep Dive

### Diff Position Mapping

GitHub's REST API requires inline review comments to specify a **diff position** (not an absolute line number). The `DiffParser` class in `github_client.py` parses the unified diff output to build a mapping from absolute line numbers to diff-relative positions. Without this, every inline comment would fail with `422 Unprocessable Entity`.

### Tree-sitter AST Parsing

Agents use tree-sitter to build a concrete syntax tree for each file. This enables accurate function extraction across Python, JavaScript, and TypeScript with a single API. The `tree-sitter-languages` package pre-compiles grammars, eliminating the need for C compilers at runtime.

### Parallel Agent Execution

All 4 agents run simultaneously via `asyncio.gather()`. Each agent processes files concurrently using per-file async tasks. The Groq client uses a semaphore (max 5 concurrent calls) and a token bucket rate limiter (30 calls/min) to stay within free-tier limits.

### Convention Learning

Before reviewing, CodeSage fetches the last 3 merged PRs and analyzes the code patterns using both AST heuristics and LLM analysis. Results are cached in Redis for 24 hours. This means the StyleAdvisor checks code against the repo's own conventions, not generic rules.

### Cross-Agent Deduplication

When BugDetector and SecurityScanner both flag the same line, the Deduplicator merges them into a single comment with combined tags `[SECURITY][BUG]`, uses the highest severity, and merges descriptions from both agents.

## Running Tests

```bash
pytest tests/ -v --tb=short
```

As of the latest `main`, the suite is **38** pytest tests covering agents, GitHub flows, and workers (external calls mocked).

## License

MIT
