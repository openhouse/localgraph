from __future__ import annotations

import json
import os
import shutil
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

    _prune_stale_entity_views(
        workspace.views_dir / "people",
        {stable_view_name(row["display_name"], row["stable_key"]) for row in people},
        generated_files={
            "index.md",
            "llm-context.md",
            "threads.md",
            "groups.md",
            "timeline.md",
            "media.md",
            "source-accounts.md",
        },
        generated_directories={"manifests", "transcripts"},
    )
    _prune_stale_entity_views(
        workspace.views_dir / "groups",
        {stable_view_name(row["display_name"], row["stable_key"]) for row in groups},
        generated_files={"index.md"},
    )
    _prune_stale_thread_views(
        workspace.views_dir / "threads",
        {
            Path(str(row["source_kind"])) / stable_view_name(row["title"], row["source_thread_key"])
            for row in threads
        },
    )

    _write_index(workspace.views_dir / "index.md", people=people, groups=groups, threads=threads)
    for row in people:
        _write_entity_view(
            db,
            workspace.views_dir / "people" / stable_view_name(row["display_name"], row["stable_key"]),
            row,
            "person",
        )
    for row in groups:
        _write_entity_view(
            db,
            workspace.views_dir / "groups" / stable_view_name(row["display_name"], row["stable_key"]),
            row,
            "group",
        )
    for row in threads:
        _write_thread_view(
            db,
            workspace.views_dir / "threads" / row["source_kind"] / stable_view_name(row["title"], row["source_thread_key"]),
            row,
        )
    instagram_account_count = _write_instagram_account_views(workspace, threads)
    facebook_account_count = _write_facebook_account_views(workspace, threads)
    _write_system_manifest(
        workspace,
        people=people,
        groups=groups,
        threads=threads,
        message_count=message_count,
        source_scan=source_scan,
    )

    result = {
        "people": len(people),
        "groups": len(groups),
        "threads": len(threads),
        "messages": message_count,
        "instagramAccounts": instagram_account_count,
        "facebookAccounts": facebook_account_count,
    }
    if source_scan is not None:
        result["sourceExports"] = len(source_scan["exports"])  # type: ignore[arg-type]
        result["sourceMessageFiles"] = int(source_scan["totalMessageFiles"])
    return result


