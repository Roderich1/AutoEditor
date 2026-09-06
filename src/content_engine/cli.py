from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from content_engine.adapters.media.ffmpeg import FFmpegAdapter
from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.adapters.transcription.faster_whisper import FasterWhisperTranscriber
from content_engine.config import Settings, config_sha256, load_settings
from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import (
    EXIT_CONFIGURATION,
    EXIT_UNKNOWN,
    ContentEngineError,
    IncompatibleArtifactError,
    InvalidMediaError,
)
from content_engine.domain.models import TRANSCRIPT_SCHEMA_VERSION, RunManifest
from content_engine.domain.transcript_rules import transcription_fingerprint
from content_engine.services.doctor_service import DoctorService
from content_engine.services.media_service import MediaService
from content_engine.services.run_service import RunService
from content_engine.services.transcription_service import (
    TranscriptionService,
    options_from_settings,
)
from content_engine.utils.hashing import sha256_file

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
console = Console()
ConfigOption = Annotated[Path | None, typer.Option("--config", help="TOML profile to merge")]

#: Stages whose artifacts are built on top of the transcript. Nothing writes here
#: yet; CE-052 will invalidate them automatically.
DOWNSTREAM_DIRECTORIES = ("analysis", "review", "previews", "clips")


def _execute(action: Callable[[], str]) -> None:
    """Run a command body, mapping every expected failure to its exit code."""
    try:
        message = action()
    except ContentEngineError as error:
        console.print(f"[red]{error.title}:[/red] {error}")
        raise typer.Exit(error.exit_code) from error
    except Exception as error:  # noqa: BLE001 - last resort, reported without a traceback
        console.print(f"[red]Unexpected error:[/red] {type(error).__name__}: {error}")
        raise typer.Exit(EXIT_UNKNOWN) from error
    console.print(message)


def _media_service() -> MediaService:
    return MediaService(FFprobeAdapter(), FFmpegAdapter())


def _report_run_context(run_path: Path, manifest: RunManifest) -> None:
    console.print(f"[yellow]Run {manifest.run_id} kept for diagnosis:[/yellow] {run_path}")


