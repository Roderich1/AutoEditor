"""The preview boundary (CE-034).

Two Protocols over domain types. The service that orchestrates preview
generation holds these rather than concrete adapters, so its tests can place a
failure exactly where they need one -- a refused encode, a file of the wrong
size, a missing audio track -- without a real encoder and without asserting on
an error message FFmpeg happens to print today.

Neither Protocol mentions FFmpeg. Nothing about "produce a proxy of this
interval" or "tell me what this file contains" is specific to it, and the
service must not be able to tell which tool answered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from content_engine.domain.models import MediaInfo
from content_engine.domain.previews import PreviewStageConfig


class PreviewRendererPort(Protocol):
    """Produces one preview file from one interval of a source."""

    def render(
        self,
        source: Path,
        start: float,
        duration: float,
        output: Path,
        config: PreviewStageConfig,
    ) -> None:
        """Write the interval to ``output``, or raise ``RenderError``.

        The implementation owns nothing but the encode. It does not decide where
        the file goes, does not clean up after a failure and does not verify
        what it produced: the service places every path and checks every result,
        because those are the parts that must hold whichever encoder is behind
        this.
        """
        ...


class MediaProbePort(Protocol):
    """Reads back what a media file actually contains."""

    def probe(self, input_path: Path) -> tuple[MediaInfo, dict[str, Any]]: ...
