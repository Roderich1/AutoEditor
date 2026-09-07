"""FFmpeg clip encoding (CE-045).

The adapter is deliberately almost empty. Every decision about what the command
contains lives in ``domain.render_rules`` as a pure function, and everything
about where files go and whether the result is acceptable lives in the service.
What is left here is the one thing that has to touch a process boundary.

ADR-007: FFmpeg is handed an argument list. There is no shell, no string
interpolation of a filename into a command, and no path through this module by
which transcript content could become an argument -- the only strings that reach
FFmpeg are two absolute paths the service constructed, two formatted numbers and
the constants from the render policy. The subtitle text itself never appears in
the command at all: it is in a file the service wrote.

ADR-035: neither does the subtitle *path*. The filtergraph is the one string
FFmpeg parses rather than receives, and an option value inside it is unescaped
twice, so no escaping of an absolute path survives an apostrophe. FFmpeg is run
**in the clip's own directory** and the filter is handed the bare basename,
which it resolves itself. Source and output stay absolute, so moving the working
directory cannot change which files those are.
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
        working_directory = output.parent.resolve()
        subtitles_name = None
        if subtitles is not None:
            # The filter resolves the name against the working directory, so a
            # document anywhere else would silently not be found -- or, worse,
            # a same-named one here would be found instead.
            if subtitles.parent.resolve() != working_directory:
                raise RenderError(
                    f"The subtitles for {output.name} are in {subtitles.parent}, not beside "
                    f"the clip in {working_directory}, so FFmpeg would not resolve them"
                )
            subtitles_name = subtitles.name
        try:
            run_command(
                render_arguments(
                    source.resolve(),
                    start,
                    duration,
                    subtitles_name,
                    output.resolve(),
                    config,
                ),
                timeout=TRANSCODE_TIMEOUT_SECONDS,
                cwd=working_directory,
            )
        except ExternalToolError as error:
            raise RenderError(f"FFmpeg could not render the clip {output.name}: {error}") from (
                error
            )
        if not output.is_file() or output.stat().st_size == 0:
            raise RenderError(f"FFmpeg reported success but produced no clip at {output}")
