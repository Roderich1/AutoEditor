"""FFmpeg preview encoding (CE-034).

The adapter is deliberately almost empty. Every decision about what the command
contains lives in ``domain.preview_rules`` as a pure function, and everything
about where files go and whether the result is acceptable lives in the service.
What is left here is the one thing that has to touch a process boundary.

ADR-007: FFmpeg is handed an argument list. There is no shell, no string
interpolation of a filename into a command, and no path through this module by
which transcript content could become an argument -- the only strings that reach
FFmpeg are two paths the service constructed, two formatted numbers and the
constants from the preview policy.
"""

from pathlib import Path

from content_engine.domain.exceptions import ExternalToolError, RenderError
from content_engine.domain.preview_rules import preview_arguments
from content_engine.domain.previews import PreviewStageConfig
from content_engine.utils.subprocess import TRANSCODE_TIMEOUT_SECONDS, run_command


class FFmpegPreviewRenderer:
    def render(
        self,
        source: Path,
        start: float,
        duration: float,
        output: Path,
        config: PreviewStageConfig,
    ) -> None:
        """Encode one interval as a low-cost vertical proxy.

        A failure is translated into ``RenderError`` here rather than left as
        ``ExternalToolError``, which ADR-018 keeps as an adapter-internal
        signal. The caller has to decide what a broken preview means for the
        run, and it can only do that if the failure names the stage it belongs
        to.
        """
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            run_command(
                preview_arguments(source, start, duration, output, config),
                timeout=TRANSCODE_TIMEOUT_SECONDS,
            )
        except ExternalToolError as error:
            raise RenderError(f"FFmpeg could not render the preview {output.name}: {error}") from (
                error
            )
        if not output.is_file() or output.stat().st_size == 0:
            raise RenderError(f"FFmpeg reported success but produced no preview at {output}")
