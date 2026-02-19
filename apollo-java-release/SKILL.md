---
name: apollo-java-release
description: Automate Apollo Java formal release operations end-to-end with a single command and explicit human checkpoints. Use when preparing a non-SNAPSHOT Apollo Java release, creating release PR/tag/workflow/discussion, relying on Sonatype Central auto-publish via Maven plugin, and opening the post-release SNAPSHOT PR.
---

# Apollo Java Release

Run this skill when you want to execute the Apollo Java formal release workflow in a controlled and resumable way.

## Workflow Contract

- Work only in the current directory.
- Require current directory to be Apollo Java repository root (`apollo-java` + root `pom.xml` artifactId `apollo-java`).
- Require at least one git remote URL to normalize to `github.com/apolloconfig/apollo-java`.
- Require an explicit checkpoint confirmation before each external publish action.
- Persist progress in a state file and support resume without repeating completed steps.

## Command Entry

```bash
python3 scripts/release_flow.py run \
  --release-version X.Y.Z \
  --next-snapshot A.B.C-SNAPSHOT \
  [--state-file .apollo-java-release-state.json] \
  [--confirm-checkpoint CHECKPOINT]
```

### Supported checkpoints

- `PUSH_RELEASE_PR`
- `CREATE_PRERELEASE`
- `TRIGGER_RELEASE_WORKFLOW`
- `CREATE_ANNOUNCEMENT_DISCUSSION`
- `PUSH_POST_RELEASE_PR`

If execution stops at a checkpoint, rerun with `--confirm-checkpoint <NAME>`.

## Step Mapping (1~7)

1. Bump root `pom.xml` revision from `X.Y.Z-SNAPSHOT` to `X.Y.Z`, create release branch/commit/PR draft.
2. Wait for release PR merge, then create GitHub prerelease (`vX.Y.Z`, target `main`).
3. Trigger `.github/workflows/release.yml`; the workflow publishes through `central-publishing-maven-plugin` with `autoPublish=true` and waits until `published`.
4. Publish announcement discussion in `Announcements` with title/body aligned to existing Apollo Java style.
5. Post-release housekeeping: bump to next SNAPSHOT, archive `CHANGES.md`, close/create milestones, and open post-release PR.
6. Promote prerelease tag to official release after workflow publish succeeds.
7. Print final report with PR/release/workflow/discussion links for traceability.

## Scripts

- `scripts/release_flow.py`
  - Main orchestrator.
  - Handles preflight, checkpoint gating, workflow execution, resume state, and final summary.
- `scripts/release_notes_builder.py`
  - Uses `CHANGES.md` as primary source.
  - Merges GitHub generated notes for `New Contributors` and changelog link.
- `.github/workflows/release.yml` (apollo-java repo)
  - Publishes artifacts via Sonatype Central Maven plugin with auto-publish enabled.
- `scripts/github_discussion.py`
  - Creates discussions via GraphQL using category name/slug.
- `scripts/test_release_helpers.py`
  - Unit tests for parser and helper behavior.

## References

- `references/templates.md`: Release note and announcement templates.

## Operational Notes

- Prefer `--dry-run` first to validate plan and checkpoints without side effects.
- Keep release operations on clean working tree unless explicitly bypassing with `--allow-dirty`.
- Do not push, create PRs, create releases, trigger workflows, or publish discussions without checkpoint confirmation.
- Release notes `Highlights` should be concise summaries of key user-facing changes (usually 2~3 items), and must not be hardcoded to a fixed topic.
- Always show the generated `Highlights` draft to the user and confirm wording before checkpoint `CREATE_PRERELEASE`.
- Release notes `What's Changed` entries should include PR authors using `by @<author> in <PR URL>` format so contributors are notified.
