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
from content_engine.adapters.analysis.gemini_analyzer import (
    build_gemini_analyzer,
    gemini_identity,
)
from content_engine.adapters.analysis.prompt import Prompt, select_prompt
from content_engine.adapters.media.ffmpeg import FFmpegAdapter
from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.adapters.transcription.faster_whisper import FasterWhisperTranscriber
from content_engine.config import Settings, config_sha256, load_settings
from content_engine.domain.analysis_rules import AnalyzerIdentity
from content_engine.domain.candidates import CANDIDATES_SCHEMA_VERSION, CandidateCollection
from content_engine.domain.enums import AnalysisProvider, RunStage, RunStatus
from content_engine.domain.exceptions import (
    EXIT_CONFIGURATION,
    EXIT_UNKNOWN,
    ConfigurationError,
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
    AnalysisOutcome,
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
        Path | None,
        typer.Option("--fixture", help="Replay recorded answers instead of calling a provider"),
    ] = None,
    config: ConfigOption = None,
    force: Annotated[bool, typer.Option("--force", help="Replace existing candidates")] = False,
) -> None:
    """Turn a transcript into a ranked candidate list.

    With ``--fixture`` the recorded answers in that file are replayed: no
    provider is called, no credential is read and no network is touched. Without
    it, ``analysis.provider`` decides, and for Gemini that means a real call per
    chunk against a real key.

    Which of the two ran is never inferred later. The analyzer names itself in
    the manifest and in the stage configuration, and because the stage
    configuration digest is what reuse compares, artifacts produced one way are
    never reused by the other.
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

        # Resolved before anything else, so a profile naming a prompt this
        # build does not have is refused rather than silently given another one.
        prompt = select_prompt(settings.analysis.prompt_version)

        # The identity a run *would* have, computed without a credential, an SDK
        # or a client. Reuse compares this against what is recorded on disk, and
        # verifying four finished artifacts must not require a key the machine
        # may no longer have. Only producing needs one.
        replay, identity = _expected_identity(settings, fixture, prompt)
        plan = plan_analysis(transcript, settings, identity)
        if replay is not None:
            require_fixture_covers(replay, [chunk.id for chunk in plan.chunks.chunks])

        analysis_directory = run_path.joinpath("analysis")
        # Whether this stage has run is a question for the manifest, not for the
        # presence of one file. Gating on candidates.json alone meant deleting it
        # put the command back on the produce path, so a run whose manifest still
        # recorded a completed analysis quietly got a new one. Either signal now
        # sends the invocation through verification, which refuses whatever is
        # inconsistent and leaves the decision to --force.
        already_analysed = (
            RunStage.ANALYSIS.value in manifest.stages
            or analysis_directory.joinpath(CANDIDATES_FILENAME).is_file()
        )
        if already_analysed and not force:
            return _reuse_or_recover(
                run_service, run_path, manifest, analysis_directory, transcript, plan
            )

        if force:
            _warn_about_stale(run_path, ANALYSIS_DOWNSTREAM_DIRECTORIES, "candidates")

        # Now, and only now: candidates must actually be produced, so the SDK,
        # the model and the credential are validated and a client is built.
        analyzer = replay if replay is not None else build_gemini_analyzer(settings, prompt)
        try:
            outcome = AnalysisService(analyzer, identity).analyze(
                plan, analysis_directory, datetime.now(UTC)
            )
        except ContentEngineError as error:
            run_service.fail(run_path, manifest, RunStage.ANALYSIS, error)
            _report_run_context(run_path, manifest)
            raise

        _record_analysis(run_service, run_path, manifest, outcome, identity)
        return _describe_analysis(outcome, plan.policy.min_score)

    _execute(action)


def _record_analysis(
    run_service: RunService,
    run_path: Path,
    manifest: RunManifest,
    outcome: AnalysisOutcome,
    identity: AnalyzerIdentity,
) -> None:
    """Advance the run and write down what produced its candidates."""
    manifest = run_service.advance(run_path, manifest, RunStatus.ANALYZED)
    # The manifest must name what actually produced the candidates. When the
    # executor is a fixture it says so, rather than leaving the configured
    # provider in place and asserting a call that never happened.
    manifest.versions.analysis_provider = outcome.stage_config.analyzer
    manifest.versions.analysis_model = outcome.stage_config.model
    # Which versioned prompt this run sent, or null because it sent none.
    # Assigned unconditionally rather than only when there is a value, so that
    # forcing from Gemini back to a fixture clears the fields instead of leaving
    # the fixture's artifacts described by Gemini's prompt.
    #
    # Not the same question as the stage configuration's prompt_version, which
    # records the identity of whatever ran and for which a fixture's
    # `fake-fixture/v1` is a truthful answer.
    sent = identity.uses_packaged_prompt
    manifest.versions.prompt_version = identity.prompt.version if sent else None
    manifest.versions.prompt_sha256 = identity.prompt.sha256 if sent else None
    run_service.record_stage(
        run_path,
        manifest,
        RunStage.ANALYSIS,
        outcome.fingerprint,
        outcome.stage_config_sha256,
        CANDIDATES_SCHEMA_VERSION,
    )


def _describe_analysis(outcome: AnalysisOutcome, min_score: float) -> str:
    """Report the funnel, warning about the two outcomes worth interrupting for."""
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
        f"{counts.below_min_score} below {min_score}, "
        f"{counts.deduplicated} duplicates, {counts.not_in_top_n} beyond the cap) "
        f"by {outcome.stage_config.analyzer}/{outcome.stage_config.model}, "
        f"fingerprint {outcome.fingerprint[:12]}"
    )


def _reuse_or_recover(
    run_service: RunService,
    run_path: Path,
    manifest: RunManifest,
    analysis_directory: Path,
    transcript: Transcript,
    plan: AnalysisPlan,
) -> str:
    """Report a proved reuse, settling the run's status if it was left failed.

    Split out of `analyze` rather than inlined: the command was over the
    cognitive complexity limit, and this is the part of it that answers a
    different question from the rest. Everything above decides what to run;
    this decides what an already-complete stage means.
    """
    collection = _refuse_or_reuse_analysis(manifest, analysis_directory, transcript, plan)
    summary = f"{collection.counts.selected} selected of {collection.counts.proposed} proposed"
    if manifest.status is RunStatus.ANALYZED:
        # Already settled: reuse must not touch a single byte, so the manifest
        # is not rewritten either.
        return f"[green]Candidates reused:[/green] {summary}. Use --force to rerun."
    # The run was left failed by an attempt that came after these artifacts and
    # did not replace them. The verification above proved they still match the
    # transcript, the analyzer and the settings, so the stage is complete and the
    # failure no longer describes anything. Reporting a reuse while leaving the
    # run FAILED_ANALYSIS would make the status and the message contradict each
    # other, and every later stage reads the status.
    previous = manifest.status
    run_service.advance(run_path, manifest, RunStatus.ANALYZED)
    return (
        f"[green]Candidates recovered:[/green] {summary}. The artifacts still match "
        f"the current inputs, so the run moves from {previous} to "
        f"{RunStatus.ANALYZED}. Use --force to rerun."
    )


def _expected_identity(
    settings: Settings, fixture: Path | None, prompt: Prompt
) -> tuple[FixtureAnalyzer | None, AnalyzerIdentity]:
    """Who would produce this run's candidates, without building anything heavy.

    Returns the fixture analyzer when one is named -- it is already fully built
    by reading the file, so there is nothing to defer and no credential involved
    -- and otherwise only an identity, leaving the client, the SDK and the
    environment untouched.

    That asymmetry is the fix for a real defect. The analyzer used to be
    constructed before the reuse decision, which meant verifying four finished
    artifacts demanded an API key and an SDK client. Nothing about reading four
    files back needs either, and a machine that has lost its key can still be
    asked what a completed run contains.

    ``--fixture`` wins over the configuration on purpose: it is the explicit
    instruction on the command line, and every profile in this repository names
    Gemini by default.
    """
    if fixture is not None:
        analyzer = FixtureAnalyzer(load_fixture(fixture))
        return analyzer, analyzer.identity
    if settings.analysis.provider is AnalysisProvider.GEMINI:
        return None, gemini_identity(settings, prompt)
    # Unreachable while AnalysisProvider has one member, and deliberately not
    # written as an else on the branch above: a provider added to the enum
    # without an adapter must fail here by name rather than silently become
    # Gemini.
    raise ConfigurationError(
        f"analysis.provider is {settings.analysis.provider}, which this build has no "
        "adapter for. Use --fixture to replay recorded answers instead."
    )


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
        plan,
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
