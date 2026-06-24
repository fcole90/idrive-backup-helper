"""Sample resource usage of an IDrive backup run over time (cross-platform).

Run this alongside a real download session to attribute long-session resource
growth to a layer (our Python process vs the Chromium browser it drives) and to
test the hypotheses behind a system going sluggish / another app crashing while
physical RAM is only moderately used. It uses psutil and only *reads* process and
system counters, so it does not connect to the browser or disturb the download.

    uv run python scripts/monitor-resource-usage.py --interval 30 --out usage.csv

For each group (python download process, Chromium process tree) it reports:
- rss: resident (physical) memory.
- uss: unique set size = memory private to the process (freed if it died); the
  best per-process "real footprint" and the analog of Windows Private Bytes.
- vms: virtual memory size (address space committed/reserved).
- handles: OS handles (Windows) or open file descriptors (Unix) — a leak here
  can exhaust system limits while RAM stays low.
- threads.

System-wide it reports physical RAM %, swap/page-file usage, and an approximate
**commit charge** (used physical + used page file vs their totals). When physical
RAM looks fine but other apps crash, commit charge or page-file pressure is the
usual culprit — watch commit% and swap%, not just RAM%.

Peaks (high-water marks) are tracked across the run and printed on exit, to catch
sudden spikes a coarse interval would otherwise miss; use a small --interval
during a suspect window.

Note: this does NOT capture GPU/VRAM or Windows GDI/USER objects. If commit,
handles, and RSS/USS all look flat but the system still degrades, suspect GPU
memory next (nvidia-smi / Windows "GPU Process Memory" perf counter).
"""

import argparse
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil

from idrive_backup_helper.filesystem.paths import browser_profile_dir, find_repo_root

_DEFAULT_PYTHON_MATCH = r"download-folder|retry-manifest"
_SELF_MARKER = "monitor-resource-usage"
_MB = 1024 * 1024


@dataclass(frozen=True)
class GroupMem:
    proc_count: int
    rss_mb: float
    uss_mb: float | None
    vms_mb: float
    handles: int | None
    threads: int


@dataclass(frozen=True)
class SystemMem:
    phys_percent: float
    phys_used_mb: float
    swap_used_mb: float
    swap_percent: float
    commit_used_mb: float
    commit_limit_mb: float
    commit_percent: float


def _cmdline(proc: psutil.Process) -> str:
    try:
        return " ".join(proc.cmdline())
    except psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess:
        return ""


def _matching_processes(
    profile_marker: str, python_pattern: re.Pattern[str]
) -> tuple[list[psutil.Process], list[psutil.Process]]:
    chrome_roots: list[psutil.Process] = []
    python_by_pid: dict[int, psutil.Process] = {}

    for proc in psutil.process_iter():
        cmdline = _cmdline(proc)
        if not cmdline or _SELF_MARKER in cmdline:
            continue
        if profile_marker in cmdline:
            chrome_roots.append(proc)
        if python_pattern.search(cmdline):
            python_by_pid[proc.pid] = proc

    chrome_by_pid: dict[int, psutil.Process] = {}
    for root in chrome_roots:
        chrome_by_pid[root.pid] = root
        try:
            for child in root.children(recursive=True):
                chrome_by_pid[child.pid] = child
        except psutil.NoSuchProcess, psutil.AccessDenied:
            continue

    return list(python_by_pid.values()), list(chrome_by_pid.values())


def _proc_handles(proc: psutil.Process) -> int | None:
    # num_handles on Windows, num_fds on Unix; not all platforms expose both.
    getter = getattr(proc, "num_handles", None) or getattr(proc, "num_fds", None)
    if getter is None:
        return None
    try:
        return int(getter())
    except psutil.NoSuchProcess, psutil.AccessDenied:
        return None


def _group_mem(procs: list[psutil.Process]) -> GroupMem:
    count = 0
    rss = 0
    vms = 0
    uss = 0
    uss_available = False
    handles = 0
    handles_available = False
    threads = 0

    for proc in procs:
        try:
            mem = proc.memory_info()
        except psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess:
            continue
        count += 1
        rss += mem.rss
        vms += mem.vms

        try:
            uss += proc.memory_full_info().uss
            uss_available = True
        except psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess:
            pass

        proc_handles = _proc_handles(proc)
        if proc_handles is not None:
            handles += proc_handles
            handles_available = True

        try:
            threads += proc.num_threads()
        except psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess:
            pass

    return GroupMem(
        proc_count=count,
        rss_mb=rss / _MB,
        uss_mb=(uss / _MB) if uss_available else None,
        vms_mb=vms / _MB,
        handles=handles if handles_available else None,
        threads=threads,
    )


