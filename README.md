# apollo-skills

Maintainer skills and workflows for semi-automated Apollo community operations:

- issue review
- issue-to-PR
- PR review
- Java client release orchestration

These skills are designed for human-in-the-loop operation by default, with optional machine-readable handoff blocks for pipeline chaining.

## Skills

### 1) `apollo-issue-review`

Review and triage issues, propose labels, ask for missing details, and draft maintainer replies.

Default output: human-friendly summary and reply draft.  
Optional: append pipeline handoff fields when `output_mode=pipeline`.

### 2) `apollo-issue-to-pr`

Convert qualified issues (`bug` / `feature request` / `enhancement`) into minimal draft PRs with risk gating and focused validation.

Default output: human-friendly implementation and validation summary.  
Optional: append pipeline handoff fields when `output_mode=pipeline`.

### 3) `apollo-pr-review`

Run maintainer-grade PR review with compatibility/regression focus, then produce a publish-ready review draft.

Default output: human-friendly review decision and findings.  
Optional: append pipeline handoff fields when `output_mode=pipeline`.

### 4) `apollo-java-release`

Run Apollo Java formal release flow with checkpoint-gated automation:

- release version bump PR
- GitHub prerelease creation
- release workflow trigger (auto-publish via central-publishing-maven-plugin)
- announcement discussion creation
- post-release SNAPSHOT bump PR
- prerelease promotion to official release after publish workflow succeeds

## Recommended Flow

1. `apollo-issue-review`
2. `apollo-issue-to-pr` (only when issue is ready)
3. `apollo-pr-review`
4. `apollo-java-release` (for formal Java SDK release cycles)

All publish actions remain confirmation-gated by default.

## Quick Usage Examples

```text
Use $apollo-issue-review issue #12345
```

```text
Use $apollo-issue-to-pr issue #12345
```

```text
Use $apollo-pr-review PR #6789
```

```text
Use $apollo-java-release 2.5.0
```

## Repository Layout

```text
apollo-issue-review/
apollo-issue-to-pr/
apollo-pr-review/
apollo-java-release/
```

Each skill contains its own `SKILL.md` and optional `references/` content.
