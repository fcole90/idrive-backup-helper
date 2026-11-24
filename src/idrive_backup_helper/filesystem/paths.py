import os
from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    start_dir = (start or Path.cwd()).resolve()

    for candidate in (start_dir, *start_dir.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / ".git").exists():
            return candidate

    raise RuntimeError("Could not find repository root from current working directory.")


def local_dir(repo_root: Path) -> Path:
    return repo_root / ".agents" / "local"


def playground_dir(repo_root: Path) -> Path:
    return local_dir(repo_root) / "playground"


def private_dir(repo_root: Path) -> Path:
    # Everything that names the user's files, folders, IDrive URLs, or sign-in
    # lives here. The repo's agent policy denies reads under any `secrets/`
    # folder, so agents can read only the anonymized logs, never these.
    return local_dir(repo_root) / "secrets" / "idrive"


def browser_profile_dir(repo_root: Path) -> Path:
    return private_dir(repo_root) / "browser-state" / "idrive-chromium"


def runs_dir(repo_root: Path) -> Path:
    """Raw progress logs, manifests, crash reports, and the folder cache."""
    return private_dir(repo_root) / "runs"


def anon_key_path(repo_root: Path) -> Path:
    return private_dir(repo_root) / "anon-key"


def config_path(repo_root: Path) -> Path:
    return private_dir(repo_root) / "config.toml"


def default_anon_log_dir(repo_root: Path) -> Path:
    return playground_dir(repo_root) / "anon-logs"


def _legacy_moves(repo_root: Path) -> tuple[tuple[Path, Path], ...]:
    old_playground = playground_dir(repo_root)
    return (
        (old_playground / "browser-state", private_dir(repo_root) / "browser-state"),
        (old_playground / "downloads", runs_dir(repo_root)),
    )


def migrate_private_layout(repo_root: Path) -> list[tuple[Path, Path]]:
    """Move private data out of the readable playground, once.

    Returns the (old, new) pairs that moved. Raises when both the old and the
    new folder exist, rather than merging or overwriting either.
    """
    moved: list[tuple[Path, Path]] = []
    for old, new in _legacy_moves(repo_root):
        if not old.exists():
            continue
        if new.exists():
            raise RuntimeError(
                f"Both {old.relative_to(repo_root)} and {new.relative_to(repo_root)} "
                "exist. Merge them by hand, then remove the old one."
            )
        new.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old, new)
        moved.append((old, new))
    return moved


STAGING_DIR_NAME = ".idrive-staging"


def staging_dir_for_destination(destination: Path) -> Path:
    # Downloads must be staged on the destination's own volume so finalizing a
    # file is a same-volume rename, not a cross-device copy. A single dir at the
    # top of the --to root covers every child folder since they share the volume.
    return destination / STAGING_DIR_NAME
