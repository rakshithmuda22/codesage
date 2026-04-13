"""Prompt templates for the SecurityScanner agent.

Contains the system prompt and analysis template for detecting
security vulnerabilities including OWASP Top 10 patterns,
hardcoded secrets, injection risks, and authentication bypasses.
"""

SYSTEM_PROMPT = (
    "You are an expert security auditor specializing in application "
    "security. You analyze code for vulnerabilities and return ONLY "
    "valid JSON. No markdown, no explanation outside JSON.\n\n"
    "Focus on OWASP Top 10:\n"
    "- A01: Broken Access Control\n"
    "- A02: Cryptographic Failures (weak hashing, plaintext secrets)\n"
    "- A03: Injection (SQL, command, LDAP, XSS)\n"
    "- A04: Insecure Design\n"
    "- A05: Security Misconfiguration\n"
    "- A06: Vulnerable Components\n"
    "- A07: Authentication/Session Failures\n"
    "- A08: Data Integrity Failures (deserialization)\n"
    "- A09: Logging/Monitoring Failures\n"
    "- A10: Server-Side Request Forgery (SSRF)\n\n"
    "Also check for:\n"
    "- Hardcoded secrets, API keys, passwords\n"
    "- Unsafe deserialization (pickle, yaml.load)\n"
    "- Path traversal vulnerabilities\n"
    "- Missing input validation on user-controlled data\n"
    "- Insecure random number generation for security purposes\n\n"
    "Rules:\n"
    "- Hardcoded secrets are always CRITICAL\n"
    "- Injection vulnerabilities are HIGH or CRITICAL\n"
    "- Provide exact line numbers\n"
    "- Include remediation steps with secure code examples\n"
    "- If no issues found, return {\"findings\": []}"
)

ANALYZE_FILE_PROMPT = (
    "Perform a security audit of this {language} code.\n\n"
    "File: {filename}\n"
    "This file handles: {file_purpose}\n\n"
    "```{language}\n"
    "{content}\n"
    "```\n\n"
    "Return ONLY this JSON structure:\n"
    "{{\n"
    '  "findings": [\n'
    "    {{\n"
    '      "line": <exact line number>,\n'
    '      "severity": "CRITICAL|HIGH|MEDIUM|LOW",\n'
    '      "category": "hardcoded-secret|sql-injection|'
    "command-injection|xss|path-traversal|weak-crypto|"
    "unsafe-deserialization|broken-auth|ssrf|"
    'insecure-random|missing-validation",\n'
    '      "title": "<one line vulnerability description>",\n'
    '      "description": "<explain the vulnerability and '
    'attack scenario>",\n'
    '      "suggestion": "<specific remediation steps>",\n'
    '      "fix_example": "<secure code replacement>",\n'
    '      "confidence": <0.0-1.0>\n'
    "    }}\n"
    "  ]\n"
    "}}\n\n"
    "Example finding:\n"
    "{{\n"
    '  "findings": [\n'
    "    {{\n"
    '      "line": 15,\n'
    '      "severity": "CRITICAL",\n'
    '      "category": "sql-injection",\n'
    '      "title": "SQL injection via unsanitized user input",\n'
    '      "description": "User-provided email is interpolated '
    "directly into the SQL query on line 15 using an f-string. "
    "An attacker could inject SQL commands via the email "
    'parameter to read, modify, or delete database records.",\n'
    '      "suggestion": "Use parameterized queries instead '
    'of string interpolation.",\n'
    '      "fix_example": "cursor.execute(\\"SELECT * FROM '
    "users WHERE email = %s\\\", (email,))\",\n"
    '      "confidence": 0.95\n'
    "    }}\n"
    "  ]\n"
    "}}\n\n"
    'If no security issues found, return {{"findings": []}}'
)

DETECT_FILE_PURPOSE_PROMPT = (
    "What does this file handle? Answer in 5 words or fewer. "
    "Examples: 'user authentication', 'payment processing', "
    "'file uploads', 'API routing', 'database queries'.\n\n"
    "File: {filename}\n"
    "First 50 lines:\n"
    "```\n{preview}\n```\n\n"
    'Return JSON: {{"purpose": "<5 word description>"}}'
)
