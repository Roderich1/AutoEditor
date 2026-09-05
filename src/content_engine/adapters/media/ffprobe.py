from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from content_engine.domain.exceptions import InvalidMediaError, NoAudioStreamError
from content_engine.domain.models import MediaInfo
from content_engine.utils.subprocess import run_command


class FFprobeAdapter:
    def probe(self, input_path: Path) -> tuple[MediaInfo, dict[str, Any]]:
        path = input_path.expanduser().resolve()
        if not path.is_file():
            raise InvalidMediaError(f"Input file does not exist: {path}")

        try:
            result = run_command(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    str(path),
                ]
            )
            raw: dict[str, Any] = json.loads(result.stdout)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise InvalidMediaError(f"Invalid FFprobe response for {path}") from error

        streams = raw.get("streams", [])
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        if video is None:
            raise InvalidMediaError("Media has no video stream")
        if audio is None:
            raise NoAudioStreamError("Media has no audio stream")

        format_info = raw.get("format", {})
        duration = float(format_info.get("duration") or video.get("duration") or 0)
        frame_rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0"
        fps = float(Fraction(frame_rate)) if frame_rate != "0/0" else 0
        try:
            media = MediaInfo(
                duration_seconds=duration,
                video_codec=video["codec_name"],
                width=int(video["width"]),
                height=int(video["height"]),
                fps=fps,
                audio_codec=audio.get("codec_name"),
                sample_rate=int(audio["sample_rate"]) if audio.get("sample_rate") else None,
                channels=int(audio["channels"]) if audio.get("channels") else None,
                container=str(format_info.get("format_name", "unknown")),
                file_size=path.stat().st_size,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidMediaError(f"Incomplete media metadata for {path}") from error
        return media, raw
