from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
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
from content_engine.adapters.media.preview import FFmpegPreviewRenderer
from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.adapters.transcription.faster_whisper import FasterWhisperTranscriber
from content_engine.config import Settings, config_sha256, load_settings
from content_engine.domain.analysis_rules import AnalyzerIdentity
from content_engine.domain.candidates import (
    CANDIDATES_SCHEMA_VERSION,
    CandidateCollection,
    ValidatedCandidate,
)
from content_engine.domain.enums import (
    REASON_REQUIRING_DETAIL,
    AnalysisProvider,
    EditorialReason,
    RunStage,
    RunStatus,
)
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
from content_engine.domain.preview_rules import (
    PREVIEW_INDEX_FILENAME,
    preview_filename,
    preview_stage_config,
)
from content_engine.domain.previews import PREVIEW_INDEX_SCHEMA_VERSION, PreviewIndex
from content_engine.domain.review import (
    DECISIONS_SCHEMA_VERSION,
    DETAIL_MAX_LENGTH,
    ApprovedDecision,
    EditedDecision,
    RejectedDecision,
    ReviewDecisionCollection,
)
from content_engine.domain.run_state import validate_transition
from content_engine.domain.transcript_rules import stage_config, transcription_fingerprint
from content_engine.services.analysis_service import (
    CANDIDATES_FILENAME,
    AnalysisOutcome,
    AnalysisPlan,
    AnalysisService,
    plan_analysis,
    read_candidates,
    verify_analysis,
)
from content_engine.services.chunking_service import transcript_sha256
from content_engine.services.doctor_service import Check, DoctorService
from content_engine.services.media_service import MediaService
from content_engine.services.preview_service import (
    PreviewPlan,
    PreviewService,
    require_previews,
    verify_previews,
)
from content_engine.services.review_service import (
    DECISIONS_FILENAME,
    Decision,
    ReviewPlan,
    ReviewSession,
    empty_collection,
    require_decisions,
)
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


@app.command()
def preview(
    run_id: Annotated[str, typer.Argument(help="Existing run identifier")],
    config: ConfigOption = None,
    force: Annotated[bool, typer.Option("--force", help="Replace existing previews")] = False,
) -> None:
    """Cut one low-cost vertical proxy per selected candidate (CE-034).

    Previews exist so a person can watch what the analyzer proposed before
    anything expensive is rendered. They are 540x960 by default, encoded fast
    and lossy, with the whole source frame fitted inside the vertical frame and
    padded rather than cropped or stretched: a technical recording usually has
    the point of the clip in a corner of a terminal, and cropping would remove
    it. No subtitles and no final styling -- those belong to CE-040 to CE-045.

    Nothing reaches the previews directory until every proxy has been encoded
    and read back with ffprobe, so a failure leaves the previous set intact and
    the run never claims READY_FOR_REVIEW on the strength of files that were
    not produced.
    """

    def action() -> str:
        settings: Settings = load_settings(config)
        workspace = RunWorkspace(settings.workspace.root)
        run_service = RunService(settings, workspace)
        run_path = workspace.require(run_id)
        manifest = workspace.read_manifest(run_path)

        # Refused before anything is read, hashed or encoded, so a run that
        # cannot reach READY_FOR_REVIEW says so instead of failing at the end.
        validate_transition(manifest.status, RunStatus.READY_FOR_REVIEW)

        if not settings.preview.enabled:
            # Advancing here would put a run in READY_FOR_REVIEW with an empty
            # previews directory, and every later stage reads the status rather
            # than the directory.
            raise ConfigurationError(
                "preview.enabled is false, so no preview can be produced and this run cannot "
                "become READY_FOR_REVIEW. Enable it in the profile, or omit --config to use "
                "the packaged defaults."
            )

        _warn_about_configuration_drift(manifest, config_sha256(settings))
        plan = _plan_previews(run_path, manifest, settings)

        previews_directory = run_path.joinpath("previews")
        # Whether this stage has run is a question for the manifest and for the
        # index together. Either signal sends the invocation through
        # verification, which refuses whatever is inconsistent and leaves the
        # decision to --force.
        already_previewed = (
            RunStage.PREVIEW.value in manifest.stages
            or previews_directory.joinpath(PREVIEW_INDEX_FILENAME).is_file()
        )
        if already_previewed and not force:
            return _reuse_or_recover_previews(
                run_service, run_path, manifest, previews_directory, plan
            )

        if force:
            _warn_about_stale(run_path, ("review", "clips"), "previews")

        try:
            outcome = PreviewService(FFmpegPreviewRenderer(), FFprobeAdapter()).generate(
                plan, previews_directory, datetime.now(UTC)
            )
        except ContentEngineError as error:
            run_service.fail(run_path, manifest, RunStage.PREVIEW, error)
            _report_run_context(run_path, manifest)
            raise

        manifest = run_service.advance(run_path, manifest, RunStatus.READY_FOR_REVIEW)
        run_service.record_stage(
            run_path,
            manifest,
            RunStage.PREVIEW,
            outcome.fingerprint,
            outcome.stage_config_sha256,
            PREVIEW_INDEX_SCHEMA_VERSION,
        )
        return _describe_previews(outcome.index, outcome.fingerprint, previews_directory)

    _execute(action)


