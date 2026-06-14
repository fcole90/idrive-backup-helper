from pathlib import Path

import pytest

from idrive_backup_helper.filesystem.moves import move_download_to_destination


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
