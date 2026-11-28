---
name: apollo-quick-start-release
description: Orchestrate Apollo quick-start release updates with checkpoint-gated GitHub workflow triggers and resume support. Use when Apollo server publishes a new version and you need to trigger quick-start asset sync PR creation, wait for PR merge, then publish the versioned ZIP and Docker image.
---

# Apollo Quick Start Release

Run this skill when an Apollo release is finished and you want to drive `apolloconfig/apollo-quick-start` release follow-up in two human-gated phases:

1. Trigger quick-start asset sync workflow to create/update the release PR.
2. After PR merge, publish the ZIP, checksum and Docker image for the same release version.

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
  - Optional Docker alias passed as `tag`. The workflow also receives the explicit Apollo release version.
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
- Trigger `.github/workflows/docker-publish.yml` on `master` with `release_version=<release_version>` and `tag=<docker_tag>`. The workflow stages the ZIP, builds both image architectures from the verified archive, then publishes the GitHub Release.
- The helper saves the dispatched run ID, watches that run and records its success before checking the public ZIP and checksum. Resume with the same state file after a timeout. If the run failed, inspect it and rerun the failed jobs on that run before resuming.
- A custom alias updates the version tag and that alias. Only a current release can also update `latest`.
- Output final report with the release URL, image reference, run URLs, PR metadata, and current pending checkpoint.

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

### First ZIP for the existing v3.0.0 tag

The original Quick Start tag predates the VERSION file and already has a published Docker image. Prepare its ZIP from the migration checkout with `python3 scripts/package_release.py --version 3.0.0 --jar <original-jar> --output dist`. Verify that the JAR SHA-256 matches the OID in `git show v3.0.0:apollo-all-in-one.jar`. After explicit publication approval, attach the verified ZIP and its checksum to a GitHub Release on the existing tag using `gh release create v3.0.0 --verify-tag` with both files. This bootstrap reuses the published 3.0.0 image.
