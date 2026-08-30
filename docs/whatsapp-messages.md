# Maintained WhatsApp exports

## Operator how-to

Use this protocol for an explicitly approved chat in an authenticated native Mac
WhatsApp account. It does not enroll every chat, extract the app's private
database, change security settings, or send correspondence.

### 1. Bind the approved chat

Use a private workspace under the internal Application Support directory. The
examples below use a shell variable pointing to that already-created workspace;
account names, chat titles, and transcripts must never enter Git.

```sh
python -m localgraph --root "$LOCALGRAPH_ROOT" configure-whatsapp-chat \
  --account personal --chat planning --title 'Planning group' --kind group \
  --date-order mdy --timezone America/New_York
```

Choose the date order and timezone from the exporting device, not a parser guess.
Local keys are stable, independent of display names. Reconfiguring a title keeps
the original view label and path. Date interpretation cannot silently change.
Account ownership and each chat binding require operator verification: WhatsApp
TXT archives do not supply authoritative account or conversation identifiers.

### 2. Acquire a native export

Use the Computer Use skill in a Codex task. Inspect the active account's profile
first and compare it with the private task's expected account. Stop on mismatch.
Locate only an approved chat, open its context menu, and select **Export chat**.
If offered, select **Attach media**. Some chats export directly without a media
choice. Observe completion and independently verify the new ZIP in Downloads.
Never substitute a previous ZIP when export creation fails.

Record the exact file and observation time:

```sh
python -m localgraph --root "$LOCALGRAPH_ROOT" whatsapp-deliver \
  --account personal --chat planning --observed-title 'Planning group' \
  --archive '/absolute/path/to/new-export.zip' \
  --exported-at '2026-08-30T15:00:00Z' --origin mac-native --media-requested
```

Omit `--media-requested` when no media option was offered. A phone export uses
`--origin phone-export`. Explicitly reviewed older local archives use
`--origin historical-local`; their supplied file/observation timestamps do not
establish a successful current native acquisition.

Delivery validates the archive, transcript, timestamp convention, and chat-title
binding before creating private checksum-addressed custody and an acquisition
receipt. Repeated acquisition of identical bytes creates a new receipt, not a
second canonical message set. Retain the original download until custody is
verified; this workflow never deletes it automatically.

### 3. Import and inspect

```sh
python -m localgraph --root "$LOCALGRAPH_ROOT" whatsapp-sync
python -m localgraph --root "$LOCALGRAPH_ROOT" status
```

Each accepted chat produces an `index.md`, `messages.md`, `coverage.json`, and
`media-manifest.json`. The sync result supplies its exact `viewPath`. These live
under `views/threads/whatsapp/` and are also compatible with the normal global
Localgraph renderer. Archive copies live under
`sources/whatsapp/<account>/<chat>/archives/`; acquisition receipts are siblings
under `receipts/`. Referenced and unreferenced exported media bytes are retained
under checksum-addressed `objects/whatsapp/` paths.

### 4. Schedule both halves

Use a daily Codex thread heartbeat for the native acquisition procedure. Its
private prompt must enumerate approved account/chat bindings, require fresh UI
inspection, verify the newly created file, record delivery, and run sync/status.
Do not use cached accessibility element numbers. Do not install UI-control
scripts, read the application's database, or broaden chat scope on failure.

Install the separate hourly deterministic importer:

```sh
python -m localgraph --root "$LOCALGRAPH_ROOT" install-whatsapp-sync
```

Load the returned plist with `launchctl bootstrap gui/<uid> <plist>`. It runs at
login and hourly. The installed runtime lives on the internal disk and uses the
same workspace writer lock as the other connectors. A failed import returns a
nonzero CLI exit code while independent healthy chats can still advance.

The LaunchAgent does **not** create exports. Native acquisition requires a usable
logged-in desktop and an available Codex execution session. A configured
heartbeat is not proof that its next daily run succeeded. Inspect the receipts.

### 5. Record and recover failures

```sh
python -m localgraph --root "$LOCALGRAPH_ROOT" whatsapp-acquisition-failed \
  --account personal --chat planning --reason app-disconnected
```

Allowed reasons are `session-unavailable`, `app-disconnected`,
`export-control-changed`, `export-failed`, and `identity-unverified`. Never put
private correspondence or raw application errors in this field. A later import
of old files cannot clear an acquisition failure; a newer successful native
export is required. Reauthentication or new permissions require the applicable
human approval, not a workaround.

### 6. Verify the candidate

Run `make hill-climb`. The WhatsApp eval suite uses synthetic ZIPs and real local
imports, filesystem rendering, status inspection, and an executed installed
watcher. Live acceptance separately requires two native exports of each approved
chat, unchanged canonical counts for unchanged source content, and verified
checksums and paths. Keep live chat names, contents, provider identities and
private receipts out of pull requests.

## Architecture and coverage reference

The pipeline is **native acquisition → verified immutable delivery → cumulative
canonical import → staged transcript publication → health evidence**.

Each chat advances independently. Importers never clear old messages simply
because a new linked-device export is shorter. Invalid/empty input, tampered
custody, and render failures preserve the previous canonical projection and
transcript. Rendering is prepared before publication and rolls back the chat
transaction on handled publication failures. This is not a cross-filesystem,
power-loss-atomic database/filesystem transaction; recovery requires a subsequent
successful sync, and status detects missing or mismatching projections.

The source report separates native acquisition time, import time, message date
range, archive count, canonical count, render checksum, and media gaps. Two
missed daily acquisition intervals produce `stale-acquisition`; two missed hourly
imports produce `stale-sync`. Delivery can be evidenced before any import.
`requested` and `preparing` remain `not-recorded` when no provider-stage receipt
exists. Status is body-free; hashes and counts are not statements of completeness.

### Deliberate limits

- Linked-device history may be shorter than the primary phone's history. Use
  phone exports to reconcile known gaps. `historyCoverage` remains
  `available-export-history-unverified`; this connector never upgrades a desktop
  export to all-time completeness automatically.
- Content plus occurrence ordinal within an export substitutes for unavailable
  provider message IDs. It preserves repeated identical records and deduplicates
  identical overlaps. Partial overlap containing indistinguishable same-time
  messages cannot be resolved perfectly. Changed sender labels, edits, and
  media-placeholder changes can remain separate preserved variants.
- Missing messages in a later export are not deletion evidence. Deleted,
  disappearing, view-once, reaction, and revision history is only as complete as
  the supplied export. Nothing is reconstructed or invented.
- Sender labels are scoped to the account **and chat**, not merged with people
  from Instagram or another WhatsApp conversation merely by name. TXT sender
  labels and colon-delimited system messages are not authoritative identity data.
- Supported date headers are bracketed Apple-style and unbracketed Android-style
  slash dates with explicit `mdy`/`dmy` and 12/24-hour time. Ambiguous/nonexistent
  DST instants and unsupported timestamp-looking records fail closed. Correcting
  an exporting timezone requires an explicit migration.
- Attachments present in the ZIP are preserved. Missing attachment references and
  English omitted-media markers are reported. Other languages or silent provider
  omissions can escape these checks, so zero detected gaps is not proof of full
  media coverage.
- Private local files are plaintext outside WhatsApp's encryption boundary.
  Archive files and generated transcripts are owner-only; disk encryption and
  private backups remain the operator's responsibility.
