## What changed

- Released Apollo Helm charts and refreshed the Helm repository index.

## Version matrix

| Chart | version | appVersion |
| --- | --- | --- |
| apollo-portal | `{portal_version}` | `{portal_app_version}` |
| apollo-service | `{service_version}` | `{service_app_version}` |

## Helm release artifacts

{artifacts}

## Commands executed

{commands}

## Checklist

- [x] Only expected release files are changed (`apollo-portal/Chart.yaml`, `apollo-service/Chart.yaml`, `docs/index.yaml`, `docs/apollo-*.tgz`)
- [x] `helm lint` {lint_note}
