# Community review rollout and validation plan

This is a local implementation proposal. Nothing in this repository enables a schedule,
changes the active model, grants private advisory access, or publishes a GitHub message.

## Operator acceptance before activation

- Confirm the intended actor and repository maintainer map. The authenticated login is
  checked again before publication. Do not run the public and private workflows with
  a shared workspace, queue, logs, state file or model conversation.
- Verify the actual model and reasoning configuration supported by the deployed runner.
  A YAML prompt does not prove which model the scheduler will execute.
- Verify private vulnerability access separately, with minimum permissions and a
  confidential destination. Do not test that access from the public scanner. Private
  and GHSA repositories are excluded even when the public scan policy is overridden.
- Review the exact allowed automatic actions: evidence-backed issue/PR comments and
  P1/P2 request-changes with validation tied to the current PR head. Approvals, merges,
  releases, and other administrative actions remain outside the automatic publisher.
- Choose an explicit timezone and two run times approximately 12 hours apart. The
  proposed cadence is twice daily, not every four hours. No cron or automation is installed.
- All invocations for one actor must use the same durable state file and POSIX lock.
  Multiple hosts need a single publisher or an external shared lock/store, not copies
  of the JSON state. Back up state before upgrading or resetting anything.

## Offline checks

Run from the repository root:

```sh
python3 -m unittest discover -s apollo-community-review/scripts -p 'test_*.py' -v
python3 -m compileall -q apollo-community-review/scripts
git diff --check
```

Regression areas:

1. Public organization discovery, archived/disabled exclusions, bot noise and maintainer
   suppression, private/GHSA separation and absent visibility fail-closed behavior.
2. Repository-specific scan-start cursor, at least 24-hour bootstrap and one-hour overlap,
   pending queue recovery, partial scan rollback, independent repositories, old worker
   acknowledgements and concurrent atomic updates.
3. Typed decision schema, explicit evidence, minimal missing-info requests, high-confidence
   validated comments, supported request-changes, and unsupported approvals/administration.
4. Full public thread, comments/reviews, changed-file patches and outgoing text security
   checks; missing/truncated diffs or checks route to manual review.
5. Post-time actor, public repo, open/unlocked state, draft flag, newer maintainer activity,
   complete context fingerprint, PR head and eligibility revalidation.
6. Durable intention before write, confirmed-send reporting, duplicate suppression,
   ambiguous timeout recovery without blind resends, and dry-run with no GitHub writes.
7. Raw check-output/status-description screening and freshness, passing Check-Runs-only
   commits, equal commit/maintainer timestamps, Draft → Ready transitions, confirmed
   deletion versus ambiguous failures, reverted pending content, original-snapshot
   publication binding, and crash-safe recovery before refresh/acknowledgement.

## Dry run and limited activation

First run discovery and review with no publication. Save the exact candidate snapshot
and review output. Run the posting command with `--dry-run`: it still reads current GitHub
state and applies the real gate, but does not send. Compare a representative issue,
ordinary PR, draft PR, outdated review, missing-info case and supported request-changes.

After operator acceptance, use a separately authorized test issue/PR for live integration
verification. Validate the returned GitHub URL, posting identity, AI disclosure, head-pinned
formal review, duplicate run, and crash/reconciliation behavior before any general rollout.
The offline suite mocks GitHub and does not prove token scopes, server permissions,
GitHub API behavior, scheduler execution, or the chosen model configuration.

## Incident and recovery rules

- A read/parse/scan failure must not advance that repository's cursor. Retry read-only
  discovery later; pending queue entries remain available.
- Never mark `ready-to-send`, `dry-run`, error, pending or uncertain publication as processed.
  Only confirmed `auto-sent`, a deliberate `needs-review` handoff, or an intentional `skipped`
  result is terminal. Manual handoffs remain an operator responsibility after acknowledgement.
- An interrupted write may already have reached GitHub. The next run searches the original
  endpoint for the exact operation marker and authenticated actor, including the exact head
  and review state for request-changes. If no exact result is found, stop and let the operator
  reconcile. Absence in one read is not permission to retry or clear the journal.
- Keep old state and logs for diagnosis. Do not delete the journal to force a resend.
- The scanner reconciles unresolved operations before refreshing candidates. A confirmed
  result and its original snapshot are saved together in an operation-keyed recovery
  outbox. A durable recovery flag resumes matching-queue completion after interruption;
  a later operation cannot overwrite the earlier recovered result. If newer work is
  already queued, recovery records the old success without acknowledging that newer work.
- Use `scan-results --state-file <path> --actor <login> --since <last_successful_report_iso>`
  to include recovery and confirmed-deletion outcomes in the run summary. Unresolved
  writes are always included, remain held, and cannot be acknowledged as manual/skipped.
  Retain the reporting timestamp until the summary is successfully delivered.
- A legacy `sent` journal that lacks the original decision cannot certify a caller's
  fresh context. An orphan matching remote marker also cannot reconstruct that context.
  Both require operator reconciliation; neither permits an automatic resend.
- Only the GitHub CLI's explicit HTTP 410 response to a pending issue detail read is
  treated as confirmed deletion. Its skipped outcome is retained without deleted-thread
  content. A 404, authorization failure or transient error is not proof of deletion.

## Known boundaries

- GitHub provides no atomic compare-and-comment endpoint. A race can remain between the
  final GET and POST. Formal request-changes pins `commit_id`; comments cannot be conditional.
  Revalidation reduces this window but cannot eliminate it.
- Evidence schema and security keyword checks are conservative filters, not proof of
  technical correctness or an exhaustive vulnerability/secret scanner. A reviewer must
  verify source links, path/line references, exact validation output and impact. Never
  execute contributor-controlled code with publishing credentials or private data present.
- Git commit dates are contributor-controlled, not push times. Future-dated commits and
  commit dates that would outrank a maintainer reply without a newer external social
  event are held for manual review. Genuine push-only updates can therefore be held too.
- REST root-body timestamps cannot distinguish a first-seen old issue edit from unrelated
  bot churn. First-seen recently updated threads with older external activity are queued
  with a manual uncertainty hold, never automatically answered. Subsequent observed
  title/body/head changes are detected by stored fingerprints. For untouched historical
  backlog, authorize a backfill rather than claim the bootstrap covers every old thread.
- Discovery and publication both fetch the issue detail representation before fingerprinting.
  This defends against possible list/detail shape differences at an extra read cost; it
  is not a claim that current live GitHub responses differ for ordinary issues.
- Full diff payloads can be absent for binary/large files or capped by GitHub. Those PRs
  remain manual. Required-branch-rule discovery is not implemented; the helper requires
  complete fetched checks/statuses and does not certify merge readiness.
- Local state locks are POSIX-only. Private vulnerability automation, schedule activation,
  model selection, live token validation and remote publication are intentionally not done.
