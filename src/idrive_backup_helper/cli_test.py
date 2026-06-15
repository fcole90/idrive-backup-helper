from idrive_backup_helper.cli import build_parser


def test_build_parser_accepts_auth_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["auth"])

    assert args.command == "auth"
    assert args.url == "https://www.idrive.com/idrive/login/loginForm"


def test_build_parser_accepts_auth_url_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["auth", "--url", "https://example.com"])

    assert args.command == "auth"
    assert args.url == "https://example.com"


def test_build_parser_accepts_browse_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["browse"])

    assert args.command == "browse"
    assert args.url == "https://www.idrive.com/idrive/home"


def test_build_parser_accepts_browse_url_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["browse", "--url", "https://example.com/home"])

    assert args.command == "browse"
    assert args.url == "https://example.com/home"


def test_build_parser_accepts_download_folder_command() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "download-folder",
            "--url",
            "https://example.com/folder",
            "--to",
            "/tmp/output",
        ]
    )

    assert args.command == "download-folder"
    assert args.url == "https://example.com/folder"
    assert str(args.to) == "/tmp/output"
    assert args.headed is False
    assert args.timeout_ms == 120_000
    assert args.cooldown_ms == 1_500
    assert args.overwrite == "skip"
    assert args.no_folder_cache is False
    assert args.no_resume_logs is False


def test_build_parser_accepts_download_folder_resume_and_cache_overrides() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "download-folder",
            "--url",
            "https://example.com/folder",
            "--to",
            "/tmp/output",
            "--no-folder-cache",
            "--no-resume-logs",
        ]
    )

    assert args.command == "download-folder"
    assert args.no_folder_cache is True
    assert args.no_resume_logs is True


def test_build_parser_accepts_verify_manifest_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["verify-manifest", "--manifest", "/tmp/run.json"])

    assert args.command == "verify-manifest"
    assert str(args.manifest) == "/tmp/run.json"


def test_build_parser_accepts_retry_manifest_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["retry-manifest", "--manifest", "/tmp/run.json"])

    assert args.command == "retry-manifest"
    assert str(args.manifest) == "/tmp/run.json"
    assert args.headed is False
    assert args.timeout_ms == 120_000
    assert args.cooldown_ms == 1_500
    assert args.overwrite == "replace"
