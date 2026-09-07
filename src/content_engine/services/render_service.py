"""Orchestration of the final render stage (CE-045, CE-046).

Cuts one finished clip per decision a person kept, builds its two subtitle
documents, verifies every clip with ffprobe, and writes the index that lets a
later invocation reuse them without re-encoding.

Four properties are enforced by the shape of this module rather than by care,
and each one is the answer to a way this could go wrong.

**Nothing reaches the clips directory until everything is verified.** Every
clip is encoded inside a staging directory and probed there. Only once the whole
set exists and has passed is anything moved into place, so a failure cannot
leave a half-rendered set that a later run would read as complete, and a failed
``--force`` cannot destroy the previous one.

**A record is a measurement, not a request.** The dimensions, the sample aspect
ratio, the codecs and the duration come from ffprobe reading the finished MP4;
the digests and sizes come from the bytes of all three artifacts. Recording what
FFmpeg was asked for would produce an index that stays correct while the encoder
silently does something else -- which is exactly what happened to the preview
stage's audio codec, and was found only because someone thought to check.

**Reuse is proved, never assumed.** Verification reads the index and the stage
configuration back, revalidates both under their schemas, re-hashes every
artifact on disk, reads every metadata file back and compares it against the
record, parses both subtitle documents, checks the set against the decisions the
review recorded, refuses any clip directory the index does not name, and
rebuilds the fingerprint. A digest that still looks right proves nothing if the
file it addresses was replaced.

**The decisions are obeyed exactly.** Which intervals are cut is not decided
here at all -- ``domain.render_targets`` derives that from the decision file, and
this module renders the list it is given. A rejected candidate has no entry in
that list, so there is no branch here that could render one.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from content_engine.domain.exceptions import (
    ClipRollbackError,
    IncompatibleArtifactError,
    RenderError,
)
from content_engine.domain.models import MediaInfo, Transcript, TranscriptWord
from content_engine.domain.render_rules import (
    RENDER_INDEX_FILENAME,
    RENDER_STAGE_CONFIG_FILENAME,
    ass_style,
    render_coherence_problem,
    render_fingerprint,
    render_stage_config_sha256,
    subtitle_rules,
)
from content_engine.domain.render_targets import RenderTarget, RenderTargetClip
from content_engine.domain.renders import (
    CLIP_FILENAME,
    CLIP_METADATA_FILENAME,
    CLIP_METADATA_SCHEMA_VERSION,
    RENDER_INDEX_SCHEMA_VERSION,
    RENDER_STAGE_CONFIG_SCHEMA_VERSION,
    SUBTITLES_ASS_FILENAME,
    SUBTITLES_SRT_FILENAME,
    ClipMetadata,
    ClipRecord,
    RenderIndex,
    RenderStageConfig,
    clip_dirname,
)
from content_engine.domain.subtitles import (
    SubtitleCue,
    build_cues,
    read_ass_events,
    read_srt_events,
    render_ass,
    render_srt,
)
from content_engine.ports.preview import MediaProbePort
from content_engine.ports.render import ClipRendererPort
from content_engine.services.publication import PublicationLayout, publish, resolve_pending
from content_engine.utils.hashing import sha256_file
from content_engine.utils.json import read_json, write_json, write_text

#: Where clips are encoded before they are accepted. Inside the clips directory
#: so the move onto the final name is a rename on one filesystem, and prefixed
#: with a dot so a directory listing does not present it as content.
STAGING_DIRNAME = ".staging"
#: Where the previously published set is held while the new one is assembled.
ROLLBACK_DIRNAME = ".rollback"
ROLLBACK_JOURNAL = "rollback.json"

#: Written in this order, and the order matters: the file the reuse check looks
#: for first is written last, so an interrupted run cannot leave a directory
#: that looks complete.
ARTIFACT_FILENAMES = (RENDER_STAGE_CONFIG_FILENAME, RENDER_INDEX_FILENAME)

#: The prefix every clip directory carries, and the whole of how publication
#: recognises one. It is the same prefix ``clip_dirname`` builds, named here so
#: the ownership test does not have to parse a template.
CLIP_DIR_PREFIX = "clip_"


def _is_publishable(path: Path) -> bool:
    """Whether a path is one of the entries publication owns.

    The clip directories and the two artifacts, and nothing else -- not the
    staging or rollback directories, and not anything an operator happened to
    leave here.
    """
    if path.is_dir():
        return path.name.startswith(CLIP_DIR_PREFIX)
    return path.name in ARTIFACT_FILENAMES


#: How this stage publishes. The protocol lives in ``services.publication`` and
#: is shared with the preview stage (ADR-033); what is local here is the names,
#: which entries publication owns, and the exception a failure raises.
CLIP_PUBLICATION = PublicationLayout(
    staging_dirname=STAGING_DIRNAME,
    rollback_dirname=ROLLBACK_DIRNAME,
    journal_filename=ROLLBACK_JOURNAL,
    owns=_is_publishable,
    error=ClipRollbackError,
    command="render",
    set_noun="clip set",
    plural_noun="clips",
)


@dataclass(frozen=True)
class RenderPlan:
    """Everything decided before an encoder is started.

    Built by the caller so the reuse path can rebuild exactly what it expects
    without running anything. The transcript is carried whole rather than as a
    list of words, because the refusal for a transcript with no word timestamps
    has to be able to tell "this transcript has no words" from "this clip covers
    silence", and only the whole transcript answers that.
    """

    target: RenderTarget
    config: RenderStageConfig
    source_path: Path
    transcript: Transcript
    run_id: str

    @property
    def words(self) -> list[TranscriptWord]:
        return [word for segment in self.transcript.segments for word in segment.words]


@dataclass(frozen=True)
class RenderOutcome:
    index: RenderIndex
    config: RenderStageConfig
    stage_config_sha256: str
    fingerprint: str


@dataclass(frozen=True)
class _Staged:
    """One finished clip directory, before it is published."""

    record: ClipRecord
    metadata: ClipMetadata


class RenderService:
    def __init__(self, renderer: ClipRendererPort, probe: MediaProbePort) -> None:
        self.renderer = renderer
        self.probe = probe

    def generate(
        self,
        plan: RenderPlan,
        directory: Path,
        generated_at: datetime,
    ) -> RenderOutcome:
        """Produce, verify and commit the whole set, or leave the directory alone."""
        if plan.target.clips:
            self._require_renderable(plan)

        # A backup left pending by an earlier failure is finished, or refused,
        # before anything else happens. Encoding over it would be building a new
        # set on top of a directory that is still half of an old one.
        resolve_pending_rollback(directory)

        staging = directory.joinpath(STAGING_DIRNAME)
        try:
            staged = self._render_all(plan, staging, generated_at)
            index = self._build_index(plan, [item.record for item in staged], generated_at)
            self._publish(directory, staging, index, plan.config)
        finally:
            # The staging directory only. `.rollback` is never removed here:
            # deleting it unconditionally is how a failed restore turned a
            # recoverable state into a lost one in the preview stage. It is
            # removed in one place only, and that place is inside `publish`.
            shutil.rmtree(staging, ignore_errors=True)

        return RenderOutcome(
            index=index,
            config=plan.config,
            stage_config_sha256=render_stage_config_sha256(plan.config),
            fingerprint=render_fingerprint(index, plan.config),
        )

    @staticmethod
    def _require_renderable(plan: RenderPlan) -> None:
        """Refuse the two conditions no clip can be produced under.

        The word-timestamp check is a stated contract rather than a fallback.
        A transcript without word timestamps could be turned into subtitles from
        its segment bounds, and the result would be five-to-thirty-second blocks
        of text appearing all at once -- not subtitles in any sense a viewer
        would recognise, and an artifact claiming a synchronisation it does not
        have. ``transcription.word_timestamps`` is true in the packaged defaults;
        a run made without it is refused here and told why, rather than shipped
        something worse than nothing.

        It is deliberately a property of the transcript rather than of a clip.
        An interval containing no speech is a legitimate clip with an empty
        subtitle file, and conflating the two would let the subtitle rules
        decide which moments a person is allowed to publish.
        """
        if not plan.source_path.is_file():
            raise RenderError(
                f"The run source is missing, so no clip can be cut from it: {plan.source_path}"
            )
        if plan.transcript.segments and not plan.words:
            raise RenderError(
                "The transcript holds no word timestamps, so clip-local subtitles cannot be "
                "built from it. Subtitles are built from words, never approximated from "
                "segment bounds. Re-run `transcribe` with transcription.word_timestamps "
                "enabled, which is the packaged default."
            )

    def _render_all(self, plan: RenderPlan, staging: Path, generated_at: datetime) -> list[_Staged]:
        """Encode, subtitle and measure every clip inside the staging directory."""
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        return [self._render_one(plan, clip, staging, generated_at) for clip in plan.target.clips]

    def _render_one(
        self,
        plan: RenderPlan,
        clip: RenderTargetClip,
        staging: Path,
        generated_at: datetime,
    ) -> _Staged:
        directory = staging.joinpath(clip_dirname(clip.candidate.id))
        directory.mkdir(parents=True, exist_ok=True)

        cues = self._build_cues(plan, clip)
        srt_path = directory.joinpath(SUBTITLES_SRT_FILENAME)
        ass_path = directory.joinpath(SUBTITLES_ASS_FILENAME)
        write_text(srt_path, render_srt(cues))
        write_text(
            ass_path,
            render_ass(cues, plan.config.width, plan.config.height, ass_style(plan.config)),
        )

        output = directory.joinpath(CLIP_FILENAME)
        self.renderer.render(
            plan.source_path,
            clip.start,
            clip.duration,
            ass_path if plan.config.burn_subtitles else None,
            output,
            plan.config,
        )
        record = self._measure(clip, output, srt_path, ass_path, len(cues), plan.config)
        # Read back before the clip is published rather than trusted: CE-046
        # asks whether the documents on disk are subtitle files a player will
        # accept, and the only honest way to answer is to parse them.
        _require_subtitles_within(directory, record)

        metadata = self._metadata(plan, clip, record, generated_at)
        write_json(directory.joinpath(CLIP_METADATA_FILENAME), metadata.model_dump(mode="json"))
        return _Staged(record=record, metadata=metadata)

    @staticmethod
    def _build_cues(plan: RenderPlan, clip: RenderTargetClip) -> list[SubtitleCue]:
        try:
            return build_cues(plan.words, clip.start, clip.end, subtitle_rules(plan.config))
        except ValueError as error:
            raise RenderError(
                f"The subtitles for {clip.candidate.id} cannot be built: {error}"
            ) from error

    def _measure(
        self,
        clip: RenderTargetClip,
        output: Path,
        srt_path: Path,
        ass_path: Path,
        cue_count: int,
        config: RenderStageConfig,
    ) -> ClipRecord:
        """Read back what was produced and refuse it if it is not what was asked for."""
        media = self._probe_clip(output)
        self._require_picture(clip, media, config)
        self._require_audio(clip, media, config)
        drift = abs(media.duration_seconds - clip.duration)
        if drift > config.duration_tolerance_seconds:
            raise RenderError(
                f"The clip for {clip.candidate.id} is {media.duration_seconds:.3f}s long for a "
                f"{clip.duration:.3f}s interval, {drift:.3f}s beyond the "
                f"{config.duration_tolerance_seconds}s duration tolerance"
            )
        try:
            return ClipRecord(
                candidate_id=clip.candidate.id,
                rank=clip.rank,
                decision=clip.decision,
                original_start=clip.original_start,
                original_end=clip.original_end,
                start=clip.start,
                end=clip.end,
                duration=clip.duration,
                directory=clip_dirname(clip.candidate.id),
                clip_filename=CLIP_FILENAME,
                srt_filename=SUBTITLES_SRT_FILENAME,
                ass_filename=SUBTITLES_ASS_FILENAME,
                metadata_filename=CLIP_METADATA_FILENAME,
                width=media.width,
                height=media.height,
                sample_aspect_ratio=media.sample_aspect_ratio or "",
                measured_duration_seconds=media.duration_seconds,
                video_codec=media.video_codec,
                audio_codec=media.audio_codec or "",
                sha256=sha256_file(output),
                size_bytes=output.stat().st_size,
                srt_sha256=sha256_file(srt_path),
                srt_size_bytes=srt_path.stat().st_size,
                ass_sha256=sha256_file(ass_path),
                ass_size_bytes=ass_path.stat().st_size,
                cue_count=cue_count,
                subtitles_burned=config.burn_subtitles,
            )
        except ValidationError as error:
            raise RenderError(
                f"The render stage produced a clip it cannot describe: {error}"
            ) from error

    @staticmethod
    def _require_picture(
        clip: RenderTargetClip, media: MediaInfo, config: RenderStageConfig
    ) -> None:
        if (media.width, media.height) != (config.width, config.height):
            raise RenderError(
                f"The clip for {clip.candidate.id} is {media.width}x{media.height}; "
                f"{config.width}x{config.height} was requested"
            )
        if media.video_codec != config.expected_video_codec:
            raise RenderError(
                f"The clip for {clip.candidate.id} holds {media.video_codec} video; "
                f"{config.expected_video_codec} was requested"
            )
        # Dimensions alone do not say whether a player will stretch the picture
        # back out of 9:16. A clip that lost `setsar` is 1080x1920 and wrong.
        if media.sample_aspect_ratio is None:
            raise RenderError(
                f"The clip for {clip.candidate.id} declares no sample aspect ratio, so it "
                f"cannot be shown to have square pixels; "
                f"{config.expected_sample_aspect_ratio} was requested"
            )
        if media.sample_aspect_ratio != config.expected_sample_aspect_ratio:
            raise RenderError(
                f"The clip for {clip.candidate.id} has a sample aspect ratio of "
                f"{media.sample_aspect_ratio}; {config.expected_sample_aspect_ratio} was "
                "requested, so a player would stretch it"
            )

    @staticmethod
    def _require_audio(clip: RenderTargetClip, media: MediaInfo, config: RenderStageConfig) -> None:
        if media.audio_codec is None:
            raise RenderError(
                f"The clip for {clip.candidate.id} has no audio stream. A silent clip is not "
                "publishable."
            )
        # A track that exists is not the track that was asked for. Checking only
        # that one exists let a stream-copied or transcoded track through the
        # preview stage and be recorded as AAC.
        if media.audio_codec != config.expected_audio_codec:
            raise RenderError(
                f"The clip for {clip.candidate.id} holds {media.audio_codec} audio; "
                f"{config.expected_audio_codec} was requested"
            )

    def _probe_clip(self, path: Path) -> MediaInfo:
        try:
            media, _ = self.probe.probe(path)
        except Exception as error:  # noqa: BLE001 - any unreadable result is one refusal
            raise RenderError(f"The clip {path.name} cannot be read back: {error}") from error
        return media

    @staticmethod
    def _metadata(
        plan: RenderPlan,
        clip: RenderTargetClip,
        record: ClipRecord,
        generated_at: datetime,
    ) -> ClipMetadata:
        candidate = clip.candidate
        return ClipMetadata(
            schema_version=CLIP_METADATA_SCHEMA_VERSION,
            generated_at=generated_at,
            run_id=plan.run_id,
            candidate_id=candidate.id,
            rank=record.rank,
            decision=record.decision,
            category=candidate.category,
            topic=candidate.topic,
            hook=candidate.hook,
            summary=candidate.summary,
            reason=candidate.reason,
            total_score=candidate.total_score,
            original_start=record.original_start,
            original_end=record.original_end,
            start=record.start,
            end=record.end,
            duration=record.duration,
            preset=plan.config.preset,
            width=record.width,
            height=record.height,
            sample_aspect_ratio=record.sample_aspect_ratio,
            video_codec=record.video_codec,
            audio_codec=record.audio_codec,
            measured_duration_seconds=record.measured_duration_seconds,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
            srt_sha256=record.srt_sha256,
            ass_sha256=record.ass_sha256,
            cue_count=record.cue_count,
            subtitles_burned=record.subtitles_burned,
            analysis_fingerprint=plan.target.analysis_fingerprint,
            review_fingerprint=plan.target.review_fingerprint,
            source_sha256=plan.target.source_sha256,
            transcript_sha256=plan.target.transcript_sha256,
        )

    @staticmethod
    def _build_index(
        plan: RenderPlan, records: list[ClipRecord], generated_at: datetime
    ) -> RenderIndex:
        try:
            index = RenderIndex(
                generated_at=generated_at,
                analysis_fingerprint=plan.target.analysis_fingerprint,
                review_fingerprint=plan.target.review_fingerprint,
                decisions_sha256=plan.target.decisions_sha256,
                transcript_sha256=plan.target.transcript_sha256,
                source_sha256=plan.target.source_sha256,
                source_duration_seconds=plan.target.source_duration_seconds,
                width=plan.config.width,
                height=plan.config.height,
                preset=plan.config.preset,
                burn_subtitles=plan.config.burn_subtitles,
                clips=records,
            )
        except ValidationError as error:
            # Translated rather than allowed to escape: the caller decides what a
            # failure means for the run, and it can only do that for a
            # RenderError. A pydantic error reaching the CLI would be reported as
            # an unexpected internal fault instead of a render failure.
            raise RenderError(
                f"The render stage produced an index it cannot describe: {error}"
            ) from error
        # Built together, so this should be impossible; checked anyway, because
        # the alternative is writing an index a later run will read and believe.
        problem = render_coherence_problem(index, plan.config, plan.target)
        if problem is not None:
            raise RenderError(f"The render stage produced records that disagree: {problem}.")
        return index

    @staticmethod
    def _publish(
        directory: Path,
        staging: Path,
        index: RenderIndex,
        config: RenderStageConfig,
    ) -> None:
        """Replace the published set with the staged one.

        The durable protocol is in ``services.publication``, which explains the
        mechanism and the guarantee. What is left here is the part that is
        actually about clips: whole directories are moved rather than files, and
        the index is written last because it is the file the reuse check looks
        for first.
        """
        with publish(CLIP_PUBLICATION, directory):
            for entry in index.clips:
                staging.joinpath(entry.directory).replace(directory.joinpath(entry.directory))
            for name, payload in (
                (RENDER_STAGE_CONFIG_FILENAME, config.model_dump(mode="json")),
                (RENDER_INDEX_FILENAME, index.model_dump(mode="json")),
            ):
                write_json(directory.joinpath(name), payload)


def resolve_pending_rollback(directory: Path) -> str | None:
    """Finish a restore an earlier failure could not, or refuse to touch it."""
    return resolve_pending(CLIP_PUBLICATION, directory)


def _require_subtitles_within(directory: Path, record: ClipRecord) -> None:
    """Both documents parse, are ordered, and lie inside the clip.

    The bound is the interval that was asked for rather than the duration
    ffprobe measured. Both are known and they differ by the encoder's rounding;
    the requested one is the bound the builder actually enforced, so comparing
    against it makes this a check of the subtitle documents rather than a second
    check of the encoder's duration -- which ``_measure`` has already made
    against a stated tolerance.
    """
    limit = round(record.duration * 1000)
    for name, parse in (
        (record.srt_filename, read_srt_events),
        (record.ass_filename, read_ass_events),
    ):
        path = directory.joinpath(name)
        try:
            events = parse(path.read_text(encoding="utf-8"))
        # ValueError covers UnicodeDecodeError, which is a subclass of it, and
        # is also what both parsers raise on a document they cannot read. One
        # unreadable file, one refusal.
        except (OSError, ValueError) as error:
            raise RenderError(
                f"The subtitles for {record.candidate_id} in {name} cannot be read back: {error}"
            ) from error
        previous = 0
        for position, event in enumerate(events, start=1):
            if event.start_ms < previous:
                raise RenderError(
                    f"{name} for {record.candidate_id} has event {position} starting before "
                    "the one in front of it"
                )
            if event.end_ms > limit:
                raise RenderError(
                    f"{name} for {record.candidate_id} has event {position} ending at "
                    f"{event.end_ms}ms, past the {limit}ms clip"
                )
            previous = event.end_ms


# --- reading a published set back --------------------------------------------


def _load(path: Path, description: str) -> dict[str, object]:
    if not path.is_file():
        raise IncompatibleArtifactError(
            f"Clips exist but {path.name} is missing from {path.parent}, so there is no "
            f"record of {description}. Rerun with --force."
        )
    try:
        payload = read_json(path)
    except Exception as error:  # noqa: BLE001 - every read failure is one refusal
        raise IncompatibleArtifactError(
            f"{path} cannot be read as {description}: {error}. Rerun with --force."
        ) from error
    if not isinstance(payload, dict):
        raise IncompatibleArtifactError(
            f"{path} does not contain {description}. Rerun with --force."
        )
    return payload


def read_index(directory: Path) -> RenderIndex:
    """Load the index of what was produced, or refuse it."""
    path = directory.joinpath(RENDER_INDEX_FILENAME)
    payload = _load(path, "the clips that were produced")
    declared = payload.get("schema_version")
    if declared != RENDER_INDEX_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"{path} declares render index schema {declared!r}; this build understands "
            f"{RENDER_INDEX_SCHEMA_VERSION}. Rerun with --force."
        )
    try:
        return RenderIndex.model_validate(payload)
    except ValidationError as error:
        raise IncompatibleArtifactError(
            f"{path} is not a valid render index: {error}. Rerun with --force."
        ) from error


def read_stage_config(directory: Path) -> RenderStageConfig:
    """Load the configuration the render stage recorded, or refuse it."""
    path = directory.joinpath(RENDER_STAGE_CONFIG_FILENAME)
    payload = _load(path, "the configuration of the render stage")
    declared = payload.get("schema_version")
    if declared != RENDER_STAGE_CONFIG_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"{path} declares render stage configuration schema {declared!r}; this build "
            f"understands {RENDER_STAGE_CONFIG_SCHEMA_VERSION}. Rerun with --force."
        )
    try:
        return RenderStageConfig.model_validate(payload)
    except ValidationError as error:
        raise IncompatibleArtifactError(
            f"{path} is not a valid render stage configuration: {error}. Rerun with --force."
        ) from error


def _require_files(directory: Path, record: ClipRecord) -> None:
    """Every artifact of one clip exists, with the size and digest it claims."""
    clip_directory = directory.joinpath(record.directory)
    if not clip_directory.is_dir():
        raise IncompatibleArtifactError(
            f"The clip directory {record.directory} is missing from {directory}, so candidate "
            f"{record.candidate_id} has no clip. Rerun with --force."
        )
    for name, digest, size in (
        (record.clip_filename, record.sha256, record.size_bytes),
        (record.srt_filename, record.srt_sha256, record.srt_size_bytes),
        (record.ass_filename, record.ass_sha256, record.ass_size_bytes),
    ):
        path = clip_directory.joinpath(name)
        if not path.is_file():
            raise IncompatibleArtifactError(
                f"{name} is missing from {clip_directory}, so the clip for "
                f"{record.candidate_id} is incomplete. Rerun with --force."
            )
        if path.stat().st_size != size or sha256_file(path) != digest:
            raise IncompatibleArtifactError(
                f"{name} in {clip_directory} has changed since it was produced "
                f"({size} bytes recorded, {path.stat().st_size} on disk). Rerun with --force."
            )


def _require_metadata(directory: Path, record: ClipRecord) -> None:
    """The metadata beside a clip still says what the index says.

    Not covered by a digest, because it would then have to hold its own. Parsing
    it and comparing the fields is the stronger check anyway: a digest proves the
    bytes have not moved, this proves the two artifacts still agree.
    """
    path = directory.joinpath(record.directory, record.metadata_filename)
    payload = _load(path, f"the metadata for {record.candidate_id}")
    declared = payload.get("schema_version")
    if declared != CLIP_METADATA_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"{path} declares clip metadata schema {declared!r}; this build understands "
            f"{CLIP_METADATA_SCHEMA_VERSION}. Rerun with --force."
        )
    try:
        metadata = ClipMetadata.model_validate(payload)
    except ValidationError as error:
        raise IncompatibleArtifactError(
            f"{path} is not valid clip metadata: {error}. Rerun with --force."
        ) from error
    for field in (
        "candidate_id",
        "rank",
        "decision",
        "start",
        "end",
        "duration",
        "sha256",
        "size_bytes",
        "srt_sha256",
        "ass_sha256",
        "cue_count",
        "sample_aspect_ratio",
        "video_codec",
        "audio_codec",
    ):
        if getattr(metadata, field) != getattr(record, field):
            raise IncompatibleArtifactError(
                f"The metadata in {path} disagrees with the index about {field} "
                f"({getattr(metadata, field)!r} beside {getattr(record, field)!r}). "
                "Rerun with --force."
            )


def _require_no_strays(directory: Path, index: RenderIndex) -> None:
    """No clip directory the index does not name.

    This is what catches a shortlist that shrank: the clips of the candidates
    that are gone would otherwise sit beside the current set, verify
    individually, and be published as though they were part of it.
    """
    named = {record.directory for record in index.clips}
    strays = sorted(
        path.name
        for path in directory.iterdir()
        if path.is_dir() and path.name.startswith(CLIP_DIR_PREFIX) and path.name not in named
    )
    if strays:
        raise IncompatibleArtifactError(
            f"{directory} holds clip directories that are not in the index: "
            f"{', '.join(strays)}. They belong to an earlier shortlist. Rerun with --force."
        )


def require_clips(
    directory: Path,
    recorded_fingerprint: str,
    recorded_stage_config_sha256: str,
    target: RenderTarget,
) -> RenderIndex:
    """Prove the clips on disk are the ones this run recorded, and intact.

    Seven claims, in the order that gives the most specific message first:

    1. the index and the stage configuration are present, readable and valid;
    2. the configuration on disk is the one the manifest recorded;
    3. every artifact the index describes exists with the size and digest it
       claims -- which is what catches a deleted, truncated or replaced file;
    4. every metadata file still agrees with its record;
    5. both subtitle documents still parse, in order and inside the clip;
    6. no clip directory belongs to a shortlist this is not;
    7. the set describes this analysis, this review, this transcript, this
       source and exactly the decisions that were kept, and the fingerprint
       rebuilds from the index and the configuration.

    Nothing here writes. Every refusal leaves the directory exactly as it was.
    """
    index = read_index(directory)
    config = read_stage_config(directory)

    recomputed = render_stage_config_sha256(config)
    if recomputed != recorded_stage_config_sha256:
        raise IncompatibleArtifactError(
            f"{directory.joinpath(RENDER_STAGE_CONFIG_FILENAME)} does not match the manifest "
            f"(recorded {recorded_stage_config_sha256[:12]}, recomputed {recomputed[:12]}). "
            "The render configuration was changed after the clips were produced. Rerun with "
            "--force."
        )

    for record in index.clips:
        _require_files(directory, record)
        _require_metadata(directory, record)
        _require_subtitles_within(directory.joinpath(record.directory), record)
    _require_no_strays(directory, index)

    problem = render_coherence_problem(index, config, target)
    if problem is not None:
        raise IncompatibleArtifactError(
            f"The clips in {directory} disagree with the run: {problem}. Rerun with --force."
        )

    rebuilt = render_fingerprint(index, config)
    if rebuilt != recorded_fingerprint:
        raise IncompatibleArtifactError(
            f"The recorded render fingerprint cannot be rebuilt from the artifacts in "
            f"{directory} (recorded {recorded_fingerprint[:12]}, rebuilt {rebuilt[:12]}). One "
            "of them was edited after the run. Rerun with --force."
        )
    return index


def verify_clips(
    directory: Path,
    recorded_fingerprint: str,
    recorded_stage_config_sha256: str,
    plan: RenderPlan,
) -> RenderIndex:
    """Everything ``require_clips`` proves, plus that the settings still match.

    The extra check catches a profile that changed the preset, the encoder
    settings or the subtitle burn: the clips are intact and describe the right
    decisions, but they are not what this invocation would produce, so they are
    not reused.
    """
    index = require_clips(
        directory, recorded_fingerprint, recorded_stage_config_sha256, plan.target
    )
    wanted = render_stage_config_sha256(plan.config)
    if wanted != recorded_stage_config_sha256:
        raise IncompatibleArtifactError(
            f"The existing clips were produced under different settings (recorded "
            f"{recorded_stage_config_sha256[:12]}, current {wanted[:12]}). They will not be "
            "reused. Rerun with --force."
        )
    return index
