from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from content_engine.adapters.analysis.fixture_analyzer import (
    FixtureAnalyzer,
    load_fixture,
    require_fixture_covers,
)
from content_engine.adapters.media.ffmpeg import FFmpegAdapter
from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.adapters.transcription.faster_whisper import FasterWhisperTranscriber
from content_engine.config import Settings, config_sha256, load_settings
from content_engine.domain.candidates import CANDIDATES_SCHEMA_VERSION, CandidateCollection
from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import (
    EXIT_CONFIGURATION,
    EXIT_UNKNOWN,
    ContentEngineError,
    IncompatibleArtifactError,
    InvalidMediaError,
)
from content_engine.domain.models import (
    TRANSCRIPT_SCHEMA_VERSION,
    RunManifest,
    Transcript,
)
from content_engine.domain.run_state import validate_transition
from content_engine.domain.transcript_rules import stage_config, transcription_fingerprint
from content_engine.services.analysis_service import (
    CANDIDATES_FILENAME,
    AnalysisPlan,
    AnalysisService,
    plan_analysis,
    verify_analysis,
)
from content_engine.services.chunking_service import transcript_sha256
from content_engine.services.doctor_service import Check, DoctorService
from content_engine.services.media_service import MediaService
from content_engine.services.run_service import RunService
from content_engine.services.transcription_service import (
    TranscriptionService,
    options_from_settings,
    read_transcript,
    verify_stage_config,
)
from content_engine.utils.hashing import sha256_file

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
console = Console()
ConfigOption = Annotated[Path | None, typer.Option("--config", help="TOML profile to merge")]

#: Stages whose artifacts are built on top of the transcript. CE-052 will
#: invalidate them automatically; until then rerunning a stage says which of them
#: went stale.
DOWNSTREAM_DIRECTORIES = ("analysis", "review", "previews", "clips")
#: The same, for the analysis stage: everything downstream of the candidates.
ANALYSIS_DOWNSTREAM_DIRECTORIES = ("review", "previews", "clips")


def _execute(action: Callable[[], str]) -> None:
    """Run a command body, mapping every expected failure to its exit code."""
    try:
        message = action()
    except ContentEngineError as error:
        console.print(f"[red]{error.title}:[/red] {error}")
        raise typer.Exit(error.exit_code) from error
    # Last resort. An unexpected exception is still reported as a message with an
    # exit code rather than a traceback, so the CLI never leaks a stack trace.
    except Exception as error:  # noqa: BLE001
        console.print(f"[red]Unexpected error:[/red] {type(error).__name__}: {error}")
        raise typer.Exit(EXIT_UNKNOWN) from error
    console.print(message)


def _media_service() -> MediaService:
    return MediaService(FFprobeAdapter(), FFmpegAdapter())


