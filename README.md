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
  docs/         # public architecture notes
```

The public repository contains code and design notes only. Personal messages,
media, exports, indexes, and annotations belong on local disk.

## Scaffold B CLI

This branch includes a dependency-free Node.js CLI. It uses only Node built-ins
and the built-in test runner.

```bash
npm test
node src/cli.js init
node src/cli.js doctor
node src/cli.js scan --json
node src/cli.js render --json
```

`init` creates private local directories and `localgraph.config.json`. Those
paths are ignored by git. `scan` detects Instagram transfer exports from
`sources/instagram` without reading message bodies. `render` writes a
symlink-friendly `views/` skeleton and `_system/source-manifest.json`.