def _plan_previews(run_path: Path, manifest: RunManifest, settings: Settings) -> PreviewPlan:
    """Everything the preview stage needs, resolved before an encoder starts.

    The source is hashed rather than merely checked for existence. A file at the
    recorded path that is not the recorded file would produce previews of the
    wrong video, and every check downstream -- the index, the fingerprint, the
    reviewer's judgement -- would be about material this run never analysed.
    """
    collection = read_candidates(run_path.joinpath("analysis"))
    source = manifest.input.path
    if not source.is_file():
        raise InvalidMediaError(
            f"The run source is missing, so no preview can be cut from it: {source}"
        )
    digest = sha256_file(source)
    if digest != manifest.input.sha256:
        raise InvalidMediaError(
            f"The file at the run source path is not the one this run was created from "
            f"(recorded {manifest.input.sha256[:12]}, on disk {digest[:12]}): {source}"
        )
    return PreviewPlan(
        candidates=tuple(collection.candidates),
        config=preview_stage_config(width=settings.preview.width, height=settings.preview.height),
        analysis_fingerprint=_analysis_fingerprint(manifest),
        source_path=source,
        source_sha256=digest,
        source_duration_seconds=collection.source_duration_seconds,
    )


def _analysis_fingerprint(manifest: RunManifest) -> str:
    """The analysis execution a downstream stage is being asked to build on."""
    record = manifest.stages.get(RunStage.ANALYSIS.value)
    if record is None:
        raise IncompatibleArtifactError(
            "This run has no recorded analysis, so there is no shortlist to work from. "
            f"Run `content-engine analyze {manifest.run_id}` first."
        )
    if record.schema_version != CANDIDATES_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"The existing candidates use schema {record.schema_version}; this build produces "
            f"{CANDIDATES_SCHEMA_VERSION}. Rerun the analysis with --force."
        )
    return record.fingerprint


def _describe_previews(index: PreviewIndex, fingerprint: str, directory: Path) -> str:
    if not index.previews:
        console.print(
            "[yellow]Warning:[/yellow] the analysis selected no candidates, so there was "
            "nothing to preview and there will be nothing to review."
        )
    return (
        f"[green]Previews ready:[/green] {len(index.previews)} previews at "
        f"{index.width}x{index.height} in {directory}, fingerprint {fingerprint[:12]}"
    )


