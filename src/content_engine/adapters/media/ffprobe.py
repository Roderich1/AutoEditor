from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Any

from content_engine.domain.exceptions import (
    ExternalToolError,
    InvalidMediaError,
    NoAudioStreamError,
)
from content_engine.domain.models import MediaInfo
from content_engine.utils.subprocess import PROBE_TIMEOUT_SECONDS, run_command


def _parse_frame_rate(video: dict[str, Any]) -> float:
    """Return frames per second, or 0.0 when the container does not declare one.

    An unknown frame rate is not a reason to reject a source: the V0 pipeline
    needs duration and an audio stream, not a frame rate.
    """
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = str(video.get(key) or "").strip()
        if not raw or raw.endswith("/0"):
            continue
        try:
            value = float(Fraction(raw))
        except (ValueError, ZeroDivisionError, OverflowError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return 0.0


class FFprobeAdapter:
    def _run(self, path: Path) -> dict[str, Any]:
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
                ],
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except ExternalToolError as error:
            raise InvalidMediaError(f"ffprobe could not read {path}: {error}") from error
        try:
            raw: dict[str, Any] = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise InvalidMediaError(f"Invalid ffprobe response for {path}") from error
        return raw

    @staticmethod
    def _existing(input_path: Path) -> Path:
        path = input_path.expanduser().resolve()
        if not path.is_file():
            raise InvalidMediaError(f"Input file does not exist: {path}")
        return path

    def probe(self, input_path: Path) -> tuple[MediaInfo, dict[str, Any]]:
        path = self._existing(input_path)
        raw = self._run(path)

        streams = raw.get("streams", [])
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        if video is None:
            raise InvalidMediaError(f"Media has no video stream: {path}")
        if audio is None:
            raise NoAudioStreamError(f"Media has no audio stream: {path}")

        format_info = raw.get("format", {})
        duration = _positive_duration(path, format_info.get("duration") or video.get("duration"))
        try:
            media = MediaInfo(
                duration_seconds=duration,
                video_codec=str(video["codec_name"]),
                width=int(video["width"]),
                height=int(video["height"]),
                fps=_parse_frame_rate(video),
                audio_codec=audio.get("codec_name"),
                sample_rate=int(audio["sample_rate"]) if audio.get("sample_rate") else None,
                channels=int(audio["channels"]) if audio.get("channels") else None,
                container=str(format_info.get("format_name", "unknown")),
                file_size=path.stat().st_size,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidMediaError(f"Incomplete media metadata for {path}: {error}") from error
        return media, raw

    def probe_audio_duration(self, input_path: Path) -> float:
        """Measure the real duration of an audio file.

        The transcriber also reports a duration; keeping an independent
        measurement is what allows the two to be compared instead of trusted.
        """
        path = self._existing(input_path)
        raw = self._run(path)
        streams = raw.get("streams", [])
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        if audio is None:
            raise NoAudioStreamError(f"File has no audio stream: {path}")
        format_info = raw.get("format", {})
        return _positive_duration(path, format_info.get("duration") or audio.get("duration"))


def _positive_duration(path: Path, value: object) -> float:
    try:
        duration = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as error:
        raise InvalidMediaError(f"Media does not declare a duration: {path}") from error
    if not math.isfinite(duration):
        raise InvalidMediaError(f"Media declares a non-finite duration ({duration}): {path}")
    if duration <= 0:
        raise InvalidMediaError(f"Media duration is not positive ({duration}): {path}")
    return duration