def _rows(db: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    return list(db.execute(sql, parameters).fetchall())


def _write_index(path: Path, *, people: list[sqlite3.Row], groups: list[sqlite3.Row], threads: list[sqlite3.Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Localgraph Views\n\n"
        f"- People: {len(people)}\n"
        f"- Groups: {len(groups)}\n"
        f"- Threads: {len(threads)}\n",
        encoding="utf-8",
    )


def _write_entity_view(db: sqlite3.Connection, path: Path, row: sqlite3.Row, kind: str) -> None:
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
    if kind == "person":
        _write_person_view(db, path, row, accounts)
        return

    if kind == "group":
        threads = _rows(
            db,
            """
            SELECT DISTINCT t.source_kind, t.source_thread_key, t.title, t.thread_kind, t.first_message_at, t.last_message_at
            FROM graph_edges AS ge
            JOIN threads AS t ON ge.from_key = ('thread:' || t.source_kind || ':' || t.source_thread_key)
            WHERE ge.from_kind = 'thread'
              AND ge.edge_kind = 'represents_group'
              AND ge.to_kind = 'identity'
              AND ge.to_key = ?
            ORDER BY COALESCE(t.last_message_at, ''), t.title
            """,
            (row["stable_key"],),
        )
    else:
        threads = _rows(
            db,
            """
            SELECT DISTINCT t.source_kind, t.source_thread_key, t.title, t.thread_kind, t.first_message_at, t.last_message_at
            FROM threads AS t
            JOIN thread_participants AS tp ON tp.thread_id = t.id
            WHERE tp.identity_id = ?
            ORDER BY COALESCE(t.last_message_at, ''), t.title
            """,
            (row["id"],),
        )
    participants: list[sqlite3.Row] = []
    if kind == "group":
        participants = _rows(
            db,
            """
            SELECT i.display_name, i.stable_key
            FROM graph_edges AS ge
            JOIN identities AS i ON i.stable_key = ge.to_key
            WHERE ge.from_kind = 'identity'
              AND ge.from_key = ?
              AND ge.edge_kind = 'has_participant'
              AND ge.to_kind = 'identity'
            ORDER BY i.display_name
            """,
            (row["stable_key"],),
        )
    account_lines = "".join(
        f"- {account['source_kind']}: `{account['account_key']}` ({account['display_name']})\n"
        for account in accounts
    ) or "- None\n"
    participant_block = ""
    if kind == "group":
        participant_lines = "".join(
            f"- {participant['display_name']} (`{participant['stable_key']}`)\n" for participant in participants
        ) or "- None\n"
        participant_block = f"\n## Participants\n\n{participant_lines}"
    thread_lines = "".join(
        f"- {thread['title']} ({thread['source_kind']}, {thread['thread_kind']}, last: {thread['last_message_at'] or 'unknown'})\n"
        for thread in threads
    ) or "- None\n"
    (path / "index.md").write_text(
        f"# {row['display_name']}\n\n"
        f"- Kind: {kind}\n"
        f"- Stable key: `{row['stable_key']}`\n"
        f"\n## Accounts\n\n{account_lines}"
        f"{participant_block}"
        f"\n## Threads\n\n{thread_lines}",
        encoding="utf-8",
    )


def _write_person_view(db: sqlite3.Connection, path: Path, row: sqlite3.Row, accounts: list[sqlite3.Row]) -> None:
    threads = _person_threads(db, int(row["id"]))
    direct_threads = [thread for thread in threads if thread["thread_kind"] == "direct"]
    group_threads = [thread for thread in threads if thread["thread_kind"] == "group"]
    message_count = sum(int(thread["message_count"] or 0) for thread in threads)
    media_count = _person_media_count(db, int(row["id"]))
    first_seen = min((str(thread["first_message_at"]) for thread in threads if thread["first_message_at"]), default="unknown")
    last_seen = max((str(thread["last_message_at"]) for thread in threads if thread["last_message_at"]), default="unknown")
    transcript_links = _write_person_transcript_links(path, threads)

    account_lines = _account_lines(accounts)
    direct_lines = _thread_bullets(direct_threads)
    group_lines = _thread_bullets(group_threads)
    recent_lines = _recent_message_lines(db, int(row["id"]))
    transcript_lines = _transcript_bullets(transcript_links)

    (path / "index.md").write_text(
        f"# {row['display_name']}\n\n"
        "Person context capsule.\n\n"
        f"- Stable key: `{row['stable_key']}`\n"
        f"- Source accounts: {len(accounts)}\n"
        f"- Direct threads: {len(direct_threads)}\n"
        f"- Shared group threads: {len(group_threads)}\n"
        f"- Messages in visible threads: {message_count}\n"
        f"- Media references in visible threads: {media_count}\n"
        f"- First seen: {first_seen}\n"
        f"- Last seen: {last_seen}\n"
        "\n## Read First\n\n"
        "- [llm-context.md](llm-context.md) gives an LLM-oriented orientation brief.\n"
        "- [timeline.md](timeline.md) shows recent message context across shared threads.\n"
        "- [transcripts/](transcripts/) contains symlinks to full source conversation transcripts.\n"
        "- [notes.md](notes.md) is preserved for user-authored notes.\n"
        "\n## Accounts\n\n"
        f"{account_lines}"
        "\n## Direct Threads\n\n"
        f"{direct_lines}"
        "\n## Shared Groups\n\n"
        f"{group_lines}",
        encoding="utf-8",
    )
    (path / "llm-context.md").write_text(
        f"# {row['display_name']}: LLM Context\n\n"
        "Use this folder as portable, private project context. Start here for orientation, "
        "then follow transcript links only when full evidence is needed.\n\n"
        "## Identity\n\n"
        f"- Stable key: `{row['stable_key']}`\n"
        f"- First seen: {first_seen}\n"
        f"- Last seen: {last_seen}\n"
        f"- Direct threads: {len(direct_threads)}\n"
        f"- Shared group threads: {len(group_threads)}\n"
        f"- Messages in visible threads: {message_count}\n"
        f"- Media references in visible threads: {media_count}\n"
        "\n## Source Accounts\n\n"
        f"{account_lines}"
        "\n## Full Transcript Links\n\n"
        f"{transcript_lines}"
        "\n## Recent Context\n\n"
        f"{recent_lines}"
        "\n## User Notes\n\n"
        "See [notes.md](notes.md). Treat user-authored notes as interpretation and transcript links as evidence.\n",
        encoding="utf-8",
    )
    (path / "threads.md").write_text(_threads_markdown(row, threads), encoding="utf-8")
    (path / "groups.md").write_text(_threads_markdown(row, group_threads, title="Shared Groups"), encoding="utf-8")
    (path / "timeline.md").write_text(_person_timeline_markdown(row, recent_lines), encoding="utf-8")
    (path / "media.md").write_text(_person_media_markdown(db, row), encoding="utf-8")
    (path / "source-accounts.md").write_text(_source_accounts_markdown(row, accounts), encoding="utf-8")
    if not (path / "notes.md").exists():
        (path / "notes.md").write_text(
            f"# Notes: {row['display_name']}\n\n"
            "User-authored notes go here. This file is created once and preserved across renders.\n",
            encoding="utf-8",
        )
    _write_person_manifests(path, row, accounts, threads, transcript_links)


def _person_threads(db: sqlite3.Connection, identity_id: int) -> list[sqlite3.Row]:
    return _rows(
        db,
        """
        SELECT DISTINCT
          t.id,
          t.source_kind,
          t.source_thread_key,
          t.title,
          t.thread_kind,
          t.first_message_at,
          t.last_message_at,
          (
            SELECT COUNT(*)
            FROM messages AS m
            WHERE m.thread_id = t.id
          ) AS message_count
        FROM threads AS t
        JOIN thread_participants AS tp ON tp.thread_id = t.id
        WHERE tp.identity_id = ?
        ORDER BY COALESCE(t.last_message_at, ''), t.source_kind, t.title
        """,
        (identity_id,),
    )


def _person_media_count(db: sqlite3.Connection, identity_id: int) -> int:
    row = db.execute(
        """
        SELECT COUNT(DISTINCT mo.id) AS count
        FROM media_objects AS mo
        JOIN messages AS m ON m.id = mo.message_id
        JOIN thread_participants AS tp ON tp.thread_id = m.thread_id
        WHERE tp.identity_id = ?
        """,
        (identity_id,),
    ).fetchone()
    return int(row["count"] if row else 0)


def _write_person_transcript_links(path: Path, threads: list[sqlite3.Row]) -> list[dict[str, object]]:
    transcripts_dir = path / "transcripts"
    _reset_generated_dir(transcripts_dir / "direct")
    _reset_generated_dir(transcripts_dir / "groups")
    links: list[dict[str, object]] = []
    for thread in threads:
        category = "direct" if thread["thread_kind"] == "direct" else "groups"
        filename = f"{thread['source_kind']}-{stable_view_name(thread['title'], thread['source_thread_key'])}.md"
        link_path = transcripts_dir / category / filename
        target_path = (
            path.parents[1]
            / "threads"
            / str(thread["source_kind"])
            / stable_view_name(thread["title"], thread["source_thread_key"])
            / "messages.md"
        )
        _replace_symlink(link_path, target_path)
        links.append(
            {
                "title": thread["title"],
                "sourceKind": thread["source_kind"],
                "threadKind": thread["thread_kind"],
                "sourceThreadKey": thread["source_thread_key"],
                "messageCount": int(thread["message_count"] or 0),
                "firstMessageAt": thread["first_message_at"],
                "lastMessageAt": thread["last_message_at"],
                "path": _display_path(path, link_path),
                "target": _display_path(path, target_path),
            }
        )
    return links


def _reset_generated_dir(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _prune_stale_entity_views(
    root: Path,
    active_names: set[str],
    *,
    generated_files: set[str],
    generated_directories: set[str] | None = None,
) -> None:
    if not root.is_dir():
        return
    for path in root.iterdir():
        if not path.is_dir() or path.is_symlink() or path.name in active_names:
            continue
        _remove_managed_view_artifacts(
            path,
            generated_files=generated_files,
            generated_directories=generated_directories or set(),
        )


def _prune_stale_thread_views(root: Path, active_paths: set[Path]) -> None:
    if not root.is_dir():
        return
    for source_dir in root.iterdir():
        if not source_dir.is_dir() or source_dir.is_symlink():
            continue
        for path in source_dir.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            if Path(source_dir.name) / path.name in active_paths:
                continue
            _remove_managed_view_artifacts(
                path,
                generated_files={"index.md", "messages.md"},
                generated_directories=set(),
            )
        _remove_empty_directory(source_dir)


def _remove_managed_view_artifacts(
    path: Path,
    *,
    generated_files: set[str],
    generated_directories: set[str],
) -> None:
    for name in generated_files:
        target = path / name
        if target.is_file() or target.is_symlink():
            target.unlink()
    for name in generated_directories:
        target = path / name
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    _remove_empty_directory(path)


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _replace_symlink(link_path: Path, target_path: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_dir() and not link_path.is_symlink():
            shutil.rmtree(link_path)
        else:
            link_path.unlink()
    relative_target = os.path.relpath(target_path, link_path.parent)
    link_path.symlink_to(relative_target)


def _account_lines(accounts: list[sqlite3.Row]) -> str:
    return "".join(
        f"- {account['source_kind']}: `{account['account_key']}` ({account['display_name']})\n"
        for account in accounts
    ) or "- None\n"


def _thread_bullets(threads: list[sqlite3.Row]) -> str:
    return "".join(
        f"- {thread['title']} ({thread['source_kind']}, {thread['message_count']} messages, "
        f"last: {thread['last_message_at'] or 'unknown'})\n"
        for thread in threads
    ) or "- None\n"


def _transcript_bullets(links: list[dict[str, object]]) -> str:
    return "".join(
        f"- [{link['title']}]({link['path']}) "
        f"({link['sourceKind']}, {link['threadKind']}, {link['messageCount']} messages)\n"
        for link in links
    ) or "- None\n"


def _recent_message_lines(db: sqlite3.Connection, identity_id: int, *, limit: int = 40) -> str:
    messages = _rows(
        db,
        """
        SELECT DISTINCT
          m.sent_at,
          t.source_kind,
          t.title AS thread_title,
          t.thread_kind,
          COALESCE(sender.display_name, account.display_name, 'Unknown') AS sender_name,
          m.body_text
        FROM messages AS m
        JOIN threads AS t ON t.id = m.thread_id
        JOIN thread_participants AS tp ON tp.thread_id = t.id
        LEFT JOIN identities AS sender ON sender.id = m.sender_identity_id
        LEFT JOIN accounts AS account ON account.id = m.sender_account_id
        WHERE tp.identity_id = ?
        ORDER BY m.sent_at DESC, m.id DESC
        LIMIT ?
        """,
        (identity_id, limit),
    )
    lines = []
    for message in messages:
        body = _snippet(str(message["body_text"] or "[no text]"))
        lines.append(
            f"- {message['sent_at']} - {message['sender_name']} in {message['thread_title']} "
            f"({message['source_kind']}, {message['thread_kind']}): {body}\n"
        )
    return "".join(lines) or "- No messages imported yet.\n"


def _threads_markdown(row: sqlite3.Row, threads: list[sqlite3.Row], *, title: str = "Threads") -> str:
    lines = [f"# {row['display_name']} {title}", ""]
    if not threads:
        lines.append("No threads imported yet.")
    else:
        lines.extend(["| Source | Thread | Kind | Messages | First | Last |", "|---|---|---|---:|---|---|"])
        for thread in threads:
            lines.append(
                f"| {thread['source_kind']} | {thread['title']} | {thread['thread_kind']} | "
                f"{thread['message_count']} | {thread['first_message_at'] or 'unknown'} | "
                f"{thread['last_message_at'] or 'unknown'} |"
            )
    return "\n".join(lines).rstrip() + "\n"


def _person_timeline_markdown(row: sqlite3.Row, recent_lines: str) -> str:
    return (
        f"# {row['display_name']} Timeline\n\n"
        "Recent imported messages across threads where this person participates. "
        "Use transcript symlinks for full conversation evidence.\n\n"
        f"{recent_lines}"
    )


def _person_media_markdown(db: sqlite3.Connection, row: sqlite3.Row, *, limit: int = 100) -> str:
    media = _rows(
        db,
        """
        SELECT DISTINCT
          mo.source_uri,
          mo.local_path,
          mo.mime_type,
          t.source_kind,
          t.title AS thread_title,
          m.sent_at
        FROM media_objects AS mo
        JOIN messages AS m ON m.id = mo.message_id
        JOIN threads AS t ON t.id = m.thread_id
        JOIN thread_participants AS tp ON tp.thread_id = t.id
        WHERE tp.identity_id = ?
        ORDER BY m.sent_at DESC, mo.id DESC
        LIMIT ?
        """,
        (row["id"], limit),
    )
    lines = [f"# {row['display_name']} Media", ""]
    if not media:
        lines.append("No media references imported yet.")
    else:
        for item in media:
            source = item["local_path"] or item["source_uri"] or "unknown media"
            lines.append(
                f"- {item['sent_at']} - {source} ({item['mime_type'] or 'unknown type'}, "
                f"{item['source_kind']} / {item['thread_title']})"
            )
    return "\n".join(lines).rstrip() + "\n"


def _source_accounts_markdown(row: sqlite3.Row, accounts: list[sqlite3.Row]) -> str:
    return f"# {row['display_name']} Source Accounts\n\n" + _account_lines(accounts)


def _write_person_manifests(
    path: Path,
    row: sqlite3.Row,
    accounts: list[sqlite3.Row],
    threads: list[sqlite3.Row],
    transcript_links: list[dict[str, object]],
) -> None:
    manifests_dir = path / "manifests"
    _reset_generated_dir(manifests_dir)
    person = {
        "displayName": row["display_name"],
        "stableKey": row["stable_key"],
        "kind": "person",
        "accounts": len(accounts),
        "threads": len(threads),
        "transcripts": len(transcript_links),
    }
    accounts_payload = [
        {
            "sourceKind": account["source_kind"],
            "accountKey": account["account_key"],
            "displayName": account["display_name"],
        }
        for account in accounts
    ]
    _write_json(manifests_dir / "person.json", person)
    _write_json(manifests_dir / "accounts.json", accounts_payload)
    _write_json(manifests_dir / "transcripts.json", transcript_links)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")


def _snippet(value: str, *, limit: int = 180) -> str:
    clean = " ".join(value.split())
    if len(clean) <= limit:
        return clean
    return f"{clean[: limit - 3]}..."


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _write_thread_view(db: sqlite3.Connection, path: Path, row: sqlite3.Row) -> None:
    path.mkdir(parents=True, exist_ok=True)
    participants = _rows(
        db,
        """
        SELECT DISTINCT i.display_name, i.stable_key, a.source_kind, a.account_key
        FROM thread_participants AS tp
        LEFT JOIN identities AS i ON i.id = tp.identity_id
        LEFT JOIN accounts AS a ON a.id = tp.account_id
        WHERE tp.thread_id = ?
        ORDER BY i.display_name, a.source_kind, a.account_key
        """,
        (row["id"],),
    )
    messages = _rows(
        db,
        """
        SELECT
          m.sent_at,
          m.body_text,
          m.source_message_key,
          COALESCE(i.display_name, a.display_name, 'Unknown') AS sender_name,
          (
            SELECT COUNT(*)
            FROM media_objects AS mo
            WHERE mo.message_id = m.id
          ) AS media_count
        FROM messages AS m
        LEFT JOIN identities AS i ON i.id = m.sender_identity_id
        LEFT JOIN accounts AS a ON a.id = m.sender_account_id
        WHERE m.thread_id = ?
        ORDER BY m.sent_at, m.id
        """,
        (row["id"],),
    )
    participant_lines = "".join(
        f"- {participant['display_name'] or 'Unknown'}"
        f" ({participant['source_kind'] or 'unknown'}: `{participant['account_key'] or participant['stable_key'] or 'unknown'}`)\n"
        for participant in participants
    ) or "- None\n"
    (path / "index.md").write_text(
        f"# {row['title']}\n\n"
        f"- Source: {row['source_kind']}\n"
        f"- Thread kind: {row['thread_kind']}\n"
        f"- Source thread key: `{row['source_thread_key']}`\n"
        f"- First message: {row['first_message_at'] or 'unknown'}\n"
        f"- Last message: {row['last_message_at'] or 'unknown'}\n"
        f"- Messages: {len(messages)}\n"
        f"\n## Participants\n\n{participant_lines}"
        f"\n## Transcript\n\nSee [messages.md](messages.md).\n",
        encoding="utf-8",
    )
    (path / "messages.md").write_text(_thread_messages_markdown(row, messages), encoding="utf-8")


def _write_instagram_account_views(workspace: Workspace, threads: list[sqlite3.Row]) -> int:
    account_threads: dict[str, list[sqlite3.Row]] = {}
    for thread in threads:
        if thread["source_kind"] != "instagram":
            continue
        source_key = str(thread["source_thread_key"])
        if ":" not in source_key:
            continue
        account_key, relative_key = source_key.split(":", 1)
        if not account_key or not relative_key.startswith(("your_instagram_activity/", "messages/")):
            continue
        account_threads.setdefault(account_key, []).append(thread)

    root = workspace.views_dir / "instagram-accounts"
    root.mkdir(parents=True, exist_ok=True)
    for account_key, rows in sorted(account_threads.items()):
        account_root = root / account_key
        thread_links = account_root / "threads"
        thread_links.mkdir(parents=True, exist_ok=True)
        desired: set[str] = set()
        for row in rows:
            view_name = stable_view_name(row["title"], row["source_thread_key"])
            desired.add(view_name)
            link = thread_links / view_name
            target = workspace.views_dir / "threads" / "instagram" / view_name
            if link.is_symlink() and link.resolve() == target.resolve():
                continue
            if link.exists() or link.is_symlink():
                if link.is_symlink():
                    link.unlink()
                else:
                    continue
            link.symlink_to(Path(os.path.relpath(target, start=thread_links)), target_is_directory=True)
        for existing in thread_links.iterdir():
            if existing.is_symlink() and existing.name not in desired:
                existing.unlink()
        (account_root / "index.md").write_text(
            f"# Instagram: @{account_key}\n\n"
            f"- Threads: {len(rows)}\n"
            "- Combined graph: [../../index.md](../../index.md)\n"
            "- Account threads: [threads/](threads/)\n",
            encoding="utf-8",
        )
    return len(account_threads)


def _write_facebook_account_views(workspace: Workspace, threads: list[sqlite3.Row]) -> int:
    account_threads: dict[str, list[sqlite3.Row]] = {}
    for thread in threads:
        if thread["source_kind"] != "facebook":
            continue
        source_key = str(thread["source_thread_key"])
        if ":" not in source_key:
            continue
        account_key, relative_key = source_key.split(":", 1)
        if not account_key or not relative_key.startswith(("your_facebook_activity/", "messages/")):
            continue
        account_threads.setdefault(account_key, []).append(thread)

    root = workspace.views_dir / "facebook-accounts"
    root.mkdir(parents=True, exist_ok=True)
    for account_key, rows in sorted(account_threads.items()):
        account_root = root / account_key
        thread_links = account_root / "threads"
        thread_links.mkdir(parents=True, exist_ok=True)
        desired: set[str] = set()
        for row in rows:
            view_name = stable_view_name(row["title"], row["source_thread_key"])
            desired.add(view_name)
            link = thread_links / view_name
            target = workspace.views_dir / "threads" / "facebook" / view_name
            if link.is_symlink() and link.resolve() == target.resolve():
                continue
            if link.exists() or link.is_symlink():
                if link.is_symlink():
                    link.unlink()
                else:
                    continue
            link.symlink_to(Path(os.path.relpath(target, start=thread_links)), target_is_directory=True)
        for existing in thread_links.iterdir():
            if existing.is_symlink() and existing.name not in desired:
                existing.unlink()
        (account_root / "index.md").write_text(
            f"# Facebook: {account_key}\n\n"
            f"- Threads: {len(rows)}\n"
            "- Combined graph: [../../index.md](../../index.md)\n"
            "- Account threads: [threads/](threads/)\n",
            encoding="utf-8",
        )
    return len(account_threads)


def _thread_messages_markdown(row: sqlite3.Row, messages: list[sqlite3.Row]) -> str:
    lines = [
        f"# {row['title']} Messages",
        "",
        f"Source: `{row['source_kind']}`",
        "",
    ]
    current_day = ""
    for message in messages:
        sent_at = str(message["sent_at"])
        day = sent_at[:10]
        if day != current_day:
            current_day = day
            lines.extend(["", f"## {day}", ""])
        body = str(message["body_text"] or "").strip()
        if not body:
            body = "[no text]"
        media_count = int(message["media_count"] or 0)
        if media_count:
            suffix = "s" if media_count != 1 else ""
            body = f"{body}\n\n[{media_count} media attachment{suffix}]"
        lines.extend(
            [
                f"**{message['sender_name']}** `{sent_at}`",
                "",
                body,
                "",
            ]
        )
    if not messages:
        lines.append("No messages imported for this thread.")
    return "\n".join(lines).rstrip() + "\n"


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