def _reuse_or_recover_previews(
    run_service: RunService,
    run_path: Path,
    manifest: RunManifest,
    previews_directory: Path,
    plan: PreviewPlan,
) -> str:
    """Report a proved reuse, settling the run's status if it was left failed.

    The same shape the analysis stage uses, for the same reason: everything
    before this decides what to run, and this decides what an already-complete
    stage means.
    """
    record = manifest.stages.get(RunStage.PREVIEW.value)
    if record is None:
        raise IncompatibleArtifactError(
            "Previews exist but no fingerprint was recorded for them, so they cannot be "
            "shown to match the current candidates and settings. Rerun with --force."
        )
    if record.schema_version != PREVIEW_INDEX_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"The existing previews use index schema {record.schema_version}; this build "
            f"produces {PREVIEW_INDEX_SCHEMA_VERSION}. Rerun with --force."
        )
    index = verify_previews(
        previews_directory, record.fingerprint, record.stage_config_sha256, plan
    )
    summary = f"{len(index.previews)} previews at {index.width}x{index.height}"
    if manifest.status in {RunStatus.READY_FOR_REVIEW, RunStatus.REVIEWED}:
        # Already settled: reuse must not touch a single byte, so the manifest
        # is not rewritten either.
        return f"[green]Previews reused:[/green] {summary}. Use --force to regenerate."
    previous = manifest.status
    run_service.advance(run_path, manifest, RunStatus.READY_FOR_REVIEW)
    return (
        f"[green]Previews recovered:[/green] {summary}. The files still match the current "
        f"candidates, source and settings, so the run moves from {previous} to "
        f"{RunStatus.READY_FOR_REVIEW}. Use --force to regenerate."
    )


