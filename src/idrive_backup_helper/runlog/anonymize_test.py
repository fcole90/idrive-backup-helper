import re
from pathlib import Path

from idrive_backup_helper.runlog.anonymize import Anonymizer, load_or_create_key

KEY = bytes(range(32))


def _anon() -> Anonymizer:
    return Anonymizer(KEY)


def test_name_tokens_are_stable_and_keyed() -> None:
    first = _anon().name("Holiday Plans")
    again = _anon().name("Holiday Plans")
    other_key = Anonymizer(bytes(32)).name("Holiday Plans")

    assert first == again
    assert first != other_key
    assert re.fullmatch(r"n~[0-9a-f]{8}", first)


def test_name_keeps_only_real_looking_extensions() -> None:
    anon = _anon()

    assert anon.name("Tax Return.pdf").endswith(".pdf")
    assert anon.name("IMG_001.JPG").endswith(".jpg")
    # "Smith" is part of the name, not an extension, and must not leak.
    assert "smith" not in anon.name("Mr.Smith").lower()


def test_path_tokenizes_each_segment_so_siblings_share_their_folder() -> None:
    anon = _anon()

    first = anon.path(r"D:\Backup\Family Photos\2024\IMG_001.jpg")
    second = anon.path(r"D:\Backup\Family Photos\2024\IMG_002.jpg")

    assert first.startswith("D:\\")
    assert first.rsplit("\\", 1)[0] == second.rsplit("\\", 1)[0]
    assert first != second
    assert first.endswith(".jpg")
    for private in ("Backup", "Family", "Photos", "2024", "IMG_001"):
        assert private not in first


def test_path_keeps_tool_folders_and_run_file_names(tmp_path: Path) -> None:
    anon = _anon()
    run_file = (
        tmp_path
        / ".agents"
        / "local"
        / "secrets"
        / "idrive"
        / "runs"
        / "download-folder-run-2026-06-15T10-00-00.ndjson"
    )

    result = anon.path(run_file)

    assert result.endswith(
        "/.agents/local/secrets/idrive/runs/download-folder-run-2026-06-15T10-00-00.ndjson".replace(
            "/", "\\" if "\\" in str(run_file) else "/"
        )
    )


def test_url_keeps_idrive_routes_and_matches_local_folder_tokens() -> None:
    anon = _anon()

    remote = anon.url(
        "https://www.idrive.com/idrive/home/DESKTOP-ABC_123/F/BACKUP%202/Taxes"
    )
    local = anon.path(r"E:\copy\BACKUP 2")

    assert remote.startswith("https://www.idrive.com/idrive/home/d~")
    for private in ("DESKTOP", "BACKUP", "Taxes"):
        assert private not in remote
    # The remote folder and its local copy get the same token.
    assert local.rsplit("\\", 1)[1] in remote


def test_url_tokenizes_download_query_but_keeps_numbers() -> None:
    result = _anon().url(
        "https://evsweb5505.idrive.com/evs/v1/downloadFile"
        "?version=0&p=%2FTaxes%2Freturn.pdf"
    )

    assert result.startswith("https://evsweb5505.idrive.com/evs/v1/downloadFile?")
    assert "version=0" in result
    assert "Taxes" not in result
    assert "return" not in result


def test_url_tokenizes_foreign_hosts() -> None:
    result = _anon().url("https://files.example-family.net/share/Holiday")

    assert "example-family" not in result
    assert "Holiday" not in result


def test_text_scrubs_playwright_errors_but_keeps_their_shape() -> None:
    error = (
        "Timeout 30000ms exceeded.\n"
        "Call log:\n"
        '  - waiting for locator("text=\\"Secret Report.pdf\\"")\n'
        '  - navigated to "https://www.idrive.com/idrive/home/DEV/Private%20Stuff"'
    )

    result = _anon().text(error)

    assert "Timeout 30000ms exceeded." in result
    assert "Call log:" in result
    for private in ("Secret", "Report", "Private", "Stuff", "DEV"):
        assert private not in result


def test_text_keeps_idrive_error_codes() -> None:
    result = _anon().text('Download failed: {"desc":"INVALID PATH"}')

    assert result == 'Download failed: {"desc":"INVALID PATH"}'


def test_text_replaces_names_seen_earlier_even_unquoted() -> None:
    anon = _anon()
    token = anon.name("Holiday Plans")

    result = anon.text("Could not open Holiday Plans after 3 tries")

    assert result == f"Could not open {token} after 3 tries"


def test_text_scrubs_paths_with_spaces_urls_and_emails() -> None:
    result = _anon().text(
        r"Cannot move D:\Backup\My Photos\beach.jpg for someone@example.org "
        "from https://example.org/a/b"
    )

    for private in ("Backup", "Photos", "beach", "someone", "example"):
        assert private not in result
    assert result.startswith("Cannot move D:")


def test_text_scrubs_registered_home_and_user(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "jane-doe"
    anon = Anonymizer(KEY, private_paths=[home], private_names=["jane-doe"])

    result = anon.text(f"profile at {home} owned by jane-doe")

    assert "jane-doe" not in result


def test_text_is_idempotent_on_its_own_output() -> None:
    anon = _anon()
    once = anon.text(
        r"Failed D:\Backup\Family\a.pdf at https://www.idrive.com/idrive/home/X/Y"
    )

    assert anon.text(once) == once


def test_load_or_create_key_persists_one_key(tmp_path: Path) -> None:
    key_path = tmp_path / "secrets" / "anon-key"

    first = load_or_create_key(key_path)
    second = load_or_create_key(key_path)

    assert first == second
    assert len(first) == 32
