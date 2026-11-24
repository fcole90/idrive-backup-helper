import argparse
from collections.abc import Sequence
from pathlib import Path

from idrive_backup_helper.browser.downloads.download_filters import (
    validate_exclude_pattern,
)
from idrive_backup_helper.browser.downloads.downloads import (
    download_current_folder,
    retry_missing_files_from_manifest,
    verify_download_manifest,
)
from idrive_backup_helper.browser.engine import DEFAULT_BROWSER_DEBUG_URL
from idrive_backup_helper.browser.session import (
    DEFAULT_BROWSE_URL,
    login_and_save_state,
    open_authenticated_browser,
)
from idrive_backup_helper.filesystem.paths import (
    browser_profile_dir,
    find_repo_root,
    default_anon_log_dir,
    migrate_private_layout,
    runs_dir,
)
from idrive_backup_helper.runlog.anon_sink import open_anon_run_log
from idrive_backup_helper.runlog.console import log, log_error, use_sink

DEFAULT_TIMEOUT_MS = 120 * 60 * 1_000


def _exclude_pattern_arg(value: str) -> str:
    # ArgumentTypeError keeps the message; a plain ValueError would be replaced
    # by argparse's generic "invalid value" text.
    try:
        return validate_exclude_pattern(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _add_anon_log_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--with-anon-log",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help=(
            "Also write an anonymized log, safe to share with an AI agent, to DIR "
            "(default .agents/local/playground/anon-logs), and print the anonymized "
            "lines to the terminal; the raw logs stay under .agents/local/secrets/"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="IDrive backup helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser(
        "auth", help="Open a headed browser and cache IDrive login state"
    )
    auth_parser.add_argument(
        "--url",
        default="https://www.idrive.com/idrive/login/loginForm",
        help="IDrive URL to open for login",
    )
    auth_parser.add_argument(
        "--browser-debug-url",
        default=DEFAULT_BROWSER_DEBUG_URL,
        help="Chromium remote debugging URL for the persistent browser session",
    )

    browse_parser = subparsers.add_parser(
        "browse",
        help="Open an authenticated IDrive browser for manual folder inspection",
    )
    browse_parser.add_argument(
        "--url",
        default=DEFAULT_BROWSE_URL,
        help="IDrive URL to open in the authenticated browser",
    )
    browse_parser.add_argument(
        "--browser-debug-url",
        default=DEFAULT_BROWSER_DEBUG_URL,
        help="Chromium remote debugging URL for the persistent browser session",
    )

    download_parser = subparsers.add_parser(
        "download-folder",
        help="List visible files in one IDrive web folder (download step pending)",
    )
    download_parser.add_argument("--url", required=True, help="IDrive folder URL")
    download_parser.add_argument(
        "--to",
        required=True,
        type=Path,
        help="Local destination directory for downloaded files",
    )
    download_parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed mode for debugging",
    )
    download_parser.add_argument(
        "--timeout-ms",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
        help="Playwright timeout for page operations",
    )
    download_parser.add_argument(
        "--cooldown-ms",
        type=int,
        default=1_500,
        help="Pause between file download interactions",
    )
    download_parser.add_argument(
        "--overwrite",
        choices=["skip", "replace", "fail"],
        default="skip",
        help="How to handle already-existing destination files",
    )
    download_parser.add_argument(
        "--no-folder-cache",
        action="store_true",
        help="Disable cached folder listings and always re-scan folder contents",
    )
    download_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        type=_exclude_pattern_arg,
        metavar="GLOB",
        help=(
            "Skip files matching GLOB (repeatable, case-insensitive; quote it so "
            "the shell does not expand it). Without '/' it matches the file name "
            "at any depth; with '/' it matches the path relative to --to, where "
            "'*' stays within one folder and '**' spans any depth"
        ),
    )
    download_parser.add_argument(
        "--no-resume-logs",
        action="store_true",
        help="Disable resume indexing from prior manifests for this URL/destination",
    )
    download_parser.add_argument(
        "--browser-debug-url",
        default=None,
        help=(
            "Attach to a persistent Chromium browser over CDP. "
            "Defaults to the local session when --headed is set."
        ),
    )

    verify_parser = subparsers.add_parser(
        "verify-manifest",
        help="Verify which files from a download manifest are still missing locally",
    )
    verify_parser.add_argument("--manifest", required=True, type=Path)
    _add_anon_log_argument(verify_parser)

    retry_parser = subparsers.add_parser(
        "retry-manifest",
        help="Retry only missing files from a manifest generated by download-folder",
    )
    retry_parser.add_argument("--manifest", required=True, type=Path)
    retry_parser.add_argument("--headed", action="store_true")
    retry_parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    retry_parser.add_argument("--cooldown-ms", type=int, default=1_500)
    retry_parser.add_argument(
        "--overwrite",
        choices=["skip", "replace", "fail"],
        default="replace",
    )
    retry_parser.add_argument(
        "--browser-debug-url",
        default=None,
        help=(
            "Attach to a persistent Chromium browser over CDP. "
            "Defaults to the local session when --headed is set."
        ),
    )

    _add_anon_log_argument(download_parser)
    _add_anon_log_argument(retry_parser)

    return parser