#: Named without an `Error` suffix on purpose: this is control flow, not a
#: failure. The rationale lives here rather than after the suppression code,
#: because a `noqa` followed by prose is not valid suppression syntax and every
#: analyser reading it has to guess where the code list ends.
class _SessionOver(Exception):  # noqa: N818
    """The reviewer stopped. Carries how, so the report can say which it was.

    Not a ``ContentEngineError``: quitting, reaching the end of input and
    pressing Ctrl+C are all ordinary ways to finish a review session, and none
    of them is a failure of the run. Modelling them as an exception rather than
    a sentinel return keeps the prompt helpers able to end the session from
    wherever they are, which is what makes EOF handling uniform across the
    action, reason, detail and boundary prompts.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _read_line(prompt: str) -> str:
    """Ask one question. Raises EOFError at end of input, as ``input`` does.

    Extracted so the suite can drive a session, and so an interrupt can be
    placed at an exact question. ``input`` is used rather than ``click.prompt``
    because click collapses EOF and Ctrl+C into one ``Abort``, and these two
    have to be told apart in the message the session ends with.
    """
    console.print(prompt, end="")
    return input()


def _ask(prompt: str) -> str:
    """Read one answer, or end the session the way the reviewer ended it."""
    try:
        return _read_line(prompt).strip()
    except EOFError as error:
        raise _SessionOver("input ended") from error
    except KeyboardInterrupt as error:
        raise _SessionOver("interrupted") from error


@app.command()
def review(
    run_id: Annotated[str, typer.Argument(help="Existing run identifier")],
    config: ConfigOption = None,
    force: Annotated[
        bool, typer.Option("--force", help="Discard existing decisions and review again")
    ] = False,
) -> None:
    """Approve, reject or retime each candidate (CE-035 to CE-039).

    One candidate at a time, with its preview path, and five keys: approve,
    reject, edit the range, skip, or quit. Every explicit decision is written
    to ``review/decisions.json`` before the next candidate is shown, so a
    closed terminal costs nothing that was already decided.

    Skipping records nothing, which is what makes it different from rejecting:
    a skipped candidate is still pending and comes back in the next session.
    The run reaches REVIEWED only once every selected candidate has an explicit
    decision.
    """

    def action() -> str:
        settings: Settings = load_settings(config)
        workspace = RunWorkspace(settings.workspace.root)
        run_service = RunService(settings, workspace)
        run_path = workspace.require(run_id)
        manifest = workspace.read_manifest(run_path)

        if manifest.status not in {RunStatus.READY_FOR_REVIEW, RunStatus.REVIEWED}:
            raise IncompatibleArtifactError(
                f"This run is {manifest.status}, so there are no previews to review. "
                f"Run `content-engine preview {run_id}` first."
            )

        collection = read_candidates(run_path.joinpath("analysis"))
        plan = ReviewPlan(
            candidates=tuple(collection.candidates),
            analysis_fingerprint=_analysis_fingerprint(manifest),
            source_duration_seconds=collection.source_duration_seconds,
        )
        previews_directory = run_path.joinpath("previews")
        _require_reviewable_previews(manifest, previews_directory, plan)

        review_directory = run_path.joinpath("review")
        session = ReviewSession(
            review_directory,
            plan,
            _existing_or_new_decisions(review_directory, plan, force, datetime.now(UTC)),
        )
        return _run_review_session(
            run_service, run_path, manifest, session, previews_directory, force
        )

    _execute(action)


def _require_reviewable_previews(
    manifest: RunManifest, previews_directory: Path, plan: ReviewPlan
) -> None:
    """Every candidate must have an intact preview before anyone is asked about it.

    Checked with the recorded stage configuration rather than the one this
    invocation would use. Whether the previews are the size a profile asks for
    today is a question for ``preview``; the only thing review needs is that
    the file the reviewer is about to watch is the one that was produced for
    this candidate.
    """
    record = manifest.stages.get(RunStage.PREVIEW.value)
    if record is None:
        raise IncompatibleArtifactError(
            "This run has no recorded previews, so there is nothing to watch. Run "
            f"`content-engine preview {manifest.run_id}` first."
        )
    if record.schema_version != PREVIEW_INDEX_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"The existing previews use index schema {record.schema_version}; this build "
            f"produces {PREVIEW_INDEX_SCHEMA_VERSION}. Regenerate them with "
            f"`content-engine preview {manifest.run_id} --force`."
        )
    require_previews(
        previews_directory,
        record.fingerprint,
        record.stage_config_sha256,
        plan.candidates,
        plan.analysis_fingerprint,
        manifest.input.sha256,
    )


def _existing_or_new_decisions(
    review_directory: Path,
    plan: ReviewPlan,
    force: bool,
    now: datetime,
) -> ReviewDecisionCollection:
    """Resume the decisions on disk, or start over because --force said so.

    ``--force`` does not delete anything here. The replacement collection is
    only written when the first new decision is taken, or when a session with
    nothing to decide is opened, and that write is atomic -- so a forced session
    that fails before deciding anything leaves the previous decisions exactly
    where they were.
    """
    path = review_directory.joinpath(DECISIONS_FILENAME)
    if not path.is_file():
        return empty_collection(plan, now)
    if force:
        _warn_about_discarding(review_directory, plan)
        return empty_collection(plan, now)
    return require_decisions(review_directory, plan)


def _warn_about_discarding(review_directory: Path, plan: ReviewPlan) -> None:
    """Say exactly how much human judgement --force is about to throw away.

    Read defensively: a file too damaged to interpret is exactly the case
    ``--force`` exists for, so failing to count its decisions must not stop the
    reviewer from starting again.
    """
    try:
        existing = require_decisions(review_directory, plan)
    except ContentEngineError:
        console.print(
            "[yellow]Warning:[/yellow] the existing decisions cannot be read, and --force "
            "will replace them with a new review. They are not recoverable afterwards."
        )
        return
    counts = existing.counts
    console.print(
        f"[yellow]Warning:[/yellow] --force will discard {len(existing.decisions)} decision(s) "
        f"({counts['approved']} approved, {counts['rejected']} rejected, "
        f"{counts['edited']} edited) and ask about every candidate again. Human decisions "
        "cannot be regenerated."
    )


def _run_review_session(
    run_service: RunService,
    run_path: Path,
    manifest: RunManifest,
    session: ReviewSession,
    previews_directory: Path,
    force: bool,
) -> str:
    """Drive the prompt loop, then settle the run's status against what was decided."""
    total = len(session.plan.candidates)
    if force and manifest.status is RunStatus.REVIEWED:
        # ADR-030. The decisions are about to be replaced, so the run must stop
        # claiming a finished review while the reviewer works through the list.
        manifest = run_service.advance(run_path, manifest, RunStatus.READY_FOR_REVIEW)

    if not session.plan.candidates:
        session.open(datetime.now(UTC))
        console.print(
            "[yellow]Warning:[/yellow] the analysis selected no candidates, so there is "
            "nothing to decide."
        )
        return _complete_review(run_service, run_path, manifest, session, total)

    if session.complete:
        return _settle_finished_review(run_service, run_path, manifest, session, total)

    console.print(
        f"Reviewing {len(session.pending)} of {total} candidates. "
        "Watch each preview, then choose an action."
    )
    ended: str | None = None
    try:
        _prompt_for_every_pending(session, previews_directory, total)
    except _SessionOver as over:
        ended = over.reason

    counts = session.collection.counts
    decided = (
        f"{len(session.collection.decisions)} of {total} decided "
        f"({counts['approved']} approved, {counts['rejected']} rejected, "
        f"{counts['edited']} edited)"
    )
    if session.complete:
        return _complete_review(run_service, run_path, manifest, session, total)
    # Nothing is written to the manifest. The run stays READY_FOR_REVIEW, which
    # is the truth: some candidates still have no decision.
    stopped = f" Session ended: {ended}." if ended else ""
    return (
        f"[green]Decisions saved:[/green] {decided}.{stopped} "
        f"Run the same command again to continue with the {len(session.pending)} left."
    )


