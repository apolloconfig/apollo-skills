# apollo-skills

Maintainer skills and workflows for semi-automated Apollo community operations:

- issue review
- issue-to-PR
- PR review
- server release orchestration
- Java client release orchestration
- helm chart release orchestration
- quick-start release orchestration

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

### 5) `apollo-release`

Run Apollo server formal release flow with checkpoint-gated automation:

- release revision bump PR (`pom.xml` revision only)
- prerelease draft from `CHANGES.md` with user-selected highlight PRs (`--highlight-prs`)
- package workflow trigger (`release-packages.yml`) and release asset verification
- docker publish workflow trigger (`docker-publish.yml`)
- prerelease promotion to official release
- announcement discussion creation
- post-release SNAPSHOT bump PR with `CHANGES.md` archive and milestone management

### 6) `apollo-helm-chart-release`

Run Apollo Helm chart release flow for `apolloconfig/apollo-helm-chart` with local automation and publish gates:

- detect version trigger from chart `version` / `appVersion` diff with docs lag fallback
- run `helm lint`, package both charts, move tgz to `docs`, and regenerate `docs/index.yaml`
- enforce whitelist-only git changes before commit
- create standardized release commit and generate ready-for-review PR draft
- stop at push/PR gate commands for explicit human confirmation

### 7) `apollo-quick-start-release`

Run Apollo quick-start release follow-up flow with checkpoint-gated automation:

- trigger quick-start asset sync workflow (`sync-apollo-release.yml`) for release version updates
- wait for sync workflow completion and inspect fixed-branch PR status (`codex/quick-start-sync-<version>`)
- require PR merge confirmation before docker publish when sync changes exist
- trigger docker publish workflow (`docker-publish.yml`) with configurable tag (default: release version)
- support resume via state file and explicit checkpoint confirmations

## Recommended Flow

1. `apollo-issue-review`
2. `apollo-issue-to-pr` (only when issue is ready)
3. `apollo-pr-review`
4. `apollo-release` (for Apollo server release cycles)
5. `apollo-java-release` (for formal Java SDK release cycles)
6. `apollo-helm-chart-release` (for apollo-helm-chart packaging/index/PR flow)
7. `apollo-quick-start-release` (for apollo-quick-start sync PR + docker publish flow)

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

```text
Use $apollo-release 2.5.0
```

```text
Use $apollo-helm-chart-release to release apollo-helm-chart after chart version updates.
```

```text
Use $apollo-quick-start-release to sync quick-start release assets and then publish docker image with checkpoints.
```

## Repository Layout

```text
apollo-issue-review/
apollo-issue-to-pr/
apollo-pr-review/
apollo-release/
apollo-java-release/
apollo-helm-chart-release/
apollo-quick-start-release/
```

Each skill contains its own `SKILL.md` and optional `references/` content.
