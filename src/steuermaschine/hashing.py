"""Digests of exact bytes, for review stops and receipts."""

from __future__ import annotations

import hashlib
import json


def canonical_sha256(text: str) -> str:
    """SHA256 of canonical JSON: sorted keys, compact separators, ASCII escapes, UTF-8, no trailing newline.
    Two documents that differ only in formatting have the same digest. Raises ValueError for text that is not JSON."""
    return hashlib.sha256(json.dumps(json.loads(text), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
