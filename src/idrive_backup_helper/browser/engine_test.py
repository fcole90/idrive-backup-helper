from pathlib import Path

import pytest

from idrive_backup_helper.browser.engine import (
    BrowserConfig,
    BrowserEngine,
    DETACHED_CHROMIUM_STARTUP_FLAGS,
    ensure_browser_executable,
    remove_stale_browser_profile_lock_files,
)


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeBrowserContext:
    def __init__(self) -> None:
        self.closed = False
        self.timeout_ms: int | None = None
        self.page = FakePage("https://www.idrive.com/idrive/home/current")
        self.pages = [self.page]
        self.new_page_calls = 0

    def set_default_timeout(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms

    def new_page(self) -> FakePage:
        self.new_page_calls += 1
        return self.page

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, context: FakeBrowserContext) -> None:
        self.contexts = [context]
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    executable_path = "/tmp/chromium"

    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.connected_urls: list[str] = []

    def connect_over_cdp(self, endpoint_url: str, *, timeout: int) -> FakeBrowser:
        self.connected_urls.append(endpoint_url)
        assert timeout == 5_000
        return self.browser


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium


class FakePlaywrightContext:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright
        self.entered = False
        self.exited = False

    def __enter__(self) -> FakePlaywright:
        self.entered = True
        return self.playwright

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self.exited = True


def test_browser_engine_attaches_to_cdp_without_closing_browser_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = FakeBrowserContext()
    browser = FakeBrowser(context)
    chromium = FakeChromium(browser)
    playwright_context = FakePlaywrightContext(FakePlaywright(chromium))
    monkeypatch.setattr(
        "idrive_backup_helper.browser.engine.sync_playwright",
        lambda: playwright_context,
    )

    config = BrowserConfig(
        profile_dir=tmp_path / "profile",
        staging_dir=tmp_path / "downloads",
        headless=False,
        timeout_ms=1234,
        browser_debug_url="http://127.0.0.1:9222",
    )

    with BrowserEngine(config) as engine:
        assert engine.new_page() is context.page

    assert chromium.connected_urls == ["http://127.0.0.1:9222"]
    assert context.timeout_ms == 1234
    assert context.closed is False
    assert browser.closed is False
    assert playwright_context.exited is True


def test_browser_engine_logs_detached_browser_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = FakeBrowserContext()
    browser = FakeBrowser(context)
    chromium = FakeChromium(browser)
    playwright_context = FakePlaywrightContext(FakePlaywright(chromium))
    monkeypatch.setattr(
        "idrive_backup_helper.browser.engine.sync_playwright",
        lambda: playwright_context,
    )

    config = BrowserConfig(
        profile_dir=tmp_path / "profile",
        staging_dir=tmp_path / "downloads",
        headless=False,
        timeout_ms=1234,
        browser_debug_url="http://127.0.0.1:9222",
    )

    with BrowserEngine(config) as engine:
        engine.new_page()

    output = capsys.readouterr().out
    assert "[browser-session] Connecting to Chromium CDP endpoint" in output
    assert "[browser-session] Attached to existing Chromium CDP endpoint" in output
    assert (
        "[browser-session] Using detached browser context with 1 existing page(s)"
        in output
    )
    assert "[browser-session] Opening new page in browser context" in output
    assert (
        "[browser-session] Command finished; leaving detached browser running" in output
    )


