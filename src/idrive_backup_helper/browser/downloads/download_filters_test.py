import pytest

from idrive_backup_helper.browser.downloads.download_filters import (
    matching_exclude_pattern,
    validate_exclude_pattern,
)


@pytest.mark.parametrize(
    ("pattern", "file_name", "relative_path", "expected"),
    [
        ("*crypt*", "backup.CRYPT.bin", "a/backup.CRYPT.bin", True),
        ("*.mkv", "movie.MKV", "Videos/2020/movie.MKV", True),
        ("*.mkv", "movie.mp4", "Videos/movie.mp4", False),
        ("Videos/*", "a.mkv", "Videos/a.mkv", True),
        ("Videos/*", "a.mkv", "Videos/sub/a.mkv", False),
        ("Videos/*", "a.mkv", "x/Videos/a.mkv", False),
        ("Videos/**", "a.mkv", "Videos/sub/a.mkv", True),
        ("**/Videos/**", "a.mkv", "x/Videos/a.mkv", True),
        ("**/Videos/**", "a.mkv", "Videos/a.mkv", True),
    ],
)
def test_matching_exclude_pattern(
    pattern: str,
    file_name: str,
    relative_path: str,
    expected: bool,
) -> None:
    match = matching_exclude_pattern(
        [pattern], file_name=file_name, relative_path=relative_path
    )

    assert (match == pattern) is expected


def test_matching_exclude_pattern_returns_first_match() -> None:
    match = matching_exclude_pattern(
        ["*.txt", "*crypt*", "*.bin"],
        file_name="data.crypt.bin",
        relative_path="data.crypt.bin",
    )

    assert match == "*crypt*"


def test_matching_exclude_pattern_without_patterns_matches_nothing() -> None:
    assert matching_exclude_pattern([], file_name="a", relative_path="a") is None


@pytest.mark.parametrize("pattern", ["*.mkv", "Videos/**", "[!]draft*"])
def test_validate_exclude_pattern_accepts_supported_syntax(pattern: str) -> None:
    assert validate_exclude_pattern(pattern) == pattern


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("", "must not be empty"),
        ("/Videos/*", "starts with '/'"),
        ("Videos/", "ends with '/'"),
        ("!keep.txt", "negation is not supported"),
    ],
)
def test_validate_exclude_pattern_rejects_gitignore_only_syntax(
    pattern: str, reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        validate_exclude_pattern(pattern)
