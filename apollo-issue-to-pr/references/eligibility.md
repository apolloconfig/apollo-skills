# Eligibility and Risk Gate

Use this decision matrix before coding.

This file is for semi-automatic issue-to-PR only. If the issue does not qualify, hand back to issue triage/review workflow.

## Allowed Entry Types

- `bug`
- `feature request`
- `enhancement`

Any other issue type: stop and route to manual handling.

## Basic Qualification

Require all:
- clear problem statement or target outcome
- concrete acceptance criteria
- impacted module guess (at least one)

For bug issues, also require:
- minimal reproduce steps
- expected vs actual behavior
- version/commit info

## Hard Vetoes (force L1)

- Any SQL/schema change in:
  - `scripts/sql/**`
  - `apollo-*/src/main/resources/**/*.sql`
- Security/auth/permission sensitive change
- Backend change across 2+ modules
- Breaking public API behavior

If any veto matches: stop auto-coding and ask for manual ownership.

## L2 Scope (allowed for this skill)

Typical safe patterns:
- single-module bug fix
- small feature with narrow blast radius
- portal UI fix without auth/security behavior change
- tests-only and docs-adjacent follow-up in same module

## Test Minimum for L2

- `./mvnw spotless:apply`
- `./mvnw -pl <target-module> -am test`
- add/adjust tests for changed behavior
