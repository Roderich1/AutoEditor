from datetime import UTC, datetime
from pathlib import Path

from content_engine.config import Settings, TranscriptionSettings
from content_engine.domain.models import Transcript, TranscriptSegment, TranscriptWord
from content_engine.services.transcription_service import TranscriptionService


class FakeTranscriber:
    def transcribe(self, audio_path: Path, options: TranscriptionSettings) -> Transcript:
        return Transcript(
            language="es",
            language_probability=0.99,
            duration_seconds=2.0,
            segments=[
                TranscriptSegment(
                    index=0,
                    start=0.25,
                    end=1.75,
                    text="Hola mundo",
                    words=[
                        TranscriptWord(word="Hola", start=0.25, end=0.8, probability=0.98),
                        TranscriptWord(word="mundo", start=0.9, end=1.75, probability=0.97),
                    ],
                )
            ],
            model=options.model,
            created_at=datetime.now(UTC),
        )


def test_transcription_exports_all_formats(tmp_path: Path, settings: Settings) -> None:
    audio = tmp_path.joinpath("source.wav")
    audio.write_bytes(b"wav")
    output = tmp_path.joinpath("transcript")

    TranscriptionService(FakeTranscriber()).transcribe(
        audio,
        output,
        settings.transcription,
    )

    assert output.joinpath("transcript.json").is_file()
    assert output.joinpath("transcript.txt").read_text(encoding="utf-8") == "Hola mundo\n"
    assert "00:00:00,250 --> 00:00:01,750" in output.joinpath("transcript.srt").read_text(
        encoding="utf-8"
    )
