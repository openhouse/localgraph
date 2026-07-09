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

## Quick Start

This scaffold uses Node.js 24 built-ins only; no third-party install is
required for the first tests and CLI.

```bash
npm test
node src/cli.js plan
node src/cli.js init ~/Localgraph
node src/cli.js view-name person "Alice Example" "instagram:alice"
```

## Current Surface

- `localgraph plan` prints the private root and view layout.
- `localgraph init` creates a local private root with ignored source, state,
  object, view, annotation, and export directories, then initializes the first
  canonical SQLite schema.
- `localgraph doctor` checks private directories and schema readiness.
- `localgraph scan` detects local Instagram transfer exports without reading
  message bodies.
- `localgraph render` writes symlink-friendly view directories and a source
  manifest.
- `localgraph view-name` returns deterministic symlink-friendly view paths.

See [docs/scaffold-plan.md](docs/scaffold-plan.md) for this branch's scope.
