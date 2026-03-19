---
name: apollo-community-review
description: Run the periodic Apollo GitHub community review automation for apolloconfig/apollo, using the existing issue/pr review skills plus local helper scripts for discovery, policy, posting, and state.
---

# Apollo Community Review

Use this skill when running the recurring Apollo community review automation.

This skill orchestrates:
- discovery of new/newly active issue and PR threads
- structured review via [$apollo-issue-review](../apollo-issue-review/SKILL.md) or [$apollo-pr-review](../apollo-pr-review/SKILL.md)
- deterministic policy evaluation via `$SKILL_ROOT/scripts/community_review.py`
- optional auto-posting for high-confidence issue comments and PR top-level comments only

Discovery notes:
- Ignore bot-authored comments/reviews and automation-only update churn from accounts such as `stale[bot]` and `mergify[bot]`.
- Candidate scanning should only treat non-bot external activity after the lookback window as actionable.

Path notes:
- The commands below assume `SKILL_ROOT` points to the `apollo-community-review` skill directory.
- Example: `export SKILL_ROOT=/path/to/apollo-community-review`

## Defaults

- repo: `apolloconfig/apollo`
- actor: resolve in this order: `--actor` → `APOLLO_COMMUNITY_REVIEW_ACTOR` → current `gh auth` login
- maintainers: prefer `--maintainers` / `repoMaintainers`; otherwise fall back to the resolved actor
- mirror dir: `~/.codex/tmp/apollo-review-mirror`
- state file: if not passed explicitly, default to `~/.codex/tmp/apollo-community-review/<actor>/state.json`
- default branch: `master`
- shared org policy: `$SKILL_ROOT/references/repo-policy.json`
- optional shared maintainer map: `--maintainers-file /path/to/repo-maintainers.json` (see `$SKILL_ROOT/references/repo-maintainers.example.json`)
- optional local operator override: `--operator-file /path/to/operator-config.json` (see `$SKILL_ROOT/references/operator-config.example.json`)

## Workflow

1. Sync the isolated mirror:
```bash
python3 "$SKILL_ROOT/scripts/community_review.py" \
  sync-mirror \
  --repo apolloconfig/apollo \
  --mirror-dir ~/.codex/tmp/apollo-review-mirror \
  --default-branch master
```

2. Discover candidates:
- Single-repo scan (legacy / focused mode):
```bash
python3 "$SKILL_ROOT/scripts/community_review.py" \
  scan \
  --repo apolloconfig/apollo \
  --actor <github-login> \
  --maintainers <comma-separated-maintainers> \
  --state-file ~/.codex/tmp/apollo-community-review/<github-login>/state.json \
  --initial-lookback-hours 4
```
If `--actor` is omitted, the helper resolves it from `APOLLO_COMMUNITY_REVIEW_ACTOR` or the current `gh auth` login. If `--maintainers` is omitted, it falls back to the resolved actor.
- Org-level scan (preferred for scheduled Apollo maintenance):
```bash
python3 "$SKILL_ROOT/scripts/scan_org.py" \
  --policy-file "$SKILL_ROOT/references/repo-policy.json" \
  --maintainers-file /path/to/repo-maintainers.json \
  --operator-file /path/to/operator-config.json \
  --initial-lookback-hours 4
```
This enumerates accessible `apolloconfig` repositories, skips archived / disabled repos, keeps the configured priority repo order first, and returns a flat candidate list plus per-repo scan results/errors. Use `repo-maintainers.json` for the shared repo→maintainer map and `operator-config.json` only for local actor overrides so the shared skill does not hardcode a specific maintainer identity.

3. For each candidate:
- Fetch thread context with `fetch-thread`.
- If it is a PR, also check out the PR head in the isolated mirror before reviewing:
```bash
python3 "$SKILL_ROOT/scripts/community_review.py" \
  checkout-pr-head \
  --repo apolloconfig/apollo \
  --mirror-dir ~/.codex/tmp/apollo-review-mirror \
  --number <pr_number>
```
- Run the appropriate review skill in `output_mode=pipeline`.
- After the review output, append exactly one `COMMUNITY_REVIEW_DECISION` JSON block with:
  - `language`
  - `decision_confidence`
  - `recommended_action`
  - `validation_completed`
  - `missing_info_fields`
  - `blocking`
  - `blocking_reasons`
  - `draft_body`
- Save the combined output to a temp file, then evaluate policy with:
```bash
python3 "$SKILL_ROOT/scripts/community_review.py" \
  decide \
  --candidate-file <candidate_json> \
  --review-file <review_output_txt>
```

4. Auto-post only when `auto_send_eligible=true`.
- Allowed auto-post actions:
  - issue top-level comment
  - PR top-level comment
- Never auto-post:
  - `approve`
  - `request changes`
  - `merge-ready`
  - security/admin threads
  - draft PRs
  - threads with a newer maintainer reply after the latest external activity
- Post via:
```bash
python3 "$SKILL_ROOT/scripts/community_review.py" \
  post-comment \
  --decision-file <decision_json>
```

5. After each handled thread, persist the processed activity signature:
```bash
python3 "$SKILL_ROOT/scripts/community_review.py" \
  mark-processed \
  --state-file ~/.codex/tmp/apollo-community-review/<github-login>/state.json \
  --candidate-file <candidate_json> \
  --decision-file <decision_json>
```
If `--state-file` is omitted, `mark-processed` reuses the actor-scoped default derived from the candidate metadata.

6. At the end of the run, summarize all results:
```bash
python3 "$SKILL_ROOT/scripts/community_review.py" \
  summarize \
  --results-file <results_json>
```

## Output Rules

- Always produce one inbox item with these sections:
  - `Auto-sent`
  - `Needs review`
  - `Skipped/Error`
- If there is no actionable work, include `::archive-thread{}` after the inbox item.
- Keep the full draft body in the `Needs review` section.
- Auto-sent comments must include the localized AI disclaimer; manual drafts must not add it automatically.

## Hard Safety Rules

- Never use the current workspace remote configuration to decide which repository to query.
- Never post to GitHub outside the helper CLI flow above.
- Never auto-send a formal review state.
- If discovery, parsing, or GitHub API calls fail for a thread, route it to `Skipped/Error`.
