# Localgraph

Localgraph is a local-first correspondence graph for private source texts,
conversation archives, annotations, and project-specific views.

The project starts from Instagram and iMessage exports, but the core idea is
broader: preserve full source texts, normalize them into a local store, and
generate filesystem-native views that can be symlinked into other projects.

## Principles

- Keep raw source exports intact and auditable.
- Keep private archives and generated data out of git.
- Treat person, group, thread, project, and annotation directories as generated
  or curated views over canonical local state.
- Preserve attribution, provenance, timestamps, media references, gaps, and
  parser uncertainty.
- Make every view rebuildable from source data plus local annotations.

## Planned Layout

```text
localgraph/
  sources/      # raw private imports, ignored by git
  state/        # SQLite and derived indexes, ignored by git
  objects/      # copied/content-addressed private media, ignored by git
  views/        # generated symlink-friendly person/group/thread/project views
  annotations/  # private notes and tags, ignored by git by default
  exports/      # private packaged exports and handoff bundles, ignored by git
  docs/         # public architecture notes
```

The public repository contains code and design notes only. Personal messages,
media, exports, indexes, and annotations belong on local disk.

## Composite Scaffold CLI

The scaffold uses only the Python standard library. It combines the strongest
parts of the first scaffold family:

- a SQLite-first canonical state model;
- an explicit private-root and filesystem-view contract;
- body-safe Instagram transfer scanning;
- deterministic symlink-friendly view paths.

```bash
python -m localgraph --root ~/Localgraph plan
python -m localgraph --root ~/Localgraph init
python -m localgraph --root ~/Localgraph doctor
python -m localgraph --root ~/Localgraph scan
python -m localgraph --root ~/Localgraph import --me "Jamie Burkart" --render
python -m localgraph --root ~/Localgraph drive-pull
python -m localgraph --root ~/Localgraph daily-import --me "Jamie Burkart"
python -m localgraph --root ~/Localgraph instagram-sync --me "Jamie Burkart"
python -m localgraph --root ~/Localgraph render
python -m localgraph --root ~/Localgraph view-name person "Alice Example" "instagram:alice"
```

`init` creates the private local workspace directories and a SQLite database.
`scan` detects Instagram transfer exports under `sources/instagram` without
returning message bodies. `import` reads Instagram JSON message exports and an
iMessage `chat.db`, normalizes people, accounts, groups, threads, messages, and
media references into SQLite, and can immediately `--render` filesystem views.
`drive-pull` uses a private authenticated Google Drive API token to mirror a
configured Drive folder into `sources/instagram-drive-cache`. `instagram-sync`
accumulates completed `instagram-*` packets under a stable Drive container,
publishes their cumulative set through the stable `sources/instagram-current`
directory symlink, imports it, records freshness and history-coverage state,
and renders views. `daily-import` remains the combined Instagram and iMessage path.
`render` builds deterministic
filesystem views from canonical SQLite state and writes
`_system/source-manifest.json`.

Default private import locations:

```text
sources/instagram/          # Meta/Instagram export folders
sources/imessage/chat.db    # copied macOS Messages database
```

You can also point at real source paths directly:

```bash
python -m localgraph --root ~/Localgraph import \
  --instagram-source "/path/to/instagram-export-root-or-parent" \
  --imessage-db "/path/to/chat.db" \
  --me "Jamie Burkart" \
  --me-instagram "Jamie" \
  --me-imessage "jamie@example.com" \
  --render
```

On macOS, `~/Library/Messages/chat.db` is usually protected by Full Disk Access.
The simplest repeatable workflow is to copy `chat.db` plus its `chat.db-wal` and
`chat.db-shm` siblings into `sources/imessage/`, then run the import there.

## Maintained Google Drive Mirror

Preferred setup is authenticated Drive API pull, not public folder sharing.
Create a Google OAuth desktop client JSON in Google Cloud, keep it private, then
authorize Localgraph:

For scheduled use on macOS, keep the operational workspace on the internal
volume. Background LaunchAgents cannot reliably read removable-volume
worktrees under macOS privacy controls. The installer snapshots the required
Python runtime under `~/Library/Application Support/Localgraph/runtime`; the
recommended maintained directory is:

```text
~/Library/Application Support/Localgraph/workspace/sources/instagram-current
```

```bash
python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" init
python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" drive-auth \
  --client-secrets "/path/to/oauth-client-secret.json"
```

Configure a stable private Drive container ID from its Google Drive URL. The
container may hold direct `instagram-*` exports or dated
`meta-*/instagram-*` streams. Localgraph lists only those bounded export
folders, downloads the message subtree from every not-yet-completed Instagram
packet, and never walks unrelated container or account-export contents:

```bash
python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" configure-drive-api \
  --folder-id "GOOGLE_DRIVE_FOLDER_ID"
```

Then test the maintained mirror:

```bash
python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" instagram-sync \
  --me "Jamie Burkart" \
  --me-instagram "jamieburkart"
```

