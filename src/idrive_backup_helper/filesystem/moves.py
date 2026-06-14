import shutil
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
    temporary_final_path = final_path.with_name(f".{final_path.name}.partial")

    if final_path.exists() and not replace_existing:
        raise FileExistsError(f"Destination file already exists: {final_path}")

    if temporary_final_path.exists():
        temporary_final_path.unlink()

    shutil.move(str(staged_path), str(temporary_final_path))
    temporary_final_path.replace(final_path)
    return final_path
