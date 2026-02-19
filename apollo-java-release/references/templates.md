# Release Message Templates

## GitHub Release title

- `{version} Release`

## GitHub Release body skeleton

Highlights are generated from selected PRs provided via `--highlight-prs`.

```md
## Highlights

### <highlight title 1>
Apollo Java client now supports/includes <concise summary 1>.

### <highlight title 2>
Apollo Java client now supports/includes <concise summary 2>.

## What's Changed
* <short PR title> by @<author> in <PR URL>

## New Contributors
* <from GitHub generated notes>

**Full Changelog**: <compare url>
```

## Announcement discussion title

- `[Announcement] Apollo Java {version} released`

## Announcement discussion body skeleton

```md
Hi all,

Apollo Team is glad to announce the release of Apollo Java {version}.

This release includes the following changes.

* <short PR title> by @<author> in <PR URL>

New contributors in this release:
* <from generated notes>

Full changelog: <compare url>

Apollo website: https://www.apolloconfig.com/

Maven Artifacts: https://mvnrepository.com/artifact/com.ctrip.framework.apollo

Apollo Resources:
* GitHub: https://github.com/apolloconfig/apollo-java
* Issue: https://github.com/apolloconfig/apollo-java/issues
* Mailing list: [apollo-config@googlegroups.com](mailto:apollo-config@googlegroups.com)

Apollo Team
```
