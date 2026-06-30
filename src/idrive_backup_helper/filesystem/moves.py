import os
from pathlib import Path


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

    # staged_path lives on the destination's volume, so this is an atomic
    # same-volume rename (no read+write copy, no second antivirus scan). A
    # cross-device staged_path would raise OSError here, which is the loud
    # failure we want if staging is ever misconfigured onto another disk.
    os.replace(staged_path, final_path)
    return final_path


def clear_staging_dir(staging_dir: Path) -> list[str]:
    # An interrupted download can leave a partial artifact (e.g. a *.crdownload
    # or a save_as partial) in the staging dir; without this they accumulate on
    # the destination volume across runs. Best-effort at run start: a directory
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
