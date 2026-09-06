import json
from pathlib import Path
from typing import Any

from content_engine.domain.exceptions import CorruptArtifactError


def _write_atomic(path: Path, content: str) -> None:
    """Write UTF-8 with LF endings through a temporary file.

    Every run artifact is written this way, not only JSON: a reader must never
    observe a half-written transcript, and a failure must not leave a partial
    file at the final path or a ``.tmp`` beside it. The newline translation
    Python applies by default on Windows is disabled explicitly so a run
    produced on Windows is byte-comparable with the same run on Linux.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_json(path: Path, value: Any) -> None:
    """Write a run artifact atomically as UTF-8 with LF line endings.

    Serialization happens before anything is created on disk, so an
    unserializable value fails without leaving a temporary file behind.
    """
    _write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_text(path: Path, value: str) -> None:
    """Write a text artifact atomically as UTF-8 with LF line endings."""
    _write_atomic(path, value)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CorruptArtifactError(f"Cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise CorruptArtifactError(f"{path} is not valid JSON: {error}") from error
