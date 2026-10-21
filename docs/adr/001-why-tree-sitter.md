# ADR 001: Why Tree-sitter for Code Parsing

## Status
Accepted

## Context
CodeSage agents need to understand code structure (functions, classes, imports) to provide accurate line-level findings. We needed a parsing approach that works across Python, JavaScript, and TypeScript.

## Options Considered

1. **Regex-based parsing** - Simple pattern matching for function definitions
2. **Python's ast module** - Built-in AST parser (Python only)
3. **Tree-sitter** - Incremental parsing library with grammar support for 100+ languages

## Decision
We chose **tree-sitter** as the primary parser with regex fallback.

## Rationale

- **AST accuracy**: Tree-sitter builds a full concrete syntax tree, not just token matches. It correctly handles nested functions, decorators, multiline signatures, and edge cases that break regex patterns.
- **Language support**: A single API for Python, JavaScript, TypeScript, and future languages. Adding Go or Rust support requires only adding a grammar dependency.
- **Robustness**: Tree-sitter uses error recovery during parsing, producing partial trees even for files with syntax errors. Regex approaches fail completely on malformed code.
- **Performance**: Tree-sitter is written in C. Measured on a laptop with tree-sitter 0.21.3, parsing a generated 10,000-line Python file takes about 17 ms, so parsing is never the bottleneck next to the LLM calls.
- **Ecosystem**: The `tree-sitter-languages` package pre-compiles grammars, eliminating the need to manage C compilers in our Docker image.

## Consequences

- Added `tree-sitter==0.21.3` and `tree-sitter-languages==1.10.2` as dependencies
- Requires `gcc` in the Docker build stage (handled by tree-sitter-languages precompilation)
- Regex fallback ensures the system works even if tree-sitter fails to load
- Function extraction (names, start/end lines) is what the agents use the tree for today; it covers declarations, methods, arrow functions and function expressions in JS/TS
- The import-based dependency graph does not use tree-sitter yet (Python `ast` + regex for JS/TS)
