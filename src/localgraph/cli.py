from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .imessage import import_imessage_chat_db
from .instagram import scan_instagram_source
from .instagram import import_instagram_source
from .paths import Workspace
from .render import render_views
from .schema import connect, initialize_schema
from .views import view_kinds, view_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="localgraph",
        description="Local-first correspondence graph for private source texts and views.",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Localgraph workspace root. Defaults to the current directory.",
    )

    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("plan", help="Print the planned private root and view layout.")

    init = commands.add_parser("init", help="Create workspace directories and SQLite schema.")
    init.add_argument("--force", action="store_true", help="Allow initialization in a non-empty directory.")

    commands.add_parser("doctor", help="Check workspace directories and database schema.")

    scan = commands.add_parser("scan", help="Detect Instagram transfer exports without reading message bodies.")
    scan.add_argument("--source", help="Instagram source directory. Defaults to sources/instagram.")

    render = commands.add_parser("render", help="Render filesystem views from canonical SQLite state.")
    render.add_argument("--source", help="Optionally scan an Instagram source directory and include it in the render manifest.")

    import_cmd = commands.add_parser("import", help="Import private source messages into canonical SQLite state.")
    import_sources = import_cmd.add_subparsers(dest="source_kind", required=True)

    import_instagram = import_sources.add_parser("instagram", help="Import Instagram transfer message JSON.")
    import_instagram.add_argument("--source", help="Instagram source directory. Defaults to sources/instagram.")

    import_imessage = import_sources.add_parser("imessage", help="Import macOS Messages chat.db.")
    import_imessage.add_argument("--chat-db", help="Path to a readable iMessage chat.db. Defaults to ~/Library/Messages/chat.db.")
    import_imessage.add_argument("--limit", type=int, help="Import only the newest N iMessage rows from chat.db.")
    import_imessage.add_argument("--immutable", action="store_true", help="Open a copied chat.db as immutable read-only input.")

    import_all = import_sources.add_parser("all", help="Import Instagram and iMessage sources.")
    import_all.add_argument("--instagram-source", help="Instagram source directory. Defaults to sources/instagram.")
    import_all.add_argument("--imessage-chat-db", help="Path to a readable iMessage chat.db. Defaults to ~/Library/Messages/chat.db.")
    import_all.add_argument("--imessage-limit", type=int, help="Import only the newest N iMessage rows from chat.db.")
    import_all.add_argument("--imessage-immutable", action="store_true", help="Open a copied iMessage chat.db as immutable read-only input.")

    view_name = commands.add_parser("view-name", help="Print a deterministic symlink-friendly view path.")
    view_name.add_argument("kind", choices=view_kinds())
    view_name.add_argument("label")
    view_name.add_argument("source_key")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = Workspace(Path(args.root).expanduser().resolve())

    try:
        if args.command == "plan":
            summary = command_plan(workspace)
        elif args.command == "init":
            summary = command_init(workspace, force=args.force)
        elif args.command == "doctor":
            summary = command_doctor(workspace)
        elif args.command == "scan":
            summary = command_scan(workspace, source=args.source)
        elif args.command == "render":
            summary = command_render(workspace, source=args.source)
        elif args.command == "import":
            summary = command_import(workspace, args)
        elif args.command == "view-name":
            summary = command_view_name(workspace, args.kind, args.label, args.source_key)
        else:
            parser.error(f"unknown command: {args.command}")
    except (LocalgraphError, OSError, sqlite3.Error, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def command_plan(workspace: Workspace) -> dict[str, object]:
    return workspace.plan()


def command_init(workspace: Workspace, *, force: bool = False) -> dict[str, object]:
    workspace.ensure_workspace(force=force)
    with connect(workspace.database_path) as db:
        initialize_schema(db)
    return {
        "root": str(workspace.root),
        "database": str(workspace.database_path),
        "config": str(workspace.config_path),
        "created": [str(path) for path in workspace.managed_directories],
        "views": [str(path) for path in workspace.view_directories],
    }


def command_doctor(workspace: Workspace) -> dict[str, object]:
    checks = workspace.check()
    schema_ok = False
    if workspace.database_path.exists():
        with connect(workspace.database_path) as db:
            schema_ok = bool(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'").fetchone())
    return {
        "root": str(workspace.root),
        "directories": checks,
        "database": "ok" if schema_ok else "missing schema",
    }


def command_scan(workspace: Workspace, *, source: str | None) -> dict[str, object]:
    source_path = Path(source).expanduser() if source else workspace.instagram_source_dir
    if not source_path.is_absolute():
        source_path = workspace.root / source_path
    return scan_instagram_source(source_path)


def command_render(workspace: Workspace, *, source: str | None = None) -> dict[str, object]:
    if not workspace.database_path.exists():
        raise LocalgraphError(f"database does not exist: {workspace.database_path}")
    source_scan = command_scan(workspace, source=source)
    with connect(workspace.database_path) as db:
        result = render_views(db, workspace, source_scan=source_scan)
    return result


def command_import(workspace: Workspace, args: argparse.Namespace) -> dict[str, object]:
    workspace.ensure_workspace()
    with connect(workspace.database_path) as db:
        initialize_schema(db)
        if args.source_kind == "instagram":
            source_path = _resolve_source(workspace, args.source, default=workspace.instagram_source_dir)
            return import_instagram_source(db, source_path)
        if args.source_kind == "imessage":
            chat_db_path = Path(args.chat_db).expanduser() if args.chat_db else Path("~/Library/Messages/chat.db").expanduser()
            return import_imessage_chat_db(db, chat_db_path, limit=args.limit, immutable=args.immutable)
        if args.source_kind == "all":
            instagram_source = _resolve_source(workspace, args.instagram_source, default=workspace.instagram_source_dir)
            imessage_chat_db = Path(args.imessage_chat_db).expanduser() if args.imessage_chat_db else Path("~/Library/Messages/chat.db").expanduser()
            results = {
                "instagram": import_instagram_source(db, instagram_source),
            }
            try:
                results["imessage"] = import_imessage_chat_db(db, imessage_chat_db, limit=args.imessage_limit, immutable=args.imessage_immutable)
            except (OSError, sqlite3.Error) as exc:
                results["imessage"] = {"sourceKind": "imessage", "sourcePath": str(imessage_chat_db), "error": str(exc), "messages": 0, "threads": 0}
            return {
                "sourceKind": "all",
                "sources": results,
                "messages": int(results["instagram"]["messages"]) + int(results["imessage"]["messages"]),
                "threads": int(results["instagram"]["threads"]) + int(results["imessage"]["threads"]),
            }
    raise LocalgraphError(f"unsupported import source: {args.source_kind}")


def command_view_name(workspace: Workspace, kind: str, label: str, source_key: str) -> dict[str, object]:
    path = view_path(workspace.root, kind, label, source_key)
    return {
        "kind": kind,
        "label": label,
        "sourceKey": source_key,
        "path": str(path),
    }


def _resolve_source(workspace: Workspace, source: str | None, *, default: Path) -> Path:
    source_path = Path(source).expanduser() if source else default
    if not source_path.is_absolute():
        source_path = workspace.root / source_path
    return source_path


class LocalgraphError(Exception):
    """User-facing command error."""


if __name__ == "__main__":
    raise SystemExit(main())
