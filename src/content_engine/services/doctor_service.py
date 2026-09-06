from __future__ import annotations

import importlib.util
import os
import platform
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from content_engine.config import (
    ANALYSIS_CREDENTIAL_ENV_VAR,
    ANALYSIS_MODEL_PLACEHOLDER,
    Settings,
    config_sources,
)
from content_engine.domain.exceptions import ContentEngineError
from content_engine.utils.subprocess import run_command

#: Re-exported: both names were defined here before the Gemini adapter needed
#: them too, and an adapter importing a service would invert the layering.
__all__ = ["ANALYSIS_CREDENTIAL_ENV_VAR", "ANALYSIS_MODEL_PLACEHOLDER", "Check", "DoctorService"]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


class DoctorService:
    def __init__(
        self,
        settings: Settings,
        config_path: Path | None = None,
        require_ai: bool = False,
    ) -> None:
        self.settings = settings
        self.config_path = config_path
        self.require_ai = require_ai

    def run(self) -> list[Check]:
        return [
            self._python_check(),
            self._command_check("FFmpeg", "ffmpeg", "-version"),
            self._command_check("FFprobe", "ffprobe", "-version"),
            self._ass_check(),
            self._workspace_check(),
            self._module_check("faster-whisper", "faster_whisper"),
            self._configuration_check(),
            self._credentials_check(),
            self._analysis_model_check(),
        ]

    @staticmethod
    def _python_check() -> Check:
        ok = sys.version_info[:2] == (3, 12)
        return Check("Python", ok, platform.python_version())

    @staticmethod
    def _command_check(name: str, executable: str, argument: str) -> Check:
        try:
            lines = run_command([executable, argument]).stdout.splitlines()
        except ContentEngineError as error:
            return Check(name, False, str(error))
        return Check(name, True, lines[0] if lines else "no version reported")

    @staticmethod
    def _ass_check() -> Check:
        try:
            output = run_command(["ffmpeg", "-hide_banner", "-filters"]).stdout
        except ContentEngineError as error:
            return Check("ASS subtitles", False, str(error))
        ok = any(line.split()[1:2] == ["ass"] for line in output.splitlines())
        return Check("ASS subtitles", ok, "filter available" if ok else "filter unavailable")

    def _workspace_check(self) -> Check:
        runs_root = self.settings.workspace.root.joinpath("runs")
        try:
            runs_root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=runs_root, delete=True):
                pass
        except OSError as error:
            return Check("Workspace", False, f"{runs_root}: {error}")
        return Check("Workspace", True, str(self.settings.workspace.root))

    def _configuration_check(self) -> Check:
        """The settings parsed, so report where they came from rather than a tautology."""
        return Check("Configuration", True, " + ".join(config_sources(self.config_path)))

    def _credentials_check(self) -> Check:
        """Report only whether the credential exists.

        ``bool()`` is the whole interaction with it: the value is never read into
        a variable, printed, logged or written to an artifact.
        """
        present = bool(os.getenv(ANALYSIS_CREDENTIAL_ENV_VAR))
        return Check(
            "Analysis credentials",
            present,
            "configured" if present else f"{ANALYSIS_CREDENTIAL_ENV_VAR} is missing",
            required=self.require_ai,
        )

    def _analysis_model_check(self) -> Check:
        model = self.settings.analysis.model
        configured = model != ANALYSIS_MODEL_PLACEHOLDER
        return Check(
            "Analysis model",
            configured,
            model if configured else f"{model} (placeholder)",
            required=self.require_ai,
        )

    @staticmethod
    def _module_check(name: str, module: str) -> Check:
        ok = importlib.util.find_spec(module) is not None
        return Check(name, ok, "installed" if ok else "not installed")
