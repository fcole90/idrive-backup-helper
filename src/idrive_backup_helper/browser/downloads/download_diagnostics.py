from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import psutil

from idrive_backup_helper.browser.engine import BrowserHealthReport

_MB = 1024 * 1024
_PROC_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess)


@dataclass(frozen=True)
class ResourceSnapshot:
    """Memory/handle usage of this process, the browser tree, and the system.

    Captured at the moment the browser dies (and per folder as telemetry) so a
    crash can be correlated with renderer heap growth and system memory pressure
    — the confirmed cause class of the 2026-07-05 tab death.
    """

    process_rss_mb: float
    process_uss_mb: float | None
    process_handles: int | None
    process_threads: int | None
    browser_rss_mb: float | None
    browser_process_count: int | None
    system_available_mb: float
    system_used_percent: float


@dataclass(frozen=True)
class BrowserCrashContext:
    error: str
    elapsed_seconds: float
    folders_processed: int
    current_folder_url: str | None
    current_folder_destination: str | None
    discovered: int
    downloaded: int
    skipped: int
    failed: int


def _process_handles(proc: psutil.Process) -> int | None:
    # num_handles on Windows, num_fds on Unix; not every platform exposes both.
    getter = getattr(proc, "num_handles", None) or getattr(proc, "num_fds", None)
    if getter is None:
        return None
    try:
        return int(getter())
    except _PROC_GONE:
        return None


def browser_cmdline_markers(
    profile_dir: Path, browser_debug_url: str | None
) -> list[str]:
    """Chrome-specific cmdline substrings identifying the run's browser processes.

    ``--user-data-dir`` matches an owned or self-launched browser;
    ``--remote-debugging-port`` matches an externally launched attached browser
    that may use a different profile. Both are Chrome switches, so our own Python
    process can never match.
    """
    markers = [f"--user-data-dir={profile_dir}"]
    if browser_debug_url is not None:
        port = urlparse(browser_debug_url).port
        if port is not None:
            markers.append(f"--remote-debugging-port={port}")
    return markers


def _browser_tree_memory(
    cmdline_markers: Sequence[str],
) -> tuple[float, int] | None:
    """Sum RSS over browser processes matched by cmdline marker, plus descendants.

    Markers are Chrome-specific switches (``--user-data-dir=…``,
    ``--remote-debugging-port=…``) so our own Python process never matches. Roots
    are matched by cmdline; children are collected recursively because not every
    Chrome child process repeats the switches.
    """
    if not cmdline_markers:
        return None

    roots: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if cmdline and any(marker in cmdline for marker in cmdline_markers):
            roots.append(proc)

    tree_by_pid: dict[int, psutil.Process] = {}
    for root in roots:
        tree_by_pid[root.pid] = root
        try:
            for child in root.children(recursive=True):
                tree_by_pid[child.pid] = child
        except _PROC_GONE:
            continue

    total_rss = 0
    counted = 0
    for proc in tree_by_pid.values():
        try:
            total_rss += proc.memory_info().rss
        except _PROC_GONE:
            continue
        counted += 1

    if counted == 0:
        return None
    return total_rss / _MB, counted


def capture_resource_snapshot(
    browser_cmdline_markers: Sequence[str] = (),
) -> ResourceSnapshot | None:
    # Best-effort: a failure here must never mask the browser-death it describes.
    try:
        proc = psutil.Process()
        mem = proc.memory_info()
        try:
            uss_mb: float | None = proc.memory_full_info().uss / _MB
        except _PROC_GONE:
            uss_mb = None
        try:
            threads: int | None = proc.num_threads()
        except _PROC_GONE:
            threads = None
        try:
            browser_memory = _browser_tree_memory(browser_cmdline_markers)
        except Exception:
            browser_memory = None
        virtual = psutil.virtual_memory()
        return ResourceSnapshot(
            process_rss_mb=mem.rss / _MB,
            process_uss_mb=uss_mb,
            process_handles=_process_handles(proc),
            process_threads=threads,
            browser_rss_mb=browser_memory[0] if browser_memory else None,
            browser_process_count=browser_memory[1] if browser_memory else None,
            system_available_mb=virtual.available / _MB,
            system_used_percent=virtual.percent,
        )
    except Exception:
        return None


