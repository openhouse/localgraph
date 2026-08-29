# Source health and acceptance

## Purpose

Localgraph uses one body-free operator report for Instagram, Facebook, and Apple Messages:

```bash
python -m localgraph \
  --root "$HOME/Library/Application Support/Localgraph/workspace" \
  status
```

The report is an evidence index, not a substitute for provider inspection. It includes every configured account, its source-specific scheduler, authorization state, local packet and canonical counts, history coverage, findings, and lifecycle. It never returns correspondence bodies, OAuth secrets, or raw error text.

## Health findings

`status` evaluates these conditions independently:

| Finding | Evidence |
| --- | --- |
| `launchagent-missing` | The expected user LaunchAgent plist does not exist. |
| `launchagent-unloaded` | The plist exists but `launchctl` has no loaded job. |
| `launchagent-failed` | The loaded job reports a nonzero last exit code. |
| `launchagent-never-run` | The loaded job has no completed run. |
| `stale-sync` | The last successful receipt is older than twice the configured hourly interval. |
| `authorization-missing` | A Drive-backed account has no token file. |
| `authorization-expired` | Its access credential is expired and has no refresh credential. |
| `authorization-invalid` | Its token file cannot be parsed as the expected private JSON state. |
| `missing-export` | No completed provider packet is locally evidenced for that account. |
| `unexpected-empty-snapshot` | A completed/current candidate has no messages, message files, or snapshot bytes where populated custody is expected. |
| `historical-completeness-not-established` | Current packets exist, but no exact account baseline establishes all-time coverage. |
| `provider-export-capability-unverified` | A Facebook Page has not passed its individual provider-surface check. |

A refreshable expired access token is reported as `refreshable`, not as lost authorization. A scheduled interval job may correctly be `not running` between runs; Localgraph uses its load state, run count, exit code, and source receipt rather than treating idle as failure.

## Lifecycle ledger

Each account is evaluated against this ordered acceptance model:

```text
configured → requested → preparing → delivered → imported → rendered → current / complete
```

The stages deliberately use different evidence:

| Stage | Required evidence |
| --- | --- |
| `configured` | Account exists in the ignored local registry. |
| `requested` | Provider activity was observed after submission. |
| `preparing` | Provider activity visibly entered preparation. |
| `delivered` | A completed account-prefix packet or validated Messages snapshot exists locally. |
| `imported` | The canonical database contains account-scoped source imports. |
| `rendered` | The corresponding private filesystem view exists. |
| `current` | Delivery, import, and rendering are present and the successful receipt is fresh and nonempty. |
| `complete` | An account-specific all-time baseline is recorded, or Apple Messages has a validated complete-through-snapshot receipt. |

`current` and `complete` are separate assertions. A subsequent incremental Instagram or Facebook packet can be delivered, imported, rendered, and current while history remains `baseline-required`. Packet counts advance without turning that state into a completeness claim.

Only the provider-observed `requested` and `preparing` stages are entered manually:

```bash
python -m localgraph --root "$LOCALGRAPH_ROOT" record-lifecycle \
  --source instagram --account ACCOUNT --stage requested \
  --evidence provider-activity-record \
  --observed-at 2026-08-29T14:40:00Z
```

Localgraph derives delivery and every later stage from local custody. The private ledger is written owner-only under `state/lifecycle/<source>/<account>.json`.

## Instagram acceptance

An Instagram account is accepted as current only after an exact-prefix packet is locally complete, represented in canonical state, and rendered. A one-time all-time baseline must then be recorded by exact completed folder name before the account is complete. Later daily packets remain separately counted in the completed-export registry and do not weaken or silently manufacture the baseline claim.

## Facebook Page acceptance

Registering or administering a Page is not evidence that its Messages can be exported. Every Page begins with `exportCapability.status: unverified` and `syncEligible: false`. Local packets found for that Page are held without import or baseline acceptance.

Inspect the Page's own authorized settings surface. Then record only what was observed for that exact Page:

```bash
python -m localgraph --root "$LOCALGRAPH_ROOT" \
  verify-facebook-export-capability \
  --account PAGE_KEY \
  --capability supported \
  --provider-surface facebook-page-settings \
  --observed-at 2026-08-29T22:00:00Z
```

Use `--capability unsupported` when the inspected Page surface does not offer an export Localgraph can accept. Verification never propagates to another Page. Deactivated, disabled, unverified, and verified-unsupported Pages remain visible in status but are not synchronized.

## Release acceptance

For a Source Health and Acceptance release:

1. Run `status` against the real private workspace and retain only body-free counts and finding codes in public review material.
2. Run an authenticated source sync when delivery must be checked; do not infer delivery from a provider `preparing` record.
3. Confirm each LaunchAgent is loaded, has run, and last exited zero.
4. Confirm current and complete separately for every intended account.
5. Run `make hill-climb` on the exact candidate and report each eval-suite count plus the complete unit-test count.
