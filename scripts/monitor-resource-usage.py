"""Sample memory usage of an IDrive backup run over time.

Run this alongside a real download session to attribute long-session memory
growth to a layer: our Python process vs the Chromium browser it drives. It only
reads ``/proc``, so it does not connect to the browser or interfere with the
running download.

    uv run python scripts/monitor-resource-usage.py --interval 30 --out mem.csv

Processes are matched by command line:
- Chromium: the process whose cmdline references the browser profile dir, plus
  its whole descendant tree (renderers, gpu, zygote) found via parent PID.
- Python:  any python process whose cmdline references the download subcommand
  (``download-folder`` / ``retry-manifest``), excluding this monitor.

For each group it reports resident memory and process count. PSS (proportional
set size, from ``smaps_rollup``) is preferred because summing plain RSS across
the many Chromium child processes double-counts shared pages; RSS is reported too
and used as a fallback when PSS is unavailable. A rising trend in one group's PSS
points at the leaking layer.
"""

import argparse
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from idrive_backup_helper.filesystem.paths import browser_profile_dir, find_repo_root

_PROC = Path("/proc")
_VMRSS_RE = re.compile(r"^VmRSS:\s+(\d+)\s+kB", re.MULTILINE)
_PSS_RE = re.compile(r"^Pss:\s+(\d+)\s+kB", re.MULTILINE)
_DEFAULT_PYTHON_MATCH = r"download-folder|retry-manifest"
_SELF_MARKER = "monitor-resource-usage"


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    cmdline: str


@dataclass(frozen=True)
class GroupMem:
    proc_count: int
    rss_kb: int
    pss_kb: int | None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError, ValueError:
        return None


def _read_cmdline(pid: int) -> str | None:
    try:
        raw = (_PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _read_ppid(pid: int) -> int | None:
    stat = _read_text(_PROC / str(pid) / "stat")
    if stat is None:
        return None
    # comm (field 2) may contain spaces/parens; everything after the final ')'
    # is space-delimited starting at field 3 (state), so ppid is the 2nd token.
    close_paren = stat.rfind(")")
    if close_paren == -1:
        return None
    fields = stat[close_paren + 2 :].split()
    if len(fields) < 2 or not fields[1].isdigit():
        return None
    return int(fields[1])


def _read_rss_kb(pid: int) -> int | None:
    status = _read_text(_PROC / str(pid) / "status")
    if status is None:
        return None
    match = _VMRSS_RE.search(status)
    return int(match.group(1)) if match else None


def _read_pss_kb(pid: int) -> int | None:
    rollup = _read_text(_PROC / str(pid) / "smaps_rollup")
    if rollup is None:
        return None
    match = _PSS_RE.search(rollup)
    return int(match.group(1)) if match else None


def _iter_proc_infos() -> list[ProcInfo]:
    infos: list[ProcInfo] = []
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmdline = _read_cmdline(pid)
        ppid = _read_ppid(pid)
        if cmdline is None or ppid is None:
            continue
        infos.append(ProcInfo(pid=pid, ppid=ppid, cmdline=cmdline))
    return infos


def _descendants(root_pids: set[int], infos: list[ProcInfo]) -> set[int]:
    children: dict[int, list[int]] = {}
    for info in infos:
        children.setdefault(info.ppid, []).append(info.pid)

    collected: set[int] = set()
    stack = list(root_pids)
    while stack:
        pid = stack.pop()
        if pid in collected:
            continue
        collected.add(pid)
        stack.extend(children.get(pid, []))
    return collected


def _group_mem(pids: set[int]) -> GroupMem:
    rss_total = 0
    pss_total = 0
    pss_available = False
    for pid in pids:
        rss = _read_rss_kb(pid)
        if rss is not None:
            rss_total += rss
        pss = _read_pss_kb(pid)
        if pss is not None:
            pss_total += pss
            pss_available = True
    return GroupMem(
        proc_count=len(pids),
        rss_kb=rss_total,
        pss_kb=pss_total if pss_available else None,
    )


def _sample(
    profile_dir_marker: str, python_pattern: re.Pattern[str]
) -> tuple[GroupMem, GroupMem]:
    infos = _iter_proc_infos()

    chrome_roots = {
        info.pid
        for info in infos
        if profile_dir_marker in info.cmdline and _SELF_MARKER not in info.cmdline
    }
    chrome_pids = _descendants(chrome_roots, infos)

    python_pids = {
        info.pid
        for info in infos
        if python_pattern.search(info.cmdline) and _SELF_MARKER not in info.cmdline
    }

    return _group_mem(python_pids), _group_mem(chrome_pids)


def _mb(kb: int | None) -> str:
    return f"{kb / 1024:.1f}" if kb is not None else ""


def _csv_row(values: list[str]) -> str:
    return ",".join(values) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval", type=float, default=30.0, help="Seconds between samples"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="CSV file to append samples to (created with a header if missing)",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help="Browser profile dir used to identify the Chromium tree "
        "(default: the repo's browser-state dir)",
    )
    parser.add_argument(
        "--python-match",
        default=_DEFAULT_PYTHON_MATCH,
        help="Regex matched against process cmdlines to find the download process",
    )
    parser.add_argument(
        "--once", action="store_true", help="Take a single sample and exit"
    )
    args = parser.parse_args()

    profile_dir: Path = args.profile_dir or browser_profile_dir(find_repo_root())
    profile_dir_marker = str(profile_dir)
    python_pattern = re.compile(args.python_match)

    out_path: Path | None = args.out
    if out_path is not None and not out_path.exists():
        out_path.write_text(
            _csv_row(
                [
                    "timestamp",
                    "py_procs",
                    "py_rss_mb",
                    "py_pss_mb",
                    "chrome_procs",
                    "chrome_rss_mb",
                    "chrome_pss_mb",
                ]
            ),
            encoding="utf-8",
        )

    print(f"Monitoring Chromium tree under: {profile_dir_marker}")
    print(f"Python match: /{args.python_match}/")
    print(f"Interval: {args.interval}s" + ("  (single sample)" if args.once else ""))
    print()

    try:
        while True:
            timestamp = datetime.now().isoformat(timespec="seconds")
            python_mem, chrome_mem = _sample(profile_dir_marker, python_pattern)

            print(
                f"{timestamp}  "
                f"python: {python_mem.proc_count}p "
                f"rss={_mb(python_mem.rss_kb)}MB pss={_mb(python_mem.pss_kb) or 'n/a'}MB  |  "
                f"chrome: {chrome_mem.proc_count}p "
                f"rss={_mb(chrome_mem.rss_kb)}MB pss={_mb(chrome_mem.pss_kb) or 'n/a'}MB",
                flush=True,
            )

            if out_path is not None:
                with out_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        _csv_row(
                            [
                                timestamp,
                                str(python_mem.proc_count),
                                _mb(python_mem.rss_kb),
                                _mb(python_mem.pss_kb),
                                str(chrome_mem.proc_count),
                                _mb(chrome_mem.rss_kb),
                                _mb(chrome_mem.pss_kb),
                            ]
                        )
                    )

            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
