"""Decide, from a field's name, how its value is anonymized.

The rule is fail-closed: a text value under a field name this module does not
recognise becomes a token, never raw text. Numbers, booleans, and None carry no
private data and pass unchanged whatever the field is called.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Literal, cast

from idrive_backup_helper.runlog.anonymize import Anonymizer

type FieldKind = Literal["safe", "url", "path", "name", "text", "glob", "unknown"]

# Text fields that only ever hold this tool's own vocabulary.
SAFE_TEXT_FIELDS = frozenset(
    {
        "timestamp",
        "event",
        "source",
        "template",
        "mode",
        "overwrite",
        "browser_mode",
        "digest",
        "build_id",
        "run_id",
        "stream",
        "attempt",
        "state",
        "kind",
        "error_type",
        # Text this tool or Chromium builds without user data.
        "excluded_note",
        "cdp_version",
        "lock_files",
    }
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def snake_case(key: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", key).lower()


def field_kind(key: str) -> FieldKind:
    name = snake_case(key)
    if name in SAFE_TEXT_FIELDS:
        return "safe"
    if name in ("url", "href") or name.endswith(("_url", "_href")):
        return "url"
    if name.endswith(("patterns", "pattern")):
        return "glob"
    if name in ("path", "dir", "destination", "profile", "downloads") or name.endswith(
        ("_path", "_dir", "destination", "_paths")
    ):
        return "path"
    if name in ("name", "file", "folder", "title") or name.endswith(
        ("_name", "_names", "_title", "_titles", "_parts")
    ):
        return "name"
    if name in ("error", "reason", "message", "detail") or name.endswith(
        ("_error", "_reason")
    ):
        return "text"
    return "unknown"


def anonymize_value(anonymizer: Anonymizer, kind: FieldKind, value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return anonymize_fields(anonymizer, cast(Mapping[str, object], value))
    if isinstance(value, (list, tuple, set, frozenset)):
        items = cast(Iterable[object], value)
        return [anonymize_value(anonymizer, kind, item) for item in items]
    text = str(value)
    match kind:
        case "safe":
            return text
        case "url":
            return anonymizer.url(text)
        case "path":
            return anonymizer.path(text)
        case "name":
            return anonymizer.name(text)
        case "glob":
            return anonymizer.glob(text)
        case "text":
            return anonymizer.text(text)
        case "unknown":
            return anonymizer.name(text, prefix="x")


def anonymize_fields(
    anonymizer: Anonymizer, fields: Mapping[str, object]
) -> dict[str, object]:
    return {
        key: anonymize_value(anonymizer, field_kind(key), value)
        for key, value in fields.items()
    }
