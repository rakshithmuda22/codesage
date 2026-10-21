"""Send a signed pull_request webhook to a running CodeSage and watch the job.

Works against the offline stack (docker-compose.demo.yml) or any CodeSage
instance whose GITHUB_WEBHOOK_SECRET you know::

    python devtools/send_webhook.py                 # sign + send + watch
    python devtools/send_webhook.py --bad-signature # expect 401
    python devtools/send_webhook.py --unsigned      # expect 401

With the fake GitHub API (devtools/fake_services.py) running it also
prints the review that CodeSage posted, line by line.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time

import httpx

PAYLOAD = {
    "action": "opened",
    "pull_request": {
        "number": 42,
        "title": "Add cart totals and user profile lookup",
        "head": {"sha": "a" * 40},
        "base": {"sha": "b" * 40},
    },
    "repository": {"full_name": "acme/shop"},
    "installation": {"id": 12345},
}


def sign(secret: str, body: bytes) -> str:
    """X-Hub-Signature-256 value for ``body``."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def show_review(fake_url: str) -> bool | None:
    """Print the review the fake GitHub API recorded.

    Returns:
        True if a review with inline comments was recorded, False if not,
        None if the fake API is not reachable (e.g. a real deployment).
    """
    try:
        reviews = httpx.get(f"{fake_url}/__reviews", timeout=5).json()
    except httpx.HTTPError:
        print("(fake GitHub API not reachable; skipping review dump)")
        return None
    if not reviews:
        print("fake GitHub API has recorded no review")
        return False
    review = reviews[-1]
    print(f"\nReview posted to PR #{review['pr']} "
          f"(event={review['event']}, {len(review['comments'])} inline comments)")
    for c in sorted(review["comments"], key=lambda c: (c["path"], c["line"])):
        title = re.search(r"\*\*\S+ (\w+: .*?)\*\*", c["body"])
        label = title.group(1) if title else c["body"].splitlines()[0]
        print(f"  {c['path']}:{c['line']:<3} {label}")
        print(f"      on: {c['source']}")
    extra = review["body"].split("**Findings outside the changed lines", 1)
    if len(extra) == 2:
        print("\nNot attachable inline (line outside every hunk), listed in "
              "the summary instead:")
        for line in extra[1].splitlines():
            if line.startswith("- **"):
                print("  " + line[2:].split(" — ")[0].replace("**", ""))
    return bool(review["comments"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--fake-url", default="http://localhost:9000")
    ap.add_argument(
        "--secret",
        default=os.environ.get("GITHUB_WEBHOOK_SECRET", "demo-webhook-secret"),
    )
    ap.add_argument("--bad-signature", action="store_true")
    ap.add_argument("--unsigned", action="store_true")
    ap.add_argument("--timeout", type=float, default=120)
    args = ap.parse_args()

    body = json.dumps(PAYLOAD).encode()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
    }
    if not args.unsigned:
        secret = "not-the-secret" if args.bad_signature else args.secret
        headers["X-Hub-Signature-256"] = sign(secret, body)

    print(f"POST {args.url}/webhook/github  "
          f"signature={'none' if args.unsigned else headers['X-Hub-Signature-256'][:24] + '...'}")
    resp = httpx.post(f"{args.url}/webhook/github", content=body,
                      headers=headers, timeout=10)
    print(f"-> {resp.status_code} {resp.text}")
    if args.bad_signature or args.unsigned:
        return 0 if resp.status_code == 401 else 1  # expected: rejected
    if resp.status_code != 200:
        return 1

    job_id = resp.json()["job_id"]
    seen, start = None, time.time()
    while time.time() - start < args.timeout:
        job = httpx.get(f"{args.url}/jobs/{job_id}", timeout=10).json()
        status = job.get("status")
        if status != seen:
            print(f"job {job_id[:8]}: {status}  (+{time.time() - start:.1f}s)")
            seen = status
        if status in ("finished", "failed"):
            break
        time.sleep(0.5)
    else:
        print("timed out waiting for the job")
        return 1

    result = job.get("result") or {}
    if status == "finished":
        print(f"decision={result.get('review_decision')} "
              f"findings={result.get('total_findings')} "
              f"(critical={result.get('critical_count')} "
              f"high={result.get('high_count')} "
              f"medium={result.get('medium_count')} "
              f"low={result.get('low_count')}) "
              f"files={result.get('files_reviewed')} "
              f"pipeline={result.get('processing_time_seconds', 0):.1f}s")
        if show_review(args.fake_url) is False:
            return 1
    return 0 if status == "finished" else 1


if __name__ == "__main__":
    sys.exit(main())
