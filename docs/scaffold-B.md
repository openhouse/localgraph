# Scaffold B Plan

This branch turns the initial public notes into a runnable scaffold while keeping
private data out of git.

## Scope

- Add a dependency-free Node.js CLI.
- Create private local workspace directories with `localgraph init`.
- Check local workspace state with `localgraph doctor`.
- Scan local Instagram transfer exports without reading message bodies.
- Render rebuildable view directories and a source manifest.
- Add tests using Node's built-in test runner.

## Non-goals

- No message parsing yet.
- No SQLite schema yet.
- No Google Drive API downloader yet.
- No identity resolution yet.
- No committed private exports, annotations, media, generated views, or indexes.

## Next Work

- Add an append-only import ledger.
- Add a SQLite state schema.
- Parse Instagram conversations into canonical thread/message/media rows.
- Add Google Drive source discovery for Meta transfer folders.
- Generate person and group views from canonical state.
