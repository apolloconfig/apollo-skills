---
name: apollo-community-review
description: Run the periodic Apollo GitHub community review automation for apolloconfig/apollo, using the existing issue/pr review skills plus local helper scripts for discovery, policy, posting, and state.
---

# Apollo Community Review

Use this skill when running the recurring Apollo community review automation.

This skill orchestrates:
- discovery of new/newly active issue and PR threads
- structured review via [$apollo-issue-review](/Users/jason/git/mine/apollo-skills/apollo-issue-review/SKILL.md) or [$apollo-pr-review](/Users/jason/git/mine/apollo-skills/apollo-pr-review/SKILL.md)
- deterministic policy evaluation via `/Users/jason/git/mine/apollo-skills/apollo-community-review/scripts/community_review.py`
- optional auto-posting for high-confidence issue comments and PR top-level comments only

Discovery notes:
- Ignore bot-authored comments/reviews and automation-only update churn from accounts such as `stale[bot]` and `mergify[bot]`.
- Candidate scanning should only treat non-bot external activity after the lookback window as actionable.

## Defaults

- repo: `apolloconfig/apollo`
- actor: `nobodyiam`
- maintainers: `nobodyiam`
- mirror dir: `~/.codex/tmp/apollo-review-mirror`
- state file: `~/.codex/tmp/apollo-community-review/state.json`
- default branch: `master`

## Workflow

1. Sync the isolated mirror:
```bash
python3 /Users/jason/git/mine/apollo-skills/apollo-community-review/scripts/community_review.py \
  sync-mirror \
  --repo apolloconfig/apollo \
  --mirror-dir ~/.codex/tmp/apollo-review-mirror \
  --default-branch master
```

2. Discover candidates:
```bash
python3 /Users/jason/git/mine/apollo-skills/apollo-community-review/scripts/community_review.py \
  scan \
  --repo apolloconfig/apollo \
  --actor nobodyiam \
  --maintainers nobodyiam \
  --state-file ~/.codex/tmp/apollo-community-review/state.json \
  --initial-lookback-hours 4
```

3. For each candidate:
- Fetch thread context with `fetch-thread`.
- If it is a PR, also check out the PR head in the isolated mirror before reviewing:
```bash
python3 /Users/jason/git/mine/apollo-skills/apollo-community-review/scripts/community_review.py \
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
python3 /Users/jason/git/mine/apollo-skills/apollo-community-review/scripts/community_review.py \
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
python3 /Users/jason/git/mine/apollo-skills/apollo-community-review/scripts/community_review.py \
  post-comment \
  --decision-file <decision_json>
```

5. After each handled thread, persist the processed activity signature:
```bash
python3 /Users/jason/git/mine/apollo-skills/apollo-community-review/scripts/community_review.py \
  mark-processed \
  --state-file ~/.codex/tmp/apollo-community-review/state.json \
  --candidate-file <candidate_json> \
  --decision-file <decision_json>
```

6. At the end of the run, summarize all results:
```bash
python3 /Users/jason/git/mine/apollo-skills/apollo-community-review/scripts/community_review.py \
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
