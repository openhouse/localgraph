# Scaffold Plan

This branch establishes the first runnable project shape for Localgraph.

## Goals

- Create a dependency-light Node CLI that can run without installing third-party
  packages.
- Make private source data, state, generated views, and annotations ignored by
  git from the start.
- Provide deterministic filesystem view naming for people, groups, threads,
  projects, and tags.
- Add tests for layout creation, view path generation, and CLI behavior.

## Non-goals

- Importing Instagram or iMessage data.
- Writing SQLite migrations.
- Rendering real conversation transcripts.
- Syncing Google Drive transfer folders.

Those belong in follow-up branches after the local root and public repo contract
are stable.

## First Runnable Surface

```bash
npm test
node src/cli.js plan
node src/cli.js init ~/Localgraph
node src/cli.js view-name person "Alice Example" "instagram:alice"
```

The generated Localgraph root is deliberately private and ignored. Other
projects should symlink into `views/` paths, not into raw `sources/`.
