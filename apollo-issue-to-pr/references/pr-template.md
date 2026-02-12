# Draft PR Template

```md
## Summary
- Resolve #<issue-id>
- Type: <bug|feature request|enhancement>
- Scope: <single module / affected paths>

## What Changed
1. <change 1>
2. <change 2>
3. <change 3>

## Why This Solves The Issue
- <mapping from acceptance criteria to code changes>

## Validation
- [x] `./mvnw spotless:apply`
- [x] `./mvnw -pl <target-module> -am test`
- [ ] `./mvnw clean test` (optional if not required)

## Risk and Boundary
- Risk level: <low/medium>
- Non-goals: <what is intentionally not covered>
- Follow-up: <if any>
```

## Commit Message Example

Use Conventional Commits:

```text
fix: handle <short bug title> in <module>
```

or

```text
feat: add <small capability> for <module>
```
