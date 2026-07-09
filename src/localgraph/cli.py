from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .ingest import import_workspace_sources
from .instagram import scan_instagram_source
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

    ingest = commands.add_parser("import", help="Import Instagram and iMessage messages into canonical SQLite state.")
    ingest.add_argument("--instagram-source", help="Instagram source directory. Defaults to sources/instagram.")
    ingest.add_argument("--imessage-db", help="iMessage chat.db path. Defaults to sources/imessage/chat.db.")
    ingest.add_argument("--skip-instagram", action="store_true", help="Do not import Instagram messages.")
    ingest.add_argument("--skip-imessage", action="store_true", help="Do not import iMessage messages.")
    ingest.add_argument("--me", default="Me", help="Display name for your own identity. Defaults to 'Me'.")
    ingest.add_argument(
        "--me-instagram",
        action="append",
        default=[],
        help="Instagram participant name that should map to the self identity. May be repeated.",
    )
    ingest.add_argument(
        "--me-imessage",
        action="append",
        default=[],
        help="iMessage handle, phone, or email that should map to the self identity. May be repeated.",
    )
    ingest.add_argument("--render", action="store_true", help="Render filesystem views after import.")

    render = commands.add_parser("render", help="Render filesystem views from canonical SQLite state.")
    render.add_argument("--source", help="Optionally scan an Instagram source directory and include it in the render manifest.")

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
        elif args.command == "import":
            summary = command_import(
                workspace,
                instagram_source=args.instagram_source,
                imessage_db=args.imessage_db,
                skip_instagram=args.skip_instagram,
                skip_imessage=args.skip_imessage,
                me=args.me,
                me_instagram=args.me_instagram,
                me_imessage=args.me_imessage,
                render=args.render,
            )
        elif args.command == "render":
            summary = command_render(workspace, source=args.source)
        elif args.command == "view-name":
            summary = command_view_name(workspace, args.kind, args.label, args.source_key)
        else:
            parser.error(f"unknown command: {args.command}")
    except (FileNotFoundError, LocalgraphError, ValueError) as exc:
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


def command_import(
    workspace: Workspace,
    *,
    instagram_source: str | None,
    imessage_db: str | None,
    skip_instagram: bool,
    skip_imessage: bool,
    me: str,
    me_instagram: list[str],
    me_imessage: list[str],
    render: bool,
) -> dict[str, object]:
    workspace.ensure_workspace(force=False)
    instagram_path = _resolve_workspace_path(workspace, instagram_source) if instagram_source else workspace.instagram_source_dir
    imessage_path = _resolve_workspace_path(workspace, imessage_db) if imessage_db else workspace.imessage_chat_db_path
    with connect(workspace.database_path) as db:
        initialize_schema(db)
        result = import_workspace_sources(
            db,
            workspace,
            instagram_source=instagram_path,
            imessage_db=imessage_path,
            import_instagram=not skip_instagram,
            import_imessage=not skip_imessage,
            me_name=me,
            me_instagram_names=me_instagram,
            me_imessage_handles=me_imessage,
            explicit_instagram=instagram_source is not None and not skip_instagram,
            explicit_imessage=imessage_db is not None and not skip_imessage,
        )
        if render:
            result["render"] = render_views(db, workspace, source_scan=scan_instagram_source(instagram_path))
    return result


def command_render(workspace: Workspace, *, source: str | None = None) -> dict[str, object]:
    if not workspace.database_path.exists():
        raise LocalgraphError(f"database does not exist: {workspace.database_path}")
    source_scan = command_scan(workspace, source=source)
    with connect(workspace.database_path) as db:
        result = render_views(db, workspace, source_scan=source_scan)
    return result


def command_view_name(workspace: Workspace, kind: str, label: str, source_key: str) -> dict[str, object]:
    path = view_path(workspace.root, kind, label, source_key)
    return {
        "kind": kind,
        "label": label,
        "sourceKey": source_key,
        "path": str(path),
    }


def _resolve_workspace_path(workspace: Workspace, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else workspace.root / path


class LocalgraphError(Exception):
    """User-facing command error."""


if __name__ == "__main__":
    raise SystemExit(main())
