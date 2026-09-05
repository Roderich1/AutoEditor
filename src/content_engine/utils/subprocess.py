from __future__ import annotations

import subprocess
from collections.abc import Sequence

from content_engine.domain.exceptions import ExternalProviderError


def run_command(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(arguments),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as error:
        raise ExternalProviderError(f"Executable not found: {arguments[0]}") from error
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip() or error.stdout.strip() or str(error)
        raise ExternalProviderError(message) from error
