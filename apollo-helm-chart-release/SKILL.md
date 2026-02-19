---
name: apollo-helm-chart-release
description: Run Apollo Helm chart release workflow with local automation and publish confirmation gates. Use when apollo-helm-chart version/appVersion updates require packaging both charts, refreshing docs/index.yaml, validating whitelist changes, committing with Conventional Commit, and preparing a ready-for-review PR draft.
---

# Apollo Helm Chart Release

Run this skill when you need to publish a new Helm chart release for `apolloconfig/apollo-helm-chart` after chart version updates.

## Workflow Contract

- Work only in the current directory.
- Require current directory to be `apollo-helm-chart` repository root.
- Require at least one git remote URL normalized to `github.com/apolloconfig/apollo-helm-chart`.
- Require no existing `*.tgz` files in repository root before packaging.
- Default to strict chart consistency:
  - `apollo-portal/Chart.yaml` and `apollo-service/Chart.yaml` must share the same `version`.
  - `apollo-portal/Chart.yaml` and `apollo-service/Chart.yaml` must share the same `appVersion`.
- Never push or create PR automatically; always stop with explicit gate commands.

## Command Entry

Run from `apollo-helm-chart` repository root:

```bash
python3 scripts/release_flow.py run \
  [--allow-version-mismatch] \
  [--skip-lint] \
  [--dry-run]
```

When executing manually outside the skill runner, use the absolute script path under this skill directory.

### Options

- `--allow-version-mismatch`
  - Continue when portal/service `version` or `appVersion` are different.
  - Default behavior without this flag is fail-fast.
- `--skip-lint`
  - Skip `helm lint apollo-portal` and `helm lint apollo-service`.
  - Default behavior runs lint for both charts.
- `--dry-run`
  - Do not mutate repository files.
  - Print checks, branch/commit/PR draft, and planned release commands.

## Step Mapping

1. Preflight
- Verify repository layout:
  - `apollo-portal/Chart.yaml`
  - `apollo-service/Chart.yaml`
  - `docs/index.yaml`
- Verify required tools:
  - required: `git`, `helm`
  - warning-only for later gate: `gh`
- Verify remote includes `apolloconfig/apollo-helm-chart`.
- Verify repository root has no stale `*.tgz`.

2. Trigger Detection
- Check `git diff HEAD -- apollo-portal/Chart.yaml apollo-service/Chart.yaml`.
- If diff contains `version` or `appVersion` changes, continue.
- If diff does not contain those fields:
  - compare current chart `version` to latest version in `docs/index.yaml`.
  - if docs is behind on any chart, continue with warning.
  - otherwise stop with "no version change and docs not behind".

3. Consistency Check
- Enforce portal/service `version` and `appVersion` equality.
- Allow explicit override only via `--allow-version-mismatch`.

4. Validation
- Run:
  - `helm lint apollo-portal`
  - `helm lint apollo-service`
- Stop on any failure.

5. Package + Index (raw command style)
- Run in repo root:
  - `helm package apollo-portal`
  - `helm package apollo-service`
  - `mv *.tgz docs`
- Run in `docs`:
  - `helm repo index .`

6. Whitelist Check
- Read `git status --porcelain`.
- Allow changed files only:
  - `apollo-portal/Chart.yaml`
  - `apollo-service/Chart.yaml`
  - `docs/index.yaml`
  - `docs/apollo-portal-*.tgz`
  - `docs/apollo-service-*.tgz`
- Stop when any other file is changed.

7. Branch + Commit
- Branch naming:
  - same chart versions: `codex/helm-release-<version>`
  - different chart versions: `codex/helm-release-<portal-version>-<service-version>`
- Branch strategy:
  - reuse when already on target branch
  - otherwise checkout existing branch or create new branch
- Commit message format:
  - title: `chore(charts): release helm charts (portal <pver>, service <sver>)`
  - body:
    - `portal appVersion: <papp>`
    - `service appVersion: <sapp>`

8. Publish Gates (manual confirmation only)
- Output push gate command:
  - `git push -u origin <branch>`
- Output PR gate command:
  - `gh pr create --title "<title>" --body-file "<temp-file>"`
- Default PR state is ready-for-review (no `--draft`).
- PR body uses `references/pr-template.md`.

## Scripts

- `scripts/release_flow.py`
  - Main orchestrator.
  - Handles preflight, trigger detection, lint, package/index, whitelist gate, branch/commit, and PR draft output.
- `scripts/test_release_flow_helpers.py`
  - Unit tests for helper functions:
    - chart yaml parsing
    - git diff version field extraction
    - docs/index latest version extraction
    - branch naming
    - whitelist matching

## References

- `references/pr-template.md`
  - PR body template with fixed sections:
    - What changed
    - Version matrix
    - Helm release artifacts
    - Commands executed
    - Checklist

## Operational Notes

- Keep release runs on a clean worktree except intentional release files.
- Use `--dry-run` first when uncertain.
- Default behavior does not auto-push or auto-create PR.
- Do not bypass whitelist failures; fix unexpected changes first.
