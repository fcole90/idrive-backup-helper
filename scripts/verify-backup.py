#!/usr/bin/env python3
"""Verify local directories against an IDrive v0.3.1 JSON extraction payload and generate restore payloads."""

import argparse
import json
import sys
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast, TypedDict, NotRequired


@dataclass(frozen=True)
class VerifyConfig:
    payload_file: Path
    local_root: Path
    tolerance_pct: float = 0.05
    target_depth: Optional[int] = None
    override_base: Optional[str] = None


@dataclass(frozen=True)
class LocalStats:
    file_count: int
    total_bytes: int


class FolderEntryExportPayload(TypedDict):
    href: str
    title: str
    absolutePath: str
    depth: int
    numberMissingFiles: NotRequired[int]
    missingSize: NotRequired[int]


@dataclass
class FolderEntry:
    href: str
    title: str
    absolute_path: str
    depth: int
    expected_count: Optional[int]
    expected_bytes: Optional[int]

    # Dynamic fields populated during verification for export
    number_missing_files: Optional[int] = None
    missing_size: Optional[int] = None

    @classmethod
    def from_payload(cls, raw_item: object, index: int) -> "FolderEntry":
        if not isinstance(raw_item, dict):
            raise ValueError(f"Directory entry #{index} must be an object.")

        raw_item = cast(dict[str, object], raw_item)

        href = _read_string_field(raw_item, "href", index)
        title = _read_string_field(raw_item, "title", index)
        absolute_path = _read_string_field(raw_item, "absolutePath", index)
        depth = _read_int_field(raw_item, "depth", index)

        raw_count = raw_item.get("fileCount")
        raw_size = raw_item.get("folderSize")

        expected_count = _parse_count(raw_count) if isinstance(raw_count, str) else None
        expected_bytes = _parse_size(raw_size) if isinstance(raw_size, str) else None

        return cls(
            href=href,
            title=title,
            absolute_path=absolute_path,
            depth=depth,
            expected_count=expected_count,
            expected_bytes=expected_bytes,
        )

    def to_export_dict(self) -> FolderEntryExportPayload:
        """Serializes the entry back to v0.3.1 schema, injecting deficit data."""
        payload: FolderEntryExportPayload = {
            "href": self.href,
            "title": self.title,
            "absolutePath": self.absolute_path,
            "depth": self.depth,
        }
        if self.number_missing_files is not None:
            payload["numberMissingFiles"] = self.number_missing_files
        if self.missing_size is not None:
            payload["missingSize"] = self.missing_size

        return payload


@dataclass
class VerificationResult:
    entry: FolderEntry
    status: str  # "OK", "MISSING", "PARTIAL", "SKIP"
    issues: list[str]


def _read_string_field(raw_item: dict[str, object], key: str, index: int) -> str:
    value = raw_item.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Directory entry #{index} field '{key}' must be a string.")
    return value


def _read_int_field(raw_item: dict[str, object], key: str, index: int) -> int:
    value = raw_item.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Directory entry #{index} field '{key}' must be an integer.")
    return value


def _parse_count(count_str: str) -> Optional[int]:
    clean_str = count_str.strip().replace(",", "")
    if not clean_str or clean_str == "-":
        return None
    try:
        return int(clean_str)
    except ValueError:
        return None


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
    parser = argparse.ArgumentParser(
        description="Wolf Sync Verifier & Payload Generator"
    )
    parser.add_argument("payload", help="Path to the v0.3.1 JSON payload file")
    parser.add_argument(
        "--local-root",
        "-l",
        required=True,
        help="Path to the local base directory to verify against",
    )
    parser.add_argument(
        "--depth",
        "-d",
        type=int,
        default=None,
        help="Target folder depth to verify.",
    )
    parser.add_argument(
        "--override-base",
        "-o",
        type=str,
        default=None,
        help="Override the basePath from the JSON payload.",
    )
    return parser.parse_args()


def load_payload(
    payload_file: Path, override_base: Optional[str]
) -> tuple[str, list[FolderEntry]]:
    try:
        with payload_file.open("r", encoding="utf-8") as handle:
            payload: FolderEntryExportPayload = json.load(handle)
    except (json.JSONDecodeError, OSError) as error:
        raise ValueError(f"Failed to read or parse JSON file: {error}") from error

    version = payload.get("version")
    if version not in ["0.3.0", "0.3.1"]:
        print(f"WARNING: Expected payload version '0.3.1', found '{version}'.")

    if override_base:
        base_path = override_base
        print(f"NOTICE: Overriding JSON basePath with user input: {base_path}")
    else:
        base_path = payload.get("basePath")
        if not isinstance(base_path, str):
            raise ValueError("Payload missing valid 'basePath' string.")

    directories = payload.get("dirs", [])
    if not isinstance(directories, list):
        raise ValueError("Payload field 'dirs' must be a list.")

    entries = [
        FolderEntry.from_payload(item, idx)
        for idx, item in enumerate(directories, start=1)
    ]
    return base_path, entries


def get_relative_path(base_path: str, absolute_path: str) -> str:
    base_clean = base_path.rstrip("/")
    abs_clean = absolute_path.rstrip("/")

    if abs_clean.startswith(base_clean):
        rel = abs_clean[len(base_clean) :]
        return rel.lstrip("/")

    return abs_clean.lstrip("/")


