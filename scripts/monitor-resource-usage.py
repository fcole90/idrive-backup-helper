"""Sample resource usage of an IDrive backup run over time (cross-platform).

Run this alongside a real download session to attribute long-session resource
growth to a layer (our Python process vs the Chromium browser it drives) and to
diagnose a system going sluggish / a window "not responding" while CPU and RAM
are only moderately used. It uses psutil (+ optional nvidia-smi) and only *reads*
counters, so it does not connect to the browser or disturb the download.

    uv run python scripts/monitor-resource-usage.py --interval 30 --out .agents/playground

Per group (python download process, Chromium process tree):
- rss / uss / vms: resident, private (Windows Private Bytes analog), and virtual.
- handles: OS handles (Windows) or fds (Unix); a leak exhausts limits at low RAM.
- threads.
- cpu%: CPU summed across the group (can exceed 100% across multiple cores).
- read/write MB/s: process disk I/O rate (best-effort; resets when a renderer
  recycles, shown blank that tick).

System-wide:
- CPU %, physical RAM %, swap/page-file usage, approximate commit charge.
- **disk read/write MB/s and disk busy%** (busiest device).
- **disk %time / queue length / ms-per-IO (Windows only, via typeperf).** This is
  the real saturation signal that psutil cannot read on Windows. High % Disk Time
  or queue length with LOW throughput means latency-bound saturation: many tiny
  high-latency ops (e.g. antivirus scanning each downloaded file) block every app
  on file I/O → "not responding", low CPU/RAM. Throughput alone misses this.
- disk free space for the destination volume (--to-destination, default: the
  profile volume).
- GPU memory/util if nvidia-smi is present (otherwise blank; for non-NVIDIA
  Windows use `typeperf "\\GPU Process Memory(*)\\Dedicated Usage"`).

Peaks are tracked across the run and printed on exit to catch spikes a coarse
interval would miss; use a small --interval during a suspect window.
"""

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil

from idrive_backup_helper.filesystem.paths import browser_profile_dir, find_repo_root

_DEFAULT_PYTHON_MATCH = r"download-folder|retry-manifest"
_SELF_MARKER = "monitor-resource-usage"
_MB = 1024 * 1024
_GB = 1024 * 1024 * 1024
_PROC_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess)


@dataclass(frozen=True)
class GroupSample:
    proc_count: int
    rss_mb: float
    uss_mb: float | None
    vms_mb: float
    handles: int | None
    threads: int
    read_bytes: int | None
    write_bytes: int | None


@dataclass(frozen=True)
class SystemMem:
    phys_percent: float
    swap_used_mb: float
    swap_percent: float
    commit_used_mb: float
    commit_limit_mb: float
    commit_percent: float


@dataclass(frozen=True)
class DiskSample:
    read_bytes: int
    write_bytes: int
    busy_by_device: dict[str, float]  # cumulative I/O service time (ms) per device
    free_gb: float | None


@dataclass(frozen=True)
class GpuStats:
    mem_used_mb: float
    mem_total_mb: float
    util_percent: float


@dataclass(frozen=True)
class DiskLatency:
    pct_disk_time: float
    queue_length: float
    ms_per_io: float


class CpuTracker:
    """Per-group CPU% across ticks.

    psutil computes a process's CPU% relative to the previous call on the *same*
    Process object, so cache objects by pid and reuse them between samples. A
    process contributes 0% on the tick it first appears (its priming call).
    """

    def __init__(self) -> None:
        self._by_pid: dict[int, psutil.Process] = {}

    def group_cpu_percent(self, procs: list[psutil.Process]) -> float:
        seen: set[int] = set()
        total = 0.0
        for proc in procs:
            seen.add(proc.pid)
            cached = self._by_pid.get(proc.pid)
            if cached is None:
                self._by_pid[proc.pid] = proc
                try:
                    proc.cpu_percent()  # prime; first reading is meaningless
                except _PROC_GONE:
                    pass
                continue
            try:
                total += cached.cpu_percent()
            except _PROC_GONE:
                pass
        for pid in list(self._by_pid):
            if pid not in seen:
                del self._by_pid[pid]
        return total


