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

## Importers

The first working importers target Instagram transfer data and macOS Messages
SQLite databases.

Instagram transfer data may arrive in Google Drive under Meta export folders.
The importer supports local materialized folders and preserves the raw JSON path
as provenance. A future Google Drive API source should support both:

- Google Drive API discovery and download.
- Local Drive Desktop folders when they are actually materialized on disk.

The API path is preferred for freshness because local Drive sync may lag or omit
new transfer folders.

iMessage import reads a `chat.db` in read-only mode. Live
`~/Library/Messages/chat.db` access depends on macOS privacy permissions; copied
safety databases can be opened with `--immutable`.

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

Instagram scanning detects transfer exports and `message_*.json` locations
without returning message body text. Message contents are only read by explicit
`import` commands, and imported private state remains in ignored local
directories.
