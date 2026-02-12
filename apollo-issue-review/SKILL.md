---
name: apollo-issue-review
description: Review Apollo ecosystem issues with a classify-first workflow (reproduce for behavior issues, evidence-check for consultative asks) and draft maintainer-grade replies that directly answer user asks, clarify support boundaries, and provide actionable next paths.
---

# Apollo Issue Review

Follow this workflow to review an Apollo issue and produce a concise maintainer response.

## Core Principles

- Classify first: behavior/regression issue vs consultative/support question.
- For behavior/regression issues: reproduce first, theorize second.
- For consultative/support questions (for example "is there an official script/doc"): do evidence check first and answer directly; do not force "reproduced/not reproduced" wording.
- Solve the user ask, do not debate whether the user is right or wrong.
- If behavior is already reproduced and conclusion is stable, do not ask for extra info.
- Do not default to "version regression" analysis unless the user explicitly asks for version comparison or it changes the recommendation.
- Match the issue language: English issue -> English reply, Chinese issue -> Chinese reply (unless the user explicitly asks for bilingual output).
- Use canonical Apollo module names from repository reality (AGENTS/module layout/root `pom.xml`), and correct misnamed terms succinctly when needed.
- If an existing comment already answers the same ask (including bot replies), avoid duplicate long replies; prefer a short addendum that only contributes corrections or missing deltas.
- Never wrap GitHub @mention handles in backticks/code spans; use plain @handle so notifications are actually triggered.
- If a community user volunteers to implement ("认领"/"first contribution"), acknowledge and encourage first, then evaluate the proposal with explicit feasibility boundaries and concrete refinement suggestions.
- For OpenAPI-related asks, explicitly separate Portal web APIs (e.g., `/users`) and OpenAPI endpoints (e.g., `/openapi/v1/*`); only claim "OpenAPI supports X" when token-based OpenAPI path is verified.
- Before concluding "capability not available", cross-check code + docs/scripts + module/dependency hints from `pom.xml` to avoid false negatives caused by path assumptions.

## Workflow

1. Collect issue facts and user ask
- Read issue body and comments before concluding.
- Extract: primary ask, symptom, expected behavior, actual behavior, and whether user asks one path or an either-or path.
- Keep user asks explicit (for example "better parsing API OR raw text API": answer both).
- Detect whether the thread includes a contribution-claim ask (for example "can I take this issue?") and treat it as a guidance+boundary response, not only a capability yes/no response.
- Detect main language from issue title/body/recent comments and set reply language before drafting.
- Decide issue type up front:
  - behavior/regression (needs reproducibility check)
  - consultative/support (needs evidence check)
- Normalize names to canonical module/service terms used by Apollo repo (e.g., `apollo-portal`, not invented service names).
- If GitHub API access is unstable, use:
```bash
curl -L -s https://api.github.com/repos/<owner>/<repo>/issues/<id>
curl -L -s https://api.github.com/repos/<owner>/<repo>/issues/<id>/comments
```

2. Run the right validation path (mandatory)
- For behavior/regression issues:
  - Build a minimal, local, runnable reproduction for the reported behavior.
  - Prefer repo-native unit tests or a tiny temporary script over speculation.
  - Record exact observed output and types, not just interpretation.
- For consultative/support questions:
  - Verify by repository evidence scan (docs/scripts/code paths), not by speculative reproduction framing.
  - For API availability asks, verify in three places before concluding:
    1) actual controller paths, 2) docs/openapi scripts, 3) module/dependency pointers in `pom.xml`.
  - Record exact files/paths searched and what exists vs does not exist.
- Example checks:
```bash
rg -n "<api_or_path_related_to_issue>" -S
go test ./... -run <target_test_name>
# or a minimal go run script under /tmp for one-off validation
# consultative evidence scan example:
rg --files | rg -i "<keyword1|keyword2>"
rg -n "<keyword>" docs scripts apollo-* -S
```

3. Branch by validation result
- Behavior/regression path:
  - If reproducible:
    - State clearly that behavior is confirmed.
    - Identify whether this is supported behavior, usage mismatch, or current feature gap.
    - Then answer user asks directly (existing API/workaround/unsupported).
  - If not reproducible:
    - Ask for minimal missing evidence only:
      - input sample
      - exact read/access code
      - expected vs actual output
    - Keep this short and concrete.
