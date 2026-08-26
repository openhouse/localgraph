# Architecture

Localgraph separates evidence from projections.

## Layers

1. Sources

   Immutable raw exports from providers such as Instagram, iMessage, email,
   Slack, Google Drive, and other local archives.

2. State

   Canonical local database state: imports, identities, accounts, threads,
   messages, media, reactions, annotations, tags, provenance, and graph edges.

3. Objects

   Private media and extracted source artifacts, ideally addressed by stable
   hashes so repeated imports can deduplicate content.

4. Views

   Rebuildable filesystem projections:

   - `people/<person>/`
   - `groups/<group-chat>/`
   - `threads/<source>/<thread>/`
   - `projects/<project>/`
   - `tags/<tag>/`
   - `_system/source-manifest.json`

5. Annotations

   Human-authored notes, aliases, links, tags, and project context. These should
   be stored separately from generated transcripts so render jobs can be
   rerun without destroying interpretation.

## First Importers

The first implemented importers consume local source material:

- Instagram transfer data under Meta export folders, including split
  `message_*.json` files, participant lists, message text, and media URI
  references.
- Apple Messages `chat.db` SQLite databases, including `chat`, `handle`,
  `message`, `chat_message_join`, `chat_handle_join`, and attachment join
  tables.

Both importers normalize into the same canonical tables:

- `identities` for people and generated group identities.
- `accounts` for source-specific handles.
- `threads` and `thread_participants` for direct and group conversations.
- `messages` for timestamped text and raw provenance payloads.
- `media_objects` for referenced photos, videos, files, and iMessage
  attachments.
- `graph_edges` for derived thread, group, and participant relationships.

The importers are intentionally local-first. Google Drive automation lists only
folder metadata under a configured stable container, selects the newest direct
`instagram-*` or nested `meta-*/instagram-*` export, and downloads only that
private subtree into `sources/instagram-drive-cache/`. After the pull completes,
an atomic `sources/instagram-current` symlink advances to the selected export.
Provider, network, or partial-transfer failures retain the last completed
pointer and mark `state/instagram-sync-status.json` degraded. The focused
`instagram-sync` path imports and renders that pointer; a run-at-login and
hourly macOS LaunchAgent provides a bounded freshness loop. The same
`daily-import` path can
also use Drive Desktop as a local source fallback: it resolves an explicit,
configured, or shallow-discovered Instagram transfer folder, bootstraps all
materialized exports when no prior Instagram imports exist, narrows later
scheduled runs to the newest detected export by default, runs the same canonical
importer, writes a private run log, and regenerates views. Message parsing stays
in the importer layer no matter whether the source is `sources/instagram`, the
authenticated Drive cache, or a synced Drive folder.

Person views are portable context capsules. Generated files provide orientation
(`index.md`, `llm-context.md`, `timeline.md`, `threads.md`, `groups.md`,
`source-accounts.md`, `media.md`) while `transcripts/` contains symlinks to
canonical full thread transcripts. `notes.md` is created once and preserved as a
user-authored interpretation layer. This lets a project temporarily symlink a
person directory into its working tree without copying private source data or
breaking the canonical thread views.

## Filesystem View Contract

Generated view paths should be stable enough to symlink into other local
projects. Human-readable names are paired with a short hash suffix derived from
the source key:

```text
views/people/alice-example--3a1f0d22/
views/groups/residency-planning--a7c91f8e/
views/threads/instagram/alice-example--9bc4d1a0/
```

This keeps paths readable while avoiding collisions when two accounts, group
chats, or project labels share a display name.

## Body-Safe Source Scans

Early Instagram scanning detects transfer exports and `message_*.json` locations
without returning message body text. Parsing message contents belongs in the
importer layer after provenance, privacy boundaries, and canonical state are
settled.

The `scan` command remains body-safe. The `import` command is the explicit
privacy boundary where message bodies are read and written to private SQLite
state and generated private views.
