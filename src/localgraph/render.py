from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from .paths import Workspace
from .slug import stable_view_name


def render_views(db: sqlite3.Connection, workspace: Workspace, *, source_scan: dict[str, object] | None = None) -> dict[str, int]:
    workspace.views_dir.mkdir(parents=True, exist_ok=True)
    for directory in workspace.view_directories:
        directory.mkdir(parents=True, exist_ok=True)
    _reset_generated_views(workspace)
    people = _rows(db, "SELECT id, stable_key, display_name FROM identities WHERE kind = 'person' ORDER BY display_name")
    groups = _rows(db, "SELECT id, stable_key, display_name FROM identities WHERE kind = 'group' ORDER BY display_name")
    threads = _rows(db, "SELECT id, source_kind, source_thread_key, title, thread_kind, first_message_at, last_message_at FROM threads ORDER BY source_kind, title")
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
    threads = _threads_for_identity(db, int(row["id"]))
    sent_count = int(db.execute("SELECT COUNT(*) AS count FROM messages WHERE sender_identity_id = ?", (row["id"],)).fetchone()["count"])
    (path / "index.md").write_text(
        f"# {row['display_name']}\n\n"
        f"- Kind: person\n"
        f"- Stable key: `{row['stable_key']}`\n"
        f"- Accounts: {len(accounts)}\n"
        f"- Threads: {len(threads)}\n"
        f"- Messages sent: {sent_count}\n"
        "\n"
        "## Accounts\n\n"
        + "".join(f"- {account['source_kind']}: `{account['account_key']}` ({account['display_name']})\n" for account in accounts)
        + "\n## Threads\n\n"
        + "".join(f"- {thread['source_kind']}: {thread['title']} (`{thread['source_thread_key']}`)\n" for thread in threads),
        encoding="utf-8",
    )


def _write_group_view(db: sqlite3.Connection, path: Path, row: sqlite3.Row) -> None:
    path.mkdir(parents=True, exist_ok=True)
    threads = _rows(
        db,
        """
        SELECT t.source_kind, t.source_thread_key, t.title, t.thread_kind
        FROM graph_edges e
        JOIN threads t ON e.to_kind = 'thread' AND e.to_key = t.source_kind || ':' || t.source_thread_key
        WHERE e.from_kind = 'identity'
          AND e.from_key = ?
          AND e.edge_kind = 'contains_thread'
        ORDER BY t.source_kind, t.title
        """,
        (row["stable_key"],),
    )
    members = _rows(
        db,
        """
        SELECT i.display_name, i.stable_key
        FROM graph_edges e
        JOIN identities i ON e.to_kind = 'identity' AND e.to_key = i.stable_key
        WHERE e.from_kind = 'identity'
          AND e.from_key = ?
          AND e.edge_kind = 'has_member'
        ORDER BY i.display_name
        """,
        (row["stable_key"],),
    )
    (path / "index.md").write_text(
        f"# {row['display_name']}\n\n"
        "- Kind: group\n"
        f"- Stable key: `{row['stable_key']}`\n"
        f"- Members: {len(members)}\n"
        f"- Threads: {len(threads)}\n"
        "\n## Members\n\n"
        + "".join(f"- {member['display_name']} (`{member['stable_key']}`)\n" for member in members)
        + "\n## Threads\n\n"
        + "".join(f"- {thread['source_kind']}: {thread['title']} (`{thread['source_thread_key']}`)\n" for thread in threads),
        encoding="utf-8",
    )


def _write_thread_view(db: sqlite3.Connection, path: Path, row: sqlite3.Row) -> None:
    path.mkdir(parents=True, exist_ok=True)
    participants = _rows(
        db,
        """
        SELECT DISTINCT i.display_name, i.stable_key
        FROM thread_participants tp
        LEFT JOIN identities i ON i.id = tp.identity_id
        WHERE tp.thread_id = ?
        ORDER BY i.display_name
        """,
        (row["id"],),
    )
    messages = _rows(
        db,
        """
        SELECT
          m.id,
          m.sent_at,
          m.body_text,
          m.body_format,
          i.display_name AS sender_name
        FROM messages m
        LEFT JOIN identities i ON i.id = m.sender_identity_id
        WHERE m.thread_id = ?
        ORDER BY m.sent_at, m.id
        """,
        (row["id"],),
    )
    (path / "index.md").write_text(
        f"# {row['title']}\n\n"
        f"- Source: {row['source_kind']}\n"
        f"- Thread kind: {row['thread_kind']}\n"
        f"- Source thread key: `{row['source_thread_key']}`\n",
        encoding="utf-8",
    )
    (path / "transcript.md").write_text(_render_transcript(row, participants, messages, db), encoding="utf-8")


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


def _rows(db: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    return list(db.execute(sql, params).fetchall())


def _threads_for_identity(db: sqlite3.Connection, identity_id: int) -> list[sqlite3.Row]:
    return _rows(
        db,
        """
        SELECT DISTINCT t.source_kind, t.source_thread_key, t.title, t.thread_kind
        FROM threads t
        JOIN thread_participants tp ON tp.thread_id = t.id
        LEFT JOIN accounts a ON a.id = tp.account_id
        WHERE tp.identity_id = ? OR a.identity_id = ?
        ORDER BY t.source_kind, t.title
        """,
        (identity_id, identity_id),
    )


def _render_transcript(row: sqlite3.Row, participants: list[sqlite3.Row], messages: list[sqlite3.Row], db: sqlite3.Connection) -> str:
    lines = [
        f"# {row['title']}",
        "",
        f"- Source: {row['source_kind']}",
        f"- Thread kind: {row['thread_kind']}",
        f"- Source thread key: `{row['source_thread_key']}`",
        f"- Messages: {len(messages)}",
        "",
        "## Participants",
        "",
    ]
    lines.extend(f"- {participant['display_name']} (`{participant['stable_key']}`)" for participant in participants if participant["display_name"])
    lines.extend(["", "## Transcript", ""])
    for message in messages:
        sender = message["sender_name"] or "Unknown"
        body = message["body_text"] or "<empty>"
        lines.append(f"- {message['sent_at']} - {sender}: {_single_line(body)}")
        media = _rows(db, "SELECT source_uri, local_path, mime_type FROM media_objects WHERE message_id = ? ORDER BY id", (message["id"],))
        for item in media:
            source = item["source_uri"] or item["local_path"] or "attachment"
            lines.append(f"  - Attachment: {source}")
    return "\n".join(lines) + "\n"


def _single_line(value: str) -> str:
    escaped = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return " ".join(escaped.splitlines())


def _reset_generated_views(workspace: Workspace) -> None:
    for directory in (workspace.views_dir / "people", workspace.views_dir / "groups", workspace.views_dir / "threads", workspace.views_dir / "_system"):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)
