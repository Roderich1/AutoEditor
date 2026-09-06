"""Orchestration of the preview stage (CE-034).

Encodes one proxy per selected candidate, verifies each one with ffprobe, and
writes the index that lets a later invocation reuse them without re-encoding.

Three properties are enforced by the shape of this module rather than by care,
and each one is the answer to a way this could go wrong.

**Nothing reaches the previews directory until everything is verified.** Every
encode happens inside a staging directory, and every file is probed there. Only
once the whole set exists and has been checked is anything moved into place.
A failure therefore cannot leave a half-generated set that a later run would
read as complete, and a failed ``--force`` cannot destroy the previews that were
already there -- the old files are still the only files in the directory.

**A record is a measurement, not a request.** The dimensions, duration and
codecs in the index come from ffprobe reading the finished file; the digest and
the size come from its bytes. Recording what FFmpeg was asked for would produce
an index that stays correct when the encode silently does something else.

**Reuse is proved, never assumed.** The verification path reads the index and
the stage configuration back, revalidates both under their schemas, re-hashes
every preview on disk, checks the set against the current shortlist and
rebuilds the fingerprint. A digest that still looks right proves nothing if the
file it addresses was replaced, and a preview nobody checked is a reviewer
watching the wrong video.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from content_engine.domain.candidates import ValidatedCandidate
from content_engine.domain.exceptions import (
    IncompatibleArtifactError,
    RenderError,
)
from content_engine.domain.models import MediaInfo
from content_engine.domain.preview_rules import (
    PREVIEW_INDEX_FILENAME,
    PREVIEW_STAGE_CONFIG_FILENAME,
    preview_coherence_problem,
    preview_filename,
    preview_fingerprint,
    preview_stage_config_sha256,
)
from content_engine.domain.previews import (
    PREVIEW_INDEX_SCHEMA_VERSION,
    PREVIEW_STAGE_CONFIG_SCHEMA_VERSION,
    PreviewIndex,
    PreviewRecord,
    PreviewStageConfig,
)
from content_engine.ports.preview import MediaProbePort, PreviewRendererPort
from content_engine.utils.hashing import sha256_file
from content_engine.utils.json import read_json, write_json

#: Where previews are encoded before they are accepted. Inside the previews
#: directory so the move onto the final name is a rename on one filesystem, and
#: prefixed with a dot so a directory listing does not present it as content.
STAGING_DIRNAME = ".staging"

#: Where the previously published set is held while the new one is assembled,
#: so a failure part-way through can put it back. Beside the staging directory
#: and for the same reason: restoring must be a rename, never a copy, so it
#: cannot fail for want of space at the moment things have already gone wrong.
ROLLBACK_DIRNAME = ".rollback"

#: Written in this order, and the order matters: the file the reuse check looks
#: for first is written last, so an interrupted run cannot leave a directory
#: that looks complete.
ARTIFACT_FILENAMES = (PREVIEW_STAGE_CONFIG_FILENAME, PREVIEW_INDEX_FILENAME)


@dataclass(frozen=True)
class PreviewPlan:
    """Everything decided before an encoder is started.

    Built by the caller so the reuse path can rebuild exactly what it expects
    without running anything, and so nothing here depends on reading the
    source: ``source_sha256`` is passed in rather than computed, because
    ``preview`` hashes the file it is about to read while ``review`` only needs
    the identity the manifest already recorded.
    """

    candidates: tuple[ValidatedCandidate, ...]
    config: PreviewStageConfig
    analysis_fingerprint: str
    source_path: Path
    source_sha256: str
    source_duration_seconds: float


@dataclass(frozen=True)
class PreviewOutcome:
    index: PreviewIndex
    config: PreviewStageConfig
    stage_config_sha256: str
    fingerprint: str


class PreviewService:
    def __init__(self, renderer: PreviewRendererPort, probe: MediaProbePort) -> None:
        self.renderer = renderer
        self.probe = probe

    def generate(
        self,
        plan: PreviewPlan,
        directory: Path,
        generated_at: datetime,
    ) -> PreviewOutcome:
        """Produce, verify and commit the whole set, or leave the directory alone."""
        if plan.candidates and not plan.source_path.is_file():
            raise RenderError(
                f"The run source is missing, so no preview can be cut from it: {plan.source_path}"
            )

        staging = directory.joinpath(STAGING_DIRNAME)
        try:
            records = self._render_all(plan, staging)
            index = self._build_index(plan, records, generated_at)
            self._publish(directory, staging, index, plan.config)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(directory.joinpath(ROLLBACK_DIRNAME), ignore_errors=True)

        return PreviewOutcome(
            index=index,
            config=plan.config,
            stage_config_sha256=preview_stage_config_sha256(plan.config),
            fingerprint=preview_fingerprint(plan.analysis_fingerprint, index, plan.config),
        )

    def _render_all(self, plan: PreviewPlan, staging: Path) -> list[PreviewRecord]:
        """Encode and measure every candidate inside the staging directory."""
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        records: list[PreviewRecord] = []
        for candidate in plan.candidates:
            # `rank` is not None for a selected candidate -- ValidatedCandidate
            # refuses a rank on anything else -- but the type is optional, so
            # the impossible case is refused rather than asserted away.
            if candidate.rank is None:
                raise RenderError(
                    f"candidate {candidate.id} has no rank, so it was never selected and "
                    "must not be previewed"
                )
            name = preview_filename(candidate.id)
            path = staging.joinpath(name)
            self.renderer.render(
                plan.source_path, candidate.start, candidate.duration, path, plan.config
            )
            records.append(self._measure(candidate, candidate.rank, path, plan.config))
        return records

    def _measure(
        self,
        candidate: ValidatedCandidate,
        rank: int,
        path: Path,
        config: PreviewStageConfig,
    ) -> PreviewRecord:
        """Read back what was produced and refuse it if it is not what was asked for."""
        media = self._probe_preview(path)
        if (media.width, media.height) != (config.width, config.height):
            raise RenderError(
                f"The preview for {candidate.id} is {media.width}x{media.height}; "
                f"{config.width}x{config.height} was requested"
            )
        if media.video_codec != config.expected_video_codec:
            raise RenderError(
                f"The preview for {candidate.id} holds {media.video_codec} video; "
                f"{config.expected_video_codec} was requested"
            )
        if media.audio_codec is None:
            raise RenderError(
                f"The preview for {candidate.id} has no audio stream. A silent proxy cannot "
                "be reviewed."
            )
        # A track that exists is not the track that was asked for. Checking the
        # video codec and not this one let an encode that stream-copied the
        # source audio, or transcoded it to something else, pass verification
        # and be recorded in the index as though it were AAC.
        if media.audio_codec != config.expected_audio_codec:
            raise RenderError(
                f"The preview for {candidate.id} holds {media.audio_codec} audio; "
                f"{config.expected_audio_codec} was requested"
            )
        drift = abs(media.duration_seconds - candidate.duration)
        if drift > config.duration_tolerance_seconds:
            raise RenderError(
                f"The preview for {candidate.id} is {media.duration_seconds:.3f}s long for a "
                f"{candidate.duration:.3f}s interval, {drift:.3f}s beyond the "
                f"{config.duration_tolerance_seconds}s duration tolerance"
            )
        return PreviewRecord(
            candidate_id=candidate.id,
            rank=rank,
            start=candidate.start,
            end=candidate.end,
            duration=candidate.duration,
            filename=path.name,
            width=media.width,
            height=media.height,
            measured_duration_seconds=media.duration_seconds,
            video_codec=media.video_codec,
            audio_codec=media.audio_codec,
            sha256=sha256_file(path),
            size_bytes=path.stat().st_size,
        )

    def _probe_preview(self, path: Path) -> MediaInfo:
        try:
            media, _ = self.probe.probe(path)
        except Exception as error:  # noqa: BLE001 - any unreadable result is one refusal
            raise RenderError(f"The preview {path.name} cannot be read back: {error}") from error
        return media

    def _build_index(
        self,
        plan: PreviewPlan,
        records: list[PreviewRecord],
        generated_at: datetime,
    ) -> PreviewIndex:
        try:
            index = PreviewIndex(
                generated_at=generated_at,
                analysis_fingerprint=plan.analysis_fingerprint,
                source_sha256=plan.source_sha256,
                source_duration_seconds=plan.source_duration_seconds,
                width=plan.config.width,
                height=plan.config.height,
                previews=records,
            )
        except ValidationError as error:
            # Translated rather than allowed to escape: the caller decides what
            # a failure means for the run, and it can only do that for a
            # RenderError. A pydantic error reaching the CLI would be reported
            # as an unexpected internal fault instead of a preview failure.
            raise RenderError(
                f"The preview stage produced an index it cannot describe: {error}"
            ) from error
        # Built together, so this should be impossible; checked anyway, because
        # the alternative is writing an index a later run will read and believe.
        problem = preview_coherence_problem(
            index,
            plan.config,
            plan.analysis_fingerprint,
            plan.source_sha256,
            plan.candidates,
        )
        if problem is not None:
            raise RenderError(f"The preview stage produced records that disagree: {problem}.")
        return index

    @staticmethod
    def _publish(
        directory: Path,
        staging: Path,
        index: PreviewIndex,
        config: PreviewStageConfig,
    ) -> None:
        """Replace the published set with the staged one, all of it or none of it.

        Publication touches several files: one MP4 per candidate, some of which
        may have to disappear because the shortlist changed, plus the stage
        configuration and the index. Doing that in place cannot be made atomic
        by ordering alone -- every ordering has a point at which a failure
        leaves half of one set and half of another, with the index describing
        neither. And an unverifiable mixture is strictly worse than either set:
        the previous previews were reusable, and a `--force` that fails is
        supposed to cost nothing.

        So the whole published set is moved aside first, into a directory beside
        the staging one, and only then is the new set assembled. A failure at
        any point puts the old set back exactly as it was, byte for byte, and
        re-raises. Success deletes the copy.

        The moves are renames within one directory, so they are cheap however
        large the previews are, and the restore path is renames too -- it
        performs no encode, allocates no space and cannot fail for want of any.
        """
        directory.mkdir(parents=True, exist_ok=True)
        rollback = directory.joinpath(ROLLBACK_DIRNAME)
        shutil.rmtree(rollback, ignore_errors=True)
        rollback.mkdir(parents=True, exist_ok=True)

        # What actually happened, rather than what was meant to. The undo below
        # needs to distinguish an old file that has been moved aside from a new
        # one that has been placed, and it cannot tell them apart by name --
        # regenerating an unchanged shortlist reuses every name. Inferring it
        # from the directory contents is what made the first version of this
        # delete previous previews that had not been moved aside yet.
        moved_aside: list[str] = []
        placed: list[str] = []

        try:
            # Moving the old set aside is itself part of the transaction. Doing
            # it before the guard would leave a failure half-way through the
            # move with a partly emptied directory and nothing to put back --
            # the same defect this method exists to remove, one step earlier.
            for path in sorted(directory.iterdir()):
                if path.is_file() and (path.suffix == ".mp4" or path.name in ARTIFACT_FILENAMES):
                    path.replace(rollback.joinpath(path.name))
                    moved_aside.append(path.name)
            for entry in index.previews:
                staging.joinpath(entry.filename).replace(directory.joinpath(entry.filename))
                placed.append(entry.filename)
            for name, payload in (
                (PREVIEW_STAGE_CONFIG_FILENAME, config.model_dump(mode="json")),
                (PREVIEW_INDEX_FILENAME, index.model_dump(mode="json")),
            ):
                write_json(directory.joinpath(name), payload)
                placed.append(name)
        except BaseException:
            PreviewService._roll_back(directory, rollback, moved_aside, placed)
            raise
        shutil.rmtree(rollback, ignore_errors=True)

    @staticmethod
    def _roll_back(
        directory: Path,
        rollback: Path,
        moved_aside: list[str],
        placed: list[str],
    ) -> None:
        """Undo a partial publication, leaving the previous set byte for byte.

        Two steps, in this order and driven by what was recorded rather than by
        what is on disk. Everything the failed attempt managed to place is
        removed, so a new preview whose name is not in the old set cannot
        survive as a stray. Then everything that was moved aside goes back.

        ``missing_ok`` is deliberate: ``write_json`` is atomic, so a name can be
        in ``placed`` and yet absent if the failure happened inside the write,
        and refusing to continue over one absent file would abandon the restore
        half-done -- which is the state this whole method exists to prevent.
        """
        for name in placed:
            directory.joinpath(name).unlink(missing_ok=True)
        for name in moved_aside:
            rollback.joinpath(name).replace(directory.joinpath(name))
        shutil.rmtree(rollback, ignore_errors=True)


def _load(path: Path, description: str) -> dict[str, object]:
    if not path.is_file():
        raise IncompatibleArtifactError(
            f"Previews exist but {path.name} is missing from {path.parent}, so there is no "
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


def read_index(directory: Path) -> PreviewIndex:
    """Load the index of what was produced, or refuse it."""
    path = directory.joinpath(PREVIEW_INDEX_FILENAME)
    payload = _load(path, "the previews that were produced")
    declared = payload.get("schema_version")
    if declared != PREVIEW_INDEX_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"{path} declares preview index schema {declared!r}; this build understands "
            f"{PREVIEW_INDEX_SCHEMA_VERSION}. Rerun with --force."
        )
    try:
        return PreviewIndex.model_validate(payload)
    except ValidationError as error:
        raise IncompatibleArtifactError(
            f"{path} is not a valid preview index: {error}. Rerun with --force."
        ) from error


def read_stage_config(directory: Path) -> PreviewStageConfig:
    """Load the configuration the preview stage recorded, or refuse it."""
    path = directory.joinpath(PREVIEW_STAGE_CONFIG_FILENAME)
    payload = _load(path, "the configuration of the preview stage")
    declared = payload.get("schema_version")
    if declared != PREVIEW_STAGE_CONFIG_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"{path} declares preview stage configuration schema {declared!r}; this build "
            f"understands {PREVIEW_STAGE_CONFIG_SCHEMA_VERSION}. Rerun with --force."
        )
    try:
        return PreviewStageConfig.model_validate(payload)
    except ValidationError as error:
        raise IncompatibleArtifactError(
            f"{path} is not a valid preview stage configuration: {error}. Rerun with --force."
        ) from error


def require_previews(
    directory: Path,
    recorded_fingerprint: str,
    recorded_stage_config_sha256: str,
    candidates: tuple[ValidatedCandidate, ...],
    analysis_fingerprint: str,
    source_sha256: str,
) -> PreviewIndex:
    """Prove the previews on disk are the ones this run recorded, and intact.

    Five claims, in the order that gives the most specific message first:

    1. the index and the stage configuration are present, readable and valid;
    2. the configuration on disk is the one the manifest recorded;
    3. every file the index describes exists, with the size and digest it
       claims -- which is what catches a deleted, truncated or replaced preview;
    4. the set describes this analysis, this source and this shortlist;
    5. the fingerprint rebuilds from the index and the configuration.

    Used by both ``preview`` and ``review``. It deliberately does *not* compare
    the recorded settings against the settings being asked for now: that is a
    question about whether to re-encode, which only ``preview`` has, and
    ``review`` must not refuse to show a reviewer intact previews because a
    profile changed the preview size.

    Nothing here writes. Every refusal leaves the directory exactly as it was.
    """
    index = read_index(directory)
    config = read_stage_config(directory)

    recomputed = preview_stage_config_sha256(config)
    if recomputed != recorded_stage_config_sha256:
        raise IncompatibleArtifactError(
            f"{directory.joinpath(PREVIEW_STAGE_CONFIG_FILENAME)} does not match the manifest "
            f"(recorded {recorded_stage_config_sha256[:12]}, recomputed {recomputed[:12]}). The "
            "preview configuration was changed after the previews were produced. Rerun with "
            "--force."
        )

    for entry in index.previews:
        path = directory.joinpath(entry.filename)
        if not path.is_file():
            raise IncompatibleArtifactError(
                f"The preview {entry.filename} is missing from {directory}, so candidate "
                f"{entry.candidate_id} cannot be reviewed. Rerun with --force."
            )
        if path.stat().st_size != entry.size_bytes or sha256_file(path) != entry.sha256:
            raise IncompatibleArtifactError(
                f"The preview {entry.filename} has changed since it was produced "
                f"({entry.size_bytes} bytes recorded, {path.stat().st_size} on disk). "
                "Rerun with --force."
            )

    problem = preview_coherence_problem(
        index, config, analysis_fingerprint, source_sha256, candidates
    )
    if problem is not None:
        raise IncompatibleArtifactError(
            f"The previews in {directory} disagree with the run: {problem}. Rerun with --force."
        )

    rebuilt = preview_fingerprint(analysis_fingerprint, index, config)
    if rebuilt != recorded_fingerprint:
        raise IncompatibleArtifactError(
            f"The recorded preview fingerprint cannot be rebuilt from the artifacts in "
            f"{directory} (recorded {recorded_fingerprint[:12]}, rebuilt {rebuilt[:12]}). One "
            "of them was edited after the run. Rerun with --force."
        )
    return index


def verify_previews(
    directory: Path,
    recorded_fingerprint: str,
    recorded_stage_config_sha256: str,
    plan: PreviewPlan,
) -> PreviewIndex:
    """Everything ``require_previews`` proves, plus that the settings still match.

    The extra check is what catches a profile that changed the preview
    dimensions: the previews are intact and describe the right candidates, but
    they are not what this invocation would produce, so they are not reused.
    """
    index = require_previews(
        directory,
        recorded_fingerprint,
        recorded_stage_config_sha256,
        plan.candidates,
        plan.analysis_fingerprint,
        plan.source_sha256,
    )
    wanted = preview_stage_config_sha256(plan.config)
    if wanted != recorded_stage_config_sha256:
        raise IncompatibleArtifactError(
            f"The existing previews were produced under different settings (recorded "
            f"{recorded_stage_config_sha256[:12]}, current {wanted[:12]}). They will not be "
            "reused. Rerun with --force."
        )
    return index
