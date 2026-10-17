# Domain docs

This repository uses a single-context layout:

- `GLOSSARY.md`: domain terms at the repository root.
- `docs/adr/`: architectural decision records.

## Before exploring

Read the root glossary and ADRs relevant to the area being explored.
If these files do not exist, proceed silently. Domain-modeling work
creates them when terms or decisions are resolved.

If legacy `CONTEXT.md` or `CONTEXT-MAP.md` files exist, treat them as
the corresponding glossary files, follow their links, and retain
their names until migration is explicitly requested. If conventions
conflict, follow the documented source of truth or clarify first.

## Vocabulary

Use glossary terms when naming domain concepts in issues, proposals,
hypotheses, and tests.

When a needed concept is missing, check whether existing terminology
covers it; otherwise record the gap for domain-modeling work.

## ADR conflicts

Explicitly identify any existing ADR contradicted by a proposal,
and explain why the decision should be reopened.