def _settle_finished_review(
    run_service: RunService,
    run_path: Path,
    manifest: RunManifest,
    session: ReviewSession,
    total: int,
) -> str:
    """A session that had nothing left to ask: report it, and settle the status.

    Reaching here with the run already REVIEWED is the ordinary case, and it
    must not write a byte: the decisions and the manifest are exactly what the
    completing session left behind.

    Reaching here with the run **not** REVIEWED is the case this exists for.
    `review --force` moves a finished run back to READY_FOR_REVIEW before
    asking anything, and writes no decisions until the first new answer -- so a
    reviewer who forces and then quits leaves a complete set of decisions
    beside a status that says the review is open. Reporting "already reviewed"
    and stopping, which is what this used to do, stranded the run there
    permanently: every later invocation took the same branch and said the same
    thing while the status never moved.

    The decisions have already been proved coherent against this analysis and
    this shortlist by `require_decisions`, so the review really is finished and
    the status is the only thing that disagrees. It is corrected here, which is
    the same recovery the analysis and preview stages perform when their
    artifacts outlive a failure that came after them.
    """
    counts = session.collection.counts
    tally = (
        f"{counts['approved']} approved, {counts['rejected']} rejected, {counts['edited']} edited"
    )
    if manifest.status is RunStatus.REVIEWED:
        return (
            f"[green]Already reviewed:[/green] all {total} candidates have a decision "
            f"({tally}). Use --force to review them again."
        )
    previous = manifest.status
    fingerprint, _ = _record_completed_review(run_service, run_path, manifest, session)
    return (
        f"[green]Review recovered:[/green] all {total} candidates already have a decision "
        f"({tally}), and they still match this analysis, so the run moves from {previous} "
        f"to {RunStatus.REVIEWED}. Fingerprint {fingerprint[:12]}."
    )


def _record_completed_review(
    run_service: RunService,
    run_path: Path,
    manifest: RunManifest,
    session: ReviewSession,
) -> tuple[str, str]:
    """Advance to REVIEWED and record the stage. Refuses a half-finished session."""
    fingerprint, stage_config_digest = session.stage_record()
    manifest = run_service.advance(run_path, manifest, RunStatus.REVIEWED)
    run_service.record_stage(
        run_path,
        manifest,
        RunStage.REVIEW,
        fingerprint,
        stage_config_digest,
        DECISIONS_SCHEMA_VERSION,
    )
    return fingerprint, stage_config_digest


def _complete_review(
    run_service: RunService,
    run_path: Path,
    manifest: RunManifest,
    session: ReviewSession,
    total: int,
) -> str:
    """A session that just finished the last pending candidate."""
    fingerprint, _ = _record_completed_review(run_service, run_path, manifest, session)
    counts = session.collection.counts
    return (
        f"[green]Review complete:[/green] {total} candidates decided — "
        f"{counts['approved']} approved, {counts['rejected']} rejected, "
        f"{counts['edited']} edited. Fingerprint {fingerprint[:12]}."
    )