def _check_status(check: Check) -> str:
    """A failed optional check is a warning, not a failure: V0 has no analysis stage."""
    if check.ok:
        return "[green]OK[/green]"
    if check.required:
        return "[red]FAIL[/red]"
    return "[yellow]WARN[/yellow]"


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
        table.add_row(check.name, _check_status(check), check.detail)
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
        audio_sha256 = sha256_file(audio_path)
        fingerprint = transcription_fingerprint(audio_sha256, stage_config(options, hardware))

        transcript_directory = run_path.joinpath("transcript")
        transcript_path = transcript_directory.joinpath("transcript.json")
        if transcript_path.is_file() and not force:
            _refuse_or_skip(
                manifest,
                transcript_directory,
                audio_sha256,
                fingerprint,
                hardware.device,
                hardware.compute_type,
            )
            return (
                f"[green]Transcript reused:[/green] fingerprint {fingerprint[:12]} "
                f"on {hardware.device}/{hardware.compute_type}. Use --force to rerun."
            )

        if force:
            _warn_about_stale(run_path, DOWNSTREAM_DIRECTORIES, "transcript")

        audio_duration = _media_service().audio_duration(audio_path)
        try:
            outcome = service.transcribe(
                audio_path,
                audio_duration,
                transcript_directory,
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
            outcome.stage_config_sha256,
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


@app.command()
def analyze(
    run_id: Annotated[str, typer.Argument(help="Existing run identifier")],
    fixture: Annotated[
        Path,
        typer.Option("--fixture", help="Recorded analyzer answers to replay"),
    ],
    config: ConfigOption = None,
    force: Annotated[bool, typer.Option("--force", help="Replace existing candidates")] = False,
) -> None:
    """Turn a transcript into a ranked candidate list, replaying a fixture.

    No provider is called and no credential is read. ``--fixture`` is required
    in this build for exactly that reason: the deterministic pipeline is
    finished, the Gemini adapter is not, and a command that silently produced
    nothing without one would be dishonest about which half exists.
    """

    def action() -> str:
        settings: Settings = load_settings(config)
        workspace = RunWorkspace(settings.workspace.root)
        run_service = RunService(settings, workspace)
        run_path = workspace.require(run_id)
        manifest = workspace.read_manifest(run_path)

        # Refused before anything is read or computed, so a run that cannot
        # reach ANALYZED says so instead of failing after the work is done.
        validate_transition(manifest.status, RunStatus.ANALYZED)

        transcript = read_transcript(run_path.joinpath("transcript"))
        _warn_about_configuration_drift(manifest, config_sha256(settings))

        analyzer = FixtureAnalyzer(load_fixture(fixture))
        plan = plan_analysis(transcript, settings, analyzer.identity)
        require_fixture_covers(analyzer, [chunk.id for chunk in plan.chunks.chunks])

        analysis_directory = run_path.joinpath("analysis")
        if analysis_directory.joinpath(CANDIDATES_FILENAME).is_file() and not force:
            collection = _refuse_or_reuse_analysis(manifest, analysis_directory, transcript, plan)
            return (
                f"[green]Candidates reused:[/green] {collection.counts.selected} selected of "
                f"{collection.counts.proposed} proposed. Use --force to rerun."
            )

        if force:
            _warn_about_stale(run_path, ANALYSIS_DOWNSTREAM_DIRECTORIES, "candidates")

        try:
            outcome = AnalysisService(analyzer, analyzer.identity).analyze(
                plan, analysis_directory, datetime.now(UTC)
            )
        except ContentEngineError as error:
            run_service.fail(run_path, manifest, RunStage.ANALYSIS, error)
            _report_run_context(run_path, manifest)
            raise

        manifest = run_service.advance(run_path, manifest, RunStatus.ANALYZED)
        # The manifest must name what actually produced the candidates. While the
        # executor is a fixture it says so, rather than leaving the configured
        # provider in place and asserting a call that never happened.
        manifest.versions.analysis_provider = outcome.stage_config.analyzer
        manifest.versions.analysis_model = outcome.stage_config.model
        run_service.record_stage(
            run_path,
            manifest,
            RunStage.ANALYSIS,
            outcome.fingerprint,
            outcome.stage_config_sha256,
            CANDIDATES_SCHEMA_VERSION,
        )

        counts = outcome.collection.counts
        if counts.proposed == 0:
            console.print(
                "[yellow]Warning:[/yellow] the analyzer proposed no candidates. "
                "The transcript may contain no usable material."
            )
        if counts.invalid:
            console.print(
                f"[yellow]Refused[/yellow] {counts.invalid} proposals before scoring; "
                f"see {CANDIDATES_FILENAME} for the reasons."
            )
        return (
            f"[green]Candidates ready:[/green] {counts.selected} selected of "
            f"{counts.proposed} proposed ({counts.invalid} invalid, "
            f"{counts.below_min_score} below {plan.policy.min_score}, "
            f"{counts.deduplicated} duplicates, {counts.not_in_top_n} beyond the cap) "
            f"by {outcome.stage_config.analyzer}/{outcome.stage_config.model}, "
            f"fingerprint {outcome.fingerprint[:12]}"
        )

    _execute(action)


def _refuse_or_reuse_analysis(
    manifest: RunManifest,
    analysis_directory: Path,
    transcript: Transcript,
    plan: AnalysisPlan,
) -> CandidateCollection:
    """Reuse candidates only when they provably match the current inputs.

    The same discipline the transcript reuse follows, applied to a stage with
    four artifacts instead of one. The manifest has to have recorded the stage,
    under a schema this build produces; the artifacts on disk have to still
    match what the manifest says about them; and the whole thing has to describe
    the transcript, the fixture and the settings being asked for right now.

    Nothing here writes. Every refusal leaves all four artifacts and the
    manifest exactly as they were, and says to use --force.
    """
    record = manifest.stages.get(RunStage.ANALYSIS.value)
    if record is None:
        raise IncompatibleArtifactError(
            "Candidates exist but no fingerprint was recorded for them, so they cannot "
            "be shown to match the current transcript and settings. Rerun with --force."
        )
    if record.schema_version != CANDIDATES_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"The existing candidates use schema {record.schema_version}; this build "
            f"produces {CANDIDATES_SCHEMA_VERSION}. Rerun with --force."
        )
    return verify_analysis(
        analysis_directory,
        record.fingerprint,
        record.stage_config_sha256,
        transcript_sha256(transcript),
        plan.stage_config,
    )


def _refuse_or_skip(
    manifest: RunManifest,
    transcript_directory: Path,
    audio_sha256: str,
    fingerprint: str,
    device: str,
    compute_type: str,
) -> None:
    """Reuse a transcript only when it provably matches the current inputs.

    Four things have to hold, and they are checked in that order: the manifest
    recorded the stage, it recorded it under a schema this build produces, the
    stage configuration still on disk matches what the manifest says about it,
    and the whole thing describes the settings being asked for now.

    The third check is the one the manifest alone cannot make. A digest that
    still looks right proves nothing if the artifact it addresses is gone or was
    edited, and reusing a transcript on that basis would put the run back in the
    state this branch exists to remove: confident about what produced an
    artifact, and wrong.

    Nothing here writes. Every refusal leaves the transcript, its configuration
    and the manifest exactly as they were.
    """
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
    verify_stage_config(transcript_directory, record, audio_sha256)
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


def _warn_about_stale(run_path: Path, directories: tuple[str, ...], produced: str) -> None:
    """Name what a rerun invalidates. CE-052 will do the invalidating."""
    stale = [
        name
        for name in directories
        if run_path.joinpath(name).is_dir() and any(run_path.joinpath(name).iterdir())
    ]
    if stale:
        console.print(
            f"[yellow]Warning:[/yellow] {', '.join(stale)} were built on the previous "
            f"{produced} and are now stale. Automatic invalidation arrives with CE-052."
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
