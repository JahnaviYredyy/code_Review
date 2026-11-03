#This file discovers all readable code files

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "site-packages",
    "dist",
    "build",
    ".tox",
    ".idea",
    ".vscode",
}

DEFAULT_IGNORED_FILES = {
    ".env",
    "credentials.json",
    "token.json",
}

MAX_FILE_SIZE = 1_000_000  # 1 MB


@dataclass
class RepositoryFile:
    path: str
    relative_path: str
    size: int
    extension: str


@dataclass
class ScanResult:
    repository_path: str
    files: list[RepositoryFile]
    skipped_files: int
    errors: list[str]

    @property
    def total_files(self) -> int:
        return len(self.files)


def scan_repository(
    repository_path: str,
    ignored_dirs: set[str] | None = None,
    max_file_size: int = MAX_FILE_SIZE,
) -> ScanResult:
    """Discover Python files in a repository without modifying them (read-only)."""
    root = Path(repository_path).expanduser().resolve()

    if not root.exists():
        raise FileNotFoundError(
            f"Repository path does not exist: {root}"
        )

    if not root.is_dir():
        raise NotADirectoryError(
            f"Provided path is not a directory: {root}"
        )

    ignored = DEFAULT_IGNORED_DIRS.copy()
    if ignored_dirs:
        ignored.update(ignored_dirs)

    discovered_files: list[RepositoryFile] = []
    skipped_files = 0
    errors: list[str] = []

    for current_dir, dirs, filenames in os.walk(
        root,
        followlinks=False,
    ):
        current_path = Path(current_dir)

        # Do not enter ignored directories or symbolic links.
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in ignored
            and not (current_path / directory).is_symlink()
        ]

        for filename in filenames:
            file_path = current_path / filename

            # Only inspect Python source files.
            if file_path.suffix != ".py":
                continue

            if filename in DEFAULT_IGNORED_FILES:
                skipped_files += 1
                continue

            try:
                if file_path.is_symlink():
                    skipped_files += 1
                    continue

                size = file_path.stat().st_size
                if size > max_file_size:
                    skipped_files += 1
                    continue

                relative_path = file_path.relative_to(root)

                discovered_files.append(
                    RepositoryFile(
                        path=str(file_path),
                        relative_path=relative_path.as_posix(),
                        size=size,
                        extension=file_path.suffix,
                    )
                )

            except OSError as exc:
                errors.append(f"{file_path}: {exc}")

    discovered_files.sort(
        key=lambda item: item.relative_path.lower()
    )

    return ScanResult(
        repository_path=str(root),
        files=discovered_files,
        skipped_files=skipped_files,
        errors=errors,
    )