def _format_mb(value: float | None) -> str:
    return f"{value:.1f} MB" if value is not None else "unknown"


def _format_int(value: int | None) -> str:
    return str(value) if value is not None else "unknown"


def _format_flag(value: bool | None) -> str:
    return str(value) if value is not None else "unknown"


def _classify_browser(health: BrowserHealthReport) -> str:
    if health.cdp_reachable is True:
        return (
            "The CDP endpoint still answers, so the **whole browser is alive** — only "
            "our tab/page was closed (closed by the user/OS, or the renderer crashed)."
        )
    if health.cdp_reachable is False:
        if health.browser_processes_on_profile:
            # An unanswered probe is not a death certificate: a browser that is
            # swapping or wedged stops serving CDP while its processes keep running.
            return (
                "The CDP endpoint does not answer, but "
                f"{health.browser_processes_on_profile} browser process(es) are still "
                "running on our profile, so the browser is **hung, not gone** — or it "
                "was too busy to answer the probe in time."
            )
        return (
            "The CDP endpoint is unreachable and no browser process is running on our "
            "profile, so the **whole browser process is gone** (it exited or crashed)."
        )
    return (
        "No CDP endpoint to probe (owned browser context), so tab-close vs full "
        "browser-exit cannot be distinguished from here."
    )


def render_crash_report(
    *,
    context: BrowserCrashContext,
    health: BrowserHealthReport,
    resources: ResourceSnapshot | None,
    captured_at: datetime,
) -> str:
    lines: list[str] = [
        "# Browser closed mid-run — crash diagnostics",
        "",
        f"Captured at: {captured_at.isoformat(timespec='seconds')}",
        "",
        "## Abort",
        "",
        f"- Error: {context.error}",
        f"- Elapsed run time: {context.elapsed_seconds:.0f}s "
        f"({context.elapsed_seconds / 3600:.2f}h)",
        f"- Folders processed: {context.folders_processed}",
        f"- Current folder URL: {context.current_folder_url or 'unknown'}",
        f"- Current folder destination: "
        f"{context.current_folder_destination or 'unknown'}",
        "",
        "## Files so far",
        "",
        f"- Discovered: {context.discovered}",
        f"- Downloaded: {context.downloaded}",
        f"- Skipped: {context.skipped}",
        f"- Failed: {context.failed}",
        "",
        "## Browser health",
        "",
        _classify_browser(health),
        "",
        f"- Mode: {health.mode}",
        f"- CDP URL: {health.cdp_url or 'n/a'}",
        f"- CDP reachable: {_format_flag(health.cdp_reachable)}",
        f"- CDP version: {health.cdp_version or 'n/a'}",
        f"- Detached PID: {_format_int(health.detached_pid)}",
        f"- Detached still running: {_format_flag(health.detached_running)}",
        f"- Detached exit code: {_format_int(health.detached_exit_code)}",
        f"- Browser processes on profile: "
        f"{_format_int(health.browser_processes_on_profile)}",
        "",
        "## Resources at death",
        "",
    ]
    if resources is None:
        lines.append("- (resource snapshot unavailable)")
    else:
        lines.extend(
            [
                f"- Process RSS: {_format_mb(resources.process_rss_mb)}",
                f"- Process USS: {_format_mb(resources.process_uss_mb)}",
                f"- Process handles/fds: {_format_int(resources.process_handles)}",
                f"- Process threads: {_format_int(resources.process_threads)}",
                f"- Browser tree RSS: {_format_mb(resources.browser_rss_mb)} "
                f"({_format_int(resources.browser_process_count)} process(es))",
                f"- System memory available: "
                f"{_format_mb(resources.system_available_mb)}",
                f"- System memory used: {resources.system_used_percent:.1f}%",
            ]
        )
    lines.extend(
        [
            "",
            "## Chromium log tail",
            "",
            "```",
            health.chromium_log_tail or "(no Chromium log captured for this browser)",
            "```",
            "",
        ]
    )
    return "\n".join(lines)
