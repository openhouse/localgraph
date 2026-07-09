from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .daily import configure_drive_source, install_launch_agent, run_daily_instagram_import
from .imessage import import_imessage_source
from .instagram import import_instagram_source
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

    configure_drive = commands.add_parser("configure-drive", help="Persist a local Google Drive Desktop Instagram source path.")
    configure_drive.add_argument("local_path", help="Local Drive Desktop path containing materialized Instagram transfer exports.")

    import_command = commands.add_parser("import", help="Import private message bodies into canonical SQLite state.")
    import_command.add_argument(
        "source_kind",
        nargs="?",
        choices=("all", "instagram", "imessage"),
        default="all",
        help="Source to import. Defaults to all configured local sources.",
    )
    import_command.add_argument("--source", help="Override source path when importing one source kind.")
    import_command.add_argument("--instagram-source", help="Instagram source directory. Defaults to sources/instagram.")
    import_command.add_argument("--imessage-source", help="iMessage source directory or chat.db. Defaults to sources/imessage.")

    daily_import = commands.add_parser("daily-import", help="Run the daily local Instagram import.")
    daily_import.add_argument("--source", help="Override configured Drive/Desktop source path.")
    daily_import.add_argument("--all-instagram-exports", action="store_true", help="Import all materialized Instagram exports.")

    install_daily = commands.add_parser("install-daily-import", help="Create a macOS LaunchAgent plist for daily import.")
    install_daily.add_argument("--output", help="Optional plist output path. Defaults to ~/Library/LaunchAgents.")
    install_daily.add_argument("--label", default="com.localgraph.daily-import", help="LaunchAgent label.")
    install_daily.add_argument("--hour", type=int, default=8, help="Daily run hour, local time.")
    install_daily.add_argument("--minute", type=int, default=15, help="Daily run minute, local time.")

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
        elif args.command == "configure-drive":
            summary = command_configure_drive(workspace, local_path=args.local_path)
        elif args.command == "import":
            summary = command_import(
                workspace,
                source_kind=args.source_kind,
                source=args.source,
                instagram_source=args.instagram_source,
                imessage_source=args.imessage_source,
            )
        elif args.command == "daily-import":
            summary = command_daily_import(
                workspace,
                source=args.source,
                all_instagram_exports=args.all_instagram_exports,
            )
        elif args.command == "install-daily-import":
            summary = command_install_daily_import(
                workspace,
                output=args.output,
                label=args.label,
                hour=args.hour,
                minute=args.minute,
            )
        elif args.command == "render":
            summary = command_render(workspace, source=args.source)
        elif args.command == "view-name":
            summary = command_view_name(workspace, args.kind, args.label, args.source_key)
        else:
            parser.error(f"unknown command: {args.command}")
    except (LocalgraphError, ValueError) as exc:
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


def command_configure_drive(workspace: Workspace, *, local_path: str) -> dict[str, object]:
    with connect(workspace.database_path) as db:
        initialize_schema(db)
        return configure_drive_source(db, workspace, Path(local_path))


def command_import(
    workspace: Workspace,
    *,
    source_kind: str,
    source: str | None,
    instagram_source: str | None,
    imessage_source: str | None,
) -> dict[str, object]:
    if not workspace.database_path.exists():
        raise LocalgraphError(f"database does not exist: {workspace.database_path}")
    if source and source_kind == "all":
        raise LocalgraphError("--source can only be used with 'import instagram' or 'import imessage'")

    results: dict[str, object] = {}
    with connect(workspace.database_path) as db:
        initialize_schema(db)
        if source_kind in {"all", "instagram"}:
            source_path = _resolve_import_source(
                workspace,
                explicit=source if source_kind == "instagram" else instagram_source,
                default=workspace.instagram_source_dir,
            )
            results["instagram"] = import_instagram_source(db, source_path)
        if source_kind in {"all", "imessage"}:
            source_path = _resolve_import_source(
                workspace,
                explicit=source if source_kind == "imessage" else imessage_source,
                default=workspace.imessage_source_dir,
            )
            results["imessage"] = import_imessage_source(db, source_path)

    return {
        "root": str(workspace.root),
        "database": str(workspace.database_path),
        "results": results,
    }


def command_daily_import(workspace: Workspace, *, source: str | None, all_instagram_exports: bool) -> dict[str, object]:
    if not workspace.database_path.exists():
        raise LocalgraphError(f"database does not exist: {workspace.database_path}")
    source_path = Path(source).expanduser() if source else None
    if source_path is not None and not source_path.is_absolute():
        source_path = workspace.root / source_path
    with connect(workspace.database_path) as db:
        initialize_schema(db)
        return run_daily_instagram_import(
            db,
            workspace,
            source_path=source_path,
            all_instagram_exports=all_instagram_exports,
        )


def command_install_daily_import(
    workspace: Workspace,
    *,
    output: str | None,
    label: str,
    hour: int,
    minute: int,
) -> dict[str, object]:
    output_path = Path(output).expanduser() if output else None
    if output_path is not None and not output_path.is_absolute():
        output_path = workspace.root / output_path
    return install_launch_agent(workspace, output_path=output_path, label=label, hour=hour, minute=minute)


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


def _resolve_import_source(workspace: Workspace, *, explicit: str | None, default: Path) -> Path:
    source_path = Path(explicit).expanduser() if explicit else default
    if not source_path.is_absolute():
        source_path = workspace.root / source_path
    return source_path


class LocalgraphError(Exception):
    """User-facing command error."""


if __name__ == "__main__":
    raise SystemExit(main())
