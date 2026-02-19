# Release Message Templates

## GitHub Release title

- `Apollo {version} Release`

## GitHub Release body skeleton

```md
## Highlights

### <highlight title 1>
Apollo now includes <concise summary 1>.

### <highlight title 2>
Apollo now includes <concise summary 2>.

## What's Changed
* [Change summary from CHANGES.md](https://github.com/apolloconfig/apollo/pull/123)

## Installation

Please refer to the [Distributed Deployment Guide](https://www.apolloconfig.com/#/en/deployment/distributed-deployment-guide).

## How to upgrade from vX.Y.Z to vA.B.C
1. Apply [apolloconfigdb-vNNN-vMMM.sql](https://github.com/apolloconfig/apollo/blob/vA.B.C/scripts/sql/profiles/mysql-default/delta/vNNN-vMMM/apolloconfigdb-vNNN-vMMM.sql) to ApolloConfigDB
2. Apply [apolloportaldb-vNNN-vMMM.sql](https://github.com/apolloconfig/apollo/blob/vA.B.C/scripts/sql/profiles/mysql-default/delta/vNNN-vMMM/apolloportaldb-vNNN-vMMM.sql) to ApolloPortalDB
3. Deploy vA.B.C executables with the following sequences:
   1. apollo-configservice
   2. apollo-adminservice
   3. apollo-portal

## New Contributors
* <from GitHub generated notes>

**Full Changelog**: <compare url>
```

## Announcement discussion title

- `[Announcement] Apollo {version} released`

## Announcement discussion body skeleton

```md
Hi all,

Apollo Team is glad to announce the release of Apollo {version}.

This release includes the following changes.

- [Change summary from CHANGES.md](https://github.com/apolloconfig/apollo/pull/123)

Please refer to the change log for the complete list of changes:
<release or changelog url>

Apollo website: https://www.apolloconfig.com/

Downloads: https://github.com/apolloconfig/apollo/releases

Apollo Resources:
GitHub: https://github.com/apolloconfig/apollo
Issue: https://github.com/apolloconfig/apollo/issues
Mailing list: [apollo-config@googlegroups.com](mailto:apollo-config@googlegroups.com)

Apollo Team
```
