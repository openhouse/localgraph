from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .paths import Workspace
from .render import render_views
from .schema import connect, initialize_schema


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

    init = commands.add_parser("init", help="Create workspace directories and SQLite schema.")
    init.add_argument("--force", action="store_true", help="Allow initialization in a non-empty directory.")

    commands.add_parser("doctor", help="Check workspace directories and database schema.")
    commands.add_parser("render", help="Render filesystem views from canonical SQLite state.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = Workspace(Path(args.root).expanduser().resolve())

    try:
        if args.command == "init":
            summary = command_init(workspace, force=args.force)
        elif args.command == "doctor":
            summary = command_doctor(workspace)
        elif args.command == "render":
            summary = command_render(workspace)
        else:
            parser.error(f"unknown command: {args.command}")
    except (LocalgraphError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def command_init(workspace: Workspace, *, force: bool = False) -> dict[str, object]:
    workspace.ensure_workspace(force=force)
    with connect(workspace.database_path) as db:
        initialize_schema(db)
    return {
        "root": str(workspace.root),
        "database": str(workspace.database_path),
        "created": [str(path) for path in workspace.managed_directories],
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


def command_render(workspace: Workspace) -> dict[str, object]:
    if not workspace.database_path.exists():
        raise LocalgraphError(f"database does not exist: {workspace.database_path}")
    with connect(workspace.database_path) as db:
        result = render_views(db, workspace)
    return result


class LocalgraphError(Exception):
    """User-facing command error."""


if __name__ == "__main__":
    raise SystemExit(main())
