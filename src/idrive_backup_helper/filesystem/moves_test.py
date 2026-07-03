import errno
from pathlib import Path

import pytest

from idrive_backup_helper.filesystem import moves
from idrive_backup_helper.filesystem.moves import (
    clear_staging_dir,
    move_download_to_destination,
)


def test_move_download_to_destination_creates_destination_and_moves_file(
    tmp_path: Path,
) -> None:
    staged_path = tmp_path / "staging" / "example.txt"
    staged_path.parent.mkdir(parents=True)
    staged_path.write_text("payload", encoding="utf-8")

    destination_dir = tmp_path / "destination"
    final_path = move_download_to_destination(
        staged_path,
        destination_dir,
        "example.txt",
        replace_existing=False,
    )

    assert final_path == destination_dir / "example.txt"
    assert final_path.read_text(encoding="utf-8") == "payload"
    assert not staged_path.exists()


def test_move_download_to_destination_rejects_existing_without_replace(
    tmp_path: Path,
) -> None:
    staged_path = tmp_path / "staging" / "example.txt"
    staged_path.parent.mkdir(parents=True)
    staged_path.write_text("new payload", encoding="utf-8")

    destination_dir = tmp_path / "destination"
    destination_dir.mkdir()
    existing_path = destination_dir / "example.txt"
    existing_path.write_text("old payload", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        move_download_to_destination(
            staged_path,
            destination_dir,
            "example.txt",
            replace_existing=False,
        )


def test_move_download_to_destination_replaces_existing_file(tmp_path: Path) -> None:
    staged_path = tmp_path / "staging" / "example.txt"
    staged_path.parent.mkdir(parents=True)
    staged_path.write_text("new payload", encoding="utf-8")

    destination_dir = tmp_path / "destination"
    destination_dir.mkdir()
    existing_path = destination_dir / "example.txt"
    existing_path.write_text("old payload", encoding="utf-8")

    final_path = move_download_to_destination(
        staged_path,
        destination_dir,
        "example.txt",
        replace_existing=True,
    )

    assert final_path.read_text(encoding="utf-8") == "new payload"


def test_move_download_to_destination_falls_back_to_copy_across_drives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate a stage on a different drive (e.g. Playwright's C: temp with a D:
    # destination): os.replace refuses the cross-device move, so we copy instead.
    staged_path = tmp_path / "staging" / "example.txt"
    staged_path.parent.mkdir(parents=True)
    staged_path.write_text("payload", encoding="utf-8")

    def fake_replace(src: str, dst: str) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(moves.os, "replace", fake_replace)

    destination_dir = tmp_path / "destination"
    final_path = move_download_to_destination(
        staged_path,
        destination_dir,
        "example.txt",
        replace_existing=False,
    )

    assert final_path == destination_dir / "example.txt"
    assert final_path.read_text(encoding="utf-8") == "payload"
    assert not staged_path.exists()


def test_move_download_to_destination_reraises_non_cross_device_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged_path = tmp_path / "staging" / "example.txt"
    staged_path.parent.mkdir(parents=True)
    staged_path.write_text("payload", encoding="utf-8")

    def fake_replace(src: str, dst: str) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(moves.os, "replace", fake_replace)

    with pytest.raises(OSError, match="Permission denied"):
        move_download_to_destination(
            staged_path,
            tmp_path / "destination",
            "example.txt",
            replace_existing=False,
        )


def test_clear_staging_dir_removes_leftover_files(tmp_path: Path) -> None:
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "guid-1.crdownload").write_text("partial", encoding="utf-8")
    (staging_dir / "guid-2").write_text("partial", encoding="utf-8")

    removed = clear_staging_dir(staging_dir)

    assert sorted(removed) == ["guid-1.crdownload", "guid-2"]
    assert list(staging_dir.iterdir()) == []


def test_clear_staging_dir_is_noop_when_missing(tmp_path: Path) -> None:
    assert clear_staging_dir(tmp_path / "does-not-exist") == []
