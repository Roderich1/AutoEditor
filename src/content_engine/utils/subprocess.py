from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from content_engine.domain.exceptions import ExternalToolError, ExternalToolNotFoundError

#: Probing metadata is fast; a hang means something is wrong with the input.
PROBE_TIMEOUT_SECONDS = 60.0
#: Transcoding a long recording is legitimately slow, but never unbounded.
TRANSCODE_TIMEOUT_SECONDS = 3600.0


def run_command(
    arguments: Sequence[str],
    timeout: float | None = PROBE_TIMEOUT_SECONDS,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an external tool with an argument list, never a shell string.

    ``cwd`` runs the tool somewhere else. It exists for one reason: a filename
    a tool resolves relative to its own working directory never has to be
    escaped into a syntax that tool parses. It is checked before the call
    because ``subprocess`` reports a missing working directory as the same
    ``FileNotFoundError`` a missing executable produces, and reporting "ffmpeg
    was not found" for a directory problem sends the reader somewhere else
    entirely.
    """
    executable = arguments[0]
    if cwd is not None and not cwd.is_dir():
        raise ExternalToolError(f"{executable} cannot be run in {cwd}, which is not a directory")
    try:
        return subprocess.run(
            list(arguments),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            cwd=None if cwd is None else str(cwd),
        )
    except FileNotFoundError as error:
        raise ExternalToolNotFoundError(
            f"{executable} was not found. Install it and make sure it is on PATH."
        ) from error
    except subprocess.TimeoutExpired as error:
        raise ExternalToolError(f"{executable} timed out after {timeout} seconds") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip() or (error.stdout or "").strip() or str(error)
        raise ExternalToolError(f"{executable} failed: {detail}") from error
