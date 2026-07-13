from datetime import datetime
from pathlib import Path
import sys

from idrive_backup_helper.browser.downloads.download_diagnostics import (
    BrowserCrashContext,
    ResourceSnapshot,
    browser_cmdline_markers,
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
        browser_rss_mb=4096.0,
        browser_process_count=18,
        system_available_mb=128.0,
        system_used_percent=94.0,
    )


def test_render_crash_report_flags_hung_browser_when_processes_outlive_the_cdp_probe() -> (
    None
):
    # The 2026-07-05 report declared "the whole browser process is gone" while its
    # own data said the process was still running — an unanswered probe was being
    # read as a death certificate. A browser that is swapping or wedged stops
    # serving CDP long before it exits, and killing it is a different fix from
    # waiting for it, so the two must not be reported as one thing.
    health = BrowserHealthReport(
        mode="launched-cdp",
        cdp_url="http://127.0.0.1:9222",
        cdp_reachable=False,
        cdp_version=None,
        detached_pid=18604,
        detached_exit_code=None,
        detached_running=True,
        chromium_log_tail=None,
        browser_processes_on_profile=12,
    )

    report = render_crash_report(
        context=_context(),
        health=health,
        resources=_resources(),
        captured_at=datetime(2026, 7, 5, 14, 1, 0),
    )

    assert "hung, not gone" in report
    assert "whole browser process is gone" not in report
    assert "Browser processes on profile: 12" in report


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
        browser_processes_on_profile=0,
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
    assert "Browser tree RSS: 4096.0 MB (18 process(es))" in report
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
    # No browser markers given: no browser tree to measure.
    assert snapshot.browser_rss_mb is None
    assert snapshot.browser_process_count is None


def test_capture_resource_snapshot_measures_marker_matched_process_tree() -> None:
    # Use our own interpreter path as the marker: this process always matches,
    # proving the cmdline scan + tree RSS aggregation works end to end.
    snapshot = capture_resource_snapshot([sys.executable])

    assert snapshot is not None
    assert snapshot.browser_rss_mb is not None
    assert snapshot.browser_rss_mb > 0
    assert snapshot.browser_process_count is not None
    assert snapshot.browser_process_count >= 1


def test_browser_cmdline_markers_are_chrome_specific_switches() -> None:
    markers = browser_cmdline_markers(Path("/data/profile"), "http://127.0.0.1:9222")

    assert markers == [
        "--user-data-dir=/data/profile",
        "--remote-debugging-port=9222",
    ]


def test_browser_cmdline_markers_without_debug_url_only_match_profile() -> None:
    markers = browser_cmdline_markers(Path("/data/profile"), None)

    assert markers == ["--user-data-dir=/data/profile"]
