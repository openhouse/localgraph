# Composite Scaffold Plan

This branch turns the initial public notes into a runnable composite scaffold
while keeping private data out of git.

## Scope

- Add a dependency-free Node.js CLI.
- Print a workspace plan with private directories and generated views.
- Create private local workspace directories with `localgraph init`.
- Write a canonical SQLite-oriented schema artifact to `state/schema.sql`.
- Check local workspace state with `localgraph doctor`.
- Scan local Instagram transfer exports without reading message bodies.
- Render rebuildable view directories and a source manifest.
- Generate deterministic symlink-friendly view paths.
- Add tests using Node's built-in test runner.

## Non-goals

- No message parsing yet.
- No SQLite runtime or migrations yet.
- No Google Drive API downloader yet.
- No identity resolution yet.
- No committed private exports, annotations, media, generated views, or indexes.

## Next Work

- Add an append-only import ledger.
- Add a SQLite state schema.
- Parse Instagram conversations into canonical thread/message/media rows.
- Add Google Drive source discovery for Meta transfer folders.
- Generate person and group views from canonical state.
