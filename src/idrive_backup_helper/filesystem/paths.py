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
