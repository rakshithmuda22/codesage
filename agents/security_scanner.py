"""SecurityScanner agent for finding vulnerabilities.

Combines fast regex pattern matching for obvious issues (hardcoded
secrets, eval(), shell injection) with deep LLM analysis for subtle
security vulnerabilities matching OWASP Top 10 patterns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import networkx as nx

from agents.base_agent import BaseAgent
from core.llm_client import LLMClient
from models.schemas import AgentResult, ChangedFile, Finding, RepoConventions
from prompts import security_prompts

logger = logging.getLogger("codesage")

SUPPORTED_LANGUAGES = {"python", "javascript", "typescript"}

CRITICAL_PATTERNS: list[tuple[str, str, str, str]] = [
    (
        r'(?i)(password|secret|api_key|token|private_key)'
        r'\s*=\s*["\'][^"\']{8,}',
        "Hardcoded secret detected",
        "CRITICAL",
        "hardcoded-secret",
    ),
    (
        r'(?i)subprocess\.call\(.*shell\s*=\s*True',
        "Shell injection risk via subprocess",
        "HIGH",
        "command-injection",
    ),
    (
        r'(?i)subprocess\.(?:Popen|run)\(.*shell\s*=\s*True',
        "Shell injection risk via subprocess",
        "HIGH",
        "command-injection",
    ),
    (
        r'(?i)os\.system\s*\(',
        "OS command execution via os.system",
        "HIGH",
        "command-injection",
    ),
    (
        r'(?i)(?<!#.*)eval\s*\(',
        "Dangerous eval() usage",
        "HIGH",
        "code-injection",
    ),
    (
        r'(?i)exec\s*\(',
        "Dangerous exec() usage",
        "HIGH",
        "code-injection",
    ),
    (
        r'(?i)execute\s*\(\s*f["\']',
        "Potential SQL injection via f-string",
        "HIGH",
        "sql-injection",
    ),
    (
        r'(?i)execute\s*\(\s*["\'].*%s.*["\'].*%\s*\(',
        "Potential SQL injection via string formatting",
        "HIGH",
        "sql-injection",
    ),
    (
        r'(?i)(?:md5|sha1)\s*\(',
        "Weak cryptographic hash function",
        "MEDIUM",
        "weak-crypto",
    ),
    (
        r'(?i)pickle\.loads?\s*\(',
        "Unsafe deserialization with pickle",
        "HIGH",
        "unsafe-deserialization",
    ),
    (
        r'(?i)yaml\.load\s*\([^)]*\)(?!\s*,\s*Loader)',
        "Unsafe YAML loading without safe loader",
        "HIGH",
        "unsafe-deserialization",
    ),
    (
        r'(?i)random\.(random|randint|choice|uniform)\s*\(',
        "Non-cryptographic random in potential security context",
        "MEDIUM",
        "insecure-random",
    ),
    (
        r'(?i)innerHTML\s*=',
        "Potential XSS via innerHTML assignment",
        "HIGH",
        "xss",
    ),
    (
        r'(?i)document\.write\s*\(',
        "Potential XSS via document.write",
        "HIGH",
        "xss",
    ),
    (
        r'(?i)dangerouslySetInnerHTML',
        "React dangerouslySetInnerHTML usage",
        "MEDIUM",
        "xss",
    ),
]

SENSITIVE_INDICATORS = [
    "auth", "login", "password", "token", "payment",
    "upload", "admin", "user", "session", "secret",
    "credential", "crypto", "encrypt", "decrypt",
    "sign", "verify", "certificate", "key", "oauth",
]


class SecurityScanner(BaseAgent):
    """Agent specialized in finding security vulnerabilities.

    Performs two-phase analysis:
    1. Fast regex scan for obvious security patterns
    2. Deep LLM analysis for subtle vulnerabilities (only on
       security-sensitive files)

    Attributes:
        name: Agent identifier string.
    """

    name: str = "SecurityScanner"

    def __init__(self, llm_client: LLMClient) -> None:
        """Initialize the SecurityScanner agent.

        Args:
            llm_client: Configured LLMClient instance.
        """
        super().__init__(llm_client)

    async def analyze(
        self,
        files: list[ChangedFile],
        repo_conventions: RepoConventions,
        dependency_graph: nx.DiGraph,
    ) -> AgentResult:
        """Analyze all changed files for security vulnerabilities.

        Args:
            files: List of changed files with content.
            repo_conventions: Learned repo conventions (unused).
            dependency_graph: File dependency graph (unused).

        Returns:
            AgentResult with security findings.
        """
        start = time.time()
        all_findings: list[Finding] = []

        analyzable = [
            f for f in files
            if f.content and f.language in SUPPORTED_LANGUAGES
        ]

        tasks = [self._analyze_file(f) for f in analyzable]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, list):
                all_findings.extend(result)
            elif isinstance(result, Exception):
                logger.error(
                    json.dumps({
                        "timestamp": time.time(),
                        "level": "ERROR",
                        "agent": self.name,
                        "job_id": "",
                        "message": (
                            f"Security scan failed for "
                            f"{analyzable[i].filename}: {result}"
                        ),
                    })
                )

        elapsed = time.time() - start
        logger.info(
            json.dumps({
                "timestamp": time.time(),
                "level": "INFO",
                "agent": self.name,
                "job_id": "",
                "message": "Security scan complete",
                "files_analyzed": len(analyzable),
                "findings": len(all_findings),
                "elapsed_s": round(elapsed, 2),
            })
        )

        return AgentResult(
            agent=self.name,
            findings=all_findings,
            processing_time_seconds=elapsed,
            files_analyzed=len(analyzable),
        )

    async def _analyze_file(
        self, file: ChangedFile
    ) -> list[Finding]:
        """Analyze a single file for security issues.

        Phase 1: Fast regex pattern matching (no LLM needed).
        Phase 2: Deep LLM analysis (only for security-sensitive files).

        Args:
            file: ChangedFile with content populated.

        Returns:
            List of security findings.
        """
        if not file.content:
            return []

        findings: list[Finding] = []

        # Phase 1: Fast regex scan
        lines = file.content.split("\n")
        for line_num, line in enumerate(lines, 1):
            for pattern, title, severity, category in CRITICAL_PATTERNS:
                try:
                    if re.search(pattern, line):
                        findings.append(self.create_finding(
                            file_path=file.filename,
                            line_number=line_num,
                            severity=severity,
                            category=category,
                            title=title,
                            description=(
                                f"Pattern detected on line {line_num}: "
                                f"{line.strip()[:100]}"
                            ),
                            suggestion=(
                                "Review this line for security "
                                "implications and apply appropriate "
                                "remediation."
                            ),
                            code_snippet=line.strip()[:200],
                            confidence=0.9,
                        ))
                except re.error:
                    continue

        # Phase 2: Deep LLM analysis for sensitive files
        if self._is_security_sensitive(file.filename, file.content):
            llm_findings = await self._llm_security_analysis(file)
            findings.extend(llm_findings)

        return findings

    async def _llm_security_analysis(
        self, file: ChangedFile
    ) -> list[Finding]:
        """Perform deep LLM-based security analysis.

        Only called for files that handle authentication, payments,
        user data, or other security-sensitive operations.

        Args:
            file: ChangedFile with content populated.

        Returns:
            List of LLM-detected security findings.
        """
        findings: list[Finding] = []

        file_purpose = self._detect_file_purpose(
            file.filename, file.content
        )

        prompt = security_prompts.ANALYZE_FILE_PROMPT.format(
            language=file.language or "python",
            filename=file.filename,
            file_purpose=file_purpose,
            content=file.content[:8000],
        )

        try:
            response = await self.llm_client.complete(
                system_prompt=security_prompts.SYSTEM_PROMPT,
                user_prompt=prompt,
            )
        except Exception as e:
            logger.warning(
                json.dumps({
                    "timestamp": time.time(),
                    "level": "WARNING",
                    "agent": self.name,
                    "job_id": "",
                    "message": (
                        f"LLM security analysis failed for "
                        f"{file.filename}: {e}"
                    ),
                })
            )
            return findings

        for item in response.get("findings", []):
            confidence = item.get("confidence", 0)
            if confidence < 0.5:
                continue

            findings.append(self.create_finding(
                file_path=file.filename,
                line_number=item.get("line", 1),
                severity=item.get("severity", "MEDIUM"),
                category=item.get(
                    "category", "security-vulnerability"
                ),
                title=item.get(
                    "title", "Potential security issue"
                ),
                description=item.get(
                    "description", "Security issue detected"
                ),
                suggestion=item.get(
                    "suggestion", "Review for security"
                ),
                code_snippet=item.get("code_snippet"),
                fix_example=item.get("fix_example"),
                confidence=confidence,
            ))

        return findings

    @staticmethod
    def _is_security_sensitive(
        filename: str, content: str
    ) -> bool:
        """Check if a file handles security-sensitive operations.

        Args:
            filename: File path.
            content: File content.

        Returns:
            True if the file appears security-sensitive.
        """
        lower_name = filename.lower()
        lower_content = content[:3000].lower()

        return any(
            indicator in lower_name or indicator in lower_content
            for indicator in SENSITIVE_INDICATORS
        )

    @staticmethod
    def _detect_file_purpose(filename: str, content: str) -> str:
        """Detect the purpose of a file from its name and content.

        Args:
            filename: File path.
            content: File content.

        Returns:
            Short description of the file's purpose.
        """
        lower = filename.lower()
        content_lower = content[:2000].lower()

        if any(w in lower for w in ["auth", "login", "signin"]):
            return "user authentication and login"
        if any(w in lower for w in ["payment", "billing", "charge"]):
            return "payment processing"
        if any(w in lower for w in ["upload", "file", "storage"]):
            return "file upload and storage"
        if "admin" in lower:
            return "admin panel operations"
        if any(w in lower for w in ["api", "route", "endpoint"]):
            return "API routing and endpoints"
        if any(w in lower for w in ["model", "schema", "db"]):
            return "database models and queries"
        if "crypto" in content_lower or "encrypt" in content_lower:
            return "cryptographic operations"
        if "session" in content_lower or "cookie" in content_lower:
            return "session management"

        return "general application logic"
