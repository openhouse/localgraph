from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .paths import Workspace


def render_views(db: sqlite3.Connection, workspace: Workspace) -> dict[str, int]:
    workspace.views_dir.mkdir(parents=True, exist_ok=True)
    people = _rows(db, "SELECT stable_key, display_name FROM identities WHERE kind = 'person' ORDER BY display_name")
    groups = _rows(db, "SELECT stable_key, display_name FROM identities WHERE kind = 'group' ORDER BY display_name")
    threads = _rows(db, "SELECT source_kind, source_thread_key, title, thread_kind FROM threads ORDER BY source_kind, title")

    _write_index(workspace.views_dir / "index.md", people=people, groups=groups, threads=threads)
    for row in people:
        _write_entity_view(workspace.views_dir / "people" / slug(row["display_name"], row["stable_key"]), row, "person")
    for row in groups:
        _write_entity_view(workspace.views_dir / "groups" / slug(row["display_name"], row["stable_key"]), row, "group")
    for row in threads:
        _write_thread_view(workspace.views_dir / "threads" / row["source_kind"] / slug(row["title"], row["source_thread_key"]), row)

    return {"people": len(people), "groups": len(groups), "threads": len(threads)}


def _rows(db: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    return list(db.execute(sql).fetchall())


def _write_index(path: Path, *, people: list[sqlite3.Row], groups: list[sqlite3.Row], threads: list[sqlite3.Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Localgraph Views\n\n"
        f"- People: {len(people)}\n"
        f"- Groups: {len(groups)}\n"
        f"- Threads: {len(threads)}\n",
        encoding="utf-8",
    )


def _write_entity_view(path: Path, row: sqlite3.Row, kind: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "index.md").write_text(
        f"# {row['display_name']}\n\n"
        f"- Kind: {kind}\n"
        f"- Stable key: `{row['stable_key']}`\n",
        encoding="utf-8",
    )


def _write_thread_view(path: Path, row: sqlite3.Row) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "index.md").write_text(
        f"# {row['title']}\n\n"
        f"- Source: {row['source_kind']}\n"
        f"- Thread kind: {row['thread_kind']}\n"
        f"- Source thread key: `{row['source_thread_key']}`\n",
        encoding="utf-8",
    )


def slug(label: str, stable_key: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "untitled"
    suffix = re.sub(r"[^a-zA-Z0-9]+", "", stable_key)[-8:] or "unknown"
    return f"{base}--{suffix}"
