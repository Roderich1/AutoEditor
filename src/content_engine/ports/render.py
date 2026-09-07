"""The render boundary (CE-045).

One Protocol over domain types. The service that orchestrates rendering holds
this rather than a concrete adapter, so its tests can place a failure exactly
where they need one -- a refused encode, a clip of the wrong size, a missing
audio track, a lost sample aspect ratio -- without a real encoder and without
asserting on an error message FFmpeg happens to print today.

The Protocol does not mention FFmpeg. Nothing about "cut this interval into a
vertical clip with these subtitles burned in" is specific to it, and the service
must not be able to tell which tool answered.

Reading a finished file back is not repeated here: ``MediaProbePort`` in
``ports.preview`` already describes exactly that, and a second Protocol with the
same shape would be two names for one boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from content_engine.domain.renders import RenderStageConfig


class ClipRendererPort(Protocol):
    """Produces one finished clip from one interval of a source."""

    def render(
        self,
        source: Path,
        start: float,
        duration: float,
        subtitles: Path | None,
        output: Path,
        config: RenderStageConfig,
    ) -> None:
        """Write the interval to ``output``, or raise ``RenderError``.

        ``subtitles`` is the ASS document to burn in, or None to burn nothing.
        The implementation owns the encode and nothing else: it does not decide
        where files go, does not clean up after a failure and does not verify
        what it produced, because those are the parts that must hold whichever
        encoder is behind this.
        """
        ...
