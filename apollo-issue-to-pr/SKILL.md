---
name: apollo-issue-to-pr
description: Convert well-scoped Apollo issues into pull requests with a semi-automatic, human-gated workflow. Use when an issue is a bug, feature request, or enhancement and you want Codex to assess eligibility, implement a minimal patch, run focused validation, and prepare a PR for maintainer review.
---

# Apollo Issue To PR

Use this skill for semi-automatic issue-to-PR execution in Apollo repositories.

## Positioning

- Upstream skill: `apollo-issue-review` for issue triage and high-quality maintainer replies.
- This skill: only handles implementation path from issue to PR.
- Downstream skill: `apollo-pr-review` for risk review and merge recommendation.
- This skill does not auto-merge.

## Workflow

0. Load repository policy (mandatory)
- Read `<repo-root>/AGENTS.md` before implementation, validation, commit, push, and PR actions.
- Treat `AGENTS.md` as hard constraints for branch/commit/PR/testing behavior.
- If this skill conflicts with `AGENTS.md`, follow `AGENTS.md`.

1. Qualify issue
- Accept only:
  - `bug`
  - `feature request`
  - `enhancement`
- Require:
  - clear problem/goal
  - acceptance criteria
  - scoped target module(s)
- For `bug`, also require:
  - minimal reproducible steps
  - expected vs actual behavior
  - version/commit
- If missing, stop and ask only for missing fields.

2. Run risk gate
- Load `references/eligibility.md`.
- Determine level:
  - `L1`: manual implementation required
  - `L2`: AI can implement + human review/merge
- If any hard veto is matched, force `L1` and stop coding.

3. Build implementation contract
- Convert issue into a compact contract:
  - `scope`
  - `non-goals`
  - `acceptance checks`
  - `tests to add or update`
- Keep behavior aligned to issue ask; do not add unrelated refactors.

4. Implement minimum viable patch
- Keep scope small and directly tied to acceptance criteria.
- Update/add tests for changed behavior.
- Use branch naming: `codex/issue-<id>-<short-topic>`.
- If `CHANGES.md` needs an entry, add/update it during implementation.
- During implementation/review iterations, allow multiple incremental commits.
- Do not repeatedly squash/rebase during each feedback cycle.

5. Validate locally
- Run focused checks first:
```bash
./mvnw spotless:apply
./mvnw -pl <target-module> -am test
```
- If runtime path is backend-sensitive, also run:
```bash
./mvnw clean test
```
- If checks fail, do not open PR.

6. Finalize history once before publish
- Use a two-phase commit strategy:
  - Phase A (implementation/review): keep incremental commits.
  - Phase B (publish): do exactly one squash+rebase cleanup.
- Run Phase B only after explicit publish confirmation (step 8).
- Rebase on latest upstream target branch (normally `apolloconfig/apollo:master`).
- Squash/fixup all feature commits into one commit.
- Verify commit count before publish:
  - `git rev-list --count <upstream>/<target-branch>..HEAD` must equal `1`.
  - If count is not `1`, stop publish and squash again.
- Final commit message rules:
  - use Conventional Commits
  - use `type: summary` format (no scope segment)
  - append `Fixes #<issue-id>` in commit message body
  - do not include any custom scoring/rating metadata
- If rebase conflicts cannot be resolved cleanly, stop and ask user before continuing.

7. Prepare PR with repository template
- Load target repo template: `<repo>/.github/PULL_REQUEST_TEMPLATE.md`.
- Do not use custom section names that differ from repo template.
- Do not add extra sections not present in template (for Apollo upstream this explicitly includes no custom scoring/rating section).
- Fill template with real content (no raw placeholder text like `XXXXX`).
- For checklist items, mark `[x]` only when the item has actually been completed.
- PR title rule: use a plain human summary, do not use Conventional Commit prefixes such as `feat:`, `fix:`, `chore:`.
- Default publish target is Ready for review (non-draft PR).
- Use Draft PR only when user explicitly requests `draft-only` mode.

8. Human gate before publish
- Present patch summary + exact PR body first.
- Ask for explicit confirmation before final history rewrite, push, and PR create.
- No explicit confirmation means no publish action.

