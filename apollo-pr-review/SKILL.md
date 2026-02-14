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
- `linked_issues`: issue IDs/URLs explicitly linked by PR metadata/text (may be empty)
- `goal_source`: `linked-issue` or `pr-description-only`
- `publish_mode`: `draft-only` (default) or `send-after-confirm`
- `output_mode`: `human` (default) or `pipeline`

Optional but recommended handoff from `apollo-issue-to-pr`:

- `goal`
- `acceptance_criteria`
- `non_goals`
- `change_plan`
- `test_results`

If `pr_number` or `head_sha` cannot be confirmed, ask one short clarification before continuing.
If linked issues exist, fetch their latest body/comments and treat reported symptoms as review contract.
If no linked issue exists, derive scope from PR title/body + latest author explanation and mark it as inferred.

## Review Workflow

Execute the following steps in order:

1. Collect latest PR context
- Pull current PR head SHA, changed files, reviews, review comments, issue comments, and checks.
- Prefer `gh` first; if `gh` GraphQL/networking is unstable, fall back to GitHub REST via `curl`.
- Always confirm you are reviewing current head SHA, not local stale branch.

2. Establish target problem and completion criteria
- If linked issue(s) exist, extract concrete scenarios/symptoms from issue body and key maintainer comments.
- Build a scenario coverage map: `scenario -> changed code -> tests -> status`.
- Mark each scenario as `resolved`, `partially resolved`, `unresolved`, or `out of scope`.
- If PR claims `Fixes #...`, do not treat as complete while any in-scope scenario remains `partially resolved`/`unresolved`.
- If no linked issue exists, derive scenarios from PR description and explicitly label assumptions as inferred.

3. Reconcile prior feedback
- Extract prior maintainer concerns and AI/Copilot concerns.
- Mark each as: `resolved`, `partially resolved`, `unresolved`, `obsolete`.
- Require code evidence (path + line) before marking `resolved`.

4. Verify CI and merge gates
- Check required checks first, then drill into failed jobs/logs.
- For Java repos, explicitly verify style gate (`spotless`) and test gates.
- Separate `blocking` vs `non-blocking` failures in review output.

5. Find new risks
- Focus on regressions and compatibility first, then style/polish.
- Prioritize: API compatibility, behavior changes, lifecycle leaks, concurrency safety, panic/error handling, migration impact.
- Call out missing tests for high-risk changes.
- Check for "same bug class, different entrypoint" gaps when issue scenarios touch shared validators/config paths.

6. Evaluate Copilot/AI suggestions
- Judge by technical correctness and user impact, not confidence tone.
- Classify each as:
  - `reasonable and fixed`
  - `reasonable but not fixed`
  - `not applicable / low value`
- If suggestion may break compatibility, verify downstream impact before accepting.

7. Decide and execute review action
- If fixes are required and maintainer expects Copilot follow-up, use `request changes` (not plain comment).
- After fixes and clean checks, submit `approve`.
- If asked to merge, follow repository policy (single-commit PR: rebase-merge; multi-commit PR: squash-merge).
- If issue coverage is partial for in-scope scenarios, prefer `request changes`.

8. Prepare comment summary for maintainer confirmation
- Findings first (P1/P2/P3), each with path and line.
- Then open questions/assumptions.
- Then issue-coverage verdict (`full` / `partial` / `not addressed`) with scenario statuses.
- Then a brief addressed-items summary and maintainer comment draft.
- Maintainer comment draft must follow `Communication Language Policy` below.
- Include explicit proposed action: `comment`, `request changes`, `approve`, or `merge`.

9. Send only after explicit user confirmation
- Do not post review/comment automatically after drafting.
- Ask for confirmation first, then execute the selected GitHub action.
- If user asks to send immediately, send using the exact confirmed draft unless user requests edits.

## Communication Language Policy

- Default: follow the primary language used by the linked issue + PR discussion.
- If issue/PR is primarily Chinese, draft maintainer comments/reviews in Chinese.
- If issue/PR is primarily English, draft maintainer comments/reviews in English.
- If language is mixed or ambiguous, prefer the language used by the latest maintainer/contributor exchange; if still unclear, ask user once or provide Chinese with a short English summary.
- If user explicitly requests a language, user request overrides defaults.

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
  - `GET /repos/{owner}/{repo}/issues/{number}`
  - `GET /repos/{owner}/{repo}/issues/{number}/comments`
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
- For linked-issue PRs, include evidence that each in-scope issue scenario is covered or explicitly still missing.

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

4. `Issue Coverage`
- if linked issue exists: verdict `full` / `partial` / `not addressed`
- list scenario statuses (`resolved`/`partially resolved`/`unresolved`) with code-test evidence
- if no linked issue: state `no linked issue` and show inferred scope source

5. `Risk and Gate Status`
- required checks summary
- residual risks and testing gaps
- merge preconditions

6. `Publish-ready Maintainer Review Draft`
- draft body for selected action

7. `Publish Gate`
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
  issue_coverage:
    linked_issue: true
    verdict: "full|partial|not addressed|inferred-no-issue"
    scenarios: []
  gate_status:
    required_checks: []
    residual_risks: []
    merge_preconditions: []
```

Default rule: no GitHub comment/review is sent until user explicitly confirms.