def calculate_local_stats(target_dir: Path) -> LocalStats:
    file_count = 0
    total_bytes = 0

    for item in target_dir.rglob("*"):
        if item.is_file():
            file_count += 1
            total_bytes += item.stat().st_size

    return LocalStats(file_count=file_count, total_bytes=total_bytes)


def verify_directory(
    entry: FolderEntry, base_path: str, local_root: Path, tolerance: float
) -> VerificationResult:
    rel_path = get_relative_path(base_path, entry.absolute_path)
    target_dir = local_root / rel_path

    # Scenario 1: Directory is completely missing from local drive
    if not target_dir.exists() or not target_dir.is_dir():
        entry.number_missing_files = entry.expected_count if entry.expected_count else 0
        entry.missing_size = entry.expected_bytes if entry.expected_bytes else 0
        return VerificationResult(
            entry, "MISSING", [f"Directory missing locally: {target_dir}"]
        )

    if entry.expected_count is None and entry.expected_bytes is None:
        return VerificationResult(
            entry, "SKIP", ["Payload lacks server stats for this directory."]
        )

    stats = calculate_local_stats(target_dir)
    errors = []

    # Calculate deficit metrics (Floored at 0 to prevent negative missing values if local has extra files)
    if entry.expected_count is not None:
        entry.number_missing_files = max(0, entry.expected_count - stats.file_count)
    if entry.expected_bytes is not None:
        entry.missing_size = max(0, entry.expected_bytes - stats.total_bytes)

    # 1. Strict File Count Check
    if entry.expected_count is not None and stats.file_count != entry.expected_count:
        errors.append(
            f"File count mismatch | Expected: {entry.expected_count} | Found: {stats.file_count}"
        )

    # 2. Fuzzy Size Check
    if entry.expected_bytes is not None:
        if entry.expected_bytes == 0 and stats.total_bytes > 0:
            errors.append(
                f"Size mismatch | Expected: 0 B | Found: {stats.total_bytes} B"
            )
        elif entry.expected_bytes > 0:
            diff = abs(stats.total_bytes - entry.expected_bytes)
            ratio = diff / entry.expected_bytes
            if ratio > tolerance:
                errors.append(
                    f"Size variance ({ratio:.1%}) exceeds tolerance | "
                    f"Expected: ~{entry.expected_bytes} B | Found: {stats.total_bytes} B"
                )

    # Scenario 2: Directory exists but fails checks
    if errors:
        return VerificationResult(entry, "PARTIAL", errors)

    # Scenario 3: Clean sync
    return VerificationResult(entry, "OK", [])


def export_payload(
    original_file: Path, base_path: str, entries: list[FolderEntry], category: str
) -> None:
    if not entries:
        return

    iso_date = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_name = f"{original_file.stem}-{category}-{iso_date}.json"
    out_path = original_file.parent / out_name

    payload = {
        "version": "0.3.1",
        "basePath": base_path,
        "totalDirectoriesFound": len(entries),
        "dirs": [entry.to_export_dict() for entry in entries],
    }

    try:
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(
            f"Generated {category} payload queue: {out_path.name} ({len(entries)} items)"
        )
    except OSError as err:
        print(f"ERROR: Failed to write {out_path.name}: {err}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    config = VerifyConfig(
        payload_file=Path(args.payload),
        local_root=Path(args.local_root),
        target_depth=args.depth,
        override_base=args.override_base,
    )

    if not config.local_root.is_dir():
        print(
            f"ERROR: Local root directory not found at {config.local_root}",
            file=sys.stderr,
        )
        return 1

    try:
        base_path, all_directories = load_payload(
            config.payload_file, config.override_base
        )
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if config.target_depth is not None:
        directories = [d for d in all_directories if d.depth == config.target_depth]
        print(
            f"Filtered to depth {config.target_depth}. Found {len(directories)} matching directories."
        )
    else:
        directories = all_directories
        print(f"Loaded {len(directories)} directories from payload.")

    if not directories:
        print("WARNING: No directories to process.", file=sys.stderr)
        return 0

    print("=" * 60)

    missing_queue: list[FolderEntry] = []
    partial_queue: list[FolderEntry] = []
    total_failed = 0

    for index, entry in enumerate(directories, start=1):
        result = verify_directory(
            entry, base_path, config.local_root, config.tolerance_pct
        )

        if result.status == "MISSING":
            missing_queue.append(entry)
            total_failed += 1
            print(f"[{index}/{len(directories)}] MISSING: {entry.absolute_path}")

        elif result.status == "PARTIAL":
            partial_queue.append(entry)
            total_failed += 1
            print(f"[{index}/{len(directories)}] PARTIAL: {entry.absolute_path}")
            for issue in result.issues:
                print(f"   -> {issue}")

        elif result.status == "SKIP":
            print(f"[{index}/{len(directories)}] SKIPPED: {entry.absolute_path}")

        else:
            print(f"[{index}/{len(directories)}] OK: {entry.absolute_path}")

    print("=" * 60)
    print("VERIFICATION COMPLETE")
    print(f"Directories Checked : {len(directories)}")
    print(f"Clean Directories   : {len(directories) - total_failed}")
    print(f"Missing Directories : {len(missing_queue)}")
    print(f"Partial Directories : {len(partial_queue)}")

    if missing_queue or partial_queue:
        print("\n--- Generating Actionable Restore Payloads ---")
        export_payload(config.payload_file, base_path, missing_queue, "missing")
        export_payload(config.payload_file, base_path, partial_queue, "partial")

    return 1 if total_failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