def _run_auth(url: str, browser_debug_url: str) -> None:
    repo_root = find_repo_root()
    profile_dir = browser_profile_dir(repo_root)
    staged_downloads_dir = runs_dir(repo_root)
    login_and_save_state(
        profile_dir=profile_dir,
        downloads_dir=staged_downloads_dir,
        start_url=url,
        browser_debug_url=browser_debug_url,
    )

    log(
        None,
        "Opened persistent browser session at: {browser_debug_url}",
        browser_debug_url=browser_debug_url,
    )
    log(None, "Browser state path: {profile_dir}", profile_dir=profile_dir)
    log(
        None,
        "Next step: uv run main browse or uv run main download-folder --url <FOLDER_URL> --to <DEST_PATH>",
    )


def _run_browse(url: str, browser_debug_url: str) -> None:
    repo_root = find_repo_root()
    profile_dir = browser_profile_dir(repo_root)
    staged_downloads_dir = runs_dir(repo_root)
    open_authenticated_browser(
        profile_dir=profile_dir,
        downloads_dir=staged_downloads_dir,
        start_url=url,
        browser_debug_url=browser_debug_url,
    )
    log(
        None,
        "Opened persistent browser session at: {browser_debug_url}",
        browser_debug_url=browser_debug_url,
    )


def _run_download_folder(
    *,
    url: str,
    destination: Path,
    headed: bool,
    timeout_ms: int,
    cooldown_ms: int,
    overwrite: str,
    browser_debug_url: str | None,
    no_folder_cache: bool,
    no_resume_logs: bool,
    exclude_patterns: list[str],
) -> int:
    repo_root = find_repo_root()
    profile_dir = browser_profile_dir(repo_root)
    effective_browser_debug_url = browser_debug_url
    if effective_browser_debug_url is None and headed:
        effective_browser_debug_url = DEFAULT_BROWSER_DEBUG_URL

    if not profile_dir.exists() and effective_browser_debug_url is None:
        raise RuntimeError("Missing browser auth state. Run: uv run main auth")

    staged_downloads_dir = runs_dir(repo_root)
    effective_headless = not headed and effective_browser_debug_url is None

    report = download_current_folder(
        profile_dir=profile_dir,
        downloads_dir=staged_downloads_dir,
        url=url,
        destination=destination,
        headless=effective_headless,
        timeout_ms=timeout_ms,
        cooldown_ms=cooldown_ms,
        overwrite=overwrite,
        browser_debug_url=effective_browser_debug_url,
        use_folder_cache=not no_folder_cache,
        resume_from_logs=not no_resume_logs,
        exclude_patterns=exclude_patterns,
    )

    log(None, "Destination: {destination}", destination=destination)
    log(None, "Downloaded: {downloaded}", downloaded=report.counts.downloaded)
    log(None, "Skipped: {skipped}", skipped=report.counts.skipped)
    log(None, "Failed: {failed}", failed=report.counts.failed)
    if report.folders_unavailable:
        log(
            None,
            "Unavailable folders (skipped): {folders_unavailable}",
            folders_unavailable=report.folders_unavailable,
        )
    if report.progress_log_path is not None:
        log(
            None,
            "Progress log: {progress_log_path}",
            progress_log_path=report.progress_log_path,
        )
    log(None, "Manifest: {manifest_path}", manifest_path=report.manifest_path)
    return report.exit_code


def _run_verify_manifest(manifest_path: Path) -> int:
    verification = verify_download_manifest(manifest_path)
    log(None, "Manifest: {manifest_path}", manifest_path=manifest_path)
    log(
        None,
        "Expected files: {expected_files}",
        expected_files=verification.expected_files,
    )
    log(
        None, "Present files: {present_files}", present_files=verification.present_files
    )
    log(
        None, "Missing files: {missing_files}", missing_files=verification.missing_files
    )
    return verification.exit_code


