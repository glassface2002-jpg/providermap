# Architecture Decision Records

This directory holds ADRs — short, numbered records of significant
architecture decisions, kept separate from the per-feature `*_DESIGN.md`
files that already live alongside the code they describe (e.g.
`adapters/providers/cigna_fhir/CONCURRENCY_DESIGN.md`,
`adapters/providers/cigna_fhir/NATIONAL_DISCOVERY_DESIGN.md`).

**Why both exist, and what each is for:**

- A **`*_DESIGN.md`** file is the full design: flow diagrams, sequence
  diagrams, comparison tables, validation plans, migration steps. It's meant
  to be read alongside the code it describes and updated as that code
  evolves (each carries a `## Status:` line — `proposal for review`,
  `approved`, `implemented (Phase X)` — for exactly that reason).
- An **ADR** is the short, durable record of *the decision itself*: what was
  decided, why, and what it costs — a couple of screens, not a design
  document. It doesn't move when the code moves, and it isn't rewritten as
  implementation details change; a decision that gets revisited gets a
  **new** ADR that supersedes the old one, so the history of *why* stays
  intact.

## Format

Each ADR is `NNNN-title-in-kebab-case.md`, numbered sequentially, and follows
four sections:

```markdown
# NNNN. Title

## Status
<Proposed | Approved | Superseded by NNNN>

## Context
What problem or question forced this decision? What constraints applied?

## Decision
What was decided, stated plainly in one or two sentences.

## Consequences
What this makes easier, what it makes harder, and what it explicitly
does *not* resolve (open items, deferred follow-ups).
```

Link to the fuller design doc from `## Context` or `## Decision` rather than
duplicating its content — the ADR is the summary, not a second copy.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-national-discovery-strategy.md) | National discovery strategy for the Cigna FHIR adapter | Superseded by 0002 |
| [0002](0002-close-alternate-discovery-traversals.md) | Reject national discovery for production use; retain per-location state-scoped discovery | Approved |
