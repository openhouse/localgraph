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
selects only the newest `instagram-*` export under a stable Drive container,
publishes it through the stable `sources/instagram-current` directory symlink
after the pull completes, imports it, records freshness state, and renders
views. `daily-import` remains the combined Instagram and iMessage path.
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
`meta-*/instagram-*` streams; Localgraph lists folder metadata and downloads
only the newest Instagram export, never unrelated container contents:

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
`sources/instagram-current` only after a complete provider pull, and writes
OAuth/token and freshness state under `state/`. These locations are ignored by
git. A failed, interrupted, offline, or unauthorized pull leaves
`sources/instagram-current` pointing to the last completed export rather than a
newer partial cache folder. `state/instagram-sync-status.json` distinguishes
`current`, `degraded`, `pending`, and local-fallback states and records the
last successful sync time without exposing message bodies.

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
bypassing a working API cache. Authenticated sync imports the newest complete
export, which is itself a full Meta message export. For local Drive fallback,
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

The deterministic Instagram suite covers bounded newest-export selection,
atomic current-mirror publication, last-known-good fallback, hourly scheduling,
authenticated acquisition precedence, overlapping-export deduplication,
canonical import and rendering, and repository workspace compatibility. It
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
