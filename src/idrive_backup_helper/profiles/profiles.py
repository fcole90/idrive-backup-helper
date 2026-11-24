"""Saved run profiles: a download-folder command's flags, stored under a name.

The file lives in the protected secrets folder because a profile holds the
IDrive URL and the local destination. An agent starts a profile by name and
never needs to see its values; `describe_profile` shows which settings exist
without printing the private ones.
"""

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from idrive_backup_helper.browser.downloads.download_manifest import (
    read_manifest_header,
)
from idrive_backup_helper.browser.downloads.folder_urls import normalized_folder_url

PROFILE_COMMAND = "download-folder"
PRIVATE_KEYS = frozenset({"url", "to", "exclude", "browser_debug_url", "with_anon_log"})
# download-folder flags a retry of the same profile carries over.
RETRY_KEYS = ("headed", "timeout_ms", "cooldown_ms", "browser_debug_url")
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

type ProfileValue = str | int | bool | list[str]
type Profile = dict[str, ProfileValue]


class ProfileError(RuntimeError):
    pass


def validate_profile_name(name: str) -> str:
    if not _PROFILE_NAME_RE.match(name):
        raise ProfileError(
            "A profile name uses only letters, digits, '-' and '_' (at most 40)."
        )
    return name


def load_profiles(config_path: Path) -> dict[str, Profile]:
    if not config_path.exists():
        return {}
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        # The message names a line and column, never a value.
        raise ProfileError(f"Cannot read the profile config: {error}") from error
    profiles = document.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ProfileError("The profile config's [profiles] is not a table.")
    return {
        str(name): cast(Profile, profile)
        for name, profile in cast(dict[str, object], profiles).items()
        if isinstance(profile, dict)
    }


def load_profile(config_path: Path, name: str) -> Profile:
    profiles = load_profiles(config_path)
    if name not in profiles:
        raise ProfileError(f"No profile named {name!r}; list them with: config list")
    profile = profiles[name]
    if profile.get("command", PROFILE_COMMAND) != PROFILE_COMMAND:
        raise ProfileError(f"Profile {name!r} is not a {PROFILE_COMMAND} profile.")
    return profile


def _toml_value(value: ProfileValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(item) for item in value) + "]"
    # A JSON string is a valid TOML basic string.
    return json.dumps(value)


def render_profiles(profiles: Mapping[str, Profile]) -> str:
    lines = [
        "# IDrive backup helper run profiles. Private: holds URLs and paths.",
        "# Written by `uv run main config save`; edits by hand are kept, comments are not.",
    ]
    for name in sorted(profiles):
        lines.append("")
        lines.append(f"[profiles.{name}]")
        for key, value in profiles[name].items():
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def save_profile(config_path: Path, name: str, profile: Profile) -> None:
    validate_profile_name(name)
    profiles = load_profiles(config_path)
    profiles[name] = profile
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(config_path.name + ".tmp")
    temporary.write_text(render_profiles(profiles), encoding="utf-8")
    temporary.replace(config_path)


def profile_from_args(values: Mapping[str, object]) -> Profile:
    """Keep the explicit, non-empty settings of a parsed download-folder run."""
    profile: Profile = {"command": PROFILE_COMMAND}
    for key, value in values.items():
        if key == "command" or value is None or value is False or value == []:
            continue
        if key == "with_anon_log" and value == "":
            profile[key] = True
        elif isinstance(value, (bool, int)):
            profile[key] = value
        elif isinstance(value, (list, tuple)):
            profile[key] = [str(item) for item in cast(Sequence[object], value)]
        else:
            profile[key] = str(value)
    return profile


def _flag(key: str) -> str:
    return "--" + key.replace("_", "-")


def _append_option(argv: list[str], key: str, value: ProfileValue) -> None:
    if isinstance(value, bool):
        if value:
            argv.append(_flag(key))
    elif isinstance(value, list):
        for item in value:
            argv.append(f"{_flag(key)}={item}")
    else:
        argv.append(f"{_flag(key)}={value}")


def anon_log_argv(setting: ProfileValue | None) -> list[str]:
    if setting is None or setting is False:
        return []
    if setting is True or setting == "":
        return ["--with-anon-log"]
    return [f"--with-anon-log={setting}"]


def download_argv(profile: Mapping[str, ProfileValue]) -> list[str]:
    argv = [PROFILE_COMMAND]
    for key, value in profile.items():
        if key not in ("command", "with_anon_log"):
            _append_option(argv, key, value)
    return argv


def retry_argv(profile: Mapping[str, ProfileValue], manifest_path: Path) -> list[str]:
    argv = ["retry-manifest", f"--manifest={manifest_path}"]
    for key in RETRY_KEYS:
        if key in profile:
            _append_option(argv, key, profile[key])
    return argv


def describe_profile(profile: Mapping[str, ProfileValue]) -> list[str]:
    """One line per setting; private values show only that they are set."""
    lines: list[str] = []
    for key, value in profile.items():
        if key == "command":
            continue
        if key == "exclude" and isinstance(value, list):
            lines.append(f"{key}: {len(value)} pattern(s)")
        elif key == "with_anon_log":
            where = "default folder" if value is True else "custom folder"
            lines.append(f"{key}: on ({where})")
        elif key in PRIVATE_KEYS:
            lines.append(f"{key}: set")
        else:
            lines.append(f"{key}: {value}")
    return lines


def find_latest_manifest(runs_dir: Path, *, url: str, destination: Path) -> Path | None:
    """The newest download-folder manifest for this URL and destination."""
    wanted_url = normalized_folder_url(url)
    wanted_destination = destination.expanduser().resolve()
    candidates = sorted(
        [
            *runs_dir.glob("download-folder-run-*.ndjson"),
            *runs_dir.glob("download-folder-run-*.json"),
        ],
        key=lambda path: path.name,
        reverse=True,
    )
    for manifest_path in candidates:
        try:
            header = read_manifest_header(manifest_path)
        except OSError, ValueError, RuntimeError:
            continue
        if (
            normalized_folder_url(header.url) == wanted_url
            and header.destination.expanduser().resolve() == wanted_destination
        ):
            return manifest_path
    return None