@app.command()
def doctor(
    config: ConfigOption = None,
    require_ai: Annotated[
        bool,
        typer.Option("--require-ai", help="Treat AI credentials and model as mandatory"),
    ] = False,
) -> None:
    """Check the local environment and optional provider configuration."""
    try:
        settings = load_settings(config)
        checks = DoctorService(settings, config, require_ai=require_ai).run()
    except ContentEngineError as error:
        console.print(f"[red]{error.title}:[/red] {error}")
        raise typer.Exit(error.exit_code) from error

    table = Table(title="Content Engine Doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for check in checks:
        status = (
            "[green]OK[/green]"
            if check.ok
            else ("[red]FAIL[/red]" if check.required else "[yellow]WARN[/yellow]")
        )
        table.add_row(check.name, status, check.detail)
    console.print(table)

    if not all(check.ok for check in checks if check.required):
        console.print("[red]Environment is not ready.[/red]")
        raise typer.Exit(EXIT_CONFIGURATION)
    console.print("[green]System ready.[/green]")


@app.command("inspect")
def inspect_media(
    video: Annotated[Path, typer.Argument(exists=True, dir_okay=False, resolve_path=True)],
) -> None:
    """Inspect and validate a video using FFprobe."""

    def action() -> str:
        media = _media_service().inspect(video)
        console.print_json(data=media.model_dump(mode="json"))
        return f"[green]Media accepted:[/green] {video}"

    _execute(action)


@app.command("run")
def create_run(
    video: Annotated[Path, typer.Argument(exists=True, dir_okay=False, resolve_path=True)],
    config: ConfigOption = None,
) -> None:
    """Create a run, inspect the video, and extract normalized WAV audio."""

    def action() -> str:
        settings = load_settings(config)
        workspace = RunWorkspace(settings.workspace.root)
        run_service = RunService(settings, workspace)
        run_path, manifest = run_service.create(video)

        try:
            _media_service().inspect(video, run_path.joinpath("media", "probe.json"))
        except ContentEngineError as error:
            run_service.fail(run_path, manifest, RunStage.INSPECT, error)
            _report_run_context(run_path, manifest)
            raise
        manifest = run_service.advance(run_path, manifest, RunStatus.INSPECTED)

        try:
            _media_service().extract_audio(video, run_path.joinpath("audio", "source.wav"))
        except ContentEngineError as error:
            run_service.fail(run_path, manifest, RunStage.AUDIO, error)
            _report_run_context(run_path, manifest)
            raise
        run_service.advance(run_path, manifest, RunStatus.AUDIO_READY)
        return f"[green]Run ready:[/green] {manifest.run_id}\n{run_path}"

    _execute(action)


@app.command()
def transcribe(
    run_id: Annotated[str, typer.Argument(help="Existing run identifier")],
    config: ConfigOption = None,
    force: Annotated[bool, typer.Option("--force", help="Replace an existing transcript")] = False,
) -> None:
    """Transcribe a run and export JSON, TXT, SRT and metrics."""

    def action() -> str:
        settings: Settings = load_settings(config)
        workspace = RunWorkspace(settings.workspace.root)
        run_service = RunService(settings, workspace)
        run_path = workspace.require(run_id)
        manifest = workspace.read_manifest(run_path)

        audio_path = run_path.joinpath("audio", "source.wav")
        if not audio_path.is_file():
            raise InvalidMediaError(f"Run audio is missing: {audio_path}")

        _warn_about_configuration_drift(manifest, config_sha256(settings))

        options = options_from_settings(settings.transcription)
        service = TranscriptionService(FasterWhisperTranscriber())
        # Hardware is resolved before the reuse decision: auto may resolve
        # differently than it did last time, and that changes the result.
        hardware = service.resolve_hardware(options)
        fingerprint = transcription_fingerprint(sha256_file(audio_path), options, hardware)

        transcript_path = run_path.joinpath("transcript", "transcript.json")
        if transcript_path.is_file() and not force:
            _refuse_or_skip(manifest, fingerprint, hardware.device, hardware.compute_type)
            return (
                f"[green]Transcript reused:[/green] fingerprint {fingerprint[:12]} "
                f"on {hardware.device}/{hardware.compute_type}. Use --force to rerun."
            )

        if force:
            _warn_about_downstream(run_path)

        audio_duration = _media_service().audio_duration(audio_path)
        try:
            outcome = service.transcribe(
                audio_path,
                audio_duration,
                run_path.joinpath("transcript"),
                options,
                hardware,
            )
        except ContentEngineError as error:
            run_service.fail(run_path, manifest, RunStage.TRANSCRIPTION, error)
            _report_run_context(run_path, manifest)
            raise

        manifest = run_service.advance(run_path, manifest, RunStatus.TRANSCRIBED)
        # The manifest must name the model that actually produced the transcript,
        # not the one configured when the run was created.
        manifest.versions.transcription_model = outcome.transcript.model
        run_service.record_stage(
            run_path,
            manifest,
            RunStage.TRANSCRIPTION,
            fingerprint,
            TRANSCRIPT_SCHEMA_VERSION,
        )

        metrics = outcome.metrics
        if metrics.segment_count == 0:
            console.print(
                "[yellow]Warning:[/yellow] the transcript has no segments. "
                "The audio may contain no recognizable speech."
            )
        if metrics.normalization.applied:
            console.print(
                f"[yellow]Normalized[/yellow] {metrics.normalization.clamped_segment_bounds} "
                f"segment and {metrics.normalization.clamped_word_bounds} word bounds, "
                f"dropped {metrics.normalization.dropped_empty_segments} empty segments."
            )
        return (
            f"[green]Transcript ready:[/green] {metrics.segment_count} segments, "
            f"{metrics.word_count} words, language={metrics.language}, "
            f"{metrics.processing_seconds}s on {metrics.device_resolved}/"
            f"{metrics.compute_type_resolved} (RTF {metrics.real_time_factor})"
        )

    _execute(action)


def _refuse_or_skip(
    manifest: RunManifest,
    fingerprint: str,
    device: str,
    compute_type: str,
) -> None:
    """Reuse a transcript only when it provably matches the current inputs."""
    record = manifest.stages.get(RunStage.TRANSCRIPTION.value)
    if record is None:
        raise IncompatibleArtifactError(
            "A transcript exists but no fingerprint was recorded for it, so it cannot "
            "be shown to match the current audio and settings. Rerun with --force."
        )
    if record.schema_version != TRANSCRIPT_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"The existing transcript uses schema {record.schema_version}; this build "
            f"produces {TRANSCRIPT_SCHEMA_VERSION}. Rerun with --force."
        )
    if record.fingerprint != fingerprint:
        raise IncompatibleArtifactError(
            f"The existing transcript was produced from different inputs "
            f"(recorded {record.fingerprint[:12]}, current {fingerprint[:12]} on "
            f"{device}/{compute_type}). It will not be reused. Rerun with --force."
        )


def _warn_about_configuration_drift(manifest: RunManifest, current: str) -> None:
    """Say so when this invocation is not the experiment the run was created for.

    ``--config`` can point at a different profile than the one ``run`` recorded.
    That is allowed, but it must never be silent: config_sha256 identifies the
    logical experiment, and a transcript produced under other settings would
    otherwise be filed under a configuration that never ran.
    """
    if current == manifest.config_sha256:
        return
    console.print(
        f"[yellow]Warning:[/yellow] this configuration ({current[:12]}) is not the one "
        f"recorded when the run was created ({manifest.config_sha256[:12]}). "
        "manifest.config_sha256 keeps the creating configuration; the transcript and "
        "its metrics record what actually ran."
    )


def _warn_about_downstream(run_path: Path) -> None:
    stale = [
        name
        for name in DOWNSTREAM_DIRECTORIES
        if run_path.joinpath(name).is_dir() and any(run_path.joinpath(name).iterdir())
    ]
    if stale:
        console.print(
            f"[yellow]Warning:[/yellow] {', '.join(stale)} were built on the previous "
            "transcript and are now stale. Automatic invalidation arrives with CE-052."
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