def _cmdline(proc: psutil.Process) -> str:
    try:
        return " ".join(proc.cmdline())
    except _PROC_GONE:
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
    except _PROC_GONE:
        return None


def _group_sample(procs: list[psutil.Process]) -> GroupSample:
    count = 0
    rss = vms = uss = 0
    uss_available = False
    handles = 0
    handles_available = False
    threads = 0
    read_bytes = write_bytes = 0
    io_available = False

    for proc in procs:
        try:
            mem = proc.memory_info()
        except _PROC_GONE:
            continue
        count += 1
        rss += mem.rss
        vms += mem.vms

        try:
            uss += proc.memory_full_info().uss
            uss_available = True
        except _PROC_GONE:
            pass

        proc_handles = _proc_handles(proc)
        if proc_handles is not None:
            handles += proc_handles
            handles_available = True

        try:
            threads += proc.num_threads()
        except _PROC_GONE:
            pass

        try:
            io = proc.io_counters()
            read_bytes += io.read_bytes
            write_bytes += io.write_bytes
            io_available = True
        except (NotImplementedError, *_PROC_GONE):
            pass

    return GroupSample(
        proc_count=count,
        rss_mb=rss / _MB,
        uss_mb=(uss / _MB) if uss_available else None,
        vms_mb=vms / _MB,
        handles=handles if handles_available else None,
        threads=threads,
        read_bytes=read_bytes if io_available else None,
        write_bytes=write_bytes if io_available else None,
    )


def _system_mem() -> SystemMem:
    virtual = psutil.virtual_memory()
    swap = psutil.swap_memory()
    # Approximate commit charge: used physical + used page file vs their totals.
    commit_used = virtual.used + swap.used
    commit_limit = virtual.total + swap.total
    commit_percent = (commit_used / commit_limit * 100) if commit_limit else 0.0
    return SystemMem(
        phys_percent=virtual.percent,
        swap_used_mb=swap.used / _MB,
        swap_percent=swap.percent,
        commit_used_mb=commit_used / _MB,
        commit_limit_mb=commit_limit / _MB,
        commit_percent=commit_percent,
    )


def _disk_free_gb(paths: list[Path]) -> float | None:
    free_values: list[float] = []
    for path in paths:
        try:
            free_values.append(psutil.disk_usage(str(path)).free / _GB)
        except OSError:
            continue
    return min(free_values) if free_values else None


def _disk_sample(watch_paths: list[Path]) -> DiskSample:
    read_bytes = write_bytes = 0
    busy_by_device: dict[str, float] = {}
    # disk_io_counters can return None on systems with no disks; fall back to {}.
    perdisk = psutil.disk_io_counters(perdisk=True) or {}
    for device, counters in perdisk.items():
        read_bytes += counters.read_bytes
        write_bytes += counters.write_bytes
        # busy_time is Linux/BSD-only; on Windows fall back to read+write service
        # time (ms) so disk activity% works there too.
        busy = getattr(counters, "busy_time", None)
        if busy is None:
            busy = (getattr(counters, "read_time", 0) or 0) + (
                getattr(counters, "write_time", 0) or 0
            )
        busy_by_device[device] = float(busy)
    return DiskSample(
        read_bytes=read_bytes,
        write_bytes=write_bytes,
        busy_by_device=busy_by_device,
        free_gb=_disk_free_gb(watch_paths),
    )


def _gpu_stats() -> GpuStats | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except OSError, subprocess.SubprocessError:
        return None

    used = total = util = 0.0
    found = False
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            used += float(parts[0])
            total += float(parts[1])
            util = max(util, float(parts[2]))
            found = True
        except ValueError:
            continue
    return (
        GpuStats(mem_used_mb=used, mem_total_mb=total, util_percent=util)
        if found
        else None
    )


