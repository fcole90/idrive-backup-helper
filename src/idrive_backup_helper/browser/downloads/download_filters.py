from collections.abc import Sequence
from pathlib import PurePath


def validate_exclude_pattern(pattern: str) -> str:
    """Return ``pattern`` unchanged, or raise ``ValueError`` for unsupported syntax.

    Patterns follow the gitignore subset that ``PurePath.full_match`` implements.
    The gitignore-only markers below would otherwise silently never match, so they
    are rejected up front instead.
    """
    if not pattern:
        raise ValueError("exclude pattern must not be empty")
    if pattern.startswith("/"):
        raise ValueError(
            f"exclude pattern {pattern!r} starts with '/': patterns containing '/' "
            "are already anchored at the destination root, so drop the leading '/'"
        )
    if pattern.endswith("/"):
        raise ValueError(
            f"exclude pattern {pattern!r} ends with '/': patterns match files only; "
            "use 'folder/**' to exclude everything under a folder"
        )
    if pattern.startswith("!"):
        raise ValueError(
            f"exclude pattern {pattern!r} starts with '!': negation is not "
            "supported; use '[!]' to match a literal leading '!'"
        )
    return pattern


def matching_exclude_pattern(
    patterns: Sequence[str],
    *,
    file_name: str,
    relative_path: str,
) -> str | None:
    """Return the first pattern that excludes this file, or ``None``.

    Matching is case-insensitive. A pattern without '/' matches the file name at
    any depth; a pattern with '/' matches the path relative to the destination
    root, where '*' stays within one segment and '**' spans any number of them.
    """
    for pattern in patterns:
        target = relative_path if "/" in pattern else file_name
        if PurePath(target).full_match(pattern, case_sensitive=False):
            return pattern
    return None
