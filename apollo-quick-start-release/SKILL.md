---
name: apollo-quick-start-release
description: Orchestrate Apollo quick-start release updates with checkpoint-gated GitHub workflow triggers and resume support. Use when Apollo server publishes a new version and you need to trigger quick-start asset sync PR creation, wait for PR merge, then trigger docker-publish with controlled tag input.
---

# Apollo Quick Start Release

Run this skill when an Apollo release is finished and you want to drive `apolloconfig/apollo-quick-start` release follow-up in two human-gated phases:

1. Trigger quick-start asset sync workflow to create/update the release PR.
2. After PR merge, trigger docker publish workflow with the target tag.

## Workflow Contract

- Work only in the current directory.
- Require current directory to be quick-start repository root (`apollo-quick-start` + `.github/workflows/docker-publish.yml`).
- Require at least one git remote URL normalized to `github.com/apolloconfig/apollo-quick-start`.
- Require explicit checkpoint confirmation before every external publish action.
- Persist progress in a state file and support resume without repeating completed steps.

## Command Entry

```bash
python3 scripts/release_flow.py run \
  --release-version X.Y.Z \
  [--docker-tag TAG] \
  [--state-file .apollo-quick-start-release-state.json] \
  [--confirm-checkpoint CHECKPOINT] \
  [--dry-run]
```

### Options

- `--docker-tag`
  - Docker workflow input `tag`.
  - Default: same as `--release-version`.
- `--state-file`
  - Resume state file path relative to current repository root.
- `--confirm-checkpoint`
  - Continue from a pending checkpoint.
- `--dry-run`
  - Print intended actions without mutating remote workflows.

### Supported checkpoints

- `TRIGGER_SYNC_WORKFLOW`
- `TRIGGER_DOCKER_WORKFLOW`

If execution stops at a checkpoint, rerun with `--confirm-checkpoint <NAME>`.

## Step Mapping

1. Preflight validation:
- toolchain (`gh`, `git`, `python3`)
- repository root and remote checks
- optional auth scope checks (`repo`, `workflow`)

2. Sync checkpoint:
- Trigger `.github/workflows/sync-apollo-release.yml` on `master` with `release_version`.
- Wait for workflow completion.
- Resolve fixed branch `codex/quick-start-sync-<release_version>` PR status.

3. Merge gate:
- If sync workflow produced PR and it is not merged, stop and require merge first.
- If no PR exists (no-change sync), allow proceeding directly.

4. Docker checkpoint:
- Trigger `.github/workflows/docker-publish.yml` on `master` with `tag=<docker_tag>`.
- Wait for workflow completion.
- Output final report with run URLs, PR metadata, and current pending checkpoint.

## Scripts

- `scripts/release_flow.py`
  - Main orchestrator.
  - Handles checkpoint gating, workflow execution/waiting, PR state checks, resume state, and final summary.
- `scripts/test_release_flow_helpers.py`
  - Unit tests for semver parsing, checkpoint persistence, workflow run selection, and PR merged detection.

## Operational Notes

- Prefer `--dry-run` first to verify checkpoints and state progression.
- Keep publish actions checkpoint-gated; never bypass confirmations.
- Reuse the same release version state file for resume instead of restarting with a different version.
