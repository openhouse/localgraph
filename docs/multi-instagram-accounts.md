# Multi-account Instagram ingestion

## Goal

Maintain one current local correspondence graph from any number of explicitly configured Instagram accounts. Each account keeps separate provider custody and health state, while the canonical graph and rendered views can be queried together.

## Account registry

`imports.instagram.accounts` is keyed by a stable local account key. Each record owns:

- the Instagram profile name and exact `instagram-<profile>-` export prefix;
- the identity represented by that export, including whether it is a person or organization;
- participant names that map to the exporting identity;
- Google Drive container and read-only OAuth token paths;
- account-scoped cache, pull manifest, completed-export registry, current mirror, completed mirror, baseline, and sync status paths.

`imports.instagram.primaryAccountKey` identifies the account that may adopt the original singleton paths. Adopting the existing primary account changes configuration only: it does not move, duplicate, or redownload its private archive. Additional accounts default to `sources/instagram-accounts/<account>/...` and `state/instagram-accounts/<account>/...`.

Accounts may share a Drive container and OAuth token. They never share a packet selector, cache, completion registry, manifest, mirror, baseline, or status file.

## Required provider export protocol

Every enabled Instagram account follows one provider-side protocol. The `instagram-accounts` command exposes this as `requiredProviderExportProtocol` for each account; it is a requirement, not a claim that Localgraph can query Meta's live settings.

| Setting | Required value |
| --- | --- |
| Destination | Google Drive |
| Information | Messages only |
| Historical baseline | Once, all time |
| Recurring cadence | Daily |
| Recurring lookback | All time |
| Recurring term | 3 years |
| Packet prefix | `instagram-<profile>-` |

The recurring values require an all-time lookback and mirror the inspected provider schedule: Meta reports selected information exported to Google Drive once a day for three years. The all-time baseline remains a separate requirement because Meta describes scheduled exports as information that was not in the previous export; incremental packets alone do not prove complete history.

Notification frequency is not part of the ingestion contract. It may remain at Meta's default without changing packet completeness, account isolation, or Localgraph freshness checks.

### Apply the protocol to a configured account

1. In the Chrome profile authenticated as the target Instagram account, confirm the visible profile identity before opening Accounts Center.
2. Open **Your information and permissions → Export your information → Create export** and select only that Instagram profile.
3. Create a one-time Google Drive export of **Messages** for **All time**. This becomes eligible as the account baseline only after the packet is locally complete and explicitly recorded with `configure-instagram-baseline --account <account>`.
4. Create a second export for the same profile and destination. Select **Messages**, **All time**, **Daily**, and **3 years**.
5. Grant Meta only the Google Drive permission shown in the provider authorization flow. Localgraph's own Drive reader remains separately authorized with read-only scope.
6. Before submission, confirm the exact profile, **Messages**, **All time**, and the current account-owned notification address. After submission, confirm that Meta reports Google Drive and once a day for three years in current activity.
7. Run `instagram-accounts` and verify that the account's `requiredProviderExportProtocol.exportNamePrefix` matches the provider packet name exactly.
8. Run an authenticated sync. Accept operational completion only after an exact-prefix packet is complete locally and the account status becomes `current`; do not infer full history until the baseline is recorded.

Repeat these steps for every enabled account in the registry. Never reuse an authenticated Instagram tab merely because another account is operated by the same person.

## Acquisition and publication flow

1. The single hourly LaunchAgent acquires the workspace writer lock.
2. For every enabled account, Localgraph lists the shared Drive container and accepts only packet names whose configured prefix is followed immediately by a provider date.
3. Each packet's message subtree is downloaded into that account's cache. Unrelated account packets and non-message export sections are excluded.
4. A completed packet advances only that account's completed set and current mirror. Failed or empty pulls leave its last-known-good mirror intact.
5. Once every configured account has a usable mirror, Localgraph rebuilds the Instagram projection in one SQLite transaction and renders combined and account-specific views.
6. Account status files record freshness, completed packet count, message-file count, baseline coverage, and last error. An aggregate status reports whether all configured accounts are current.

## Canonical identity and collision rules

Provider thread paths are unique only within an exporting account. Canonical Instagram thread, group, message occurrence, media, and source identifiers therefore include the account key.

The primary personal account may map its self participant to `person:self`. An organizational account maps its self participant to an organization identity such as `organization:instagram:nycartc`; it is not silently merged into the person identity that operates it. Other participant identities remain shared when their normalized provider name is the same, so a correspondent can be connected across the configured accounts.

## Failure semantics

- A missing new account remains `pending` and cannot erase the primary account's working projection.
- A provider or token failure marks only the affected account `degraded` and preserves its last-known-good mirror.
- An empty or partially downloaded packet never advances a current mirror.
- One global writer lock prevents scheduled and manual runs from racing.
- Full-history claims are account-specific and require a locally present, explicitly recorded all-history baseline packet.

## Backward compatibility

Workspaces without an account registry continue to use the singleton Instagram configuration. The `configure-instagram-account --adopt-legacy` migration records the existing working primary account without changing its cache, state, mirror, or scheduler paths. The scheduler reads account identities from configuration, so no account list or private path is embedded in the LaunchAgent command.

## Operational acceptance

For each account, acceptance requires:

- a one-time, all-time, messages-only baseline export to the authorized Drive container;
- an all-time-lookback, messages-only provider export scheduled daily for three years to that container;
- an exact account-prefix match during a real authenticated pull;
- a current or explicitly pending/degraded account status;
- a successful atomic import with distinct account-scoped thread keys;
- body-free counts and date bounds from canonical state and rendered views;
- an explicit baseline before reporting complete history.

Repository tests and evals verify these invariants without committing OAuth credentials, private message bodies, participant lists, Drive locators, or private workspace data.
