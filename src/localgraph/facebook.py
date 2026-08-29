from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


FACEBOOK_EXPORT_ACCOUNT_PATTERN = re.compile(
    r"^facebook-(?P<account>.+?)-\d{4}-\d{2}-\d{2}(?:-|$)",
    re.IGNORECASE,
)


@dataclass
class FacebookExportScan:
    name: str
    path: str
    relative_path: str
    message_files: int = 0
    thread_folders: set[str] = field(default_factory=set)
    latest_modified_time: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "relativePath": self.relative_path,
            "messageFiles": self.message_files,
            "threadFolders": sorted(self.thread_folders),
            "latestModifiedTime": self.latest_modified_time,
        }


def scan_facebook_source(source_path: Path) -> dict[str, object]:
    """Inventory Facebook message packets without opening their JSON bodies."""
    source = source_path.expanduser().resolve()
    exports: dict[Path, FacebookExportScan] = {}
    if not source.exists():
        return {"sourceKind": "facebook", "sourcePath": str(source), "exports": [], "totalMessageFiles": 0}

    message_files = facebook_message_files(source)
    for file_path in message_files:
        export_root = detect_facebook_export_root(source, file_path)
        relative_file = file_path.relative_to(export_root).as_posix()
        item = exports.get(export_root)
        if item is None:
            relative_export = export_root.relative_to(source).as_posix() if export_root != source else "."
            item = FacebookExportScan(
                name=export_root.name,
                path=str(export_root),
                relative_path=relative_export,
            )
            exports[export_root] = item
        item.message_files += 1
        item.thread_folders.add(str(Path(relative_file).parent).replace("\\", "/"))
        modified = _iso_mtime(file_path)
        item.latest_modified_time = max(filter(None, [item.latest_modified_time, modified]))

    return {
        "sourceKind": "facebook",
        "sourcePath": str(source),
        "exports": [item.to_json() for _, item in sorted(exports.items(), key=lambda pair: pair[1].relative_path)],
        "totalMessageFiles": len(message_files),
    }


def facebook_message_files(source_path: Path) -> list[Path]:
    """List Facebook message JSON while following only acyclic directory symlinks."""
    source = source_path.expanduser().resolve()
    if not source.exists():
        return []
    seen_directories: set[Path] = set()
    message_files: list[Path] = []
    for root, directory_names, file_names in os.walk(source, followlinks=True):
        root_path = Path(root)
        resolved_root = root_path.resolve()
        if resolved_root in seen_directories:
            directory_names[:] = []
            continue
        seen_directories.add(resolved_root)
        directory_names[:] = [
            name for name in directory_names if (root_path / name).resolve() not in seen_directories
        ]
        relative_parts = root_path.relative_to(source).parts
        if "messages" not in relative_parts and root_path != source:
            continue
        for name in file_names:
            if name == "message.json" or re.fullmatch(r"message_\d+\.json", name):
                candidate = root_path / name
                if candidate.is_file():
                    message_files.append(candidate)
    return sorted(message_files)


def detect_facebook_export_root(source_path: Path, file_path: Path) -> Path:
    parts = file_path.relative_to(source_path).parts
    if "your_facebook_activity" in parts:
        index = parts.index("your_facebook_activity")
        return source_path.joinpath(*parts[:index]) if index > 0 else source_path
    if "messages" in parts:
        index = parts.index("messages")
        return source_path.joinpath(*parts[:index]) if index > 0 else source_path
    return source_path


def facebook_export_account_key(export_name: str) -> str | None:
    match = FACEBOOK_EXPORT_ACCOUNT_PATTERN.match(export_name.strip())
    if match is None:
        return None
    account = match.group("account").strip().lower()
    return account or None


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
