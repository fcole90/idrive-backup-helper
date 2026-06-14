#!/usr/bin/env python3
"""Wolf File Verifier: Phase 2 Diff Engine for precise file-level validation."""

import argparse
import json
import sys
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast


@dataclass(frozen=True)
class FileEntry:
    file_name: str
    server_size: Optional[int]

    @classmethod
    def from_payload(cls, raw_item: object) -> "FileEntry":
        if not isinstance(raw_item, dict):
            raise ValueError("File entry must be an object.")

        raw_item = cast(dict[str, object], raw_item)
        file_name = raw_item.get("fileName")
        if not isinstance(file_name, str):
            raise ValueError("File missing 'fileName' string.")

        raw_size = raw_item.get("serverSize")
        server_size = _parse_size(raw_size) if isinstance(raw_size, str) else None

        return cls(file_name=file_name, server_size=server_size)


@dataclass(frozen=True)
class MappedFolder:
    absolute_path: str
    files: list[FileEntry]
    error: Optional[str]

    @classmethod
    def from_payload(cls, raw_item: object) -> "MappedFolder":
        if not isinstance(raw_item, dict):
            raise ValueError("Mapped folder entry must be an object.")

        raw_item = cast(dict[str, object], raw_item)

        abs_path = raw_item.get("absolutePath")
        if not isinstance(abs_path, str):
            raise ValueError("Mapped folder missing 'absolutePath'.")

        raw_files = raw_item.get("files", [])
        if not isinstance(raw_files, list):
            raw_files = []

        files = [FileEntry.from_payload(f) for f in raw_files]

        error = raw_item.get("error")
        error_str = str(error) if error is not None else None

        return cls(absolute_path=abs_path, files=files, error=error_str)


@dataclass
class DirectoryDeficit:
    absolute_path: str
    missing_files: list[str]


def _parse_size(size_str: str) -> Optional[int]:
    clean_str = size_str.strip().upper().replace(",", "")
    if not clean_str or clean_str == "-":
        return None

    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}

    parts = clean_str.split()
    if len(parts) == 2:
        try:
            val = float(parts[0])
            unit = parts[1]
            return int(val * multipliers.get(unit, 1))
        except ValueError:
            return None

    for unit, multiplier in multipliers.items():
        if clean_str.endswith(unit):
            try:
                val = float(clean_str.replace(unit, "").strip())
                return int(val * multiplier)
            except ValueError:
                pass
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wolf File-Level Diff Engine")
    parser.add_argument("payload", help="Path to the file_map_export JSON payload")
    parser.add_argument(
        "--local-root", "-l", required=True, help="Path to the local base directory"
    )
    parser.add_argument(
        "--tolerance",
        "-t",
        type=float,
        default=0.05,
        help="Size tolerance ratio (default: 0.05)",
    )
    return parser.parse_args()


def get_relative_path(base_path: str, absolute_path: str) -> str:
    base_clean = base_path.rstrip("/")
    abs_clean = absolute_path.rstrip("/")
    if abs_clean.startswith(base_clean):
        return abs_clean[len(base_clean) :].lstrip("/")
    return abs_clean.lstrip("/")


def load_payload(payload_file: Path) -> tuple[str, list[MappedFolder]]:
    try:
        with payload_file.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"Failed to load JSON: {e}") from e

    version = payload.get("version")
    if version != "file-map-1.0":
        print(f"WARNING: Expected 'file-map-1.0', found '{version}'.")

    base_path = payload.get("basePath")
    if not isinstance(base_path, str):
        raise ValueError("Payload missing valid 'basePath'.")

    raw_folders = payload.get("mappedFolders", [])
    if not isinstance(raw_folders, list):
        raise ValueError("'mappedFolders' must be a list.")

    folders = [MappedFolder.from_payload(f) for f in raw_folders]
    return base_path, folders


def verify_files(
    folder: MappedFolder, base_path: str, local_root: Path, tolerance: float
) -> list[str]:
    missing_filenames: list[str] = []

    # If the mapper threw an error (e.g., folder didn't load in browser), we flag it but can't diff it
    if folder.error:
        print(f"   -> Skipping diff: Mapper logged error: {folder.error}")
        return []

    if not folder.files:
        return []

    rel_path = get_relative_path(base_path, folder.absolute_path)
    target_dir = local_root / rel_path

    # If the local directory doesn't exist, all files are missing by default
    if not target_dir.exists() or not target_dir.is_dir():
        return [f.file_name for f in folder.files]

    for file in folder.files:
        local_file = target_dir / file.file_name

        if not local_file.exists() or not local_file.is_file():
            missing_filenames.append(file.file_name)
            continue

        if file.server_size is not None:
            local_size = local_file.stat().st_size
            if file.server_size == 0 and local_size > 0:
                missing_filenames.append(file.file_name)
            elif file.server_size > 0:
                diff = abs(local_size - file.server_size)
                ratio = diff / file.server_size
                if ratio > tolerance:
                    missing_filenames.append(file.file_name)

    return missing_filenames


def export_exact_payload(
    original_file: Path, base_path: str, deficits: list[DirectoryDeficit]
) -> None:
    if not deficits:
        return

    iso_date = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_name = f"{original_file.stem}-exact-missing-{iso_date}.json"
    out_path = original_file.parent / out_name

    payload = {
        "version": "exact-missing-1.0",
        "basePath": base_path,
        "dirs": [
            {"absolutePath": d.absolute_path, "missingFiles": d.missing_files}
            for d in deficits
        ],
    }

    try:
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nGenerated surgical download payload: {out_path.name}")
    except OSError as err:
        print(f"ERROR: Failed to write {out_path.name}: {err}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    payload_file = Path(args.payload)
    local_root = Path(args.local_root)

    if not payload_file.is_file():
        print(f"ERROR: Payload not found at {payload_file}", file=sys.stderr)
        return 1
    if not local_root.is_dir():
        print(f"ERROR: Local root not found at {local_root}", file=sys.stderr)
        return 1

    try:
        base_path, folders = load_payload(payload_file)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print("=" * 60)
    print("WOLF FILE-LEVEL DIFF ENGINE")
    print(f"Evaluating {len(folders)} mapped folders against {local_root}")
    print("=" * 60)

    total_checked_files = 0
    total_missing_files = 0
    deficits: list[DirectoryDeficit] = []

    for idx, folder in enumerate(folders, start=1):
        print(f"[{idx}/{len(folders)}] Inspecting: {folder.absolute_path}")
        total_checked_files += len(folder.files)

        missing = verify_files(folder, base_path, local_root, args.tolerance)

        if missing:
            print(f"   -> Found {len(missing)} missing/corrupted files.")
            deficits.append(
                DirectoryDeficit(
                    absolute_path=folder.absolute_path, missing_files=missing
                )
            )
            total_missing_files += len(missing)
        elif not folder.error and folder.files:
            print(f"   -> OK: All {len(folder.files)} files validated.")

    print("\n" + "=" * 60)
    print("DIFF COMPLETE")
    print(f"Total Folders Evaluated : {len(folders)}")
    print(f"Total Files Evaluated   : {total_checked_files}")
    print(f"Total Missing Files     : {total_missing_files}")
    print("=" * 60)

    if deficits:
        export_exact_payload(payload_file, base_path, deficits)

    return 0


if __name__ == "__main__":
    sys.exit(main())
