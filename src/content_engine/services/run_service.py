from __future__ import annotations

import json
import platform
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from content_engine import __version__
from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.config import Settings
from content_engine.domain.enums import RunStatus
from content_engine.domain.models import (
    ConfigManifest,
    InputManifest,
    RunManifest,
    VersionManifest,
)
from content_engine.utils.hashing import sha256_bytes, sha256_file
from content_engine.utils.json import write_json
from content_engine.utils.subprocess import run_command


class RunService:
    def __init__(self, settings: Settings, workspace: RunWorkspace) -> None:
        self.settings = settings
        self.workspace = workspace

    def create(self, input_path: Path) -> tuple[Path, RunManifest]:
        source = input_path.expanduser().resolve()
        stem = re.sub(r"[^a-z0-9]+", "-", source.stem.lower()).strip("-") or "video"
        run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{stem[:32]}-{uuid4().hex[:6]}"
        run_path = self.workspace.create(run_id)
        config_data = self.settings.model_dump(mode="json")
        config_bytes = json.dumps(config_data, sort_keys=True).encode("utf-8")
        write_json(run_path.joinpath("config.effective.json"), config_data)

        try:
            ffmpeg_version = run_command(["ffmpeg", "-version"]).stdout.splitlines()[0]
        except Exception:
            ffmpeg_version = "unavailable"
        manifest = RunManifest(
            run_id=run_id,
            created_at=datetime.now(UTC),
            input=InputManifest(
                path=source,
                sha256=sha256_file(source),
                size=source.stat().st_size,
            ),
            config=ConfigManifest(sha256=sha256_bytes(config_bytes)),
            versions=VersionManifest(
                content_engine=__version__,
                ffmpeg=ffmpeg_version,
                python=platform.python_version(),
                transcription_model=self.settings.transcription.model,
                analysis_provider=self.settings.analysis.provider,
                analysis_model=self.settings.analysis.model,
            ),
            status=RunStatus.CREATED,
        )
        self.workspace.write_manifest(run_path, manifest)
        return run_path, manifest

    def set_status(self, run_path: Path, manifest: RunManifest, status: RunStatus) -> None:
        manifest.status = status
        self.workspace.write_manifest(run_path, manifest)
