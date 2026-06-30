from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    start_dir = (start or Path.cwd()).resolve()

    for candidate in (start_dir, *start_dir.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / ".git").exists():
            return candidate

    raise RuntimeError("Could not find repository root from current working directory.")


def playground_dir(repo_root: Path) -> Path:
    return repo_root / ".agents" / "playground"


def browser_profile_dir(repo_root: Path) -> Path:
    return playground_dir(repo_root) / "browser-state" / "idrive-chromium"


def downloads_dir(repo_root: Path) -> Path:
    return playground_dir(repo_root) / "downloads"


STAGING_DIR_NAME = ".idrive-staging"


def staging_dir_for_destination(destination: Path) -> Path:
    # Downloads must be staged on the destination's own volume so finalizing a
    # file is a same-volume rename, not a cross-device copy. A single dir at the
    # top of the --to root covers every child folder since they share the volume.
    return destination / STAGING_DIR_NAME
