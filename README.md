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
python -m localgraph --root ~/Localgraph render
python -m localgraph --root ~/Localgraph view-name person "Alice Example" "instagram:alice"
```

`init` creates the private local workspace directories and a SQLite database.
`scan` detects Instagram transfer exports under `sources/instagram` without
returning message bodies. `import` reads Instagram JSON message exports and an
iMessage `chat.db`, normalizes people, accounts, groups, threads, messages, and
media references into SQLite, and can immediately `--render` filesystem views.
`drive-pull` uses a private authenticated Google Drive API token to mirror a
configured Drive folder into `sources/instagram-drive-cache`. `daily-import`
runs that pull first when configured, bootstraps all materialized exports on the
first run, narrows subsequent runs to the newest synced transfer by default,
appends a JSONL run log, and renders views. `render` builds deterministic
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

## Daily Google Drive Import

Preferred setup is authenticated Drive API pull, not public folder sharing.
Create a Google OAuth desktop client JSON in Google Cloud, keep it private, then
authorize Localgraph:

```bash
python -m localgraph --root ~/Localgraph drive-auth \
  --client-secrets "/path/to/oauth-client-secret.json"
```

Configure the private Drive folder ID from the Google Drive URL:

```bash
python -m localgraph --root ~/Localgraph configure-drive-api \
  --folder-id "GOOGLE_DRIVE_FOLDER_ID"
```

Then test the private cache pull:

```bash
python -m localgraph --root ~/Localgraph drive-pull
```

This writes downloaded export files under `sources/instagram-drive-cache/` and
OAuth/token state under `state/`. Both locations are ignored by git.

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
bypassing a working API cache. The first scheduled run imports every
materialized export it can see, so the local
graph starts complete. Later scheduled runs import only the newest export
folder by default, so the job does not repeatedly recurse through a large Drive
archive. Pass `--all-instagram-exports` when you intentionally want an
archive-wide rescan. If Drive Desktop has not materialized an export locally
yet, the run is recorded as `pending` instead of blocking on a provider-backed
folder read.

On macOS, install a user LaunchAgent for the daily import:

```bash
python -m localgraph --root ~/Localgraph install-daily-import \
  --me "Jamie Burkart" \
  --me-instagram "jamieburkart" \
  --hour 3 \
  --minute 15
```

Omit `--instagram-drive-source` when using authenticated Drive API pull; that
lets `daily-import` pull the configured Drive folder into the private cache
before importing. The installer writes the job script under `state/bin/` and the
LaunchAgent plist under `~/Library/LaunchAgents/`. Each run appends a private
audit record to `state/daily-import-runs.jsonl` and scheduler output to
`state/daily-import.*.log`.

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

The deterministic Instagram suite covers authenticated acquisition precedence,
private-cache fallback, overlapping-export deduplication, canonical import and
rendering, and repository workspace compatibility. It never uses private
message bodies as committed fixtures.

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
