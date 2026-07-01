import json
from pathlib import Path
from typing import cast

from idrive_backup_helper.browser.downloads.download_cache import (
    load_folder_entries_cache,
    write_folder_entries_cache,
)
from idrive_backup_helper.browser.downloads.download_models import (
    RemoteEntries,
    RemoteFile,
    RemoteFolder,
)

_URL = "https://www.idrive.com/idrive/home/device/EmptyFolder"


def _read_cache_payload(downloads_dir: Path) -> dict[str, object]:
    cache_files = list((downloads_dir / "folder-cache").glob("*.json"))
    assert len(cache_files) == 1
    payload = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def test_write_read_empty_folder_is_confirmed_empty(tmp_path: Path) -> None:
    write_folder_entries_cache(tmp_path, _URL, RemoteEntries(files=[], folders=[]))

    cached = load_folder_entries_cache(tmp_path, _URL)

    assert cached is not None
    assert cached.entries.files == []
    assert cached.entries.folders == []
    assert cached.confirmed_empty is True


def test_write_read_non_empty_folder_is_not_confirmed_empty(tmp_path: Path) -> None:
    entries = RemoteEntries(
        files=[
            RemoteFile(
                file_name="song.mp3",
                row_index=0,
                server_size_text="1 MB",
                server_modified_text="2026-06-01",
            )
        ],
        folders=[RemoteFolder(folder_name="Sub", href=f"{_URL}/Sub")],
    )

    write_folder_entries_cache(tmp_path, _URL, entries)
    cached = load_folder_entries_cache(tmp_path, _URL)

    assert cached is not None
    assert [f.file_name for f in cached.entries.files] == ["song.mp3"]
    assert [f.folder_name for f in cached.entries.folders] == ["Sub"]
    assert cached.confirmed_empty is False


def test_legacy_empty_cache_without_field_is_untrusted(tmp_path: Path) -> None:
    # A cache written before the confirmedEmpty field existed must not be taken as
    # a confirmed-empty folder: dropping the field mimics that legacy payload.
    write_folder_entries_cache(tmp_path, _URL, RemoteEntries(files=[], folders=[]))
    cache_path = next((tmp_path / "folder-cache").glob("*.json"))
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    del payload["confirmedEmpty"]
    cache_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    cached = load_folder_entries_cache(tmp_path, _URL)

    assert cached is not None
    assert cached.confirmed_empty is False
    # The current-version normalized-key entry must not be re-stamped (which would
    # promote the untrusted empty back to confirmedEmpty=True).
    assert "confirmedEmpty" not in _read_cache_payload(tmp_path)


def test_missing_cache_returns_none(tmp_path: Path) -> None:
    assert load_folder_entries_cache(tmp_path, _URL) is None
