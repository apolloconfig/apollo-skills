---
name: apollo-release
description: Automate Apollo server formal release operations end-to-end with checkpoint-gated external actions and resume support. Use when preparing a non-SNAPSHOT Apollo release in apolloconfig/apollo, creating release PR/prerelease, triggering GitHub Action package and docker publish workflows, promoting release, posting announcement discussion, and opening post-release SNAPSHOT PR.
---

# Apollo Release

Run this skill when you want to execute the Apollo server formal release workflow in a controlled and resumable way.

## Workflow Contract

- Work only in the current directory.
- Require current directory to be Apollo repository root (`apollo` + root `pom.xml` artifactId `apollo`).
- Require at least one git remote URL that normalizes to `github.com/apolloconfig/apollo`.
- Require explicit checkpoint confirmation before each external publish action.
- Persist progress in a state file and support resume without repeating completed steps.

## Command Entry

```bash
python3 scripts/release_flow.py run \
  --release-version X.Y.Z \
  --next-snapshot A.B.C-SNAPSHOT \
  --highlight-prs PR_ID_1,PR_ID_2,PR_ID_3 \
  [--state-file .apollo-release-state.json] \
  [--previous-tag vP.Q.R] \
  [--confirm-checkpoint CHECKPOINT]
```

### Supported checkpoints

- `PUSH_RELEASE_PR`
- `CREATE_PRERELEASE`
- `TRIGGER_PACKAGE_WORKFLOW`
- `TRIGGER_DOCKER_WORKFLOW`
- `PROMOTE_RELEASE`
- `CREATE_ANNOUNCEMENT_DISCUSSION`
- `MANAGE_MILESTONES`
- `PUSH_POST_RELEASE_PR`

If execution stops at a checkpoint, rerun with `--confirm-checkpoint <NAME>`.

## Step Mapping (1~7)

1. Bump root `pom.xml` revision from `X.Y.Z-SNAPSHOT` to `X.Y.Z`, create release branch/commit/PR draft.
2. Wait for release PR merge, generate release note and announcement drafts from `CHANGES.md`, and create GitHub prerelease (`vX.Y.Z`, target `master`).
3. Trigger `.github/workflows/release-packages.yml` with JDK 8 build in GitHub Action; workflow uploads three zip packages and three sha1 assets to release.
4. Verify release assets exist in release page after package workflow succeeds.
5. Trigger `.github/workflows/docker-publish.yml` on `master` with release version and wait for completion.
6. Promote prerelease to official release and publish announcement discussion in `Announcements`.
7. Post-release housekeeping: bump to next SNAPSHOT, archive `CHANGES.md`, auto-manage milestones, and open post-release PR.

## Scripts

- `scripts/release_flow.py`
  - Main orchestrator.
  - Handles preflight, checkpoint gating, workflow execution, release asset verification, milestone management, resume state, and final summary.
- `scripts/release_notes_builder.py`
  - Uses `CHANGES.md` as the source for `What's Changed`.
  - Merges GitHub generated notes for `New Contributors` and changelog link.
  - Builds `Highlights` only from user-selected PRs (`--highlight-prs`), then extracts practical usage hints from those PRs' body/comments/docs changes.
  - Builds upgrade section from SQL delta inspection.
- `scripts/github_discussion.py`
  - Creates discussions via GraphQL using category name/slug.
- `scripts/test_release_helpers.py`
  - Unit tests for parser and helper behavior.

## Referencee

- `references/templates.md`: Release note and announcement templates.

## Operational Notes

- Prefer `--dry-run` first to validate step order and checkpoint prompts.
- Keep release operations on a clean working tree unless explicitly using `--allow-dirty`.
- Do not push, create PRs/releases, trigger workflows, edit milestones, or publish discussions without checkpoint confirmation.
- Build `What's Changed` from `CHANGES.md` entries, but render each PR item as `... by @<author> in <PR URL>` so contributor mentions are preserved.
- `Highlights` PR list must be provided explicitly via `--highlight-prs`; do not auto-pick highlights.
- Always review generated `Highlights` draft and confirm wording before checkpoint `CREATE_PRERELEASE`.