- Consultative/support path:
  - If capability/script/doc exists: provide exact path/link and usage entry point.
  - If it does not exist: state "currently not available" directly and give one practical alternative.
  - If an existing comment already covered the same conclusion: post only a concise delta/correction instead of repeating the full answer.

4. Draft maintainer reply (focus on action)
- Start with a one-paragraph summary in the thread language:
  - behavior/regression issue: reproduction summary (`复现结论` / `Reproduction Result`)
  - consultative/support issue: direct conclusion summary (`结论` / `Conclusion`)
- Then include:
  - `当前能力与边界`: what is supported today and what is not.
  - `可行方案`: exact API/command/workaround user can run now.
  - `后续路径`: either invite PR with concrete files/tests, or state maintainers may plan it later without overpromising timeline.
- If the thread includes a contribution-claim proposal, structure the main body as:
  1) appreciation and encouragement, 2) feasibility judgment, 3) concrete implementation refinements (what to reuse vs what not to reuse directly).
- If user ask is either-or, answer both explicitly.
- If already confirmed feature gap, do not request more logs/steps by default.
- Keep wording factual and concise.
- Use canonical module names in final wording; if the issue uses a non-canonical name, correct it briefly without derailing the answer.
- If there is already a correct prior comment, prefer "reference + minimal supplement" format.
- If you mention users/bots, keep mentions as plain text (e.g., @dosubot), not code-formatted mention strings.
- Use localized section labels and wording by issue language (for example: `Reproduction Result / Current Support Boundary / Practical Path / Next Step` in English threads).

5. Ask for publish confirmation (mandatory gate)
- Default behavior: generate draft only; do not post automatically.
- Present the exact comment body first, then ask for confirmation in the same thread.
- Use a direct question in the same language as the thread, e.g.:
  - Chinese: `是否直接发布到 issue #<id>？回复“发布”或“先不发”。`
  - English: `Post this to issue #<id> now? Reply "post" or "hold".`
- Treat no response or ambiguous response as `not approved`.

6. Post the response only after explicit confirmation
- Allowed confirmation examples: `发布` / `帮我发` / `直接回复上去`.
- If user intent is unclear, ask one short clarification question before any post command.
- Preferred:
```bash
gh api repos/<owner>/<repo>/issues/<id>/comments -f body='<reply>'
```
- Fallback when `gh` transport is unstable:
```bash
TOKEN=$(gh auth token)
curl --http1.1 -sS -X POST \
  -H "Authorization: token $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -d '{"body":"<reply>"}' \
  https://api.github.com/repos/<owner>/<repo>/issues/<id>/comments
```
- If local proxy is required, add `-x http://127.0.0.1:7897`.
- After posting, return the comment URL as evidence.

## Output Requirements

Every final reply should include:
- A first sentence that matches issue type:
  - behavior/regression: reproducibility status (`已复现/暂未复现` or `Reproduced/Not yet reproduced`)
  - consultative/support: direct availability conclusion (for example `目前没有官方压测脚本`)
- At least one concrete API/code path/file reference backing the conclusion.
- If behavior is unsupported today:
  - explicit statement that current feature is not supported
  - one actionable workaround available now
  - one actionable next path (PR guidance or maintainer follow-up)
- If behavior is reproducible and conclusion is stable: no extra data request.
- If behavior is not reproducible: only ask for minimal reproducible inputs.
- If a prior thread comment already covers the same answer, avoid duplicate full restatement; provide only added value (correction, boundary clarification, or missing actionable step).
- No unverified root-cause claim presented as fact.
- Avoid irrelevant version/regression statements unless they materially affect action.
- Language must match the issue's main language unless explicitly requested otherwise.
- If user has not explicitly confirmed publishing, end with a confirmation question instead of running posting commands.
- If the thread contains a contribution-claim proposal, include at least:
  - one encouragement sentence,
  - one "can reuse" recommendation,
  - one "should not directly reuse" boundary (when applicable).

## Load References When Needed

- Use `references/diagnostic-playbook.md` for scenario-specific diagnostics and command snippets.
- Use `references/reply-templates.md` for reusable Chinese maintainer reply skeletons.
