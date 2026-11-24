import json
from pathlib import Path

import pytest

from idrive_backup_helper.profiles.profiles import (
    ProfileError,
    anon_log_argv,
    describe_profile,
    download_argv,
    find_latest_manifest,
    load_profile,
    load_profiles,
    profile_from_args,
    retry_argv,
    save_profile,
    validate_profile_name,
)

URL = "https://www.idrive.com/idrive/home/PC/D/Family Photos"


def test_save_and_load_round_trip_awkward_values(tmp_path: Path) -> None:
    config = tmp_path / "secrets" / "config.toml"
    profile = profile_from_args(
        {
            "command": "download-folder",
            "url": URL,
            "to": 'D:\\Backup\\"Quoted" Ünïcode',
            "headed": False,
            "timeout_ms": 1000,
            "exclude": ["*.tmp", "Private/**"],
            "browser_debug_url": None,
            "with_anon_log": "",
        }
    )

    save_profile(config, "photos", profile)
    save_profile(config, "other", {"command": "download-folder", "url": URL})

    loaded = load_profiles(config)
    assert loaded["photos"] == {
        "command": "download-folder",
        "url": URL,
        "to": 'D:\\Backup\\"Quoted" Ünïcode',
        "timeout_ms": 1000,
        "exclude": ["*.tmp", "Private/**"],
        "with_anon_log": True,
    }
    assert set(loaded) == {"photos", "other"}


def test_profile_names_are_plain_keys() -> None:
    assert validate_profile_name("photos-2024_a") == "photos-2024_a"
    with pytest.raises(ProfileError):
        validate_profile_name("my photos")
    with pytest.raises(ProfileError):
        validate_profile_name("a.b")


def test_load_profile_rejects_unknown_names(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="No profile named 'nope'"):
        load_profile(tmp_path / "config.toml", "nope")


def test_download_argv_rebuilds_the_flags() -> None:
    argv = download_argv(
        {
            "command": "download-folder",
            "url": URL,
            "to": "D:/Backup",
            "headed": True,
            "cooldown_ms": 900,
            "exclude": ["*.tmp", "--odd"],
            "with_anon_log": True,
        }
    )

    assert argv == [
        "download-folder",
        f"--url={URL}",
        "--to=D:/Backup",
        "--headed",
        "--cooldown-ms=900",
        "--exclude=*.tmp",
        "--exclude=--odd",
    ]


def test_anon_log_argv() -> None:
    assert anon_log_argv(None) == []
    assert anon_log_argv(False) == []
    assert anon_log_argv(True) == ["--with-anon-log"]
    assert anon_log_argv("") == ["--with-anon-log"]
    assert anon_log_argv("D:/logs") == ["--with-anon-log=D:/logs"]


def test_retry_argv_keeps_only_browser_settings(tmp_path: Path) -> None:
    manifest = tmp_path / "download-folder-run-1.ndjson"

    argv = retry_argv(
        {"url": URL, "to": "D:/x", "overwrite": "skip", "timeout_ms": 5},
        manifest,
    )

    assert argv == ["retry-manifest", f"--manifest={manifest}", "--timeout-ms=5"]


def test_describe_profile_hides_private_values() -> None:
    lines = describe_profile(
        {
            "command": "download-folder",
            "url": URL,
            "to": "D:/Backup/Family Photos",
            "exclude": ["Private/**"],
            "overwrite": "skip",
            "with_anon_log": "D:/Secret Logs",
        }
    )

    text = "\n".join(lines)
    assert "overwrite: skip" in text
    assert "url: set" in text
    assert "exclude: 1 pattern(s)" in text
    for private in ("Family", "Private", "Secret", "idrive.com"):
        assert private not in text


def _write_manifest(path: Path, url: str, destination: str) -> None:
    header = {"type": "run", "url": url, "destination": destination}
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")


def test_find_latest_manifest_matches_url_and_destination(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    older = tmp_path / "download-folder-run-2026-01-01T10-00-00.ndjson"
    newer = tmp_path / "download-folder-run-2026-02-01T10-00-00.ndjson"
    other = tmp_path / "download-folder-run-2026-03-01T10-00-00.ndjson"
    retry = tmp_path / "retry-manifest-run-2026-04-01T10-00-00.ndjson"
    _write_manifest(older, URL, str(destination))
    _write_manifest(newer, URL + "/", str(destination))
    _write_manifest(other, URL + "/Other", str(destination))
    _write_manifest(retry, URL, str(destination))
    (tmp_path / "download-folder-run-2026-05-01T10-00-00.ndjson").write_text(
        "not json\n", encoding="utf-8"
    )

    found = find_latest_manifest(tmp_path, url=URL, destination=destination)

    assert found == newer
    assert find_latest_manifest(tmp_path, url=URL, destination=tmp_path / "x") is None
