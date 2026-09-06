from __future__ import annotations

import subprocess
from collections.abc import Sequence

from content_engine.domain.exceptions import ExternalToolError, ExternalToolNotFoundError

#: Probing metadata is fast; a hang means something is wrong with the input.
PROBE_TIMEOUT_SECONDS = 60.0
#: Transcoding a long recording is legitimately slow, but never unbounded.
TRANSCODE_TIMEOUT_SECONDS = 3600.0


def run_command(
    arguments: Sequence[str],
    timeout: float | None = PROBE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run an external tool with an argument list, never a shell string."""
    executable = arguments[0]
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
