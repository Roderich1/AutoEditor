from pathlib import Path

from content_engine.domain.exceptions import AudioExtractionError, ExternalToolError
from content_engine.utils.subprocess import TRANSCODE_TIMEOUT_SECONDS, run_command


class FFmpegAdapter:
    def extract_audio(self, input_path: Path, output_path: Path) -> None:
        """Extract mono 16 kHz signed 16-bit PCM, the transcription baseline."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            run_command(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(input_path.resolve()),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(output_path.resolve()),
                ],
                timeout=TRANSCODE_TIMEOUT_SECONDS,
            )
        except ExternalToolError as error:
            raise AudioExtractionError(
                f"Could not extract audio from {input_path}: {error}"
            ) from error
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise AudioExtractionError(f"FFmpeg produced no audio at {output_path}")
