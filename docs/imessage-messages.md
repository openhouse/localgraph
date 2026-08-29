# Maintained Apple Messages ingestion

## Purpose

Maintain a private local Apple Messages representation no more than one hourly
check behind the live database while the Mac is awake. Localgraph never writes
to `~/Library/Messages/chat.db`.

“Current” means that a validated SQLite snapshot was imported successfully at
the reported `lastSuccessfulSyncAt`. It does not mean instantaneous capture,
that the Mac was awake, or that attachment files were copied into Localgraph.

## Custody and consistency

The live Messages database uses SQLite WAL mode. Copying only `chat.db` can omit
committed messages still present in `chat.db-wal`. Each refresh therefore:

1. opens the live database in read-only mode;
2. uses SQLite online backup to create one consistent private candidate;
3. validates required tables and runs `PRAGMA integrity_check`;
4. retains a hard-linked last-known-good snapshot and promotes the validated candidate;
5. replaces only the iMessage canonical projection in one transaction, restoring the prior snapshot and projection if import or rendering fails;
6. writes body-free health to `state/imessage-sync-status.json`.

A hard-linked last-known-good snapshot protects the previous bytes during the
replacement. Missing access, malformed schema, failed integrity, or an
unexpected empty candidate cannot erase a populated snapshot or projection.

## Install the hourly job

Use the internal Application Support workspace. A removable-volume worktree is
not a reliable launchd runtime under macOS privacy controls.

```bash
PYTHONPATH=src python3 -m localgraph \
  --root "$HOME/Library/Application Support/Localgraph/workspace" \
  install-imessage-sync --me "Jamie Burkart" --interval-minutes 60
```

Load and start the returned plist with its printed `bootstrapCommand` and
`kickstartCommand`. The job runs at login and once per hour, snapshots its code
under `~/Library/Application Support/Localgraph/runtime`, and shares the same
single-writer lock as Instagram and Facebook.

The Python executable used by the LaunchAgent must be allowed to read the live
Messages database. If macOS blocks it, grant the applicable executable Full
Disk Access and run the kickstart command again. Localgraph records the failure
without replacing last-known-good custody.

## Manual refresh and status

```bash
python -m localgraph \
  --root "$HOME/Library/Application Support/Localgraph/workspace" \
  imessage-sync --me "Jamie Burkart"

python -m localgraph \
  --root "$HOME/Library/Application Support/Localgraph/workspace" \
  imessage-status
```

`imessage-status` reports timestamps, counts, snapshot bytes, latest message
time, coverage, and the last error. It never returns message bodies or
participant lists.

## Health states

| State | Meaning |
|---|---|
| `current` | The latest check produced and imported a validated snapshot. |
| `degraded` | A previous good snapshot remains, but the latest check failed. |
| `blocked` | No good snapshot exists and live access or validation failed. |
| `skipped-concurrent` | Another Localgraph writer held the private workspace lock. |
| `not-checked` | The job has not produced a status receipt yet. |

`historyCoverage: complete-through-snapshot` binds only to the successful
snapshot time. The next detection bound is the hourly interval plus snapshot,
import, and render time.

## Privacy boundaries

- Raw snapshots, canonical SQLite state, rendered transcripts, and status files
  remain in ignored private workspace directories.
- The live Messages database is read only and never checkpointed or modified.
- Attachment metadata and original paths may be referenced; attachment bytes
  are not copied by this synchronization path.
- Failure status may name a local path or permission problem but contains no
  message bodies.
