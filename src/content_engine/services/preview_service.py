"""Orchestration of the preview stage (CE-034).

Encodes one proxy per selected candidate, verifies each one with ffprobe, and
writes the index that lets a later invocation reuse them without re-encoding.

Three properties are enforced by the shape of this module rather than by care,
and each one is the answer to a way this could go wrong.

**Nothing reaches the previews directory until everything is verified.** Every
encode happens inside a staging directory, and every file is probed there. Only
once the whole set exists and has been checked is anything moved into place. A
failure therefore cannot leave a half-generated set that a later run would read
as complete.

**Publication is durable, not atomic.** Two outcomes are atomic: the new set is
published, or the previous one is restored byte for byte. There is a third,
because the restore is itself a sequence of renames and a rename can fail for
reasons outside this program. In that case every file of the previous set stays
in ``previews/`` or in ``previews/.rollback/``, the backup is never deleted
while the restore is unfinished, the error names the directory holding the data,
and the next invocation finishes the restore -- from any point, however many
times it was interrupted, because ``previews/.rollback/rollback.json`` records
which of the three phases the operation reached and each phase constrains what
the undo may touch. Nothing is lost; the directory is temporarily incomplete.
``_publish`` explains the mechanism and ADR-031 the reasoning, including why
claiming plain atomicity here would be a promise this code cannot keep.

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
    PreviewRollbackError,
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

#: The record inside a pending backup that says how far publication had got.
#: Without it a later invocation cannot know whether the previews directory
#: holds old files not yet moved aside or new ones already placed, and those
#: two states need opposite undo steps.
ROLLBACK_JOURNAL = "rollback.json"
#: Bumped whenever the journal changes shape. A journal this build cannot read
#: is refused rather than guessed at, because guessing decides which files get
#: deleted.
ROLLBACK_SCHEMA_VERSION = 1

#: How far the operation has got, and therefore what undoing it may touch. The
#: journal carries exactly this, because every phase forbids something the
#: previous one required.
#:
#: ``moving_aside``  Part of the previous set may still be in the previews
#:                   directory and nothing new has been placed. The undo moves
#:                   files back out of the backup and **deletes nothing**.
#:
#: ``placing``       Every file of the previous set is in the backup, so
#:                   anything publishable in the directory belongs to the
#:                   attempt that failed. The undo **deletes those first**, and
#:                   then advances to ``restoring``.
#:
#: ``restoring``     The deletion is over and files are being moved back, so the
#:                   directory now holds recovered files. The undo **must never
#:                   delete**: it only moves back whatever is still in the
#:                   backup.
#:
#: The third phase is not a refinement, it is the fix for a data loss.
#: ``placing`` and ``restoring`` are indistinguishable from the directory
#: contents alone -- both leave publishable files sitting in it -- so a restore
#: interrupted half-way through moving files back used to be resumed as though
#: the directory still held new files, and the resumed undo deleted the very
#: files it had just recovered. They were gone from the backup too, having
#: already left it. Recording the transition is what makes a resume safe.
PHASE_MOVING_ASIDE = "moving_aside"
PHASE_PLACING = "placing"
PHASE_RESTORING = "restoring"

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

        # A backup left pending by an earlier failure is finished, or refused,
        # before anything else happens. Encoding over it would be building a new
        # set on top of a directory that is still half of an old one.
        resolve_pending_rollback(directory)

        staging = directory.joinpath(STAGING_DIRNAME)
        try:
            records = self._render_all(plan, staging)
            index = self._build_index(plan, records, generated_at)
            self._publish(directory, staging, index, plan.config)
        finally:
            # The staging directory only. `.rollback` is never removed here:
            # deleting it unconditionally is exactly how a failed restore turned
            # a recoverable state into a lost one, because the `finally` ran
            # after the restore had already given up. It is removed in one place
            # only -- once publication has succeeded, or once a restore has put
            # every single file back.
            shutil.rmtree(staging, ignore_errors=True)

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
        """Replace the published set with the staged one.

        Publication touches several files: one MP4 per candidate, some of which
        may have to disappear because the shortlist changed, plus the stage
        configuration and the index. Doing that in place cannot be made atomic
        by ordering alone -- every ordering has a point at which a failure
        leaves half of one set and half of another, with the index describing
        neither. So the whole published set is moved into ``.rollback`` first,
        the new set is assembled, and the backup is deleted last.

        **What this guarantees, precisely.** Two outcomes are atomic: the new
        set is published, or the previous one is restored byte for byte. There
        is a third, and it is not atomic. A restore is a sequence of renames and
        a rename can fail for reasons outside this program -- a full disk, a
        revoked permission, a scanner holding a handle -- and no amount of
        ordering makes an operation that cannot complete complete. What is
        guaranteed in that case is **durability, not atomicity**: every file of
        the previous set remains in ``previews/`` or in ``previews/.rollback/``,
        the backup is never deleted while the restore is unfinished, the error
        names the directory holding the data, and the next invocation finishes
        the restore deterministically. Nothing is lost; the directory is
        temporarily incomplete.

        Saying "all or nothing" without that qualification would be a claim the
        design cannot keep, which is worse than a smaller promise kept.

        **Why files rather than directories.** Publishing by renaming whole
        directories -- build ``previews.new``, swap it in, drop ``previews.old``
        -- would reduce this to two renames, and was considered. It was rejected
        on three grounds. On Windows a directory rename fails while any handle
        is open to the directory or to a file inside it, and a reviewer watching
        a preview in a player is the normal state of this stage, so the swap
        would fail exactly when the feature is being used; a per-file rename
        fails only for the file actually held. The swap is not atomic either --
        between the two renames there is no ``previews`` directory at all, and a
        crash there leaves the run without a directory that ``RunWorkspace``
        created and that ``review`` and the manifest both reference. And keeping
        ``.staging`` and ``.rollback`` inside ``previews/`` is what guarantees
        every rename stays on one filesystem on both platforms, which is why the
        restore needs no space and cannot fail for want of any.
        """
        directory.mkdir(parents=True, exist_ok=True)
        rollback = directory.joinpath(ROLLBACK_DIRNAME)
        if rollback.exists():
            # Never overwritten. A pending backup is the only copy of something.
            # `generate` resolves one before calling this, so reaching here means
            # a caller skipped that step or a resolution has just failed.
            raise PreviewRollbackError(
                f"A previous publication left a backup in {rollback} that has not been "
                "restored, so a new one cannot start without discarding it. Resolve it "
                "first: the next `preview` run finishes the restore, or the files can be "
                "moved back by hand."
            )
        rollback.mkdir(parents=True)
        try:
            _write_journal(rollback, PHASE_MOVING_ASIDE)
        except OSError:
            # Provably empty: the journal is the first thing written and nothing
            # has been moved, so there is nothing here to lose.
            shutil.rmtree(rollback, ignore_errors=True)
            raise

        phase = PHASE_MOVING_ASIDE
        try:
            for path in sorted(directory.iterdir()):
                if _is_publishable(path):
                    path.replace(rollback.joinpath(path.name))
            _write_journal(rollback, PHASE_PLACING)
            phase = PHASE_PLACING
            for entry in index.previews:
                staging.joinpath(entry.filename).replace(directory.joinpath(entry.filename))
            for name, payload in (
                (PREVIEW_STAGE_CONFIG_FILENAME, config.model_dump(mode="json")),
                (PREVIEW_INDEX_FILENAME, index.model_dump(mode="json")),
            ):
                write_json(directory.joinpath(name), payload)
        except BaseException as failure:
            # The phase is taken from this frame rather than read back off disk.
            # It is the same information, and a read here could fail at the one
            # moment the undo must not be prevented from starting.
            try:
                _restore(directory, rollback, phase)
            except OSError as restore_failure:
                raise _stranded(directory, rollback, failure, restore_failure) from failure
            raise
        # Only now, with every file of the new set in place: the previous one is
        # no longer needed.
        shutil.rmtree(rollback)


def _is_publishable(path: Path) -> bool:
    """Whether a path is one of the files publication owns.

    The previews and the two artifacts, and nothing else -- not the staging or
    rollback directories, and not anything an operator happened to leave here.
    """
    return path.is_file() and (path.suffix == ".mp4" or path.name in ARTIFACT_FILENAMES)


def _write_journal(rollback: Path, phase: str) -> None:
    """Record how far publication has got, atomically."""
    write_json(
        rollback.joinpath(ROLLBACK_JOURNAL),
        {"schema_version": ROLLBACK_SCHEMA_VERSION, "phase": phase},
    )


def _read_journal(rollback: Path) -> str:
    """The phase a pending backup was left in, or a refusal.

    Every failure to read this is a refusal rather than a default. The phase
    decides whether the undo deletes files from the previews directory, so
    guessing it wrong deletes the wrong ones -- and a backup nobody can
    interpret is exactly the case where doing nothing is right.
    """
    path = rollback.joinpath(ROLLBACK_JOURNAL)
    if not path.is_file():
        raise PreviewRollbackError(
            f"{rollback} holds a backup of a previous preview set but no {ROLLBACK_JOURNAL}, "
            "so how far the publication got cannot be established and restoring it "
            "automatically could delete the wrong files. It is left untouched: the files "
            "in that directory are the previous previews and can be moved back by hand."
        )
    try:
        payload = read_json(path)
    except Exception as error:  # noqa: BLE001 - any unreadable journal is one refusal
        raise PreviewRollbackError(
            f"{path} cannot be read ({error}), so the pending backup in {rollback} is left "
            "untouched. The files in it are the previous previews."
        ) from error
    if not isinstance(payload, dict):
        raise PreviewRollbackError(f"{path} does not contain a rollback journal.")
    if payload.get("schema_version") != ROLLBACK_SCHEMA_VERSION:
        raise PreviewRollbackError(
            f"{path} declares rollback journal schema {payload.get('schema_version')!r}; this "
            f"build understands {ROLLBACK_SCHEMA_VERSION}. The backup in {rollback} is left "
            "untouched."
        )
    phase = payload.get("phase")
    if phase == PHASE_MOVING_ASIDE:
        return PHASE_MOVING_ASIDE
    if phase == PHASE_PLACING:
        return PHASE_PLACING
    if phase == PHASE_RESTORING:
        return PHASE_RESTORING
    raise PreviewRollbackError(
        f"{path} names publication phase {phase!r}, which this build does not know how "
        f"to undo. The backup in {rollback} is left untouched."
    )


def _restore(directory: Path, rollback: Path, phase: str) -> None:
    """Put a saved set back, and delete the backup only if all of it went back.

    Safe to call again on a restore that stopped part-way, which is the whole
    reason the phase is recorded. Two invariants do that work.

    **The deletion happens once, and the journal says when it is over.** In
    ``placing`` the publishable files in the directory belong to the attempt
    that failed, so they are removed; the moment that finishes, ``restoring``
    is written, *before* the first file is moved back. Every later resume reads
    ``restoring`` and deletes nothing, so a file already recovered cannot be
    mistaken for one the failed publication left behind -- which is precisely
    how an earlier version of this function lost the files it had just
    restored.

    **Moving back is idempotent.** Each move takes one file out of the backup,
    so a repeated call simply continues with whatever is left. Nothing is
    copied and nothing is compared: a file is in the backup or it is in the
    directory, never neither.

    If writing ``restoring`` fails, the phase on disk is still ``placing`` and
    nothing has moved: the previous set is complete in the backup, and a later
    resume re-runs the deletion -- which now finds nothing to delete -- and
    tries the transition again.

    The ``rmtree`` is the only place a backup is discarded here, and it is
    reached only after every move has succeeded.
    """
    if phase == PHASE_PLACING:
        for path in sorted(directory.iterdir()):
            if _is_publishable(path):
                path.unlink()
        # The order of these two statements is the fix. Recording the
        # transition before the first move is what makes the next resume able
        # to tell a recovered file from a leftover one.
        _write_journal(rollback, PHASE_RESTORING)
    for saved in sorted(rollback.iterdir()):
        if saved.is_file() and saved.name != ROLLBACK_JOURNAL:
            saved.replace(directory.joinpath(saved.name))
    shutil.rmtree(rollback)


def _stranded(
    directory: Path,
    rollback: Path,
    failure: BaseException,
    restore_failure: OSError,
) -> PreviewRollbackError:
    """The error for a publication that failed and could not be undone.

    It has one job beyond reporting: to say where the data is. The operator is
    being told that the previews directory is incomplete *and* that nothing has
    been lost, and neither half of that is useful without the path.
    """
    saved = sorted(
        path.name for path in rollback.iterdir() if path.is_file() and path.name != ROLLBACK_JOURNAL
    )
    return PreviewRollbackError(
        f"The preview publication in {directory} failed ({failure}), and undoing it failed "
        f"too ({restore_failure}). Nothing has been lost: {len(saved)} file(s) of the "
        f"previous set are held in {ROLLBACK_DIRNAME} inside that directory "
        f"({', '.join(saved) or 'none'}), and that backup is not deleted. The previews "
        "directory is incomplete until the restore finishes; the next `preview` run "
        "completes it, or the files can be moved back by hand."
    )


def resolve_pending_rollback(directory: Path) -> str | None:
    """Finish a restore an earlier failure could not, or refuse to touch it.

    Returns a description when something was restored, and None when there was
    nothing pending. Raises when the backup exists but cannot be resolved
    deterministically, in which case it is left exactly as it was.

    This is what makes the durability guarantee more than a promise: a stranded
    backup is not something an operator has to unpick by hand, it is something
    the next invocation of the same command finishes.
    """
    rollback = directory.joinpath(ROLLBACK_DIRNAME)
    if not rollback.is_dir():
        return None

    held = [path for path in rollback.iterdir() if path.is_file() and path.name != ROLLBACK_JOURNAL]
    if not held and not rollback.joinpath(ROLLBACK_JOURNAL).is_file():
        # An empty directory with no journal holds nothing recoverable, so
        # removing it is not discarding anything. This is the one case where a
        # pre-existing backup directory may be deleted without being read.
        shutil.rmtree(rollback, ignore_errors=True)
        return None

    phase = _read_journal(rollback)
    try:
        _restore(directory, rollback, phase)
    except OSError as restore_failure:
        raise _stranded(
            directory,
            rollback,
            RuntimeError("an earlier publication left this backup"),
            restore_failure,
        ) from restore_failure
    return (
        f"restored {len(held)} file(s) of the previous preview set from a backup an earlier "
        f"run could not put back"
    )


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