def _system_mem() -> SystemMem:
    virtual = psutil.virtual_memory()
    swap = psutil.swap_memory()
    # Approximate commit charge: used physical + used page file vs their totals.
    # This is the constraint that fails allocations (and crashes other apps) when
    # physical RAM still looks fine.
    commit_used = virtual.used + swap.used
    commit_limit = virtual.total + swap.total
    commit_percent = (commit_used / commit_limit * 100) if commit_limit else 0.0
    return SystemMem(
        phys_percent=virtual.percent,
        phys_used_mb=virtual.used / _MB,
        swap_used_mb=swap.used / _MB,
        swap_percent=swap.percent,
        commit_used_mb=commit_used / _MB,
        commit_limit_mb=commit_limit / _MB,
        commit_percent=commit_percent,
    )


def _fmt(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else ""


def _fmt_or_na(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "n/a"


def _csv_row(values: list[str]) -> str:
    return ",".join(values) + "\n"


_CSV_HEADER = [
    "timestamp",
    "py_procs",
    "py_rss_mb",
    "py_uss_mb",
    "py_vms_mb",
    "py_handles",
    "py_threads",
    "chrome_procs",
    "chrome_rss_mb",
    "chrome_uss_mb",
    "chrome_vms_mb",
    "chrome_handles",
    "chrome_threads",
    "phys_percent",
    "swap_used_mb",
    "swap_percent",
    "commit_used_mb",
    "commit_limit_mb",
    "commit_percent",
]


def _max_opt(current: float | None, candidate: float | None) -> float | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)


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
    profile_marker = str(profile_dir)
    python_pattern = re.compile(args.python_match)

    out_path: Path | None = args.out
    if out_path is not None and not out_path.exists():
        out_path.write_text(_csv_row(_CSV_HEADER), encoding="utf-8")

    print(f"Monitoring Chromium tree under: {profile_marker}")
    print(f"Python match: /{args.python_match}/")
    print(f"Interval: {args.interval}s" + ("  (single sample)" if args.once else ""))
    print()

    peak_chrome_uss: float | None = None
    peak_chrome_vms: float | None = 0.0
    peak_commit_percent = 0.0
    peak_swap_percent = 0.0

    try:
        while True:
            timestamp = datetime.now().isoformat(timespec="seconds")
            python_mem, chrome_mem = _matching_processes(profile_marker, python_pattern)
            python_group = _group_mem(python_mem)
            chrome_group = _group_mem(chrome_mem)
            system = _system_mem()

            peak_chrome_uss = _max_opt(peak_chrome_uss, chrome_group.uss_mb)
            peak_chrome_vms = _max_opt(peak_chrome_vms, chrome_group.vms_mb)
            peak_commit_percent = max(peak_commit_percent, system.commit_percent)
            peak_swap_percent = max(peak_swap_percent, system.swap_percent)

            print(
                f"{timestamp}  "
                f"py: {python_group.proc_count}p "
                f"uss={_fmt_or_na(python_group.uss_mb)}MB "
                f"rss={_fmt(python_group.rss_mb)}MB  |  "
                f"chrome: {chrome_group.proc_count}p "
                f"uss={_fmt_or_na(chrome_group.uss_mb)}MB "
                f"rss={_fmt(chrome_group.rss_mb)}MB "
                f"vms={_fmt(chrome_group.vms_mb)}MB "
                f"handles={chrome_group.handles if chrome_group.handles is not None else 'n/a'}  |  "
                f"sys: ram={system.phys_percent:.0f}% "
                f"commit={system.commit_percent:.0f}% "
                f"swap={system.swap_percent:.0f}%",
                flush=True,
            )

            if out_path is not None:
                with out_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        _csv_row(
                            [
                                timestamp,
                                str(python_group.proc_count),
                                _fmt(python_group.rss_mb),
                                _fmt(python_group.uss_mb),
                                _fmt(python_group.vms_mb),
                                str(python_group.handles or ""),
                                str(python_group.threads),
                                str(chrome_group.proc_count),
                                _fmt(chrome_group.rss_mb),
                                _fmt(chrome_group.uss_mb),
                                _fmt(chrome_group.vms_mb),
                                str(chrome_group.handles or ""),
                                str(chrome_group.threads),
                                _fmt(system.phys_percent),
                                _fmt(system.swap_used_mb),
                                _fmt(system.swap_percent),
                                _fmt(system.commit_used_mb),
                                _fmt(system.commit_limit_mb),
                                _fmt(system.commit_percent),
                            ]
                        )
                    )

            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")

    print()
    print(
        "Peaks  chrome uss="
        f"{_fmt_or_na(peak_chrome_uss)}MB  chrome vms={_fmt(peak_chrome_vms)}MB  "
        f"commit={peak_commit_percent:.0f}%  swap={peak_swap_percent:.0f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
