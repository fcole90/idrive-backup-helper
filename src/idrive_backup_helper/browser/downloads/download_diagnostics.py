from dataclasses import dataclass
from datetime import datetime

import psutil

from idrive_backup_helper.browser.engine import BrowserHealthReport

_MB = 1024 * 1024
_PROC_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess)


@dataclass(frozen=True)
class ResourceSnapshot:
    """Memory/handle usage of *this* process plus system-wide memory pressure.

    Captured at the moment the browser dies so a crash report can be correlated
    with the confirmed ~35 MB/hr heap climb and the suspected low-memory freeze.
    """

    process_rss_mb: float
    process_uss_mb: float | None
    process_handles: int | None
    process_threads: int | None
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


def capture_resource_snapshot() -> ResourceSnapshot | None:
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
        virtual = psutil.virtual_memory()
        return ResourceSnapshot(
            process_rss_mb=mem.rss / _MB,
            process_uss_mb=uss_mb,
            process_handles=_process_handles(proc),
            process_threads=threads,
            system_available_mb=virtual.available / _MB,
            system_used_percent=virtual.percent,
        )
    except Exception:
        return None


def _format_mb(value: float | None) -> str:
    return f"{value:.1f} MB" if value is not None else "unknown"


def _format_int(value: int | None) -> str:
    return str(value) if value is not None else "unknown"


def _classify_browser(health: BrowserHealthReport) -> str:
    if health.cdp_reachable is True:
        return (
            "The CDP endpoint still answers, so the **whole browser is alive** — only "
            "our tab/page was closed (closed by the user/OS, or the renderer crashed)."
        )
    if health.cdp_reachable is False:
        return (
            "The CDP endpoint is unreachable, so the **whole browser process is gone** "
            "(it exited or crashed)."
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
        f"- CDP reachable: {health.cdp_reachable}",
        f"- CDP version: {health.cdp_version or 'n/a'}",
        f"- Detached PID: {_format_int(health.detached_pid)}",
        f"- Detached still running: {health.detached_running}",
        f"- Detached exit code: {_format_int(health.detached_exit_code)}",
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
