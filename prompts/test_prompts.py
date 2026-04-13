"""Prompt templates for the TestCoverage agent.

Contains the system prompt and analysis templates for identifying
untested code paths and suggesting specific test cases.
"""

SYSTEM_PROMPT = (
    "You are a test coverage analyst. You identify functions and "
    "code paths that lack adequate test coverage and suggest specific "
    "test cases. Return ONLY valid JSON.\n\n"
    "Focus on:\n"
    "- Functions/methods with no corresponding test cases\n"
    "- Complex functions (>10 lines) that need edge case testing\n"
    "- Error handling paths that are untested\n"
    "- Boundary conditions and edge cases\n"
    "- Integration points that need testing\n\n"
    "Rules:\n"
    "- Missing tests for public functions are MEDIUM severity\n"
    "- Missing tests for critical paths (auth, payments) are HIGH\n"
    "- Suggest concrete test case names and what they should verify\n"
    "- Include test code examples when possible\n"
    "- If all functions are well-tested, return {\"findings\": []}"
)

ANALYZE_COVERAGE_PROMPT = (
    "Analyze test coverage for this {language} code.\n\n"
    "=== SOURCE FILE ===\n"
    "File: {filename}\n"
    "Functions defined: {functions}\n\n"
    "```{language}\n"
    "{content}\n"
    "```\n\n"
    "=== EXISTING TEST FILE ===\n"
    "Test file: {test_filename}\n"
    "```{language}\n"
    "{test_content}\n"
    "```\n\n"
    "Return ONLY this JSON structure:\n"
    "{{\n"
    '  "findings": [\n'
    "    {{\n"
    '      "line": <line number of untested function>,\n'
    '      "severity": "HIGH|MEDIUM|LOW",\n'
    '      "category": "missing-test|missing-edge-case|'
    'untested-error-path|missing-integration-test",\n'
    '      "title": "<one line description>",\n'
    '      "description": "<what is untested and why it matters>",\n'
    '      "suggestion": "<specific test cases to add>",\n'
    '      "fix_example": "<example test code>",\n'
    '      "confidence": <0.0-1.0>\n'
    "    }}\n"
    "  ]\n"
    "}}\n\n"
    "Example finding:\n"
    "{{\n"
    '  "findings": [\n'
    "    {{\n"
    '      "line": 15,\n'
    '      "severity": "MEDIUM",\n'
    '      "category": "missing-test",\n'
    '      "title": "No test for validate_email()",\n'
    '      "description": "The validate_email function on line 15 '
    "has no corresponding test case. It handles user input "
    "validation which is critical for data integrity and could "
    'allow invalid data into the system.",\n'
    '      "suggestion": "Add tests for: valid email, invalid '
    "format, empty string, very long input, special characters, "
    'unicode characters.",\n'
    '      "fix_example": "def test_validate_email_rejects_'
    "invalid():\\n    assert validate_email('not-an-email') "
    'is False",\n'
    '      "confidence": 0.9\n'
    "    }}\n"
    "  ]\n"
    "}}\n\n"
    'If all functions are tested, return {{"findings": []}}'
)

ANALYZE_NO_TEST_FILE_PROMPT = (
    "This {language} source file has NO corresponding test file.\n\n"
    "File: {filename}\n"
    "Functions defined: {functions}\n\n"
    "```{language}\n"
    "{content}\n"
    "```\n\n"
    "Identify the most critical functions that need tests and suggest "
    "specific test cases for each.\n\n"
    "Return ONLY this JSON:\n"
    "{{\n"
    '  "findings": [\n'
    "    {{\n"
    '      "line": <line number>,\n'
    '      "severity": "HIGH|MEDIUM",\n'
    '      "category": "missing-test",\n'
    '      "title": "No test file for {filename}",\n'
    '      "description": "<what needs testing>",\n'
    '      "suggestion": "<specific test cases to write>",\n'
    '      "fix_example": "<example test code>",\n'
    '      "confidence": <0.0-1.0>\n'
    "    }}\n"
    "  ]\n"
    "}}"
)

SUGGEST_EDGE_CASES_PROMPT = (
    "Suggest edge case tests for these functions.\n\n"
    "Functions:\n{function_details}\n\n"
    "For each function, suggest 2-3 edge cases that should be tested "
    "but likely aren't. Focus on boundary conditions, error states, "
    "and unusual inputs.\n\n"
    "Return ONLY this JSON:\n"
    "{{\n"
    '  "suggestions": [\n'
    "    {{\n"
    '      "function_name": "<name>",\n'
    '      "line": <line number>,\n'
    '      "edge_cases": [\n'
    "        {{\n"
    '          "test_name": "test_<function>_<case>",\n'
    '          "description": "<what to test>",\n'
    '          "test_code": "<example test code>"\n'
    "        }}\n"
    "      ]\n"
    "    }}\n"
    "  ]\n"
    "}}"
)
