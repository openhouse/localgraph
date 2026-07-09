# Architecture

Localgraph separates evidence from projections.

## Layers

1. Sources

   Immutable raw exports from providers such as Instagram, iMessage, email,
   Slack, Google Drive, and other local archives.

2. State

   Canonical local database state: imports, identities, accounts, threads,
   messages, media, reactions, annotations, tags, provenance, source locations,
   import runs, pending imports, and graph edges.

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

## First Importer

The first importer targets are Instagram transfer data and macOS Messages
`chat.db` files.

Instagram transfer data often arrives in Google Drive under Meta export folders.
The source acquisition layer should support both:

- Google Drive API discovery and download.
- Local Drive Desktop folders when they are actually materialized on disk.

The API path is preferred for freshness because local Drive sync may lag or omit
new transfer folders.

The current local importer consumes materialized Instagram `message_*.json`
files and copied or directly referenced iMessage `chat.db` files. It writes
people, accounts, direct/group threads, messages, media references, participant
edges, and group membership edges into the canonical SQLite state.

Daily Instagram import uses the configured local Google Drive Desktop path. The
first successful daily run imports all materialized exports as a bootstrap. Later
runs import the newest materialized export unless `--all-instagram-exports` is
provided. Cloud-only or missing Drive materialization is represented as
`pending_imports` rows rather than a hanging filesystem read.

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

Person directories are rendered as context capsules. Generated files include an
overview, `llm-context.md`, recent timeline, thread/group/media/account tables,
JSON manifests, and symlinked transcript evidence. `notes.md` is preserved if it
already exists, so human-authored context survives repeated renders.

## Body-Safe Source Scans

Early Instagram scanning detects transfer exports and `message_*.json` locations
without returning message body text. Parsing message contents belongs in the
importer layer after provenance, privacy boundaries, and canonical state are
settled.

`localgraph scan` remains body-safe. `localgraph import` is the private ingest
operation that reads message bodies into ignored local SQLite state.