9. Post-create normalization
- After PR is created, get actual PR number and URL.
- If `CHANGES.md` contains this item, ensure the link points to the PR URL (not issue URL).
- If `CHANGES.md` link changes, amend the single squashed commit and force-push (`--force-with-lease`) so PR still contains one commit.
- If a temporary Draft PR is used during normalization, convert it to Ready before final handoff.
- Avoid repeated history rewrites; keep this as one final amend/push cycle.

## Input Contract

Collect or derive these fields before implementation:

- `repo`: `<owner>/<repo>`
- `issue_number`: numeric ID
- `issue_context`: title/body/comments
- `publish_mode`: `ready-after-confirm` (default) or `draft-only`
- `output_mode`: `human` (default) or `pipeline`

Optional handoff from `apollo-issue-review`:

- `goal`
- `acceptance_criteria`
- `suggested_modules`
- `risk_hints`

## Output Contract

Default (`output_mode=human`) output should be human-friendly:

1. `Eligibility Summary`
- pass/fail + reason
- level (`L1`/`L2`) and matched vetoes

2. `Implementation Plan`
- goal
- scoped change plan
- non-goals/boundary

3. `Validation Summary`
- commands run + pass/fail
- residual risks

4. `PR Body`
- ready-to-use markdown for `gh pr create --body-file ...`

5. `Publish Gate`
- one-line confirmation question before push/PR create

6. `Publish Compliance Check`
- single-commit status
- rebase-base branch
- two-phase commit policy followed (`multi-commit during iteration`, `single commit at publish`)
- PR template source path
- `CHANGES.md` link target (`pull/<id>` expected)
- no custom scoring/rating section in commit message or PR body
- `AGENTS.md` path + compliance status
- final PR state (`ready` expected unless `draft-only`)

If `output_mode=pipeline`, append one machine-readable block after the human output:

```yaml
handoff:
  eligibility:
    eligible: false
    reason: ""
    level: "L1|L2"
    matched_vetoes: []
  implementation_contract:
    goal: ""
    acceptance_criteria: []
    non_goals: []
    change_plan: []
  delivery_evidence:
    branch: "not-created|codex/issue-<id>-<topic>"
    commits: []
    squashed: false
    rebase_base: ""
    two_phase_commit_policy: "pass|fail"
    agents_policy_path: ""
    agents_compliance: "pass|fail"
    test_results: []
    residual_risks: []
    pr_template_path: ""
    changes_link_target: ""
    final_pr_state: "ready|draft"
```

## Commands

Use these defaults:
```bash
# Load and apply repository policy (mandatory)
test -f AGENTS.md
sed -n '1,200p' AGENTS.md

# Create branch
git checkout -b codex/issue-<id>-<topic>

# During implementation/review: incremental commits are allowed
git commit -m "fix: <incremental change summary>"

# Run checks
./mvnw spotless:apply
./mvnw -pl <target-module> -am test

# Optional full checks (recommended for upstream PR checklist)
./mvnw clean test

# Before publish (one-time history cleanup): squash + rebase
git fetch apollo master
git rebase -i apollo/master
# In the todo list: keep the main commit as `pick`, set follow-up commits to `fixup`/`squash`
# Optional autosquash flow:
# git commit --fixup <main-commit-sha>
# git rebase -i --autosquash apollo/master

# Ensure final single commit message is compliant
git commit --amend -m "<type>: <summary>" -m "Fixes #<issue-id>"
COUNT=$(git rev-list --count apollo/master..HEAD)
test "$COUNT" -eq 1
git push --force-with-lease origin codex/issue-<id>-<topic>

# Open PR (after confirmation; default is Ready for review)
gh pr create --title "<plain title>" --body-file /tmp/pr-body.md

# Optional: create draft for draft-only mode, or temporary staging before ready handoff
gh pr create --draft --title "<plain title>" --body-file /tmp/pr-body.md

# After PR is created: align CHANGES.md with PR URL, keep one commit
# (replace <pr-number> first)
git add CHANGES.md
git commit --amend --no-edit
git push --force-with-lease origin codex/issue-<id>-<topic>

# If a temporary draft was used, convert to ready before final handoff
gh pr ready <pr-number>
```
