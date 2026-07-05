from datetime import datetime

from idrive_backup_helper.browser.downloads.download_diagnostics import (
    BrowserCrashContext,
    ResourceSnapshot,
    capture_resource_snapshot,
    render_crash_report,
)
from idrive_backup_helper.browser.engine import BrowserHealthReport


def _context() -> BrowserCrashContext:
    return BrowserCrashContext(
        error="Browser was closed mid-run",
        elapsed_seconds=7325.0,
        folders_processed=42,
        current_folder_url="https://www.idrive.com/idrive/home/DEV/RoslynNet46",
        current_folder_destination="D:/backup/RoslynNet46",
        discovered=1000,
        downloaded=930,
        skipped=60,
        failed=10,
    )


def _resources() -> ResourceSnapshot:
    return ResourceSnapshot(
        process_rss_mb=512.5,
        process_uss_mb=480.25,
        process_handles=350,
        process_threads=12,
        system_available_mb=128.0,
        system_used_percent=94.0,
    )


def test_render_crash_report_flags_dead_browser_when_cdp_unreachable() -> None:
    health = BrowserHealthReport(
        mode="attached-cdp",
        cdp_url="http://127.0.0.1:9222",
        cdp_reachable=False,
        cdp_version=None,
        detached_pid=None,
        detached_exit_code=None,
        detached_running=None,
        chromium_log_tail=None,
    )

    report = render_crash_report(
        context=_context(),
        health=health,
        resources=_resources(),
        captured_at=datetime(2026, 7, 5, 14, 1, 0),
    )

    assert "whole browser process is gone" in report
    assert "Folders processed: 42" in report
    assert "Downloaded: 930" in report
    assert "System memory available: 128.0 MB" in report
    assert "(no Chromium log captured for this browser)" in report


def test_render_crash_report_flags_tab_only_close_when_cdp_reachable() -> None:
    health = BrowserHealthReport(
        mode="launched-cdp",
        cdp_url="http://127.0.0.1:9222",
        cdp_reachable=True,
        cdp_version="Chrome/120.0",
        detached_pid=4321,
        detached_exit_code=None,
        detached_running=True,
        chromium_log_tail="Last Chromium startup log lines:\n[boom] renderer crashed",
    )

    report = render_crash_report(
        context=_context(),
        health=health,
        resources=None,
        captured_at=datetime(2026, 7, 5, 14, 1, 0),
    )

    assert "whole browser is alive" in report
    assert "renderer crashed" in report
    assert "(resource snapshot unavailable)" in report


def test_capture_resource_snapshot_reports_this_process_memory() -> None:
    snapshot = capture_resource_snapshot()

    assert snapshot is not None
    assert snapshot.process_rss_mb > 0
    assert snapshot.system_available_mb > 0
    assert 0 <= snapshot.system_used_percent <= 100
