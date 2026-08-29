from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_MEDIA_SUFFIXES = {
    ".aac",
    ".gif",
    ".heic",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".png",
    ".wav",
    ".webm",
}
_TIMESTAMP = re.compile(
    r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})"
    r"(?:\s+at)?\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2}))?\s*(?P<meridiem>[AaPp][Mm])"
)


def read_instagram_html(file_path: Path) -> dict[str, object]:
    """Read one Meta Instagram message HTML page into the JSON import shape."""
    parser = _InstagramMessageHTMLParser()
    parser.feed(_read_html_text(file_path))
    parser.close()
    return parser.payload()


def _read_html_text(file_path: Path) -> str:
    raw = file_path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


class _InstagramMessageHTMLParser(HTMLParser):
    """Parse stable semantic class hooks in Meta's downloadable HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.title_parts: list[str] = []
        self.title_depth: int | None = None
        self.current: dict[str, Any] | None = None
        self.message_depth: int | None = None
        self.sender_depth: int | None = None
        self.content_depth: int | None = None
        self.timestamp_depth: int | None = None
        self.reaction_depth: int | None = None
        self.messages: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        node_depth = self.depth + 1
        if tag not in _VOID_TAGS:
            self.depth = node_depth
        attr = {name.lower(): value or "" for name, value in attrs}
        classes = set(attr.get("class", "").split())

        if tag == "h1" and self.current is None and not self.title_parts:
            self.title_depth = node_depth
        if "_a6-g" in classes and self.current is None:
            self.current = {
                "sender": [],
                "content": [],
                "timestamp": [],
                "links": [],
                "photos": [],
                "videos": [],
                "audio_files": [],
                "files": [],
            }
            self.message_depth = node_depth
        if self.current is None:
            return
        if "_a6-h" in classes:
            self.sender_depth = node_depth
        if "_a6-p" in classes:
            self.content_depth = node_depth
        if "_a6-o" in classes:
            self.timestamp_depth = node_depth
        if "_a6-q" in classes:
            self.reaction_depth = node_depth

        if self.content_depth is not None and self.reaction_depth is None:
            if tag == "br":
                self.current["content"].append("\n")
            self._record_reference(tag, attr)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self.title_depth is not None and self.current is None:
            self.title_parts.append(data)
        if self.current is None:
            return
        if self.sender_depth is not None:
            self.current["sender"].append(data)
        elif self.timestamp_depth is not None:
            self.current["timestamp"].append(data)
        elif self.content_depth is not None and self.reaction_depth is None:
            self.current["content"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.title_depth == self.depth:
            self.title_depth = None
        if self.sender_depth == self.depth:
            self.sender_depth = None
        if self.content_depth == self.depth:
            self.content_depth = None
        if self.timestamp_depth == self.depth:
            self.timestamp_depth = None
        if self.reaction_depth == self.depth:
            self.reaction_depth = None
        if self.message_depth == self.depth and self.current is not None:
            self._finish_message()
        if tag not in _VOID_TAGS:
            self.depth = max(0, self.depth - 1)

    def payload(self) -> dict[str, object]:
        if self.current is not None:
            self._finish_message()
        title = _clean_parts(self.title_parts)
        participants: list[str] = []
        for message in self.messages:
            sender = str(message.get("sender_name") or "").strip()
            if sender and sender not in participants:
                participants.append(sender)
        return {
            "participants": [{"name": name} for name in participants],
            "title": title or None,
            "messages": self.messages,
            "source_format": "meta-html",
        }

    def _record_reference(self, tag: str, attr: dict[str, str]) -> None:
        assert self.current is not None
        if tag == "a" and attr.get("href"):
            reference = attr["href"].strip()
            if _looks_like_media(reference):
                self.current["files"].append({"uri": reference})
            elif reference and reference not in self.current["links"]:
                self.current["links"].append(reference)
        elif tag == "img" and attr.get("src"):
            self.current["photos"].append({"uri": attr["src"].strip()})
        elif tag == "video" and attr.get("src"):
            self.current["videos"].append({"uri": attr["src"].strip()})
        elif tag == "audio" and attr.get("src"):
            self.current["audio_files"].append({"uri": attr["src"].strip()})
        elif tag == "source" and attr.get("src"):
            reference = attr["src"].strip()
            bucket = _media_bucket(reference)
            self.current[bucket].append({"uri": reference})

    def _finish_message(self) -> None:
        assert self.current is not None
        sender = _clean_parts(self.current["sender"])
        content = _clean_content(self.current["content"])
        links = [str(link) for link in self.current["links"] if str(link)]
        if links:
            content = "\n".join(part for part in [content, *links] if part)
        timestamp_text = _clean_parts(self.current["timestamp"])
        has_media = any(self.current[key] for key in ("photos", "videos", "audio_files", "files"))
        if not timestamp_text and not content and not has_media:
            self._reset_message()
            return
        message: dict[str, object] = {
            "sender_name": sender or "Unknown Instagram Sender",
            "timestamp_ms": _timestamp_ms(timestamp_text),
            "content": content or None,
            "html_timestamp_text": timestamp_text or None,
            "source_format": "meta-html",
        }
        for key in ("photos", "videos", "audio_files", "files"):
            if self.current[key]:
                message[key] = self.current[key]
        self.messages.append(message)

        self._reset_message()

    def _reset_message(self) -> None:
        self.current = None
        self.message_depth = None
        self.sender_depth = None
        self.content_depth = None
        self.timestamp_depth = None
        self.reaction_depth = None


def _timestamp_ms(value: str) -> int | None:
    match = _TIMESTAMP.search(value.replace("\u202f", " ").replace("\u00a0", " "))
    if match is None:
        return None
    month = datetime.strptime(match.group("month").title(), "%b").month
    hour = int(match.group("hour")) % 12
    if match.group("meridiem").lower() == "pm":
        hour += 12
    parsed = datetime(
        int(match.group("year")),
        month,
        int(match.group("day")),
        hour,
        int(match.group("minute")),
        int(match.group("second") or 0),
        tzinfo=timezone.utc,
    )
    return int(parsed.timestamp() * 1000)


def _clean_parts(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _clean_content(parts: list[str]) -> str:
    text = " ".join(parts)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _looks_like_media(reference: str) -> bool:
    return Path(reference.split("?", 1)[0]).suffix.lower() in _MEDIA_SUFFIXES


def _media_bucket(reference: str) -> str:
    suffix = Path(reference.split("?", 1)[0]).suffix.lower()
    if suffix in {".jpeg", ".jpg", ".png", ".gif", ".heic"}:
        return "photos"
    if suffix in {".mov", ".mp4", ".webm"}:
        return "videos"
    if suffix in {".aac", ".m4a", ".mp3", ".wav"}:
        return "audio_files"
    return "files"
