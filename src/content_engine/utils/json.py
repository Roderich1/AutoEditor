import json
from pathlib import Path
from typing import Any

from content_engine.domain.exceptions import CorruptArtifactError


def write_json(path: Path, value: Any) -> None:
    """Write a run artifact atomically as UTF-8 with LF line endings.

    Artifacts must be byte-comparable across operating systems, so the newline
    translation Python applies by default on Windows is disabled explicitly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def write_text(path: Path, value: str) -> None:
    """Write a text artifact as UTF-8 with LF line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CorruptArtifactError(f"Cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise CorruptArtifactError(f"{path} is not valid JSON: {error}") from error
