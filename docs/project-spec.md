# Localgraph Project Spec

## Goal

Build a local-first correspondence graph for private message archives. Localgraph imports Instagram/Meta exports and Apple iMessage `chat.db` data, normalizes them into SQLite, and renders filesystem-native views that humans and local LLM agents can browse, symlink, and cite.

## Core Principles

- Raw private sources stay local and out of git.
- SQLite is canonical state; filesystem views are rebuildable projections.
- Views should be useful as a UI, not just dumps.
- Every generated path should be stable, readable, and symlink-friendly.
- Full evidence remains accessible through canonical thread transcripts.
- Human-authored notes are preserved and never overwritten by renders.
- Imports should degrade gracefully when provider-backed files are not materialized locally.

## Inputs

- Instagram/Meta export folders, including daily Google Drive transfers.
- Apple Messages `chat.db` copied into the workspace or passed explicitly.
- Future sources can be added behind the same source/state/view model.

## Workspace Layout

```text
localgraph/
  sources/       ignored raw imports
  state/         ignored SQLite, run logs, scheduler scripts
  objects/       ignored media/object storage
  views/         generated human/LLM filesystem UI
  annotations/   private notes/tags
  exports/       private handoff bundles
  docs/          public project notes and specifications
```

The repository tracks code and public docs. `sources/`, `state/`, `objects/`, `views/`, `annotations/`, `exports/`, `PRIVATE-DATA-README.md`, and `localgraph.config.json` are ignored private workspace artifacts.

## CLI

```bash
python -m localgraph --root ~/Localgraph plan
python -m localgraph --root ~/Localgraph init
python -m localgraph --root ~/Localgraph doctor
python -m localgraph --root ~/Localgraph scan
python -m localgraph --root ~/Localgraph import --me "Jamie Burkart" --render
python -m localgraph --root ~/Localgraph configure-drive --instagram-drive-source /path/to/Drive/Instagram
python -m localgraph --root ~/Localgraph daily-import --me "Jamie Burkart"
python -m localgraph --root ~/Localgraph install-daily-import --hour 3 --minute 15
python -m localgraph --root ~/Localgraph render
python -m localgraph --root ~/Localgraph view-name person "Alice Example" "instagram:alice"
```

Commands:

- `plan`: print the planned private root and generated view layout.
- `init`: create workspace directories, config, private-data marker, and SQLite schema.
- `doctor`: check workspace directories and database schema presence.
- `scan`: body-safe Instagram export scan; reports export and message-file locations without returning message bodies.
- `import`: import Instagram and/or iMessage sources into canonical SQLite state.
- `configure-drive`: persist the local Drive Desktop folder where Instagram transfers land.
- `daily-import`: import daily Drive-synced Instagram exports, optionally iMessage, append run logs, and render views.
- `install-daily-import`: install a macOS LaunchAgent wrapper for the daily import job.
- `render`: rebuild filesystem views from SQLite.
- `view-name`: print a deterministic view path for symlink planning.

## Import Behavior

- Instagram import accepts an export root or a parent folder containing one or more materialized Meta export roots.
- iMessage import reads a copied `sources/imessage/chat.db` by default to avoid Full Disk Access surprises; an explicit `--imessage-db` can point elsewhere.
- `--me`, `--me-instagram`, and `--me-imessage` map known source accounts to the self identity.
- Initial daily import bootstraps all materialized Instagram exports under the Drive source.
- Later daily imports default to the newest materialized export.
- `--all-instagram-exports` forces a full materialized archive rescan.
- If Google Drive Desktop has not downloaded files locally, `daily-import` records a pending source result instead of hanging on provider-backed reads.
- Every daily import appends a private JSONL record to `state/daily-import-runs.jsonl`.

## Canonical Entities

SQLite stores the canonical model:

- `source_imports`: import provenance and source roots.
- `identities`: people, groups, organizations, and unknown entities.
- `accounts`: source-specific handles, usernames, emails, or phone numbers linked to identities.
- `threads`: direct and group conversations across source systems.
- `thread_participants`: identity/account membership in threads.
- `messages`: timestamped source messages with sender attribution, text, and raw provenance JSON.
- `media_objects`: referenced photos, videos, files, GIFs, audio, and iMessage attachments.
- `graph_edges`: derived relationships such as thread participants and group membership.
- `annotations`: future local authored notes and tags.

## Filesystem Views

```text
views/
  index.md
  _system/
    README.md
    source-manifest.json
  people/
  groups/
  threads/
    instagram/
    imessage/
  projects/
  tags/
```

Thread views are canonical transcript evidence:

```text
views/threads/<source>/<thread-slug--hash>/
  index.md
  messages.md
```

Person context capsules are portable working context:

```text
views/people/alice-example--hash/
  index.md             overview and navigation
  llm-context.md       read-first brief for local agents
  timeline.md          recent cross-thread context
  threads.md           direct and group thread table
  groups.md            shared group contexts
  media.md             media references involving this person
  source-accounts.md   provenance and account mapping
  notes.md             user-authored, preserved across renders
  transcripts/
    direct/            symlinks to full direct thread messages.md
    groups/            symlinks to full shared group messages.md
  manifests/
    person.json
    accounts.json
    transcripts.json
```

Generated person, group, and thread path names combine a readable slug with a stable hash suffix derived from the source key, for example:

```text
views/people/alice-example--3a1f0d22/
views/groups/residency-planning--a7c91f8e/
views/threads/instagram/alice-example--9bc4d1a0/
```

## Project Usage

A user can symlink a person directory into a project workspace:

```bash
mkdir -p my-project/.localgraph/people
ln -s ~/Localgraph/views/people/alice-example--3a1f0d22 my-project/.localgraph/people/alice
```

That gives a project-local LLM:

- a concise orientation file;
- recent context;
- preserved human notes;
- full transcript evidence through symlinks;
- source account provenance;
- no committed copies of private raw data.

## Automation

Daily automation is local-first:

1. Resolve Instagram Drive source from explicit CLI option, config, shallow Drive Desktop discovery, or workspace fallback.
2. Detect materialized export roots without deep provider-backed reads.
3. Bootstrap all materialized exports on the first run.
4. Import only the newest export on later runs unless explicitly asked for a full rescan.
5. Import optional copied iMessage database.
6. Render filesystem views.
7. Append a JSONL run log.
8. Run under a macOS LaunchAgent when installed.

## Acceptance Criteria

- Real Instagram messages import into SQLite.
- Real copied iMessage `chat.db` messages import into SQLite.
- People, groups, accounts, threads, messages, media references, and graph edges are populated deterministically.
- Thread transcript views render as `messages.md`.
- Person folders include `llm-context.md`, timeline, thread tables, source-account provenance, media references, preserved `notes.md`, manifests, and symlinked transcript evidence.
- Notes survive repeated renders.
- Generated paths are stable, readable, and symlink-friendly.
- Daily Drive automation supports bootstrap, incremental runs, pending state, run logs, and macOS scheduler installation.
- All private source/state/view artifacts remain outside git by default.