def _prompt_for_every_pending(session: ReviewSession, previews_directory: Path, total: int) -> None:
    """Ask about each pending candidate once, in rank order.

    The pending list is recomputed from the collection each time round, so a
    decision taken in this session removes its candidate and a skip leaves it
    for the next one. Skips are tracked here rather than in the file, which is
    what keeps "no decision" and "decided to do nothing" different things.
    """
    skipped: set[str] = set()
    while True:
        remaining = [candidate for candidate in session.pending if candidate.id not in skipped]
        if not remaining:
            return
        candidate = remaining[0]
        position = total - len(session.pending) + 1
        _show_candidate(candidate, position, total, previews_directory)
        decision = _ask_for_decision(candidate, session.plan.source_duration_seconds)
        if decision is None:
            skipped.add(candidate.id)
            console.print("[yellow]Skipped.[/yellow] It stays pending for the next session.\n")
            continue
        session.record(decision, datetime.now(UTC))
        console.print(f"[green]Saved:[/green] {candidate.id} {decision.decision}.\n")


def _show_candidate(
    candidate: ValidatedCandidate, position: int, total: int, previews_directory: Path
) -> None:
    """Everything CE-035 requires be on screen before a decision is asked for."""
    scores = candidate.scores
    console.print(f"[bold]({position}/{total}) {candidate.id}[/bold]  rank {candidate.rank}")
    console.print(f"  topic:     {candidate.topic}")
    console.print(f"  category:  {candidate.category}")
    console.print(
        f"  interval:  {candidate.start:.2f}s to {candidate.end:.2f}s ({candidate.duration:.2f}s)"
    )
    console.print(f"  score:     {candidate.total_score:.2f}")
    console.print(
        f"  parts:     hook {scores.hook}, value {scores.value}, "
        f"context {scores.context_independence}, clarity {scores.clarity}, "
        f"engagement {scores.engagement_potential}, relevance {scores.relevance}"
    )
    console.print(f"  hook:      {candidate.hook}")
    console.print(f"  summary:   {candidate.summary}")
    console.print(f"  reason:    {candidate.reason}")
    # soft_wrap keeps the path on one line. A path broken across two lines is a
    # path nobody can copy into a player, which is the only thing this line is
    # for, and rich wraps to the console width by default.
    console.print(
        f"  preview:   {previews_directory.joinpath(preview_filename(candidate.id))}",
        soft_wrap=True,
    )


def _ask_for_decision(
    candidate: ValidatedCandidate, source_duration_seconds: float
) -> Decision | None:
    """One answer for one candidate, or None for a skip.

    Loops until an action is understood. An unrecognised key is a typo, and
    treating it as anything other than "ask again" would either record a
    decision the reviewer did not make or lose their place in the list.
    """
    while True:
        answer = _ask(
            "[A] Approve  [R] Reject  [E] Edit range  [S] Skip  [Q] Quit/save\nAction: "
        ).lower()
        if answer == "a":
            return ApprovedDecision(
                candidate_id=candidate.id,
                original_start=candidate.start,
                original_end=candidate.end,
                final_start=candidate.start,
                final_end=candidate.end,
                reviewed_at=datetime.now(UTC),
            )
        if answer == "r":
            return _ask_for_rejection(candidate)
        if answer == "e":
            return _ask_for_edit(candidate, source_duration_seconds)
        if answer == "s":
            return None
        if answer == "q":
            raise _SessionOver("quit at the reviewer's request")
        console.print(f"[yellow]{answer!r} is not one of A, R, E, S or Q.[/yellow]")


