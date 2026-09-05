from __future__ import annotations

import importlib.util
import os
import platform
import sys
import tempfile
from dataclasses import dataclass

from content_engine.config import Settings
from content_engine.utils.subprocess import run_command


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


class DoctorService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(self) -> list[Check]:
        return [
            self._python_check(),
            self._command_check("FFmpeg", "ffmpeg", "-version"),
            self._command_check("FFprobe", "ffprobe", "-version"),
            self._ass_check(),
            self._workspace_check(),
            self._module_check("faster-whisper", "faster_whisper"),
            Check("Configuration", True, "valid"),
            Check(
                "OpenAI credentials",
                bool(os.getenv("OPENAI_API_KEY")),
                "configured" if os.getenv("OPENAI_API_KEY") else "OPENAI_API_KEY is missing",
                required=False,
            ),
            Check(
                "Analysis model",
                self.settings.analysis.model != "SET_MODEL_HERE",
                self.settings.analysis.model,
                required=False,
            ),
        ]

    @staticmethod
    def _python_check() -> Check:
        ok = sys.version_info[:2] == (3, 12)
        return Check("Python", ok, platform.python_version())

    @staticmethod
    def _command_check(name: str, executable: str, argument: str) -> Check:
        try:
            line = run_command([executable, argument]).stdout.splitlines()[0]
            return Check(name, True, line)
        except Exception as error:
            return Check(name, False, str(error))

    @staticmethod
    def _ass_check() -> Check:
        try:
            output = run_command(["ffmpeg", "-hide_banner", "-filters"]).stdout
            ok = any(line.split()[1:2] == ["ass"] for line in output.splitlines())
            return Check("ASS subtitles", ok, "filter available" if ok else "filter unavailable")
        except Exception as error:
            return Check("ASS subtitles", False, str(error))

    def _workspace_check(self) -> Check:
        root = self.settings.workspace.root
        try:
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=root, delete=True):
                pass
            return Check("Workspace", True, str(root))
        except OSError as error:
            return Check("Workspace", False, str(error))

    @staticmethod
    def _module_check(name: str, module: str) -> Check:
        ok = importlib.util.find_spec(module) is not None
        return Check(name, ok, "installed" if ok else "not installed")