This writes immutable downloaded exports under
`sources/instagram-drive-cache/`, atomically advances
`sources/instagram-current` to a cumulative directory of completed provider
packets, and writes OAuth/token, completed-export, and freshness state under
`state/`. These locations are ignored by git. Meta scheduled transfers are
incremental: each packet contains information that was not in the prior
transfer. `instagram-sync` therefore rebuilds source-derived Instagram state
from every completed packet and deduplicates overlapping messages instead of
replacing history with the latest delta. User-authored `notes.md` and
annotations are not part of that source-derived reset. A failed, interrupted,
offline, or unauthorized pull retains the cumulative last-known-good directory
and never publishes a partial cache folder. A private workspace lock prevents
manual and scheduled `instagram-sync` runs from writing the same cache
concurrently; a second invocation exits successfully with
`status: skipped-concurrent`. A retry reuses private files whose size and
provider MD5 checksum already match, then resumes the message-only pull. Long
historical transfers recheck token lifetime during traversal and refresh the
private OAuth credential before subsequent provider requests. Drive metadata
for sibling export and message folders is listed with bounded concurrency so
large, mostly empty Meta directory skeletons do not serialize thousands of
network round trips; file writes and canonical imports remain single-writer.

Freshness and historical completeness are separate. Until a one-time
all-available-information export has completed, sync status reports
`historyCoverage: baseline-required` even when `status: current`. After
verifying the exact completed folder produced by that one-time export, record
it as the baseline:

```bash
python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" \
  configure-instagram-baseline --export-name "instagram-ACCOUNT-YYYY-MM-DD-SUFFIX"
```

Only then does `state/instagram-sync-status.json` report
`historyCoverage: complete-through-latest-export`. It also distinguishes
`current`, `degraded`, `pending`, and local-fallback freshness states without
exposing message bodies.

If you also use Drive Desktop, you can still pin the local synced folder:

```bash
python -m localgraph --root ~/Localgraph configure-drive \
  --instagram-drive-source "/Users/jamie/Library/CloudStorage/GoogleDrive-example/My Drive/Instagram"
```

Then run the daily importer manually or from automation:

```bash
python -m localgraph --root ~/Localgraph daily-import \
  --me "Jamie Burkart" \
  --me-instagram "jamieburkart" \
  --write-config
```

The importer first tries the authenticated Drive API pull when a folder ID and
token are configured. If API pull is not configured or fails, it falls back to
explicit and configured local Drive paths, then shallow discovery under Drive
Desktop roots such as `Shared drives/Instagram` or `My Drive/Instagram`. The
authenticated pull takes precedence even when an older scheduler still passes
an explicit Drive Desktop path; this prevents an online-only placeholder from
bypassing a working API cache. Authenticated sync imports every registered
completed export packet. For local Drive fallback,
the first run imports every materialized export it can see and later runs use
only the newest export by default. Pass `--all-instagram-exports` when you
intentionally want a local archive-wide rescan. If Drive Desktop has not
materialized an export locally yet, the run is recorded as `pending` instead of
blocking on a provider-backed folder read.

On macOS, install the focused Instagram user LaunchAgent:

```bash
python -m localgraph --root "$HOME/Library/Application Support/Localgraph/workspace" install-instagram-sync \
  --me "Jamie Burkart" \
  --me-instagram "jamieburkart" \
  --interval-minutes 60
```

The LaunchAgent runs at login and every hour while the Mac is awake. Therefore
the freshness bound is the next hourly check plus download/import time after a
new export becomes visible in Drive; no polling system can honestly promise
instantaneous or offline freshness. The job is Instagram-only, never pins a
Drive Desktop placeholder, snapshots its private runtime and script under
`~/Library/Application Support/Localgraph/`, and writes its plist under
`~/Library/LaunchAgents/`. Every run appends a private audit record to
`state/daily-import-runs.jsonl`; scheduler output stays under the internal
Application Support log directory.

The older `install-daily-import` command remains available when a single
once-daily job should import both Instagram and iMessage. It is not the
preferred freshness loop for the maintained Instagram mirror.

Generated view paths pair readable labels with a short hash suffix derived from
a source key:

```text
views/people/alice-example--3a1f0d22/
views/groups/residency-planning--a7c91f8e/
```

Thread views include `index.md` metadata and `messages.md` transcripts under:

```text
views/threads/instagram/<thread>/
views/threads/imessage/<thread>/
```

Person views are designed as portable context capsules. A person directory can
be temporarily symlinked into a project workspace so a local LLM has a readable
orientation layer plus links to the full source transcripts:

```text
views/people/alice-example--3a1f0d22/
  index.md
  llm-context.md
  timeline.md
  threads.md
  groups.md
  media.md
  source-accounts.md
  notes.md
  transcripts/
    direct/
      instagram-alice-example--9bc4d1a0.md -> ../../../threads/instagram/.../messages.md
    groups/
      instagram-residency-planning--a7c91f8e.md -> ../../../threads/instagram/.../messages.md
  manifests/
    person.json
    accounts.json
    transcripts.json
```

`notes.md` is user-authored and preserved across renders. The other files are
generated orientation, navigation, provenance, and transcript-link material.

## Instagram Evals and Hill Climb

The deterministic Instagram suite covers the offline PKCE and read-only OAuth
contract, bounded cumulative-export selection, explicit baseline completeness,
atomic completed-mirror publication, cumulative source replacement, stale
generated-view reconciliation, last-known-good fallback, hourly scheduling,
authenticated acquisition precedence, overlapping-export deduplication,
single-writer synchronization, canonical import and rendering, and repository
workspace compatibility. It
never uses private message bodies as committed fixtures.

```bash
make evals
make hill-climb
```

`make evals` emits a candidate-bound JSON receipt with the exact Git head and a
SHA-256 digest of every non-ignored candidate file. `make hill-climb` runs that
suite, the complete unit-test suite, and `git diff --check`.

Instagram messages are keyed from their stable exported payload plus an
occurrence ordinal within each thread/export. This keeps distinct repeated
messages while preventing overlapping exports from duplicating a message when
Meta moves it between split files or array positions.
