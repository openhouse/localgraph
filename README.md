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
python -m localgraph --root ~/Localgraph import
python -m localgraph --root ~/Localgraph import instagram --source ~/Downloads/instagram-export
python -m localgraph --root ~/Localgraph import imessage --source ~/Library/Messages/chat.db
python -m localgraph --root ~/Localgraph configure-drive ~/Library/CloudStorage/GoogleDrive-example/MyDrive/Instagram
python -m localgraph --root ~/Localgraph daily-import
python -m localgraph --root ~/Localgraph daily-import --all-instagram-exports
python -m localgraph --root ~/Localgraph install-daily-import
python -m localgraph --root ~/Localgraph render
python -m localgraph --root ~/Localgraph view-name person "Alice Example" "instagram:alice"
```

`init` creates the private local workspace directories and a SQLite database.
`scan` detects Instagram transfer exports under `sources/instagram` without
returning message bodies. `import` reads private message bodies into the local
SQLite database from Instagram `message_*.json` exports and macOS Messages
`chat.db` files. `render` builds deterministic filesystem views from canonical
SQLite state, including people, groups, thread indexes, and `messages.md`
transcripts, then writes `_system/source-manifest.json`.

By default, imports read from ignored private source directories:

```text
sources/instagram/  # Meta transfer export folders containing message_*.json
sources/imessage/   # copied chat.db, or pass --source ~/Library/Messages/chat.db
```

Re-running `import` is idempotent for the same source messages: identities,
accounts, threads, messages, and media references are upserted into the
canonical state.

`configure-drive` stores a local Google Drive Desktop source path for Instagram
transfers. `daily-import` bootstraps all materialized exports on its first
successful run, then defaults to the newest materialized export. If Drive has not
downloaded message files locally, the run records pending state in SQLite and
exits cleanly. `install-daily-import` writes a macOS LaunchAgent plist and keeps
a workspace copy under `state/scheduler/`.

Person views are context capsules for humans and local agents:

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
  manifests/
```

`notes.md` is user-authored and preserved across renders. Transcript links point
back to canonical thread `messages.md` files instead of copying private message
bodies into each person folder.

Generated view paths pair readable labels with a short hash suffix derived from
a source key:

```text
views/people/alice-example--3a1f0d22/
views/groups/residency-planning--a7c91f8e/
```
