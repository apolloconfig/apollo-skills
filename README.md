# apollo-skills

Maintainer skills and workflows for semi-automated Apollo community operations:

- issue review
- issue-to-PR
- PR review

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

## Recommended Flow

1. `apollo-issue-review`
2. `apollo-issue-to-pr` (only when issue is ready)
3. `apollo-pr-review`

All publish actions remain confirmation-gated by default.

## Output Modes

- `output_mode=human` (default): optimized for maintainers reading and deciding.
- `output_mode=pipeline`: appends a structured `handoff` block for machine chaining.

## Publish Modes

- `publish_mode=draft-only` (default): generate drafts, do not publish automatically.
- `publish_mode=post-after-confirm` / `send-after-confirm`: publish only after explicit confirmation.

## Quick Usage Examples

```text
Use $apollo-issue-review for issue #12345
publish_mode=draft-only
output_mode=human
```

```text
Use $apollo-issue-to-pr for issue #12345
publish_mode=draft-only
output_mode=pipeline
```

```text
Use $apollo-pr-review for PR #6789
publish_mode=draft-only
output_mode=human
```

## Repository Layout

```text
apollo-issue-review/
apollo-issue-to-pr/
apollo-pr-review/
```

Each skill contains its own `SKILL.md` and optional `references/` content.
