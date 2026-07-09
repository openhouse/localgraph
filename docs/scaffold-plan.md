# Scaffold Plan

This branch establishes the first runnable project shape for Localgraph.

## Goals

- Create a dependency-light Node 24 CLI that can run without installing
  third-party packages.
- Make private source data, state, generated views, and annotations ignored by
  git from the start.
- Provide deterministic filesystem view naming for people, groups, threads,
  projects, and tags.
- Add an initial canonical SQLite schema for imports, identities, accounts,
  threads, messages, media objects, annotations, and graph edges.
- Add body-safe Instagram transfer scanning and generated source manifests.
- Add tests for layout creation, schema creation, source scanning, rendering,
  view path generation, and CLI behavior.

## Non-goals

- Parsing Instagram or iMessage message bodies into canonical rows.
- Writing multi-version SQLite migrations.
- Rendering real conversation transcripts.
- Syncing Google Drive transfer folders.

Those belong in follow-up branches after the local root and public repo contract
are stable.

## First Runnable Surface

```bash
npm test
node src/cli.js plan
node src/cli.js init ~/Localgraph
node src/cli.js scan ~/Localgraph
node src/cli.js render ~/Localgraph
node src/cli.js view-name person "Alice Example" "instagram:alice"
```

The generated Localgraph root is deliberately private and ignored. Other
projects should symlink into `views/` paths, not into raw `sources/`.
