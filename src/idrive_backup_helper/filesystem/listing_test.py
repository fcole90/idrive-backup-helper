from pathlib import Path

import pytest

import idrive_backup_helper.filesystem.listing as listing
from idrive_backup_helper.filesystem.listing import (
    DirectoryListingCache,
    existing_entry_names,
)


def test_existing_entry_names_returns_direct_entries(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "sub").mkdir()

    assert existing_entry_names(tmp_path) == {"a.txt", "b.txt", "sub"}


def test_existing_entry_names_returns_empty_for_missing_directory(
    tmp_path: Path,
) -> None:
    assert existing_entry_names(tmp_path / "does-not-exist") == set()


def test_directory_listing_cache_reports_presence(tmp_path: Path) -> None:
    (tmp_path / "present.txt").write_text("x", encoding="utf-8")
    cache = DirectoryListingCache()

    assert cache.contains(tmp_path / "present.txt") is True
    assert cache.contains(tmp_path / "absent.txt") is False


def test_directory_listing_cache_snapshot_is_not_refreshed(tmp_path: Path) -> None:
    cache = DirectoryListingCache()
    # First query scans the directory while it is still empty.
    assert cache.contains(tmp_path / "later.txt") is False

    (tmp_path / "later.txt").write_text("x", encoding="utf-8")

    # A file created after the directory was first scanned is not seen, by design.
    assert cache.contains(tmp_path / "later.txt") is False


def test_directory_listing_cache_scans_each_directory_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "one.txt").write_text("1", encoding="utf-8")
    (tmp_path / "two.txt").write_text("2", encoding="utf-8")

    scanned: list[Path] = []

    def counting_existing_entry_names(directory: Path) -> set[str]:
        scanned.append(directory)
        return existing_entry_names(directory)

    monkeypatch.setattr(listing, "existing_entry_names", counting_existing_entry_names)
    cache = listing.DirectoryListingCache()

    assert cache.contains(tmp_path / "one.txt") is True
    assert cache.contains(tmp_path / "two.txt") is True

    assert scanned == [tmp_path]