def _ask_for_rejection(candidate: ValidatedCandidate) -> RejectedDecision:
    """CE-037. A structured reason, optionally, and a detail when it is needed."""
    reasons = list(EditorialReason)
    listing = "  ".join(f"{number}:{reason}" for number, reason in enumerate(reasons, start=1))
    while True:
        answer = _ask(f"Reason ({listing})\nReason [none]: ")
        if not answer:
            return _rejection(candidate, None, None)
        reason = _match_reason(answer, reasons)
        if reason is None:
            console.print(f"[yellow]{answer!r} is not one of the listed reasons.[/yellow]")
            continue
        if reason is not REASON_REQUIRING_DETAIL:
            return _rejection(candidate, reason, None)
        while True:
            detail = _ask(
                f"Detail (required for {REASON_REQUIRING_DETAIL}, "
                f"up to {DETAIL_MAX_LENGTH} characters): "
            )
            if not detail:
                console.print(
                    f"[yellow]{REASON_REQUIRING_DETAIL} carries no meaning on its own; "
                    "a detail is required.[/yellow]"
                )
                continue
            try:
                return _rejection(candidate, reason, detail)
            except ValidationError as error:
                # The model owns the length limit; the loop only re-asks. A
                # ValidationError escaping here reached the CLI's last-resort
                # handler and exited 1 as an unexpected internal error, after
                # earlier decisions had already been saved -- so a reviewer who
                # pasted too much text saw a crash and no sign that their work
                # had survived.
                console.print(f"[yellow]{_first_message(error)}[/yellow]")


def _match_reason(answer: str, reasons: list[EditorialReason]) -> EditorialReason | None:
    """Accept either the number shown beside a reason or the reason itself."""
    if answer.isdigit() and 1 <= int(answer) <= len(reasons):
        return reasons[int(answer) - 1]
    for reason in reasons:
        if answer == reason.value:
            return reason
    return None


def _rejection(
    candidate: ValidatedCandidate, reason: EditorialReason | None, detail: str | None
) -> RejectedDecision:
    return RejectedDecision(
        candidate_id=candidate.id,
        original_start=candidate.start,
        original_end=candidate.end,
        reason=reason,
        detail=detail,
        reviewed_at=datetime.now(UTC),
    )


def _ask_for_edit(candidate: ValidatedCandidate, source_duration_seconds: float) -> EditedDecision:
    """CE-038. Both bounds, re-asked from the top whenever the pair is unusable.

    Any rejected answer restarts the pair rather than re-asking one field. An
    edit is one act with two numbers, and the errors that matter -- inverted,
    unchanged, outside the source -- are properties of the pair, so a reviewer
    correcting one field would keep being told the same thing about the other.
    """
    while True:
        start = _ask_for_bound("start", candidate.start, source_duration_seconds)
        if start is None:
            continue
        end = _ask_for_bound("end", candidate.end, source_duration_seconds)
        if end is None:
            continue
        if end <= start:
            console.print(
                f"[yellow]An end of {end:.2f}s is at or before the start ({start:.2f}s).[/yellow]"
            )
            continue
        try:
            return EditedDecision(
                candidate_id=candidate.id,
                original_start=candidate.start,
                original_end=candidate.end,
                final_start=start,
                final_end=end,
                reviewed_at=datetime.now(UTC),
            )
        except ValidationError as error:
            # The model owns the invariants; the loop only re-asks. Duplicating
            # "must differ from the original" here would let the two drift.
            console.print(f"[yellow]{_first_message(error)}[/yellow]")


def _ask_for_bound(name: str, current: float, source_duration_seconds: float) -> float | None:
    """One boundary in seconds, blank to keep it, or None to start the pair again."""
    answer = _ask(f"New {name} in seconds [{current:.2f}] (blank keeps it): ")
    if not answer:
        return current
    try:
        value = float(answer)
    except ValueError:
        console.print(f"[yellow]{answer!r} is not a number of seconds.[/yellow]")
        return None
    if not isfinite(value):
        console.print(f"[yellow]{answer!r} is not a position in the source.[/yellow]")
        return None
    if value < 0:
        console.print("[yellow]A boundary cannot be negative.[/yellow]")
        return None
    if value > source_duration_seconds:
        console.print(
            f"[yellow]{value:.2f}s is past the end of the source "
            f"({source_duration_seconds:.2f}s).[/yellow]"
        )
        return None
    return value


def _first_message(error: ValidationError) -> str:
    """The one validation message worth showing a person mid-session."""
    issues = error.errors()
    detail = str(issues[0]["msg"]) if issues else str(error)
    return detail.removeprefix("Value error, ")


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
