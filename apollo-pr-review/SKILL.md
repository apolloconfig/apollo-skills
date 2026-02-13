---
name: apollo-pr-review
description: Review pull requests for Apollo ecosystem repositories (apollo, apollo-java, agollo, etc.) with maintainer-grade rigor. Use when triaging contributor updates, reconciling prior review feedback, validating Copilot/AI comments, checking compatibility/regression risks, and drafting concise publish-ready maintainer replies.
---

# Apollo PR Review

Use this skill to run high-signal PR reviews across Apollo community repos with consistent standards and low back-and-forth.

## Input Contract

Collect or derive these fields before review:

- `repo`: `<owner>/<repo>`
- `pr_number`: numeric ID
- `head_sha`: latest commit SHA on PR head
- `pr_context`: files, comments, reviews, checks
- `publish_mode`: `draft-only` (default) or `send-after-confirm`
- `output_mode`: `human` (default) or `pipeline`

Optional but recommended handoff from `apollo-issue-to-pr`:

- `goal`
- `acceptance_criteria`
- `non_goals`
- `change_plan`
- `test_results`

If `pr_number` or `head_sha` cannot be confirmed, ask one short clarification before continuing.

## Review Workflow

Execute the following steps in order:

1. Collect latest PR context
- Pull current PR head SHA, changed files, reviews, review comments, issue comments, and checks.
- Prefer `gh` first; if `gh` GraphQL/networking is unstable, fall back to GitHub REST via `curl`.
- Always confirm you are reviewing current head SHA, not local stale branch.

2. Reconcile prior feedback
- Extract prior maintainer concerns and AI/Copilot concerns.
- Mark each as: `resolved`, `partially resolved`, `unresolved`, `obsolete`.
- Require code evidence (path + line) before marking `resolved`.

3. Verify CI and merge gates
- Check required checks first, then drill into failed jobs/logs.
- For Java repos, explicitly verify style gate (`spotless`) and test gates.
- Separate `blocking` vs `non-blocking` failures in review output.

4. Find new risks
- Focus on regressions and compatibility first, then style/polish.
- Prioritize: API compatibility, behavior changes, lifecycle leaks, concurrency safety, panic/error handling, migration impact.
- Call out missing tests for high-risk changes.

5. Evaluate Copilot/AI suggestions
- Judge by technical correctness and user impact, not confidence tone.
- Classify each as:
  - `reasonable and fixed`
  - `reasonable but not fixed`
  - `not applicable / low value`
- If suggestion may break compatibility, verify downstream impact before accepting.

6. Decide and execute review action
- If fixes are required and maintainer expects Copilot follow-up, use `request changes` (not plain comment).
- After fixes and clean checks, submit `approve`.
- If asked to merge, follow repository policy (single-commit PR: rebase-merge; multi-commit PR: squash-merge).

7. Prepare comment summary for maintainer confirmation
- Findings first (P1/P2/P3), each with path and line.
- Then open questions/assumptions.
- Then a brief addressed-items summary and maintainer comment draft.
- Include explicit proposed action: `comment`, `request changes`, `approve`, or `merge`.

8. Send only after explicit user confirmation
- Do not post review/comment automatically after drafting.
- Ask for confirmation first, then execute the selected GitHub action.
- If user asks to send immediately, send using the exact confirmed draft unless user requests edits.

## Severity Rules

Use these levels consistently:

- `P1`: Breaking changes, silent behavior regressions, leaks, data loss, security risks.
- `P2`: Significant correctness risks, fragile concurrency, likely runtime surprises.
- `P3`: Observability/log quality, minor maintainability issues, polish.

Do not inflate severity for stylistic preferences.

## Apollo Ecosystem Checklists

Always verify these when relevant:

- Common (all repos)
  - Backward compatibility of public API/behavior.
  - Tests cover changed behavior and boundary conditions.
  - Docs/changelog updated when user-visible behavior changes.
  - No accidental breaking changes hidden in refactor.

- Java server repos (`apollo`, `apollo-java` style modules)
  - `spotless` formatting and style gates.
  - Multi-module impact (`-pl ... -am` scope awareness).
  - Config defaults + env override behavior are explicit and documented.
  - Service-parity changes (e.g., config/admin/assembly consistency) when feature should apply to all entrypoints.
  - Deployment docs in both Chinese and English stay consistent when changing defaults.

- Go SDK repos (`agollo`)
  - Exported interface/method signature compatibility.
  - Lifecycle correctness (`Start/Stop`, goroutine cleanup, idempotent close).
  - Panic/recover logging robustness and diagnostics.

## GitHub Ops Fallbacks

- If `gh` GraphQL fails (for example malformed HTTP response), use REST endpoints:
  - `GET /repos/{owner}/{repo}/pulls/{number}`
  - `GET /repos/{owner}/{repo}/pulls/{number}/files`
  - `GET /repos/{owner}/{repo}/pulls/{number}/reviews`
  - `GET /repos/{owner}/{repo}/commits/{sha}/check-runs`
  - `GET /repos/{owner}/{repo}/commits/{sha}/status`
- For style-check failures, include exact failing file and actionable fix hint in maintainer comment.
- Non-pending reviews cannot be deleted; supersede by newer review state and/or update body to mark obsolete.

## Evidence Standard

Before posting a finding, ensure:

- Reproducible from current head.
- Specific path and line reference exists.
- Impact statement explains who breaks (internal only vs external SDK users).
- Suggested direction is practical (compatible patch first, larger refactor optional).

## Maintainer Reply Templates

Use concise, neutral wording.

### Request Changes (Trigger Copilot Follow-up)

```text
<1-2 sentence summary of blocking issue>.

请修复以下阻塞项后再更新：
1) <blocking item with path/line and expected fix>
2) <optional second blocking item>
```

### Ask Contributor to Validate Compatibility

```text
有两个兼容性点请结合实际使用场景确认：

1) <risk-1 summary>. 实际场景里是否存在该用法？如果可能存在，请给出兼容方案。
2) <risk-2 summary>. 实际场景里是否存在该用法？如果可能存在，请补兼容处理或明确迁移路径。
```

### Release Timing Reply

```text
该修复合并后会纳入后续版本发布计划，具体发布时间会根据整体变更规模和验证情况统一评估。
```

## Output Contract

Default (`output_mode=human`) output should be human-friendly:

1. `Review Decision`
- recommended action (`comment` / `request changes` / `approve` / `merge-ready`)
- whether it is blocking
- confidence

2. `Findings`
- list findings in severity order `P1 -> P3`
- include file/line evidence and impact
- if none, state `no blocking findings`

3. `Contract Check`
- if issue-to-pr handoff is available, report goal/criteria alignment
- call out any non-goal violations

4. `Risk and Gate Status`
- required checks summary
- residual risks and testing gaps
- merge preconditions

5. `Publish-ready Maintainer Review Draft`
- draft body for selected action

6. `Publish Gate`
- explicit confirmation question before sending

If `output_mode=pipeline`, append one machine-readable block after the human output:

```yaml
handoff:
  review_decision:
    action: "comment|request changes|approve|merge-ready"
    blocking: false
    decision_confidence: "high|medium|low"
  findings: []
  contract_check:
    goal_match: "pass|partial|fail|unknown"
    acceptance_criteria_status: []
    non_goal_violations: []
  gate_status:
    required_checks: []
    residual_risks: []
    merge_preconditions: []
```

Default rule: no GitHub comment/review is sent until user explicitly confirms.
