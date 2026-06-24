"""Benchmark per-file existence checks against a single directory scan.

Run this on the actual download destination (for example an external USB drive)
to measure how much the resume "does this file already exist?" phase costs with a
per-file ``Path.exists()`` stat versus one ``os.scandir()`` of the directory. This
is the bottleneck behind the slow resume runs: ``download-folder`` and
``retry-manifest`` now check existence through a cached directory listing
(``DirectoryListingCache``) instead of one stat per file.

    uv run python scripts/benchmark-existence-check.py /media/<user>/<disk>/<folder>

By default the per-file stat is timed first, so it pays the cold-cache cost that
matches the real complaint; the scandir is timed afterwards. Pass --scan-first to
flip the order, or run the script twice to compare warm-cache behaviour.
"""

import argparse
import os
import time
from pathlib import Path

from idrive_backup_helper.filesystem.listing import existing_entry_names


def _entry_names(directory: Path, limit: int | None) -> list[str]:
    with os.scandir(directory) as entries:
        names = [entry.name for entry in entries]
    names.sort()
    if limit is not None:
        names = names[:limit]
    return names


def _time_per_file_stat(directory: Path, names: list[str]) -> tuple[float, int]:
    start = time.perf_counter()
    present = 0
    for name in names:
        if (directory / name).exists():
            present += 1
    return time.perf_counter() - start, present


def _time_scandir_lookup(directory: Path, names: list[str]) -> tuple[float, int]:
    start = time.perf_counter()
    listing = existing_entry_names(directory)
    present = sum(1 for name in names if name in listing)
    return time.perf_counter() - start, present


def _report(label: str, elapsed: float, count: int, present: int) -> None:
    per_file_ms = (elapsed / count * 1000) if count else 0.0
    print(
        f"{label:<22} {elapsed:8.3f}s total  "
        f"{per_file_ms:8.3f}ms/file  ({present}/{count} present)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Directory to benchmark")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only check the first N entries (sorted) instead of all of them",
    )
    parser.add_argument(
        "--scan-first",
        action="store_true",
        help="Time the scandir lookup before the per-file stat (warms the cache "
        "for the stat run instead of the other way around)",
    )
    args = parser.parse_args()

    directory: Path = args.directory
    if not directory.is_dir():
        parser.error(f"Not a directory: {directory}")

    names = _entry_names(directory, args.limit)
    count = len(names)
    if count == 0:
        print(f"No entries found in {directory}")
        return 1

    print(f"Directory: {directory}")
    print(f"Entries checked: {count}")
    print()

    if args.scan_first:
        scan_elapsed, scan_present = _time_scandir_lookup(directory, names)
        stat_elapsed, stat_present = _time_per_file_stat(directory, names)
    else:
        stat_elapsed, stat_present = _time_per_file_stat(directory, names)
        scan_elapsed, scan_present = _time_scandir_lookup(directory, names)

    _report("per-file Path.exists()", stat_elapsed, count, stat_present)
    _report("scandir + set lookup", scan_elapsed, count, scan_present)
    print()

    if scan_elapsed > 0:
        print(f"Speedup: {stat_elapsed / scan_elapsed:.1f}x faster with scandir")
    note = "stat" if not args.scan_first else "scandir"
    print(f"Note: the {note} run ran first and paid the cold-cache cost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
