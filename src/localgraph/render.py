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
    people = _rows(db, "SELECT stable_key, display_name FROM identities WHERE kind = 'person' ORDER BY display_name")
    groups = _rows(db, "SELECT stable_key, display_name FROM identities WHERE kind = 'group' ORDER BY display_name")
    threads = _rows(db, "SELECT source_kind, source_thread_key, title, thread_kind FROM threads ORDER BY source_kind, title")

    _write_index(workspace.views_dir / "index.md", people=people, groups=groups, threads=threads)
    for row in people:
        _write_entity_view(workspace.views_dir / "people" / stable_view_name(row["display_name"], row["stable_key"]), row, "person")
    for row in groups:
        _write_entity_view(workspace.views_dir / "groups" / stable_view_name(row["display_name"], row["stable_key"]), row, "group")
    for row in threads:
        _write_thread_view(workspace.views_dir / "threads" / row["source_kind"] / stable_view_name(row["title"], row["source_thread_key"]), row)
    _write_system_manifest(workspace, people=people, groups=groups, threads=threads, source_scan=source_scan)

    result = {"people": len(people), "groups": len(groups), "threads": len(threads)}
    if source_scan is not None:
        result["sourceExports"] = len(source_scan["exports"])  # type: ignore[arg-type]
        result["sourceMessageFiles"] = int(source_scan["totalMessageFiles"])
    return result


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


def _write_system_manifest(
    workspace: Workspace,
    *,
    people: list[sqlite3.Row],
    groups: list[sqlite3.Row],
    threads: list[sqlite3.Row],
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
        },
        "source": source_scan,
    }
    (system_dir / "README.md").write_text(
        "# Localgraph System Views\n\nGenerated manifests and diagnostics live here.\n",
        encoding="utf-8",
    )
    (system_dir / "source-manifest.json").write_text(f"{json.dumps(manifest, indent=2, sort_keys=True)}\n", encoding="utf-8")
