# Draft PR Template (Apollo Upstream Mirror)

Always load and follow the repository template first:

- `<repo>/.github/PULL_REQUEST_TEMPLATE.md`

For `apolloconfig/apollo`, use this exact section structure:

```md
## What's the purpose of this PR

<Clear purpose and why this change is needed.>

## Which issue(s) this PR fixes:
Fixes #<issue-id>

## Brief changelog

- <change 1>
- <change 2>

Follow this checklist to help us incorporate your contribution quickly and easily:

- [ ] Read the [Contributing Guide](https://github.com/apolloconfig/apollo/blob/master/CONTRIBUTING.md) before making this pull request.
- [ ] Write a pull request description that is detailed enough to understand what the pull request does, how, and why.
- [ ] Write necessary unit tests to verify the code.
- [ ] Run `mvn clean test` to make sure this pull request doesn't break anything.
- [ ] Run `mvn spotless:apply` to format your code.
- [ ] Update the [`CHANGES` log](https://github.com/apolloconfig/apollo/blob/master/CHANGES.md).
```

## Hard Rules

- Do not keep placeholder text such as `XXXXX`; replace with real content.
- Do not add sections that do not exist in the repository template (including no custom scoring/rating section).
- PR title must be plain language summary, not Conventional Commit format.
- Checklist status must reflect actual execution/results.
- `CHANGES.md` item link should point to the PR URL (`/pull/<id>`), not issue URL.
- If `CHANGES.md` link is updated after PR creation, amend the same squashed commit and force-push to keep one commit in PR.
