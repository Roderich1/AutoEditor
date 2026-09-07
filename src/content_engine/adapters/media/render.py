"""FFmpeg clip encoding (CE-045).

The adapter is deliberately almost empty. Every decision about what the command
contains lives in ``domain.render_rules`` as a pure function, and everything
about where files go and whether the result is acceptable lives in the service.
What is left here is the one thing that has to touch a process boundary.

ADR-007: FFmpeg is handed an argument list. There is no shell, no string
interpolation of a filename into a command, and no path through this module by
which transcript content could become an argument -- the only strings that reach
FFmpeg are three paths the service constructed, two formatted numbers and the
constants from the render policy. The subtitle text itself never appears in the
command at all: it is in a file the service wrote, and the filter is handed the
file's path.
"""

from pathlib import Path

from content_engine.domain.exceptions import ExternalToolError, RenderError
from content_engine.domain.render_rules import render_arguments
from content_engine.domain.renders import RenderStageConfig
from content_engine.utils.subprocess import TRANSCODE_TIMEOUT_SECONDS, run_command


class FFmpegClipRenderer:
    def render(
        self,
        source: Path,
        start: float,
        duration: float,
        subtitles: Path | None,
        output: Path,
        config: RenderStageConfig,
    ) -> None:
        """Encode one interval as a finished vertical clip.

        A failure is translated into ``RenderError`` here rather than left as
        ``ExternalToolError``, which ADR-018 keeps as an adapter-internal
        signal. The caller has to decide what a broken clip means for the run,
        and it can only do that if the failure names the stage it belongs to.

        The "reported success but produced nothing" case is not theoretical. On
        Windows FFmpeg uses the ANSI file APIs and is bound by ``MAX_PATH``:
        past 260 characters it exits 0 and writes no file. Catching it here is
        what turns that into a named failure rather than a confusing one three
        steps later, when the clip is probed.
        """
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            run_command(
                render_arguments(source, start, duration, subtitles, output, config),
                timeout=TRANSCODE_TIMEOUT_SECONDS,
            )
        except ExternalToolError as error:
            raise RenderError(f"FFmpeg could not render the clip {output.name}: {error}") from (
                error
            )
        if not output.is_file() or output.stat().st_size == 0:
            raise RenderError(f"FFmpeg reported success but produced no clip at {output}")
