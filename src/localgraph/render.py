from __future__ import annotations

import json
import os
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
    groups = _identity_groups(db, row["stable_key"])
    timeline = _identity_timeline(db, row["id"], limit=50)
    media = _identity_media(db, row["id"])
    sent_count = int(db.execute("SELECT COUNT(*) AS count FROM messages WHERE sender_identity_id = ?", (row["id"],)).fetchone()["count"])
    direct_threads = [thread for thread in threads if thread["thread_kind"] == "direct"]
    group_threads = [thread for thread in threads if thread["thread_kind"] == "group"]

    _write_person_index(path, row, sent_count, accounts, threads, groups)
    _write_person_llm_context(path, row, accounts, threads, groups, timeline)
    _write_person_timeline(path, row, timeline)
    _write_person_threads(path, row, threads)
    _write_person_groups(path, row, groups)
    _write_person_media(path, row, media)
    _write_person_accounts(path, row, accounts)
    _ensure_notes(path / "notes.md", row)
    _write_person_transcript_links(path, direct_threads, group_threads)
    _write_person_manifests(path, row, accounts, threads, groups, media)


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
        SELECT DISTINCT threads.id,
               threads.source_kind,
               threads.source_thread_key,
               threads.title,
               threads.thread_kind,
               threads.first_message_at,
               threads.last_message_at,
               (SELECT COUNT(*) FROM messages WHERE messages.thread_id = threads.id) AS message_count
        FROM thread_participants
        JOIN threads ON threads.id = thread_participants.thread_id
        WHERE thread_participants.identity_id = ?
        ORDER BY COALESCE(threads.last_message_at, '' ) DESC, threads.source_kind, threads.title
        """,
        (identity_id,),
    )


def _identity_groups(db: sqlite3.Connection, stable_key: str) -> list[sqlite3.Row]:
    return _rows(
        db,
        """
        SELECT groups.stable_key, groups.display_name
        FROM graph_edges edge
        JOIN identities groups ON groups.stable_key = edge.from_key
        WHERE edge.edge_kind = 'member'
          AND edge.to_kind = 'identity'
          AND edge.to_key = ?
          AND groups.kind = 'group'
        ORDER BY groups.display_name
        """,
        (stable_key,),
    )


def _identity_timeline(db: sqlite3.Connection, identity_id: int, *, limit: int) -> list[sqlite3.Row]:
    return _rows(
        db,
        """
        SELECT messages.sent_at,
               messages.body_text,
               COALESCE(sender.display_name, sender_account.display_name, 'Unknown') AS sender_name,
               threads.source_kind,
               threads.source_thread_key,
               threads.title,
               threads.thread_kind
        FROM thread_participants participant
        JOIN threads ON threads.id = participant.thread_id
        JOIN messages ON messages.thread_id = threads.id
        LEFT JOIN identities sender ON sender.id = messages.sender_identity_id
        LEFT JOIN accounts sender_account ON sender_account.id = messages.sender_account_id
        WHERE participant.identity_id = ?
        ORDER BY messages.sent_at DESC, messages.id DESC
        LIMIT ?
        """,
        (identity_id, limit),
    )


def _identity_media(db: sqlite3.Connection, identity_id: int) -> list[sqlite3.Row]:
    return _rows(
        db,
        """
        SELECT media_objects.object_key,
               media_objects.source_uri,
               media_objects.local_path,
               media_objects.mime_type,
               messages.sent_at,
               threads.source_kind,
               threads.source_thread_key,
               threads.title
        FROM thread_participants participant
        JOIN threads ON threads.id = participant.thread_id
        JOIN messages ON messages.thread_id = threads.id
        JOIN media_objects ON media_objects.message_id = messages.id
        WHERE participant.identity_id = ?
        ORDER BY messages.sent_at DESC, media_objects.object_key
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


