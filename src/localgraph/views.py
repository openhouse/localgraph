from __future__ import annotations

from pathlib import Path

from .slug import stable_view_name

VIEW_KIND_TO_DIR = {
    "person": "people",
    "group": "groups",
    "thread": "threads",
    "project": "projects",
    "tag": "tags",
}


def view_kinds() -> tuple[str, ...]:
    return tuple(VIEW_KIND_TO_DIR)


def view_path(root: Path, kind: str, label: str, source_key: str | None = None) -> Path:
    try:
        directory = VIEW_KIND_TO_DIR[kind]
    except KeyError as exc:
        supported = ", ".join(view_kinds())
        raise ValueError(f"unsupported view kind: {kind}; supported kinds: {supported}") from exc
    return root.resolve() / "views" / directory / stable_view_name(label, source_key)
