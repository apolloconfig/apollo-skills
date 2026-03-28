---
name: apollo-contributor-promotion-review
description: Review Apollo community contributors for quarterly promotion readiness across the apolloconfig organization. Use when Codex needs to scan the last 100 days of contributor activity, rank top contributors, inspect their historical PR/review/community evidence, and produce an internal report that recommends member invitation, committer discussion, PMC discussion, or continued observation.
---

# Apollo Contributor Promotion Review

Use this skill to run a quarterly Apollo contributor promotion review. It scans recent activity across `apolloconfig`, ranks the top contributors, backfills historical evidence, and produces an internal report for governance decisions.

Path notes:
- Assume `SKILL_ROOT` points to this skill directory.
- Example: `export SKILL_ROOT=/path/to/apollo-contributor-promotion-review`

## Defaults

- organization: `apolloconfig`
- lookback window: `100` days
- ranking size: top `5`
- primary data sources:
  - GitHub GraphQL search for recent PRs and issues
  - GitHub REST search for top-candidate historical evidence
  - `apolloconfig/apollo` `docs/en/community/team.md` for PMC / committer detection
  - `apolloconfig/apollo-community` membership request issues for member detection
- default policy file: `$SKILL_ROOT/references/review-policy.example.json`
- optional role override file: `--role-overrides /path/to/role-overrides.json`

## Workflow

1. Review the policy inputs.
- Use the default policy unless the user explicitly wants a different repo scope, lookback window, or score weights.
- Use a role override file when the public GitHub data is known to be incomplete for members, committers, PMC, aliases, or ignore rules.

2. Run the scan.
```bash
python3 "$SKILL_ROOT/scripts/contributor_promotion_review.py" \
  scan \
  --policy-file "$SKILL_ROOT/references/review-policy.example.json" \
  --pretty > /path/to/scan.json
```

3. Render the internal report.
```bash
python3 "$SKILL_ROOT/scripts/contributor_promotion_review.py" \
  summarize \
  --scan-file /path/to/scan.json
```

4. Review the recommendations manually before taking action.
- `Member` means the evidence is strong enough to consider sending a membership invitation flow, but 2FA and sponsorship remain manual checks.
- `Committer discussion` means the candidate looks ready for a PMC nomination discussion, not that the promotion should happen automatically.
- `Candidate for PMC discussion` means the candidate looks strong enough for internal PMC consideration, not that the vote has happened.
- `Continue observing` means the skill found useful evidence but not enough for a promotion recommendation.

## Output Rules

- Prefer the markdown report for user-facing output.
- Keep absolute dates in the report so the review window is explicit.
- Show 3 to 5 evidence links per ranked contributor.
- Preserve the conservative recommendation style:
  - only recommend `Member` when the evidence is clearly above threshold
  - only recommend `Committer discussion` for recognized members
  - only recommend `Candidate for PMC discussion` for recognized committers
  - route everyone else to `Continue observing`

## Safety Rules

- Never auto-post issue comments, PR comments, or discussions from this skill.
- Never create membership requests, committer nominations, PMC nominations, or team-list PRs unless the user asks for a separate follow-up task.
- Treat 2FA, internal voting, contribution difficulty, and private governance context as manual checks.
- If GitHub data is incomplete or ambiguous, keep the recommendation conservative and list the missing evidence in `blockers`.

## Resources

### scripts/

- `contributor_promotion_review.py`: fetches GitHub data, aggregates recent contributions, enriches top candidates with historical evidence, and renders a markdown report.

### references/

- `review-policy.example.json`: default scoring, scope, and threshold policy for quarterly reviews.
- `role-overrides.example.json`: optional manual overrides for known members, committers, PMC, aliases, or ignored logins.
