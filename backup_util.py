#!/usr/bin/env python3
"""Batch-restore IDrive folders from an exported JSON payload."""

import argparse
import getpass
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast
from urllib.parse import unquote


IDRIVE_HOME_PREFIX = "https://www.idrive.com/idrive/home/"
IDRIVE_BIN_DIR = Path("/opt/IDriveForLinux/bin")
IDRIVE_BASE_DATA_DIR = Path("/opt/IDriveForLinux/idriveIt/user_profile")
RESTORE_COMMAND = ["./idrive", "--restore"]


@dataclass(frozen=True)
class RestorePaths:
    payload_file: Path
    target_depth: Optional[int]
    idrive_bin_dir: Path
    user_dir: Path
    online_restore_dir: Path
    restore_data_dir: Path
    restore_set_file: Path


@dataclass(frozen=True)
class FolderEntry:
    href: str
    title: str
    absolute_path: str
    depth: int

    @classmethod
    def from_payload(cls, raw_item: object, index: int) -> "FolderEntry":
        if not isinstance(raw_item, dict):
            raise ValueError(f"Directory entry #{index} must be an object.")

        raw_item = cast(dict[str, object], raw_item)
        href = read_string_field(raw_item, "href", index)
        title = read_string_field(raw_item, "title", index)
        absolute_path = read_string_field(raw_item, "absolutePath", index)
        depth = read_int_field(raw_item, "depth", index)

        return cls(
            href=href,
            title=title,
            absolute_path=absolute_path,
            depth=depth,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wolf IDrive Batch Executor")
    parser.add_argument("payload", help="Path to the downloaded JSON payload file")
    parser.add_argument(
        "--email",
        "-e",
        required=True,
        help="Email address associated with the IDrive account",
    )
    parser.add_argument(
        "--depth",
        "-d",
        type=int,
        default=None,
        help="Target folder depth to process (0 = root, 1 = first-level subfolders, etc.)",
    )
    return parser.parse_args()


def build_restore_paths(args: argparse.Namespace) -> RestorePaths:
    current_user_name = getpass.getuser()
    user_dir = IDRIVE_BASE_DATA_DIR / current_user_name / args.email
    online_restore_dir = user_dir / "Restore" / "DefaultRestoreSet"

    return RestorePaths(
        payload_file=Path(args.payload),
        target_depth=args.depth,
        idrive_bin_dir=IDRIVE_BIN_DIR,
        user_dir=user_dir,
        online_restore_dir=online_restore_dir,
        restore_data_dir=online_restore_dir / "RestoreData",
        restore_set_file=online_restore_dir / "RestoresetFile.txt",
    )


def validate_environment(paths: RestorePaths) -> None:
    if not paths.idrive_bin_dir.is_dir():
        raise FileNotFoundError(f"IDrive directory not found at {paths.idrive_bin_dir}")

    if not paths.user_dir.is_dir():
        raise FileNotFoundError(
            "User directory not found at "
            f"{paths.user_dir}. Check the email value and make sure IDrive has run at least once."
        )

    if not paths.payload_file.is_file():
        raise FileNotFoundError(f"JSON payload not found at {paths.payload_file}")


def read_string_field(raw_item: dict[str, object], key: str, index: int) -> str:
    value = raw_item.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Directory entry #{index} field '{key}' must be a string.")
    return value


def read_int_field(raw_item: dict[str, object], key: str, index: int) -> int:
    value = raw_item.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Directory entry #{index} field '{key}' must be an integer.")
    return value


def load_directories(payload_file: Path) -> list[FolderEntry]:
    try:
        with payload_file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as error:
        raise ValueError(f"Failed to parse JSON file. {error}") from error
    except OSError as error:
        raise OSError(f"Failed to read JSON file. {error}") from error

    if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object.")

    payload = cast(dict[str, object], payload)
    directories = payload.get("dirs", [])
    if not isinstance(directories, list):
        raise ValueError("Payload field 'dirs' must be a list.")

    directories = cast(list[dict[str, object]], directories)
    return [FolderEntry.from_payload(item, index) for index, item in enumerate(directories, start=1)]


def filter_directories(directories: list[FolderEntry], target_depth: Optional[int]) -> list[FolderEntry]:
    if target_depth is None:
        return directories

    return [item for item in directories if item.depth == target_depth]


def resolve_folder_path(href: Optional[str]) -> str:
    if not href:
        return ""

    raw_path = href.replace(IDRIVE_HOME_PREFIX, "", 1)
    parts = [part for part in raw_path.split("/") if part]
    if len(parts) < 2:
        raise ValueError(f"Unexpected path format: {href}")

    return "/" + unquote("/".join(parts[1:]))


def clear_restore_files(online_restore_dir: Path) -> None:
    online_restore_dir.mkdir(parents=True, exist_ok=True)

    for entry in online_restore_dir.iterdir():
        if not entry.is_file():
            continue

        try:
            entry.unlink()
            print(f"Removed file: {entry}")
        except OSError as error:
            print(f"WARNING: Could not remove file {entry}. {error}")


def write_restore_set(restore_set_file: Path, target_path: str) -> None:
    try:
        restore_set_file.write_text(f"{target_path}\n", encoding="utf-8")
    except OSError as error:
        raise OSError(f"Could not write to {restore_set_file}. {error}") from error


def run_restore(idrive_bin_dir: Path) -> int:
    result = subprocess.run(RESTORE_COMMAND, cwd=idrive_bin_dir, check=False)
    return result.returncode


def process_directory(index: int, total: int, item: FolderEntry, paths: RestorePaths) -> bool:
    try:
        absolute_target_path = resolve_folder_path(item.href)
    except ValueError as error:
        print(f"WARNING: Skipping malformed entry [{index}/{total}]. {error}")
        return False

    if absolute_target_path in {"", "/"}:
        return False

    print("=" * 50)
    print(f"TARGET ACQUIRED [{index}/{total}]: {absolute_target_path}")
    print("=" * 50)

    clear_restore_files(paths.online_restore_dir)
    write_restore_set(paths.restore_set_file, absolute_target_path)

    print("Initiating IDrive restore engine...")
    return_code = run_restore(paths.idrive_bin_dir)

    if return_code == 0:
        print(f"COMPLETED: {absolute_target_path}\n")
        return True

    print(f"WARNING: IDrive exited with code {return_code} for {absolute_target_path}\n")
    return False


def main() -> int:
    args = parse_args()
    paths = build_restore_paths(args)

    try:
        validate_environment(paths)
        directories = filter_directories(load_directories(paths.payload_file), paths.target_depth)
    except (FileNotFoundError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if not directories:
        depth_label = paths.target_depth if paths.target_depth is not None else "ALL"
        print(f"WARNING: No directories found matching the requested criteria (Depth: {depth_label}).")
        return 0

    depth_label = paths.target_depth if paths.target_depth is not None else "ALL"
    print(f"Queue verified. Found {len(directories)} directories at depth {depth_label}.")

    try:
        for index, item in enumerate(directories, start=1):
            process_directory(index, len(directories), item, paths)
    except OSError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("All specified folders for the selected depth have been processed.")
    print(f"You can find them at {paths.restore_data_dir}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
