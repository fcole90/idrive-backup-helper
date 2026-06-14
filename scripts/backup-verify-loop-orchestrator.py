#!/usr/bin/env python3
"""Wolf Master Orchestrator: Autonomous Backup & Verification Loop."""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TERMINAL_WIDTH_CHARS = 60


@dataclass(frozen=True)
class OrchestratorConfig:
    master_payload: Path
    local_root: Path
    email: str
    target_depth: Optional[int]
    override_base: Optional[str]
    only_missing: bool
    only_partial: bool
    max_retries: int


@dataclass(frozen=True)
class DeltaPayloads:
    missing_file: Optional[Path]
    partial_file: Optional[Path]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wolf Autonomous Sync Orchestrator")
    parser.add_argument("payload", help="Path to the master v0.3.1 JSON payload file")
    parser.add_argument("--email", "-e", required=True, help="IDrive account email")
    parser.add_argument(
        "--local-root", "-l", required=True, help="Local base directory"
    )

    parser.add_argument(
        "--depth", "-d", type=int, default=None, help="Target folder depth"
    )
    parser.add_argument(
        "--override-base", "-o", type=str, default=None, help="Override JSON basePath"
    )

    parser.add_argument(
        "--only-missing", action="store_true", help="Only process missing queues"
    )
    parser.add_argument(
        "--only-partial", action="store_true", help="Only process partial queues"
    )
    parser.add_argument(
        "--max-retries", "-m", type=int, default=3, help="Maximum number of loop cycles"
    )

    return parser.parse_args()


def validate_config(config: OrchestratorConfig) -> None:
    if config.only_missing and config.only_partial:
        raise ValueError("Cannot specify both --only-missing and --only-partial.")

    if not config.master_payload.is_file():
        raise FileNotFoundError(f"Master payload not found at {config.master_payload}")

    if not config.local_root.is_dir():
        raise FileNotFoundError(f"Local root not found at {config.local_root}")


def run_restore(payload: Path, email: str, depth: Optional[int]) -> bool:
    print(f"\n>>> EXECUTING RESTORE QUEUE: {payload.name}")
    cmd = [sys.executable, "backup_util.py", str(payload), "-e", email]
    if depth is not None:
        cmd.extend(["-d", str(depth)])

    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode == 0
    except FileNotFoundError as e:
        raise FileNotFoundError(
            "backup_util.py not found in the current directory."
        ) from e


def run_verify(
    master_payload: Path,
    local_root: Path,
    depth: Optional[int],
    override_base: Optional[str],
) -> tuple[bool, DeltaPayloads]:
    print(f"\n>>> EXECUTING VERIFICATION AUDIT")
    cmd = [
        sys.executable,
        "verify-backup.py",
        str(master_payload),
        "-l",
        str(local_root),
    ]
    if depth is not None:
        cmd.extend(["-d", str(depth)])
    if override_base is not None:
        cmd.extend(["-o", override_base])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

        success = result.returncode == 0

        missing_path = None
        partial_path = None

        for line in result.stdout.splitlines():
            match = re.search(
                r"Generated (missing|partial) payload queue:\s*([a-zA-Z0-9_\-\.]+)",
                line,
            )
            if match:
                queue_type = match.group(1)
                filename = match.group(2)
                full_path = master_payload.parent / filename

                if queue_type == "missing":
                    missing_path = full_path
                elif queue_type == "partial":
                    partial_path = full_path

        return success, DeltaPayloads(
            missing_file=missing_path, partial_file=partial_path
        )

    except FileNotFoundError as e:
        raise FileNotFoundError(
            "verify-backup.py not found in the current directory."
        ) from e


def main() -> int:
    args = parse_args()
    config = OrchestratorConfig(
        master_payload=Path(args.payload).resolve(),
        local_root=Path(args.local_root).resolve(),
        email=args.email,
        target_depth=args.depth,
        override_base=args.override_base,
        only_missing=args.only_missing,
        only_partial=args.only_partial,
        max_retries=args.max_retries,
    )

    try:
        validate_config(config)
    except (ValueError, FileNotFoundError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("=" * TERMINAL_WIDTH_CHARS)
    print("WOLF ORCHESTRATOR INITIATED")
    print(f"Master Payload : {config.master_payload.name}")
    print(f"Max Retries    : {config.max_retries}")
    print("=" * TERMINAL_WIDTH_CHARS)

    active_queues = [config.master_payload]

    for attempt in range(1, config.max_retries + 1):
        print(f"\n{'#' * TERMINAL_WIDTH_CHARS}")
        print(f"CYCLE [{attempt}/{config.max_retries}]")
        print(f"{'#' * TERMINAL_WIDTH_CHARS}")

        try:
            for queue_file in active_queues:
                if queue_file.exists():
                    run_restore(queue_file, config.email, config.target_depth)
                else:
                    print(f"WARNING: Queue file {queue_file.name} not found. Skipping.")

            is_clean, deltas = run_verify(
                config.master_payload,
                config.local_root,
                config.target_depth,
                config.override_base,
            )
        except FileNotFoundError as error:
            print(f"ERROR: Sub-script missing. {error}", file=sys.stderr)
            return 1

        if is_clean:
            print("\n" + "=" * TERMINAL_WIDTH_CHARS)
            print("SUCCESS: Full system sync validated. Zero discrepancies found.")
            print("=" * TERMINAL_WIDTH_CHARS)
            return 0

        active_queues = []
        if not config.only_partial and deltas.missing_file:
            active_queues.append(deltas.missing_file)
        if not config.only_missing and deltas.partial_file:
            active_queues.append(deltas.partial_file)

        if not active_queues:
            print("\n" + "=" * TERMINAL_WIDTH_CHARS)
            print(
                "TERMINATED: Discrepancies exist, but filters blocked further queues."
            )
            print("=" * TERMINAL_WIDTH_CHARS)
            return 1

    print("\n" + "=" * TERMINAL_WIDTH_CHARS)
    print("WARNING: CIRCUIT BREAKER TRIGGERED")
    print(f"Failed to achieve full sync after {config.max_retries} cycles.")
    print(
        "Review the latest generated JSON payloads for persistent lockouts or zero-byte files."
    )
    print("=" * TERMINAL_WIDTH_CHARS)
    return 1


if __name__ == "__main__":
    sys.exit(main())
