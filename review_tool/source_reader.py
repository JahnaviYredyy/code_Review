
from pathlib import Path


def read_source(file_path: str) -> str:
    """Read a UTF-8 text file without executing it."""
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(
            f"File not found: {path}"
        )

    if not path.is_file():
        raise ValueError(
            f"Path is not a file: {path}"
        )

    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"File is not valid UTF-8 text: {path}"
        ) from exc
