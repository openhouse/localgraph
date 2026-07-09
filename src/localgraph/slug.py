from __future__ import annotations

import hashlib
import re
import unicodedata


def slugify(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower().replace("&", " and ")).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug or "untitled"


def stable_hash(value: str | None, *, length: int = 10) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:length]


def stable_view_name(label: str, source_key: str | None = None, *, suffix_length: int = 8) -> str:
    return f"{slugify(label)}--{stable_hash(source_key or label, length=suffix_length)}"
