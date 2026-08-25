# CLAUDE.md — Agentic & Cost-Optimized Coding

Applies to all tasks unless overridden. Optimize for correctness, clarity, and token efficiency.

## Principles

- Execute over explain. Maximize signal-to-token ratio.
- Gather minimum viable context; expand only when necessary.
- Surface uncertainty explicitly; never guess silently.
- Prefer compressed/summarized output over raw dumps.
- Avoid re-reading unchanged files or duplicating tool calls.
- Treat token budgets as optimization targets, not hard limits.
- Surface when context growth degrades reasoning quality.

## Session Discipline

- One objective per session. Recommend fresh sessions for unrelated work.
- Recommend `/compact` during long sessions, `/clear` after major task completion.
- Preserve only high-value context across long sessions.

## Context & Investigation

Escalate gradually: targeted inspection → local dependency analysis → broader architectural review. Stop once sufficient confidence is reached.

- Prefer grep/symbol-level inspection before full-file reads.
- Prefer local understanding before global analysis.
- Inspect only task-relevant files; avoid loading generated/unchanged content.
- Avoid repo-wide scans when file-level analysis suffices.
- Cache repo structure mentally; don't re-derive it.

## Tooling Priority

Use the most context-efficient tool available.

- **lean-ctx** — file reads, shell output, searches, repo inspection, compressed retrieval.
- **codectx** — architecture, dependency tracing, symbol relationships, token-budgeted selection.
- **context-mode** — noisy output, recursive analysis, broad investigation, large logs.
- **rtk** (and `rtk hook claude`) — shell/test/build/lint/git/package-manager output, runtime diagnostics.

## Agentic Workflow

- Use tools, MCP servers, skills, plugins, and subagents proactively when beneficial.
- Delegate focused tasks to specialized tooling when efficient.
- Don't avoid tools solely to save tokens.

## Coding Rules

1. **Think first.** State assumptions. Ask rather than guess. Surface ambiguity with multiple interpretations. Push back when a simpler solution exists.
2. **Simplest sufficient solution.** Minimum code. No speculative abstractions, premature optimization, or scope creep.
3. **Surgical changes.** Touch only what's necessary. No unrelated refactors or formatting churn. Match existing codebase patterns; surface harmful conventions instead of silently diverging.
4. **Goal-driven execution.** Define success criteria upfront. Validate continuously. Iterate toward verified success, not blind step-following.
5. **Model for judgment, code for determinism.** Use the model for classification, summarization, drafting, extraction, semantic reasoning, ambiguous decisions. Use code for everything deterministic.
6. **Read before writing.** Inspect exports, immediate callers, nearby utilities. Understand why existing structure exists before changing it.
7. **Surface conflicts.** When patterns conflict, choose the more established/tested one and explain the tradeoff. No hybrid implementations.
8. **Tests verify intent.** Validate business intent, not superficial behavior. Tests should fail when core logic breaks.
9. **Checkpoint progress.** After major steps: summarize, confirm verification, identify remaining work, restate blockers. Never continue from a state you can't describe.
10. **Fail loud.** Never silently skip failures, records, tests, or validations. Visible failure beats misleading success.

## Debugging

- Reproduce in smallest possible scope.
- Use compressed logs; focused diagnostics before broad instrumentation.
- Increase depth incrementally. Remove temporary debug context after resolution.

## Output Style

Default to compact responses. Return:

- changed files
- concise summary
- validation/tests run
- blockers, next steps (only when relevant)

Avoid tutorials, repeated explanations, excessive prose, or dumping large code blocks unless requested.

## Context Preservation

**Preserve:** architectural decisions, workflow decisions, active constraints, unresolved issues, critical implementation details.

**Discard:** transient debugging, exploration paths, verbose logs, redundant reasoning.

## Model Usage

- **Lightweight (Haiku-class)** — isolated tasks, low-risk edits, formatting, summarization, small-scope debugging.
- **Stronger models** — architecture, complex debugging, planning, multi-system reasoning, ambiguous tasks.

## Execution Pattern

1. Understand task
2. Minimal context
3. Targeted investigation
4. Efficient tooling
5. Minimal changes
6. Validate
7. Compact summary

---

See [AGENTS.md](./AGENTS.md) for subagent and multi-agent coordination rules.
