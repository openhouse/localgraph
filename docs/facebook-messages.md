# Facebook profile and managed Page messages

## Goal

Maintain a private, local, account-scoped representation of Facebook Messages for a personal profile and every explicitly registered managed Page. The system distinguishes provider configuration from local evidence: a managed Page is not called archived until a Messages packet has materialized, imported, and rendered.

## Registry and identity model

`imports.facebook.accounts` is keyed by a stable local account key. Real provider IDs are not required and should not be committed. Each record contains:

- `accountType`: `profile` or `page`;
- `providerState`: `active`, `deactivated`, or `unknown`;
- the exact expected `facebook-<account>-` packet prefix;
- self participant aliases;
- a personal or organizational owner identity;
- account-scoped incoming, Drive cache, completion registry, manifest, baseline, and sync-status paths.

The primary personal profile maps to `person:self`. A Page maps to `organization:facebook:<account>`, even when the same person administers it. Correspondents retain shared `person:facebook:<name>` identities across profile and Page conversations when their normalized provider names match.

## Provider protocol

Every account exposes `requiredProviderExportProtocol` through `facebook-accounts`. The protocol is intentionally evidence-calibrated.

### Personal profile

The verified Accounts Center path is:

1. Open **Your information and permissions → Export your information → Create export**.
2. Select only the Facebook profile.
3. Create a one-time **Messages**, **All time** export to Google Drive.
4. Create a second **Messages**, **All time** export to Google Drive, **Daily**, for **3 years**.
5. Accept the narrowly displayed Google Drive permission for provider-created files.
6. Verify both entries in Meta's export activity before calling the provider configuration scheduled.
7. After the all-time packet is fully local, run `configure-facebook-baseline` with its exact folder name.

This is the same custody shape used for Instagram, but Facebook and Instagram remain separate registry records, packet selectors, caches, and canonical source kinds.

### Managed Pages

Meta documents that people with Page access can handle direct messages in the Page Inbox and provides a Page settings surface for downloading a Page copy. Accounts Center does not expose managed Pages as personal-profile export identities in the inspected flow. Therefore Localgraph records the desired Messages-only all-time baseline and daily three-year recurrence for each Page, but reports their provider support as `provider-verification-required` until the Page's own settings show the exact available export and cadence controls.

For each active Page:

1. Sign in to Facebook.com in the authorized Chrome profile.
2. Use **See all profiles** and switch into the Page.
3. Confirm access under **Settings → Page setup → Page access**.
4. Open the Page download/export surface and inspect whether Messages and a recurring Drive destination are offered.
5. If offered, apply the standard Messages-only all-time baseline plus daily three-year recurrence and verify the activity record.
6. If only a one-time Page copy is offered, download that copy into the account's `incoming` path and leave recurrence support unverified; do not convert a manual export into a claim of automated provider coverage.

Deactivated Pages remain visible in the private registry for historical custody. They may accept an already-held export, but their provider state must not be reported as active.

Provider references: [About Facebook Page access](https://www.facebook.com/help/289207354498410/r.php/), [Manage Page settings](https://www.facebook.com/help/1206330326045914), and [See your Facebook Page access](https://www.facebook.com/help/510247025775149/).

## Configure accounts

```bash
python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" \
  configure-facebook-account \
  --account PERSONAL_KEY --display-name "Personal Name" \
  --account-type profile --self-name "Personal Name" \
  --reuse-instagram-drive

python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" \
  configure-facebook-account \
  --account PAGE_KEY --display-name "Page Name" \
  --account-type page --provider-state active --self-name "Page Name"

python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" facebook-accounts
```

Register real account names only in the ignored private workspace configuration, not in repository fixtures or documentation.

## Private paths and acquisition

Each account owns these paths:

```text
sources/facebook-accounts/<account>/incoming/
sources/facebook-accounts/<account>/drive-cache/
sources/facebook-accounts/<account>/current
state/facebook-accounts/<account>/completed-exports.json
state/facebook-accounts/<account>/pull-manifest.json
state/facebook-accounts/<account>/sync-status.json
views/facebook-accounts/<account>/threads/
```

An authenticated profile pull lists only exact-prefix Facebook export packets and downloads only `your_facebook_activity/messages` or `messages`. Local `incoming` packets use the same importer. Inbox, archived-thread, and message-request folders are accepted. Raw bodies enter only ignored SQLite state and private rendered transcripts; scan and status responses remain body-free.

Facebook accounts advance independently. A pending Page cannot erase a current personal-profile projection. Before replacing one account's projection, Localgraph requires at least one materialized packet for that account, then rebuilds that account from all of its available packets so provider repagination does not duplicate messages.

## Privacy exclusion

When a personal profile or Page is outside the authorized scope, remove it from
the active registry and create a private exclusion tombstone:

```bash
python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" \
  exclude-facebook-account --account ACCOUNT --reason former-member-privacy
```

An excluded account is not merely disabled. Scheduled runs do not enumerate or
import it, and later configuration attempts using the same account key fail.
The tombstone stays in ignored private configuration and contains only the key,
reason code, and exclusion time; Localgraph writes that registry owner-only
(`0600`). The command does not inspect or delete old raw
paths; custody cleanup, if needed, is a separate explicit action.

## Freshness and completeness

Run a manual refresh:

```bash
python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" facebook-sync
```

Install the hourly, run-at-login freshness job:

```bash
python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" \
  install-facebook-sync --interval-minutes 60
```

The Facebook and Instagram jobs use the same private workspace lock, preventing concurrent SQLite writers. Provider cadence and Localgraph check cadence are separate: an hourly check bounds detection after a provider packet appears; it does not make Meta export more frequently.

Record a verified all-time packet only after it is fully materialized:

```bash
python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" \
  configure-facebook-baseline --account ACCOUNT \
  --export-name "facebook-ACCOUNT-YYYY-MM-DD-SUFFIX"
```

Status remains `historyCoverage: baseline-required` until this gate is satisfied for that exact account. `current` means locally refreshed; it does not independently prove complete history or provider recurrence.

## Operational acceptance

An account is operational only when the applicable evidence exists:

- registry record and correct person/Page owner identity;
- verified provider activity record, if the provider exposes one;
- exact-prefix message packet completed locally;
- successful body-safe scan and canonical import;
- account-specific rendered thread links;
- hourly scheduler loaded and able to acquire the workspace lock;
- exact all-time baseline recorded before any complete-history claim.

Repository evals use fictional fixtures and verify these boundaries without credentials, provider IDs, private participant lists, or real message bodies.
