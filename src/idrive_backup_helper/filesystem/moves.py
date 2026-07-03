import errno
import os
import shutil
from pathlib import Path

# WinError 17 (ERROR_NOT_SAME_DEVICE) is the Windows equivalent of POSIX EXDEV:
# os.replace raises it when source and destination live on different drives.
_WINDOWS_CROSS_DEVICE_ERROR = 17


def move_download_to_destination(
    staged_path: Path,
    destination_dir: Path,
    final_name: str,
    *,
    replace_existing: bool,
) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    final_path = destination_dir / final_name

    if final_path.exists() and not replace_existing:
        raise FileExistsError(f"Destination file already exists: {final_path}")

    # When staged_path lives on the destination's volume this is an atomic
    # same-volume rename (no read+write copy, no second antivirus scan). But the
    # stage does not always land on the destination volume -- notably Playwright
    # on Windows writes the download into a temp dir on the system drive (C:)
    # regardless of downloads_path -- so a cross-device rename raises OSError. In
    # that case fall back to a copy+delete move rather than failing the download.
    try:
        os.replace(staged_path, final_path)
    except OSError as error:
        if not _is_cross_device_error(error):
            raise
        shutil.move(str(staged_path), str(final_path))
    return final_path


def _is_cross_device_error(error: OSError) -> bool:
    return (
        error.errno == errno.EXDEV
        or getattr(error, "winerror", None) == _WINDOWS_CROSS_DEVICE_ERROR
    )


def clear_staging_dir(staging_dir: Path) -> list[str]:
    # An interrupted download can leave a partial artifact (e.g. a *.crdownload
    # or a save_as partial) in the staging dir; without this they accumulate
    # across runs. Best-effort at run start: a directory
    # or a still-locked file is skipped rather than treated as fatal. Returns the
    # names removed so the caller can log them.
    if not staging_dir.exists():
        return []

    removed_names: list[str] = []
    for entry in staging_dir.iterdir():
        try:
            entry.unlink()
        except OSError:
            continue
        removed_names.append(entry.name)
    return removed_names