def _write_person_index(
    path: Path,
    row: sqlite3.Row,
    sent_count: int,
    accounts: list[sqlite3.Row],
    threads: list[sqlite3.Row],
    groups: list[sqlite3.Row],
) -> None:
    lines = [
        f"# {row['display_name']}",
        "",
        "- Kind: person",
        f"- Stable key: `{row['stable_key']}`",
        f"- Source accounts: {len(accounts)}",
        f"- Threads: {len(threads)}",
        f"- Shared groups: {len(groups)}",
        f"- Messages sent: {sent_count}",
        "",
        "## Navigation",
        "",
        "- [LLM context](llm-context.md)",
        "- [Timeline](timeline.md)",
        "- [Threads](threads.md)",
        "- [Groups](groups.md)",
        "- [Media](media.md)",
        "- [Source accounts](source-accounts.md)",
        "- [Notes](notes.md)",
        "- [Transcript links](transcripts/)",
        "- [Manifests](manifests/)",
        "",
    ]
    (path / "index.md").write_text("\n".join(lines), encoding="utf-8")


def _write_person_llm_context(
    path: Path,
    row: sqlite3.Row,
    accounts: list[sqlite3.Row],
    threads: list[sqlite3.Row],
    groups: list[sqlite3.Row],
    timeline: list[sqlite3.Row],
) -> None:
    lines = [
        f"# {row['display_name']} LLM Context",
        "",
        "Read this file first when this person directory is symlinked into a project workspace.",
        "",
        "## Orientation",
        "",
        f"- Stable identity: `{row['stable_key']}`",
        f"- Source accounts: {len(accounts)}",
        f"- Conversation threads: {len(threads)}",
        f"- Shared group contexts: {len(groups)}",
        "",
        "## Evidence",
        "",
        "Full transcript evidence is linked under `transcripts/direct/` and `transcripts/groups/`.",
        "Human-authored interpretation belongs in `notes.md`; renders preserve that file.",
        "",
    ]
    if timeline:
        lines.extend(["## Recent Context", ""])
        for item in timeline[:10]:
            body = _one_line(item["body_text"] or "[No text]")
            lines.append(f"- {item['sent_at']} | {item['title']} | {item['sender_name']}: {body}")
        lines.append("")
    (path / "llm-context.md").write_text("\n".join(lines), encoding="utf-8")


def _write_person_timeline(path: Path, row: sqlite3.Row, timeline: list[sqlite3.Row]) -> None:
    lines = [f"# {row['display_name']} Timeline", ""]
    if not timeline:
        lines.append("No messages found in threads involving this person.")
    for item in timeline:
        body = _one_line(item["body_text"] or "[No text]")
        lines.append(f"- {item['sent_at']} | {item['source_kind']} | {item['title']} | {item['sender_name']}: {body}")
    lines.append("")
    (path / "timeline.md").write_text("\n".join(lines), encoding="utf-8")


