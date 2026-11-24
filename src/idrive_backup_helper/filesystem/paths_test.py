from pathlib import Path

import pytest

from idrive_backup_helper.filesystem.paths import (
    STAGING_DIR_NAME,
    anon_key_path,
    browser_profile_dir,
    config_path,
    default_anon_log_dir,
    migrate_private_layout,
    playground_dir,
    runs_dir,
    staging_dir_for_destination,
)


def test_staging_dir_is_on_destination_volume() -> None:
    destination = Path("/mnt/external/backup")

    staging_dir = staging_dir_for_destination(destination)

    # Staging must be a child of the destination root so finalizing a download is
    # a same-volume rename rather than a cross-device copy.
    assert staging_dir == destination / STAGING_DIR_NAME
    assert staging_dir.parent == destination


def test_private_data_lives_under_a_secrets_folder(tmp_path: Path) -> None:
    # The agent policy denies reads under any `secrets/` folder; private run data
    # must stay inside one so agents only ever see the anonymized logs.
    for path in (
        browser_profile_dir(tmp_path),
        runs_dir(tmp_path),
        anon_key_path(tmp_path),
        config_path(tmp_path),
    ):
        assert "secrets" in path.relative_to(tmp_path).parts
    assert "secrets" not in default_anon_log_dir(tmp_path).relative_to(tmp_path).parts


def test_migrate_private_layout_moves_old_playground_folders(tmp_path: Path) -> None:
    old_profile = playground_dir(tmp_path) / "browser-state" / "idrive-chromium"
    old_runs = playground_dir(tmp_path) / "downloads"
    old_profile.mkdir(parents=True)
    old_runs.mkdir(parents=True)
    (old_profile / "Cookies").write_text("x", encoding="utf-8")
    (old_runs / "download-folder-run-1.ndjson").write_text("{}", encoding="utf-8")

    moved = migrate_private_layout(tmp_path)

    assert len(moved) == 2
    assert (browser_profile_dir(tmp_path) / "Cookies").exists()
    assert (runs_dir(tmp_path) / "download-folder-run-1.ndjson").exists()
    assert not old_runs.exists()
    assert not (playground_dir(tmp_path) / "browser-state").exists()
    assert migrate_private_layout(tmp_path) == []


def test_migrate_private_layout_refuses_to_merge(tmp_path: Path) -> None:
    (playground_dir(tmp_path) / "downloads").mkdir(parents=True)
    runs_dir(tmp_path).mkdir(parents=True)

    with pytest.raises(RuntimeError, match="Both"):
        migrate_private_layout(tmp_path)

    assert (playground_dir(tmp_path) / "downloads").exists()
