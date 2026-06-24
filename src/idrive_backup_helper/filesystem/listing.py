import os
from pathlib import Path


def existing_entry_names(directory: Path) -> set[str]:
    """Return the names of entries directly inside ``directory``.

    One directory scan replaces many per-file ``Path.exists()`` stats. On slow
    destinations (external USB drives, network or FUSE mounts) each uncached
    ``stat()`` is a round-trip that can cost on the order of a second, so scanning
    the directory once and testing membership in memory is dramatically faster.
    Returns an empty set when the directory does not exist yet.
    """
    try:
        with os.scandir(directory) as entries:
            return {entry.name for entry in entries}
    except FileNotFoundError, NotADirectoryError:
        return set()


class DirectoryListingCache:
    """Answer per-file existence checks with one directory scan per directory.

    Caches each directory's entry names on first use, so checking many files under
    the same directory costs a single ``scandir`` instead of one ``stat`` per file.

    The cache is a point-in-time snapshot: it is not refreshed when files are
    created after a directory is first scanned. That suits skip/resume decisions,
    which only ask whether a file was already present when its folder was reached,
    not whether a file written later in the same run exists.
    """

    def __init__(self) -> None:
        self._names_by_directory: dict[Path, set[str]] = {}

    def contains(self, path: Path) -> bool:
        directory = path.parent
        names = self._names_by_directory.get(directory)
        if names is None:
            names = existing_entry_names(directory)
            self._names_by_directory[directory] = names
        return path.name in names