def _write_person_threads(path: Path, row: sqlite3.Row, threads: list[sqlite3.Row]) -> None:
    lines = [
        f"# {row['display_name']} Threads",
        "",
        "| Thread | Source | Kind | Messages | Last message | Transcript |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    if not threads:
        lines.append("| None |  |  | 0 |  |  |")
    for thread in threads:
        transcript = _thread_messages_link(thread)
        lines.append(
            f"| {thread['title']} | {thread['source_kind']} | {thread['thread_kind']} | "
            f"{thread['message_count']} | {thread['last_message_at'] or ''} | {transcript} |"
        )
    lines.append("")
    (path / "threads.md").write_text("\n".join(lines), encoding="utf-8")


def _write_person_groups(path: Path, row: sqlite3.Row, groups: list[sqlite3.Row]) -> None:
    lines = [
        f"# {row['display_name']} Groups",
        "",
        "| Group | Stable key |",
        "| --- | --- |",
    ]
    if not groups:
        lines.append("| None |  |")
    for group in groups:
        lines.append(f"| {group['display_name']} | `{group['stable_key']}` |")
    lines.append("")
    (path / "groups.md").write_text("\n".join(lines), encoding="utf-8")


def _write_person_media(path: Path, row: sqlite3.Row, media: list[sqlite3.Row]) -> None:
    lines = [
        f"# {row['display_name']} Media",
        "",
        "| When | Thread | Type | Path | Object key |",
        "| --- | --- | --- | --- | --- |",
    ]
    if not media:
        lines.append("| None |  |  |  |  |")
    for item in media:
        media_path = item["local_path"] or item["source_uri"] or ""
        lines.append(
            f"| {item['sent_at']} | {item['title']} | {item['mime_type'] or ''} | "
            f"`{media_path}` | `{item['object_key']}` |"
        )
    lines.append("")
    (path / "media.md").write_text("\n".join(lines), encoding="utf-8")


def _write_person_accounts(path: Path, row: sqlite3.Row, accounts: list[sqlite3.Row]) -> None:
    lines = [
        f"# {row['display_name']} Source Accounts",
        "",
        "| Source | Account key | Display name |",
        "| --- | --- | --- |",
    ]
    if not accounts:
        lines.append("| None |  |  |")
    for account in accounts:
        lines.append(f"| {account['source_kind']} | `{account['account_key']}` | {account['display_name']} |")
    lines.append("")
    (path / "source-accounts.md").write_text("\n".join(lines), encoding="utf-8")


def _ensure_notes(path: Path, row: sqlite3.Row) -> None:
    if path.exists():
        return
    path.write_text(
        f"# Notes for {row['display_name']}\n\n"
        "This file is user-authored and preserved across renders.\n",
        encoding="utf-8",
    )


def _write_person_transcript_links(
    path: Path,
    direct_threads: list[sqlite3.Row],
    group_threads: list[sqlite3.Row],
) -> None:
    direct_dir = path / "transcripts" / "direct"
    group_dir = path / "transcripts" / "groups"
    direct_dir.mkdir(parents=True, exist_ok=True)
    group_dir.mkdir(parents=True, exist_ok=True)
    for thread in direct_threads:
        _link_thread_transcript(direct_dir, thread)
    for thread in group_threads:
        _link_thread_transcript(group_dir, thread)


def _link_thread_transcript(directory: Path, thread: sqlite3.Row) -> None:
    target = _thread_messages_path(directory.parents[3], thread)
    link = directory / f"{stable_view_name(thread['title'], thread['source_thread_key'])}.md"
    _relative_symlink(target, link)


def _write_person_manifests(
    path: Path,
    row: sqlite3.Row,
    accounts: list[sqlite3.Row],
    threads: list[sqlite3.Row],
    groups: list[sqlite3.Row],
    media: list[sqlite3.Row],
) -> None:
    manifests = path / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    person = {
        "stableKey": row["stable_key"],
        "displayName": row["display_name"],
        "threadCount": len(threads),
        "groupCount": len(groups),
        "mediaCount": len(media),
    }
    accounts_payload = [
        {"sourceKind": account["source_kind"], "accountKey": account["account_key"], "displayName": account["display_name"]}
        for account in accounts
    ]
    transcripts_payload = [
        {
            "sourceKind": thread["source_kind"],
            "sourceThreadKey": thread["source_thread_key"],
            "title": thread["title"],
            "threadKind": thread["thread_kind"],
            "messagesPath": str(_thread_messages_path(path.parents[1], thread)),
        }
        for thread in threads
    ]
    (manifests / "person.json").write_text(f"{json.dumps(person, indent=2, sort_keys=True)}\n", encoding="utf-8")
    (manifests / "accounts.json").write_text(f"{json.dumps(accounts_payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    (manifests / "transcripts.json").write_text(f"{json.dumps(transcripts_payload, indent=2, sort_keys=True)}\n", encoding="utf-8")


def _thread_messages_path(views_dir: Path, thread: sqlite3.Row) -> Path:
    return views_dir / "threads" / thread["source_kind"] / stable_view_name(thread["title"], thread["source_thread_key"]) / "messages.md"


def _thread_messages_link(thread: sqlite3.Row) -> str:
    name = stable_view_name(thread["title"], thread["source_thread_key"])
    transcript_kind = "groups" if thread["thread_kind"] == "group" else "direct"
    return f"`transcripts/{transcript_kind}/{name}.md`"


def _relative_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        if link.is_symlink():
            link.unlink()
        else:
            return
    relative_target = os.path.relpath(target, start=link.parent)
    os.symlink(relative_target, link)


def _one_line(value: str, *, limit: int = 180) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _rows(db: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    return list(db.execute(sql, params).fetchall())
