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

5. Annotations

   Human-authored notes, aliases, links, tags, and project context. These should
   be stored separately from generated transcripts so render jobs can be
   rerun without destroying interpretation.

## First Importer

The first importer target is Instagram transfer data arriving in Google Drive
under Meta export folders. The importer should support both:

- Google Drive API discovery and download.
- Local Drive Desktop folders when they are actually materialized on disk.

The API path is preferred for freshness because local Drive sync may lag or omit
new transfer folders.

## Filesystem View Contract

Generated view paths should be stable enough to symlink into other local
projects. Human-readable names should be paired with a short hash suffix derived
from a source key, for example:

```text
views/people/alice-example--3a1f0d22/
views/groups/residency-planning--a7c91f8e/
```

This keeps paths readable while avoiding collisions when two accounts or group
chats share a display name.