def _run_retry_manifest(
    *,
    manifest_path: Path,
    headed: bool,
    timeout_ms: int,
    cooldown_ms: int,
    overwrite: str,
    browser_debug_url: str | None,
) -> int:
    repo_root = find_repo_root()
    profile_dir = browser_profile_dir(repo_root)
    effective_browser_debug_url = browser_debug_url
    if effective_browser_debug_url is None and headed:
        effective_browser_debug_url = DEFAULT_BROWSER_DEBUG_URL

    if not profile_dir.exists() and effective_browser_debug_url is None:
        raise RuntimeError("Missing browser auth state. Run: uv run main auth")

    staged_downloads_dir = runs_dir(repo_root)
    effective_headless = not headed and effective_browser_debug_url is None

    report = retry_missing_files_from_manifest(
        profile_dir=profile_dir,
        downloads_dir=staged_downloads_dir,
        manifest_path=manifest_path,
        headless=effective_headless,
        timeout_ms=timeout_ms,
        cooldown_ms=cooldown_ms,
        overwrite=overwrite,
        browser_debug_url=effective_browser_debug_url,
    )
    log(None, "Manifest: {manifest_path}", manifest_path=report.manifest_path)
    log(None, "Retried downloads: {downloaded}", downloaded=report.counts.downloaded)
    log(None, "Skipped: {skipped}", skipped=report.counts.skipped)
    log(None, "Failed: {failed}", failed=report.counts.failed)
    if report.progress_log_path is not None:
        log(
            None,
            "Progress log: {progress_log_path}",
            progress_log_path=report.progress_log_path,
        )
    return report.exit_code


def _migrate_private_layout() -> None:
    repo_root = find_repo_root()
    for old, new in migrate_private_layout(repo_root):
        log(
            None,
            "Moved {old_path} to {new_path} (private data now lives under a protected secrets/ folder)",
            old_path=old.relative_to(repo_root),
            new_path=new.relative_to(repo_root),
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    anon_dir_arg: str | None = getattr(args, "with_anon_log", None)
    if anon_dir_arg is None:
        return _dispatch(parser, args)
    return _main_with_anon_log(parser, args, anon_dir_arg)


def _private_values(args: argparse.Namespace) -> dict[str, object]:
    """The run's private arguments, keyed like log fields."""
    names = {
        "url": "url",
        "to": "destination",
        "manifest": "manifest_path",
        "exclude": "exclude_patterns",
    }
    return {
        field: getattr(args, attribute)
        for attribute, field in names.items()
        if getattr(args, attribute, None) is not None
    }


def _repo_relative(path: Path, repo_root: Path) -> Path:
    try:
        return path.resolve().relative_to(repo_root)
    except ValueError:
        return path


def _main_with_anon_log(
    parser: argparse.ArgumentParser, args: argparse.Namespace, anon_dir_arg: str
) -> int:
    repo_root = find_repo_root()
    anon_dir = (
        default_anon_log_dir(repo_root) if anon_dir_arg == "" else Path(anon_dir_arg)
    )
    sink = open_anon_run_log(
        repo_root=repo_root,
        command=args.command,
        anon_dir=anon_dir,
        private_values=_private_values(args),
    )
    with use_sink(sink):
        log(
            None,
            "Anonymized log: {anon_log_path}",
            anon_log_path=_repo_relative(sink.anon_log_path, repo_root),
        )
        # A traceback would print private paths and URLs to the terminal, so
        # every exception is caught: the full traceback goes to the raw console
        # log in the secrets folder, and the terminal gets a scrubbed line.
        try:
            return _dispatch(parser, args)
        except KeyboardInterrupt as error:
            sink.record_exception(error)
            log_error("Interrupted")
            return 130
        except Exception as error:
            sink.record_exception(error)
            log_error(
                "Unexpected {error_type}: {error}",
                error_type=type(error).__name__,
                error=str(error),
            )
            return 1


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    try:
        _migrate_private_layout()
        if args.command == "auth":
            _run_auth(args.url, args.browser_debug_url)
            return 0
        if args.command == "browse":
            _run_browse(args.url, args.browser_debug_url)
            return 0
        if args.command == "download-folder":
            return _run_download_folder(
                url=args.url,
                destination=args.to,
                headed=args.headed,
                timeout_ms=args.timeout_ms,
                cooldown_ms=args.cooldown_ms,
                overwrite=args.overwrite,
                browser_debug_url=args.browser_debug_url,
                no_folder_cache=args.no_folder_cache,
                no_resume_logs=args.no_resume_logs,
                exclude_patterns=args.exclude,
            )
        if args.command == "verify-manifest":
            return _run_verify_manifest(args.manifest)
        if args.command == "retry-manifest":
            return _run_retry_manifest(
                manifest_path=args.manifest,
                headed=args.headed,
                timeout_ms=args.timeout_ms,
                cooldown_ms=args.cooldown_ms,
                overwrite=args.overwrite,
                browser_debug_url=args.browser_debug_url,
            )
    except RuntimeError as error:
        log_error("{error}", error=str(error))
        return 2

    parser.error(f"Unsupported command: {args.command}")
    return 2
