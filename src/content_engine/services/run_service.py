from __future__ import annotations

import platform
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from content_engine import __version__
from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.config import Settings, config_sha256
from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import ContentEngineError, ExternalToolError
from content_engine.domain.models import (
    InputManifest,
    RunFailure,
    RunManifest,
    StageRecord,
    VersionManifest,
)
from content_engine.domain.run_state import failure_status, validate_transition
from content_engine.utils.hashing import sha256_file
from content_engine.utils.json import write_json
from content_engine.utils.subprocess import run_command


class RunService:
    def __init__(self, settings: Settings, workspace: RunWorkspace) -> None:
        self.settings = settings
        self.workspace = workspace

    def create(self, input_path: Path) -> tuple[Path, RunManifest]:
        source = input_path.expanduser().resolve()
        run_id = self._build_run_id(source)
        run_path = self.workspace.create(run_id)

        write_json(
            run_path.joinpath("config.effective.json"),
            self.settings.model_dump(mode="json"),
        )

        manifest = RunManifest(
            run_id=run_id,
            created_at=datetime.now(UTC),
            status=RunStatus.CREATED,
            input=InputManifest(
                path=source,
                sha256=sha256_file(source),
                size=source.stat().st_size,
            ),
            config_sha256=config_sha256(self.settings),
            versions=VersionManifest(
                content_engine=__version__,
                python=platform.python_version(),
                ffmpeg=self._ffmpeg_version(),
                transcription_model=self.settings.transcription.model,
                analysis_provider=str(self.settings.analysis.provider),
                analysis_model=self.settings.analysis.model,
            ),
        )
        self.workspace.write_manifest(run_path, manifest)
        return run_path, manifest

    @staticmethod
    def _build_run_id(source: Path) -> str:
        """A unique, readable identifier for one execution.

        This is operational identity, not experiment identity. Reproducibility is
        carried by config_sha256, input.sha256 and the stage fingerprints, so the
        suffix is random rather than derived: two runs of the same source and
        configuration are two distinct experiments and must not collide.
        """
        stem = re.sub(r"[^a-z0-9]+", "-", source.stem.lower()).strip("-") or "video"
        return f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{stem[:32]}-{uuid4().hex[:6]}"

    @staticmethod
    def _ffmpeg_version() -> str:
        try:
            return run_command(["ffmpeg", "-version"]).stdout.splitlines()[0]
        except (ContentEngineError, ExternalToolError, IndexError):
            return "unavailable"

    def advance(self, run_path: Path, manifest: RunManifest, status: RunStatus) -> RunManifest:
        """Move a run forward, refusing transitions the pipeline cannot produce."""
        validate_transition(manifest.status, status)
        manifest.status = status
        manifest.failure = None
        self.workspace.write_manifest(run_path, manifest)
        return manifest

    def fail(
        self,
        run_path: Path,
        manifest: RunManifest,
        stage: RunStage,
        error: Exception,
    ) -> RunManifest:
        """Record why a run stopped, keeping the run directory for diagnosis."""
        status = failure_status(stage)
        validate_transition(manifest.status, status)
        manifest.status = status
        manifest.failure = RunFailure(
            stage=stage,
            error_type=type(error).__name__,
            message=str(error),
            occurred_at=datetime.now(UTC),
        )
        self.workspace.write_manifest(run_path, manifest)
        return manifest

    def record_stage(
        self,
        run_path: Path,
        manifest: RunManifest,
        stage: RunStage,
        fingerprint: str,
        stage_config_sha256: str,
        schema_version: int,
    ) -> RunManifest:
        """Record a completed stage and the configuration that produced it.

        The fingerprint decides whether the stage's output can be reused. The
        configuration hash ties the manifest to the readable effective
        configuration the stage wrote beside its artifacts, so the run explains
        itself without anyone having to reverse an opaque digest.
        """
        manifest.stages[stage.value] = StageRecord(
            fingerprint=fingerprint,
            stage_config_sha256=stage_config_sha256,
            schema_version=schema_version,
            completed_at=datetime.now(UTC),
        )
        self.workspace.write_manifest(run_path, manifest)
        return manifest