def _disk_latency() -> DiskLatency | None:
    # Windows only. psutil cannot read disk utilization/latency on Windows, so use
    # typeperf with locale-independent PerfLib counter indexes (names are localized
    # but indexes are not): 234=PhysicalDisk, 200=% Disk Time, 198=Current Disk
    # Queue Length, 208=Avg. Disk sec/Transfer. -sc 2 because the first sample of a
    # rate counter (% Disk Time) is unreliable; we parse the last data row.
    try:
        result = subprocess.run(
            [
                "typeperf",
                r"\234(_Total)\200",
                r"\234(_Total)\198",
                r"\234(_Total)\208",
                "-sc",
                "2",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except OSError, subprocess.SubprocessError:
        return None

    data_rows = [
        line
        for line in result.stdout.splitlines()
        if line.strip().startswith('"') and "PDH-CSV" not in line
    ]
    if not data_rows:
        return None
    fields = [field.strip().strip('"') for field in data_rows[-1].split(",")]
    if len(fields) < 4:
        return None
    try:
        pct_disk_time = float(fields[1])
        queue_length = float(fields[2])
        sec_per_io = float(fields[3])
    except ValueError:
        return None
    return DiskLatency(
        pct_disk_time=pct_disk_time,
        queue_length=queue_length,
        ms_per_io=sec_per_io * 1000.0,
    )


def _rate_mbps(
    curr: int | None, prev: int | None, elapsed: float | None
) -> float | None:
    if curr is None or prev is None or elapsed is None or elapsed <= 0 or curr < prev:
        return None
    return (curr - prev) / _MB / elapsed


def _busy_percent(
    curr: dict[str, float], prev: dict[str, float], elapsed: float | None
) -> tuple[float | None, str]:
    if not curr or elapsed is None or elapsed <= 0:
        return None, ""
    best_pct = -1.0
    best_device = ""
    for device, busy_ms in curr.items():
        prev_ms = prev.get(device)
        if prev_ms is None or busy_ms < prev_ms:
            continue
        pct = (busy_ms - prev_ms) / (elapsed * 1000.0) * 100.0
        if pct > best_pct:
            best_pct = pct
            best_device = device
    if best_pct < 0:
        return None, ""
    # read+write service time can exceed wall time with concurrent I/O; clamp so
    # the value reads as "% of time the disk was busy" (100 = saturated).
    return min(100.0, best_pct), best_device


def _fmt(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else ""


def _na(value: float | None) -> str:
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
    "py_read_mbps",
    "py_write_mbps",
    "chrome_procs",
    "chrome_rss_mb",
    "chrome_uss_mb",
    "chrome_vms_mb",
    "chrome_handles",
    "chrome_threads",
    "chrome_read_mbps",
    "chrome_write_mbps",
    "phys_percent",
    "swap_used_mb",
    "swap_percent",
    "commit_used_mb",
    "commit_limit_mb",
    "commit_percent",
    "cpu_pct",
    "py_cpu_pct",
    "chrome_cpu_pct",
    "disk_read_mbps",
    "disk_write_mbps",
    "disk_busy_pct",
    "disk_busy_dev",
    "disk_pct_time",
    "disk_queue_len",
    "disk_ms_per_io",
    "disk_free_gb",
    "gpu_mem_used_mb",
    "gpu_mem_total_mb",
    "gpu_util_pct",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval", type=float, default=30.0, help="Seconds between samples"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Directory to write a timestamped usage CSV into (created if "
        "missing). One file per run, named usage-<start-timestamp>.csv.",
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
        "--to-destination",
        type=Path,
        action="append",
        default=None,
        help="The same path you pass to the download --to flag; its volume's free "
        "space is reported. Repeatable. Default: the profile dir volume. Disk "
        "busy% and read/write rates cover all disks regardless of this.",
    )
    parser.add_argument(
        "--once", action="store_true", help="Take a single sample and exit"
    )
    args = parser.parse_args()

    profile_dir: Path = args.profile_dir or browser_profile_dir(find_repo_root())
    profile_marker = str(profile_dir)
    python_pattern = re.compile(args.python_match)
    watch_paths: list[Path] = args.to_destination or [profile_dir]

    out_dir: Path | None = args.out
    out_path: Path | None = None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        start_stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        out_path = out_dir / f"usage-{start_stamp}.csv"
        out_path.write_text(_csv_row(_CSV_HEADER), encoding="utf-8")

    gpu_enabled = _gpu_stats() is not None
    disk_latency_enabled = (
        sys.platform.startswith("win") and _disk_latency() is not None
    )
    py_cpu_tracker = CpuTracker()
    chrome_cpu_tracker = CpuTracker()
    psutil.cpu_percent(interval=None)  # prime system CPU%

    print(f"Monitoring Chromium tree under: {profile_marker}")
    print(f"Python match: /{args.python_match}/")
    if out_path is not None:
        print(f"Writing CSV: {out_path}")
    print(f"Disk free watched: {', '.join(str(p) for p in watch_paths)}")
    print(
        f"GPU sampling: {'on (nvidia-smi)' if gpu_enabled else 'off (nvidia-smi not found)'}"
    )
    print(
        "Disk latency (typeperf): "
        + ("on" if disk_latency_enabled else "off (Windows only)")
    )
    print(f"Interval: {args.interval}s" + ("  (single sample)" if args.once else ""))
    print()

    prev_mono: float | None = None
    prev_py_write: int | None = None
    prev_chrome_write: int | None = None
    prev_chrome_read: int | None = None
    prev_py_read: int | None = None
    prev_disk_read: int | None = None
    prev_disk_write: int | None = None
    prev_disk_busy: dict[str, float] = {}

    peak_disk_busy = 0.0
    peak_disk_write = 0.0
    peak_chrome_uss = 0.0
    peak_commit = 0.0
    peak_cpu = 0.0
    peak_disk_time = 0.0
    peak_disk_queue = 0.0

    try:
        while True:
            timestamp = datetime.now().isoformat(timespec="seconds")
            now_mono = time.monotonic()
            elapsed = (now_mono - prev_mono) if prev_mono is not None else None

            python_grp, chrome_grp = _matching_processes(profile_marker, python_pattern)
            python_sample = _group_sample(python_grp)
            chrome_sample = _group_sample(chrome_grp)
            system = _system_mem()
            disk = _disk_sample(watch_paths)
            gpu = _gpu_stats() if gpu_enabled else None
            latency = _disk_latency() if disk_latency_enabled else None
            system_cpu = psutil.cpu_percent(interval=None)
            py_cpu = py_cpu_tracker.group_cpu_percent(python_grp)
            chrome_cpu = chrome_cpu_tracker.group_cpu_percent(chrome_grp)

            py_read = _rate_mbps(python_sample.read_bytes, prev_py_read, elapsed)
            py_write = _rate_mbps(python_sample.write_bytes, prev_py_write, elapsed)
            chrome_read = _rate_mbps(
                chrome_sample.read_bytes, prev_chrome_read, elapsed
            )
            chrome_write = _rate_mbps(
                chrome_sample.write_bytes, prev_chrome_write, elapsed
            )
            disk_read = _rate_mbps(disk.read_bytes, prev_disk_read, elapsed)
            disk_write = _rate_mbps(disk.write_bytes, prev_disk_write, elapsed)
            disk_busy, busy_dev = _busy_percent(
                disk.busy_by_device, prev_disk_busy, elapsed
            )

            peak_disk_busy = max(peak_disk_busy, disk_busy or 0.0)
            peak_disk_write = max(peak_disk_write, disk_write or 0.0)
            peak_chrome_uss = max(peak_chrome_uss, chrome_sample.uss_mb or 0.0)
            peak_commit = max(peak_commit, system.commit_percent)
            peak_cpu = max(peak_cpu, system_cpu)
            if latency is not None:
                peak_disk_time = max(peak_disk_time, latency.pct_disk_time)
                peak_disk_queue = max(peak_disk_queue, latency.queue_length)

            lat_str = (
                f"  |  lat: %time={latency.pct_disk_time:.0f}% "
                f"q={latency.queue_length:.1f} {latency.ms_per_io:.1f}ms/io"
                if latency is not None
                else ""
            )
            print(
                f"{timestamp}  "
                f"py: {python_sample.proc_count}p uss={_na(python_sample.uss_mb)}MB "
                f"cpu={py_cpu:.0f}%  |  "
                f"chrome: {chrome_sample.proc_count}p "
                f"uss={_na(chrome_sample.uss_mb)}MB vms={_fmt(chrome_sample.vms_mb)}MB "
                f"cpu={chrome_cpu:.0f}% wr={_na(chrome_write)}MB/s  |  "
                f"sys: cpu={system_cpu:.0f}% ram={system.phys_percent:.0f}% "
                f"commit={system.commit_percent:.0f}%  |  "
                f"disk: rd={_na(disk_read)} wr={_na(disk_write)}MB/s "
                f"busy={_na(disk_busy)}%{f'({busy_dev})' if busy_dev else ''} "
                f"free={_na(disk.free_gb)}GB  |  "
                f"gpu: mem={_na(gpu.mem_used_mb) if gpu else 'n/a'}MB "
                f"util={_na(gpu.util_percent) if gpu else 'n/a'}%"
                f"{lat_str}",
                flush=True,
            )

            if out_path is not None:
                with out_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        _csv_row(
                            [
                                timestamp,
                                str(python_sample.proc_count),
                                _fmt(python_sample.rss_mb),
                                _fmt(python_sample.uss_mb),
                                _fmt(python_sample.vms_mb),
                                str(python_sample.handles or ""),
                                str(python_sample.threads),
                                _fmt(py_read),
                                _fmt(py_write),
                                str(chrome_sample.proc_count),
                                _fmt(chrome_sample.rss_mb),
                                _fmt(chrome_sample.uss_mb),
                                _fmt(chrome_sample.vms_mb),
                                str(chrome_sample.handles or ""),
                                str(chrome_sample.threads),
                                _fmt(chrome_read),
                                _fmt(chrome_write),
                                _fmt(system.phys_percent),
                                _fmt(system.swap_used_mb),
                                _fmt(system.swap_percent),
                                _fmt(system.commit_used_mb),
                                _fmt(system.commit_limit_mb),
                                _fmt(system.commit_percent),
                                _fmt(system_cpu),
                                _fmt(py_cpu),
                                _fmt(chrome_cpu),
                                _fmt(disk_read),
                                _fmt(disk_write),
                                _fmt(disk_busy),
                                busy_dev,
                                _fmt(latency.pct_disk_time) if latency else "",
                                _fmt(latency.queue_length) if latency else "",
                                _fmt(latency.ms_per_io) if latency else "",
                                _fmt(disk.free_gb),
                                _fmt(gpu.mem_used_mb) if gpu else "",
                                _fmt(gpu.mem_total_mb) if gpu else "",
                                _fmt(gpu.util_percent) if gpu else "",
                            ]
                        )
                    )

            prev_mono = now_mono
            prev_py_read = python_sample.read_bytes
            prev_py_write = python_sample.write_bytes
            prev_chrome_read = chrome_sample.read_bytes
            prev_chrome_write = chrome_sample.write_bytes
            prev_disk_read = disk.read_bytes
            prev_disk_write = disk.write_bytes
            prev_disk_busy = disk.busy_by_device

            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")

    print()
    print(
        f"Peaks  cpu={peak_cpu:.0f}%  disk %time={peak_disk_time:.0f}%  "
        f"disk queue={peak_disk_queue:.1f}  disk write={peak_disk_write:.1f}MB/s  "
        f"chrome uss={peak_chrome_uss:.1f}MB  commit={peak_commit:.0f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
