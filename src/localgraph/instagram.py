from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InstagramExportScan:
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


def scan_instagram_source(source_path: Path) -> dict[str, object]:
    source = source_path.expanduser().resolve()
    exports: dict[Path, InstagramExportScan] = {}
    if not source.exists():
        return {"sourceKind": "instagram", "sourcePath": str(source), "exports": [], "totalMessageFiles": 0}

    message_files = sorted(path for path in source.rglob("message_*.json") if path.is_file())
    for file_path in message_files:
        export_root = detect_export_root(source, file_path)
        relative_file = file_path.relative_to(export_root).as_posix()
        thread_folder = str(Path(relative_file).parent).replace("\\", "/")
        item = exports.get(export_root)
        if item is None:
            relative_export = export_root.relative_to(source).as_posix() if export_root != source else "."
            item = InstagramExportScan(
                name=export_root.name,
                path=str(export_root),
                relative_path=relative_export,
            )
            exports[export_root] = item
        item.message_files += 1
        item.thread_folders.add(thread_folder)
        modified = _iso_mtime(file_path)
        item.latest_modified_time = max(filter(None, [item.latest_modified_time, modified]))

    return {
        "sourceKind": "instagram",
        "sourcePath": str(source),
        "exports": [item.to_json() for _, item in sorted(exports.items(), key=lambda pair: pair[1].relative_path)],
        "totalMessageFiles": len(message_files),
    }


def detect_export_root(source_path: Path, file_path: Path) -> Path:
    parts = file_path.relative_to(source_path).parts
    if "your_instagram_activity" in parts:
        index = parts.index("your_instagram_activity")
        if index > 0:
            return source_path.joinpath(*parts[:index])
    if "messages" in parts:
        index = parts.index("messages")
        if index > 0:
            return source_path.joinpath(*parts[:index])
    return source_path


def _iso_mtime(path: Path) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