def test_browser_engine_reuses_current_page_without_opening_new_tab(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = FakeBrowserContext()
    browser = FakeBrowser(context)
    chromium = FakeChromium(browser)
    playwright_context = FakePlaywrightContext(FakePlaywright(chromium))
    monkeypatch.setattr(
        "idrive_backup_helper.browser.engine.sync_playwright",
        lambda: playwright_context,
    )

    config = BrowserConfig(
        profile_dir=tmp_path / "profile",
        staging_dir=tmp_path / "downloads",
        headless=False,
        timeout_ms=1234,
        browser_debug_url="http://127.0.0.1:9222",
    )

    with BrowserEngine(config) as engine:
        assert engine.current_page_or_new_page() is context.page

    assert context.new_page_calls == 0


def _health_config(tmp_path: Path, browser_debug_url: str | None) -> BrowserConfig:
    return BrowserConfig(
        profile_dir=tmp_path / "profile",
        staging_dir=tmp_path / "downloads",
        headless=False,
        timeout_ms=1234,
        browser_debug_url=browser_debug_url,
    )


def test_describe_browser_health_classifies_reachable_cdp_as_tab_only_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_probe(url: str, **_: object) -> str | None:
        return "Chrome/120.0" if url == "http://127.0.0.1:9222" else None

    monkeypatch.setattr(
        "idrive_backup_helper.browser.engine._probe_cdp_version", fake_probe
    )

    # Attached to an already-running browser (no owned detached process), and the
    # CDP endpoint still answers -> only our tab/page closed.
    engine = BrowserEngine(_health_config(tmp_path, "http://127.0.0.1:9222"))
    health = engine.describe_browser_health()

    assert health.mode == "attached-cdp"
    assert health.cdp_reachable is True
    assert health.cdp_version == "Chrome/120.0"
    assert health.detached_pid is None
    assert health.detached_running is None


def test_describe_browser_health_classifies_unreachable_cdp_as_dead_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_probe(_url: str, **_: object) -> str | None:
        return None

    monkeypatch.setattr(
        "idrive_backup_helper.browser.engine._probe_cdp_version", fake_probe
    )

    engine = BrowserEngine(_health_config(tmp_path, "http://127.0.0.1:9222"))
    health = engine.describe_browser_health()

    assert health.cdp_reachable is False
    assert health.cdp_version is None


def test_describe_browser_health_owned_context_has_no_cdp_probe(
    tmp_path: Path,
) -> None:
    engine = BrowserEngine(_health_config(tmp_path, browser_debug_url=None))
    health = engine.describe_browser_health()

    assert health.mode == "owned-context"
    assert health.cdp_url is None
    assert health.cdp_reachable is None
    assert health.chromium_log_tail is None


def test_ensure_browser_executable_reports_setup_command_for_missing_browser(
    tmp_path: Path,
) -> None:
    executable_path = tmp_path / "chromium" / "chrome"

    with pytest.raises(RuntimeError, match="uv run poe browser-setup"):
        ensure_browser_executable(executable_path)


def test_ensure_browser_executable_reuses_existing_browser(tmp_path: Path) -> None:
    executable_path = tmp_path / "chromium" / "chrome"
    executable_path.parent.mkdir(parents=True)
    executable_path.write_text("", encoding="utf-8")

    resolved_path = ensure_browser_executable(executable_path)

    assert resolved_path == executable_path


def test_detached_chromium_startup_flags_include_linux_sandbox_workaround() -> None:
    assert "--no-sandbox" in DETACHED_CHROMIUM_STARTUP_FLAGS


def test_remove_stale_browser_profile_lock_files_removes_dead_local_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    monkeypatch.setattr("socket.gethostname", lambda: "current-host")
    monkeypatch.setattr("os.kill", _raise_process_lookup_error)
    for file_name in ("SingletonCookie", "SingletonLock", "SingletonSocket"):
        (profile_dir / file_name).write_text(f"current-host-999999", encoding="utf-8")

    removed_paths = remove_stale_browser_profile_lock_files(profile_dir)

    assert [path.name for path in removed_paths] == [
        "SingletonCookie",
        "SingletonLock",
        "SingletonSocket",
    ]
    for removed_path in removed_paths:
        assert not removed_path.exists()


def test_remove_stale_browser_profile_lock_files_keeps_running_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    monkeypatch.setattr("socket.gethostname", lambda: "current-host")
    monkeypatch.setattr("os.kill", _ignore_process_signal)
    (profile_dir / "SingletonLock").write_text("current-host-1234", encoding="utf-8")

    removed_paths = remove_stale_browser_profile_lock_files(profile_dir)

    assert removed_paths == []
    assert (profile_dir / "SingletonLock").exists()


def _raise_process_lookup_error(pid: int, signal: int) -> None:
    raise ProcessLookupError(pid)


def _ignore_process_signal(pid: int, signal: int) -> None:
    return None
