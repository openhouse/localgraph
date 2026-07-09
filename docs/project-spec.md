# Localgraph Project Spec

Localgraph is a local-first correspondence graph for private message archives.
It imports Instagram Meta exports and Apple iMessage `chat.db` data, normalizes
them into SQLite, and renders filesystem-native views that humans and local LLM
agents can browse, symlink, and cite.

## Goals

- Keep raw private sources local and out of git.
- Use SQLite as canonical state.
- Treat filesystem views as rebuildable projections.
- Make views useful as a filesystem UI, not just dumps.
- Keep generated paths stable, readable, and symlink-friendly.
- Keep full evidence accessible through canonical thread transcripts.
- Preserve human-authored notes across repeated renders.

## Inputs

- Instagram/Meta export folders, including daily Google Drive transfers.
- Apple Messages `chat.db` copied into the workspace or passed explicitly.
- Future sources behind the same source/state/view model.

## Workspace

```text
localgraph/
  sources/       ignored raw imports
  state/         ignored SQLite, run logs, scheduler scripts
  objects/       ignored media/object storage
  views/         generated human/LLM filesystem UI
  annotations/   private notes/tags
  exports/       private handoff bundles
```

## CLI

- `init`: create workspace and schema.
- `doctor`: check workspace/schema.
- `scan`: body-safe Instagram export scan.
- `import`: import Instagram/iMessage sources.
- `daily-import`: automated local Google Drive Instagram import.
- `configure-drive`: persist local Drive source path.
- `install-daily-import`: create a macOS LaunchAgent job.
- `render`: rebuild filesystem views.
- `view-name`: print stable view path.

## Import Behavior

- Initial daily import bootstraps all materialized Instagram exports.
- Later daily imports default to the newest materialized export.
- `--all-instagram-exports` forces a full materialized archive rescan.
- If Google Drive Desktop has not downloaded files locally, record pending state
  instead of hanging on provider-backed reads.
- iMessage import reads copied `chat.db` by default to avoid Full Disk Access
  surprises.

## Canonical Entities

- `identities`: people and group identities.
- `accounts`: source-specific handles, emails, and phone numbers.
- `threads`: direct and group conversations.
- `thread_participants`.
- `messages`.
- `media_objects`.
- `graph_edges`.
- `source_imports`.
- `source_locations`: configured local source roots.
- `import_runs`: daily/manual run ledger.
- `pending_imports`: materialization or source availability blockers.

## Filesystem Views

```text
views/
  index.md
  _system/
  people/
  groups/
  threads/
  projects/
  tags/
```

## Person Context Capsule

```text
views/people/alice-example--hash/
  index.md             overview and navigation
  llm-context.md       read-first brief for local agents
  timeline.md          recent cross-thread context
  threads.md           direct and group thread table
  groups.md            shared group contexts
  media.md             media references involving this person
  source-accounts.md   provenance and account mapping
  notes.md             user-authored, preserved
  transcripts/
    direct/            symlinks to full direct thread messages.md
    groups/            symlinks to full shared group messages.md
  manifests/
    person.json
    accounts.json
    transcripts.json
```

## Project Usage

A user can symlink a person directory into a project workspace:

```bash
mkdir -p my-project/.localgraph/people
ln -s ~/Localgraph/views/people/alice--hash my-project/.localgraph/people/alice
```

That gives a project-local LLM:

- a concise orientation file;
- recent context;
- preserved human notes;
- full transcript evidence through symlinks;
- no copied private data committed to the project.

## Acceptance Criteria

- Real Instagram and iMessage messages import into SQLite.
- People, groups, and threads render deterministically.
- Person folders include LLM context and symlinked transcript evidence.
- Notes survive repeated renders.
- Daily Drive automation supports bootstrap, incremental runs, pending state,
  run logs, and macOS scheduler installation.
