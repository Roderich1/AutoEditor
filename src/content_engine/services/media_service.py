from pathlib import Path

from content_engine.adapters.media.ffmpeg import FFmpegAdapter
from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.domain.models import MediaInfo
from content_engine.utils.json import write_json


class MediaService:
    def __init__(self, probe: FFprobeAdapter, ffmpeg: FFmpegAdapter) -> None:
        self.probe_adapter = probe
        self.ffmpeg_adapter = ffmpeg

    def inspect(self, input_path: Path, probe_output: Path | None = None) -> MediaInfo:
        media, raw = self.probe_adapter.probe(input_path)
        if probe_output is not None:
            write_json(probe_output, raw)
        return media

    def extract_audio(self, input_path: Path, output_path: Path) -> None:
        self.ffmpeg_adapter.extract_audio(input_path, output_path)

    def audio_duration(self, audio_path: Path) -> float:
        return self.probe_adapter.probe_audio_duration(audio_path)
