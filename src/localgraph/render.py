from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .paths import Workspace
from .slug import stable_view_name


def render_views(db: sqlite3.Connection, workspace: Workspace, *, source_scan: dict[str, object] | None = None) -> dict[str, int]:
    workspace.views_dir.mkdir(parents=True, exist_ok=True)
    for directory in workspace.view_directories:
        directory.mkdir(parents=True, exist_ok=True)
    people = _rows(db, "SELECT id, stable_key, display_name FROM identities WHERE kind = 'person' ORDER BY display_name")
    groups = _rows(db, "SELECT id, stable_key, display_name FROM identities WHERE kind = 'group' ORDER BY display_name")
    threads = _rows(
        db,
        """
        SELECT id, source_kind, source_thread_key, title, thread_kind, first_message_at, last_message_at
        FROM threads
        ORDER BY source_kind, title
        """,
    )
    message_count = int(db.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"])

    _write_index(workspace.views_dir / "index.md", people=people, groups=groups, threads=threads)
    for row in people:
        _write_person_view(db, workspace.views_dir / "people" / stable_view_name(row["display_name"], row["stable_key"]), row)
    for row in groups:
        _write_group_view(db, workspace.views_dir / "groups" / stable_view_name(row["display_name"], row["stable_key"]), row)
    for row in threads:
        _write_thread_view(db, workspace.views_dir / "threads" / row["source_kind"] / stable_view_name(row["title"], row["source_thread_key"]), row)
    _write_system_manifest(workspace, people=people, groups=groups, threads=threads, message_count=message_count, source_scan=source_scan)

    result = {"people": len(people), "groups": len(groups), "threads": len(threads), "messages": message_count}
    if source_scan is not None:
        result["sourceExports"] = len(source_scan["exports"])  # type: ignore[arg-type]
        result["sourceMessageFiles"] = int(source_scan["totalMessageFiles"])
    return result


def _write_index(path: Path, *, people: list[sqlite3.Row], groups: list[sqlite3.Row], threads: list[sqlite3.Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Localgraph Views\n\n"
        f"- People: {len(people)}\n"
        f"- Groups: {len(groups)}\n"
        f"- Threads: {len(threads)}\n",
        encoding="utf-8",
    )


def _write_person_view(db: sqlite3.Connection, path: Path, row: sqlite3.Row) -> None:
    path.mkdir(parents=True, exist_ok=True)
    accounts = _rows(
        db,
        """
        SELECT source_kind, account_key, display_name
        FROM accounts
        WHERE identity_id = ?
        ORDER BY source_kind, display_name
        """,
        (row["id"],),
    )
    threads = _identity_threads(db, row["id"])
    sent_count = int(db.execute("SELECT COUNT(*) AS count FROM messages WHERE sender_identity_id = ?", (row["id"],)).fetchone()["count"])
    (path / "index.md").write_text(
        f"# {row['display_name']}\n\n"
        f"- Kind: person\n"
        f"- Stable key: `{row['stable_key']}`\n",
        encoding="utf-8",
    )
    with (path / "index.md").open("a", encoding="utf-8") as handle:
        handle.write(f"- Messages sent: {sent_count}\n")
        if accounts:
            handle.write("\n## Accounts\n\n")
            for account in accounts:
                handle.write(f"- {account['source_kind']}: `{account['account_key']}` ({account['display_name']})\n")
        if threads:
            handle.write("\n## Threads\n\n")
            for thread in threads:
                handle.write(f"- {thread['title']} ({thread['source_kind']}, {thread['thread_kind']})\n")


def _write_group_view(db: sqlite3.Connection, path: Path, row: sqlite3.Row) -> None:
    path.mkdir(parents=True, exist_ok=True)
    members = _rows(
        db,
        """
        SELECT target.display_name, target.stable_key
        FROM graph_edges edge
        JOIN identities target ON target.stable_key = edge.to_key
        WHERE edge.from_kind = 'identity'
          AND edge.from_key = ?
          AND edge.edge_kind = 'member'
        ORDER BY target.display_name
        """,
        (row["stable_key"],),
    )
    represented_threads = _rows(
        db,
        """
        SELECT threads.source_kind, threads.source_thread_key, threads.title, threads.thread_kind
        FROM graph_edges edge
        JOIN threads ON edge.from_key = threads.source_kind || ':' || threads.source_thread_key
        WHERE edge.to_kind = 'identity'
          AND edge.to_key = ?
          AND edge.edge_kind = 'represents'
        ORDER BY threads.source_kind, threads.title
        """,
        (row["stable_key"],),
    )
    (path / "index.md").write_text(
        f"# {row['display_name']}\n\n"
        "- Kind: group\n"
        f"- Stable key: `{row['stable_key']}`\n",
        encoding="utf-8",
    )
    with (path / "index.md").open("a", encoding="utf-8") as handle:
        if members:
            handle.write("\n## Members\n\n")
            for member in members:
                handle.write(f"- {member['display_name']} (`{member['stable_key']}`)\n")
        if represented_threads:
            handle.write("\n## Threads\n\n")
            for thread in represented_threads:
                handle.write(f"- {thread['title']} ({thread['source_kind']}, {thread['thread_kind']})\n")


def _write_thread_view(db: sqlite3.Connection, path: Path, row: sqlite3.Row) -> None:
    path.mkdir(parents=True, exist_ok=True)
    participants = _rows(
        db,
        """
        SELECT COALESCE(identities.display_name, accounts.display_name, 'Unknown') AS display_name,
               COALESCE(identities.stable_key, accounts.source_kind || ':' || accounts.account_key) AS stable_key,
               thread_participants.role
        FROM thread_participants
        LEFT JOIN identities ON identities.id = thread_participants.identity_id
        LEFT JOIN accounts ON accounts.id = thread_participants.account_id
        WHERE thread_participants.thread_id = ?
        ORDER BY thread_participants.role, display_name
        """,
        (row["id"],),
    )
    messages = _thread_messages(db, row["id"])
    (path / "index.md").write_text(
        f"# {row['title']}\n\n"
        f"- Source: {row['source_kind']}\n"
        f"- Thread kind: {row['thread_kind']}\n"
        f"- Source thread key: `{row['source_thread_key']}`\n",
        encoding="utf-8",
    )
    with (path / "index.md").open("a", encoding="utf-8") as handle:
        handle.write(f"- Messages: {len(messages)}\n")
        if row["first_message_at"]:
            handle.write(f"- First message: {row['first_message_at']}\n")
        if row["last_message_at"]:
            handle.write(f"- Last message: {row['last_message_at']}\n")
        if participants:
            handle.write("\n## Participants\n\n")
            for participant in participants:
                role = f", {participant['role']}" if participant["role"] != "participant" else ""
                handle.write(f"- {participant['display_name']} (`{participant['stable_key']}`{role})\n")
        if messages:
            handle.write("\n[Messages](messages.md)\n")
    _write_messages(path / "messages.md", row, messages)


def _write_system_manifest(
    workspace: Workspace,
    *,
    people: list[sqlite3.Row],
    groups: list[sqlite3.Row],
    threads: list[sqlite3.Row],
    message_count: int,
    source_scan: dict[str, object] | None,
) -> None:
    system_dir = workspace.views_dir / "_system"
    system_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "app": "localgraph",
        "formatVersion": 1,
        "workspaceRoot": str(workspace.root),
        "counts": {
            "people": len(people),
            "groups": len(groups),
            "threads": len(threads),
            "messages": message_count,
        },
        "source": source_scan,
    }
    (system_dir / "README.md").write_text(
        "# Localgraph System Views\n\nGenerated manifests and diagnostics live here.\n",
        encoding="utf-8",
    )
    (system_dir / "source-manifest.json").write_text(f"{json.dumps(manifest, indent=2, sort_keys=True)}\n", encoding="utf-8")


def _identity_threads(db: sqlite3.Connection, identity_id: int) -> list[sqlite3.Row]:
    return _rows(
        db,
        """
        SELECT DISTINCT threads.source_kind, threads.source_thread_key, threads.title, threads.thread_kind
        FROM thread_participants
        JOIN threads ON threads.id = thread_participants.thread_id
        WHERE thread_participants.identity_id = ?
        ORDER BY threads.source_kind, threads.title
        """,
        (identity_id,),
    )


def _thread_messages(db: sqlite3.Connection, thread_id: int) -> list[sqlite3.Row]:
    return _rows(
        db,
        """
        SELECT messages.sent_at,
               messages.body_text,
               messages.source_message_key,
               COALESCE(identities.display_name, accounts.display_name, 'Unknown') AS sender_name,
               (SELECT COUNT(*) FROM media_objects WHERE media_objects.message_id = messages.id) AS media_count
        FROM messages
        LEFT JOIN identities ON identities.id = messages.sender_identity_id
        LEFT JOIN accounts ON accounts.id = messages.sender_account_id
        WHERE messages.thread_id = ?
        ORDER BY messages.sent_at, messages.id
        """,
        (thread_id,),
    )


def _write_messages(path: Path, thread: sqlite3.Row, messages: list[sqlite3.Row]) -> None:
    lines = [
        f"# {thread['title']} Messages",
        "",
        f"- Source: {thread['source_kind']}",
        f"- Source thread key: `{thread['source_thread_key']}`",
        "",
    ]
    for message in messages:
        body = message["body_text"] or ""
        if message["media_count"] and not body:
            body = "[Attachment]"
        elif message["media_count"]:
            body = f"{body}\n[Attachments: {message['media_count']}]"
        lines.append(f"## {message['sent_at']} - {message['sender_name']}")
        lines.append("")
        lines.append(body if body else "[No text]")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _rows(db: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    return list(db.execute(sql, params).fetchall())
