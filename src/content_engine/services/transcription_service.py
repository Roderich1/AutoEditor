from pathlib import Path

from content_engine.config import TranscriptionSettings
from content_engine.domain.models import Transcript
from content_engine.ports.transcriber import TranscriberPort
from content_engine.utils.json import write_json
from content_engine.utils.timestamps import srt_timestamp


class TranscriptionService:
    def __init__(self, transcriber: TranscriberPort) -> None:
        self.transcriber = transcriber

    def transcribe(
        self,
        audio_path: Path,
        output_directory: Path,
        settings: TranscriptionSettings,
    ) -> Transcript:
        transcript = self.transcriber.transcribe(audio_path, settings)
        output_directory.mkdir(parents=True, exist_ok=True)
        write_json(
            output_directory.joinpath("transcript.json"),
            transcript.model_dump(mode="json"),
        )
        text = "\n".join(segment.text for segment in transcript.segments if segment.text)
        output_directory.joinpath("transcript.txt").write_text(text + "\n", encoding="utf-8")
        blocks = [
            "\n".join(
                (
                    str(index),
                    f"{srt_timestamp(segment.start)} --> {srt_timestamp(segment.end)}",
                    segment.text,
                )
            )
            for index, segment in enumerate(transcript.segments, start=1)
            if segment.text
        ]
        output_directory.joinpath("transcript.srt").write_text(
            "\n\n".join(blocks) + "\n",
            encoding="utf-8",
        )
        return transcript
