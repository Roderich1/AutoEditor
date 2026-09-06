"""Shared utilities: subprocess classification and artifact writing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from content_engine.domain.exceptions import (
    CorruptArtifactError,
    ExternalToolError,
    ExternalToolNotFoundError,
)
from content_engine.utils.json import read_json, write_json, write_text
from content_engine.utils.subprocess import run_command
from content_engine.utils.timestamps import srt_timestamp


def test_srt_timestamp_formats_seconds() -> None:
    assert srt_timestamp(782.42) == "00:13:02,420"


def test_srt_timestamp_clamps_negative_values() -> None:
    assert srt_timestamp(-1) == "00:00:00,000"


def test_srt_timestamp_handles_hours() -> None:
    assert srt_timestamp(3725.5) == "01:02:05,500"


def test_a_missing_executable_is_a_configuration_problem() -> None:
    with pytest.raises(ExternalToolNotFoundError, match="on PATH"):
        run_command(["definitely-not-a-real-executable-xyz", "--version"])


def test_a_timeout_is_reported_as_a_tool_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def timing_out(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1.0)

    monkeypatch.setattr("content_engine.utils.subprocess.subprocess.run", timing_out)

    with pytest.raises(ExternalToolError, match="timed out"):
        run_command(["ffmpeg", "-version"], timeout=1.0)


def test_a_non_zero_exit_carries_the_tool_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(1, "ffmpeg", output="", stderr="moov atom not found")

    monkeypatch.setattr("content_engine.utils.subprocess.subprocess.run", failing)

    with pytest.raises(ExternalToolError, match="moov atom not found"):
        run_command(["ffmpeg", "-i", "broken.mp4"])


def test_json_is_written_atomically_with_lf(tmp_path: Path) -> None:
    target = tmp_path.joinpath("nested", "artifact.json")

    write_json(target, {"a": 1, "text": "línea\notra"})

    data = target.read_bytes()
    assert b"\r\n" not in data
    assert not data.startswith(b"\xef\xbb\xbf")
    assert not list(tmp_path.glob("**/*.tmp"))
    assert read_json(target)["text"] == "línea\notra"


def test_text_is_written_with_lf(tmp_path: Path) -> None:
    target = tmp_path.joinpath("artifact.srt")

    write_text(target, "1\n00:00:00,000 --> 00:00:01,000\nhola\n")

    assert b"\r\n" not in target.read_bytes()


def test_unserializable_values_fail_loudly(tmp_path: Path) -> None:
    """A silent str() coercion would hide a modelling mistake in an artifact."""
    target = tmp_path.joinpath("bad.json")
    unserializable = {"path": object()}

    with pytest.raises(TypeError):
        write_json(target, unserializable)


def test_reading_invalid_json_reports_a_corrupt_artifact(tmp_path: Path) -> None:
    target = tmp_path.joinpath("broken.json")
    target.write_text("{not json", encoding="utf-8")

    with pytest.raises(CorruptArtifactError, match="not valid JSON"):
        read_json(target)


def test_reading_a_missing_file_reports_a_corrupt_artifact(tmp_path: Path) -> None:
    absent = tmp_path.joinpath("absent.json")

    with pytest.raises(CorruptArtifactError, match="Cannot read"):
        read_json(absent)


def test_a_failed_write_leaves_neither_a_partial_file_nor_a_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-write must not be observable as an artifact."""
    target = tmp_path.joinpath("artifact.json")
    target.write_text('{"previous": true}\n', encoding="utf-8")
    original = Path.write_text

    def failing(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.suffix == ".tmp":
            raise OSError("no space left on device")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing)

    with pytest.raises(OSError, match="no space left"):
        write_json(target, {"replacement": True})

    assert not list(tmp_path.glob("**/*.tmp"))
    assert read_json(target) == {"previous": True}


def test_text_artifacts_are_written_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """transcript.txt and transcript.srt get the same guarantee as the JSON."""
    target = tmp_path.joinpath("transcript.srt")
    target.write_text("1\n00:00:00,000 --> 00:00:01,000\nprevio\n", encoding="utf-8")
    original = Path.write_text

    def failing(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.suffix == ".tmp":
            raise OSError("disk full")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing)

    with pytest.raises(OSError, match="disk full"):
        write_text(target, "1\n00:00:00,000 --> 00:00:02,000\nnuevo\n")

    assert not list(tmp_path.glob("**/*.tmp"))
    assert "previo" in target.read_text(encoding="utf-8")
