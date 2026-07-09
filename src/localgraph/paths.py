from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
    def database_path(self) -> Path:
        return self.state_dir / "localgraph.sqlite"

    @property
    def managed_directories(self) -> tuple[Path, ...]:
        return (
            self.sources_dir,
            self.state_dir,
            self.objects_dir,
            self.views_dir,
            self.annotations_dir,
        )

    def ensure_workspace(self, *, force: bool = False) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        visible_entries = [entry for entry in self.root.iterdir() if entry.name not in {".git", ".gitignore"}]
        if visible_entries and not force:
            expected = {path.name for path in self.managed_directories} | {"README.md", "docs", "pyproject.toml", "src", "tests"}
            unexpected = [entry for entry in visible_entries if entry.name not in expected]
            if unexpected:
                names = ", ".join(sorted(entry.name for entry in unexpected))
                raise ValueError(f"workspace has unexpected entries; pass --force to initialize anyway: {names}")
        for directory in self.managed_directories:
            directory.mkdir(parents=True, exist_ok=True)
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
