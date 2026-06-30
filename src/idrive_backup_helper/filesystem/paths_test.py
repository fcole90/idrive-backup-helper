from pathlib import Path

from idrive_backup_helper.filesystem.paths import (
    STAGING_DIR_NAME,
    staging_dir_for_destination,
)


def test_staging_dir_is_on_destination_volume() -> None:
    destination = Path("/mnt/external/backup")

    staging_dir = staging_dir_for_destination(destination)

    # Staging must be a child of the destination root so finalizing a download is
    # a same-volume rename rather than a cross-device copy.
    assert staging_dir == destination / STAGING_DIR_NAME
    assert staging_dir.parent == destination
