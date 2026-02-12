---
name: apollo-issue-to-pr
description: Convert well-scoped Apollo issues into draft pull requests with a semi-automatic, human-gated workflow. Use when an issue is a bug, feature request, or enhancement and you want Codex to assess eligibility, implement a minimal patch, run focused validation, and prepare a draft PR for maintainer review.
---

# Apollo Issue To PR

Use this skill for semi-automatic issue-to-PR execution in Apollo repositories.

## Positioning

- Upstream skill: `apollo-issue-review` for issue triage and high-quality maintainer replies.
- This skill: only handles implementation path from issue to Draft PR.
- Downstream skill: `apollo-pr-review` for risk review and merge recommendation.
- This skill does not auto-merge.

## Workflow

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
- Load `/Users/jason/.codex/skills/apollo-issue-to-pr/references/eligibility.md`.
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

6. Prepare draft PR
- Load `/Users/jason/.codex/skills/apollo-issue-to-pr/references/pr-template.md`.
- Fill:
  - change summary
  - issue-to-change mapping
  - tests run with results
  - risk and boundary
- Always create Draft PR first.

7. Human gate before publish
- Present patch summary + exact PR body first.
- Ask for explicit confirmation before pushing branch/opening PR.
- No explicit confirmation means no publish action.

## Output Contract

Always return:
- `Eligibility`: pass/fail + reason.
- `Level`: `L1` or `L2` + matched vetoes.
- `Change Plan`: files/modules to touch and test plan.
- `Draft PR Body`: ready-to-use markdown.
- `Publish Gate`: one-line confirmation question.

## Commands

Use these defaults:
```bash
# Create branch (after confirmation)
git checkout -b codex/issue-<id>-<topic>

# Run checks
./mvnw spotless:apply
./mvnw -pl <target-module> -am test

# Open draft PR (after confirmation)
gh pr create --draft --title "<title>" --body-file /tmp/pr-body.md
```
