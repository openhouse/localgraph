from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PRIVATE_DIRECTORY_NAMES = ("sources", "state", "objects", "views", "annotations", "exports")
VIEW_DIRECTORY_NAMES = ("people", "groups", "threads", "projects", "tags", "_system")


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def sources_dir(self) -> Path:
        return self.root / "sources"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def objects_dir(self) -> Path:
        return self.root / "objects"

    @property
    def views_dir(self) -> Path:
        return self.root / "views"

    @property
    def annotations_dir(self) -> Path:
        return self.root / "annotations"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def database_path(self) -> Path:
        return self.state_dir / "localgraph.sqlite"

    @property
    def config_path(self) -> Path:
        return self.root / "localgraph.config.json"

    @property
    def managed_directories(self) -> tuple[Path, ...]:
        return (
            self.sources_dir,
            self.state_dir,
            self.objects_dir,
            self.views_dir,
            self.annotations_dir,
            self.exports_dir,
        )

    @property
    def view_directories(self) -> tuple[Path, ...]:
        return tuple(self.views_dir / name for name in VIEW_DIRECTORY_NAMES)

    @property
    def instagram_source_dir(self) -> Path:
        return self.sources_dir / "instagram"

    @property
    def imessage_source_dir(self) -> Path:
        return self.sources_dir / "imessage"

    @property
    def imessage_chat_db_path(self) -> Path:
        return self.imessage_source_dir / "chat.db"

    def plan(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "configPath": str(self.config_path),
            "privateDirectories": [
                {"name": path.name, "path": str(path)}
                for path in self.managed_directories
            ],
            "viewDirectories": [
                {"name": path.name, "path": str(path)}
                for path in self.view_directories
            ],
        }

    def ensure_workspace(self, *, force: bool = False) -> None:
        _private_mkdir(self.root)
        visible_entries = [entry for entry in self.root.iterdir() if entry.name not in {".git", ".gitignore"}]
        if visible_entries and not force:
            expected = {path.name for path in self.managed_directories} | {
                "README.md",
                "docs",
                "pyproject.toml",
                "src",
                "tests",
                "localgraph.config.json",
                "PRIVATE-DATA-README.md",
            }
            unexpected = [entry for entry in visible_entries if entry.name not in expected]
            if unexpected:
                names = ", ".join(sorted(entry.name for entry in unexpected))
                raise ValueError(f"workspace has unexpected entries; pass --force to initialize anyway: {names}")
        for directory in self.managed_directories:
            _private_mkdir(directory)
        for directory in self.view_directories:
            _private_mkdir(directory)
        _private_mkdir(self.instagram_source_dir)
        _private_mkdir(self.imessage_source_dir)
        self._write_config(force=force)
        self._write_private_marker()

    def check(self) -> dict[str, str]:
        return {
            directory.name: "ok" if directory.is_dir() else "missing"
            for directory in self.managed_directories
        }

    def _write_private_marker(self) -> None:
        marker = self.root / "PRIVATE-DATA-README.md"
        if marker.exists():
            return
        marker.write_text(
            "# Local Private Data\n\n"
            "This workspace may contain private source exports, messages, media, "
            "indexes, and annotations. These directories are intentionally ignored "
            "by git.\n",
            encoding="utf-8",
        )
        os.chmod(marker, 0o600)

    def _write_config(self, *, force: bool) -> None:
        if self.config_path.exists() and not force:
            return
        config = {
            "formatVersion": 1,
            "root": str(self.root),
            "directories": {name: name for name in PRIVATE_DIRECTORY_NAMES},
            "views": {name: f"views/{name}" for name in VIEW_DIRECTORY_NAMES},
            "imports": {
                "instagram": {
                    "localPath": "sources/instagram",
                    "googleDriveLocalPath": None,
                    "googleDriveFolderId": None,
                },
                "imessage": {
                    "localPath": "sources/imessage/chat.db",
                    "defaultMacPath": "~/Library/Messages/chat.db",
                },
            },
        }
        self.config_path.write_text(f"{json.dumps(config, indent=2, sort_keys=True)}\n", encoding="utf-8")
        os.chmod(self.config_path, 0o600)


def _private_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
