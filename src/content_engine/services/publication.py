"""Durable replacement of a published set of artifacts.

Extracted from the preview stage, which paid for every line of it. Three
defects were found there in review, and the third was the expensive one: a
restore that failed part-way could be resumed in a way that deleted the files it
had just recovered. ADR-031 records the reasoning and ADR-033 records why this
is now one module rather than two copies.

**What is guaranteed.** Two outcomes are atomic: the new set is published, or
the previous one is restored byte for byte. There is a third, and it is not
atomic. A restore is a sequence of renames and a rename can fail for reasons
outside this program -- a full disk, a revoked permission, a scanner holding a
handle -- and no amount of ordering makes an operation that cannot complete
complete. What is guaranteed in that case is **durability, not atomicity**:
every item of the previous set remains in the published directory or in the
backup beside it, the backup is never deleted while the restore is unfinished,
the error names the directory holding the data, and the next invocation finishes
the restore. Nothing is lost; the directory is temporarily incomplete.

Saying "all or nothing" without that qualification would be a claim this design
cannot keep, which is worse than a smaller promise kept.

**Why items rather than directories.** Publishing by renaming a whole directory
-- build ``clips.new``, swap it in, drop ``clips.old`` -- would reduce this to
two renames, and was rejected on three grounds. On Windows a directory rename
fails while any handle is open inside it, and somebody watching a clip in a
player is the normal state of this stage. The swap is not atomic either: between
the two renames there is no published directory at all, and a crash there leaves
the run without a directory ``RunWorkspace`` created and the manifest
references. And keeping the working directories *inside* the published one is
what guarantees every rename stays on one filesystem, which is why the restore
needs no space and cannot fail for want of any.

**What generalising cost.** One thing only: an item is now a file *or* a
directory, because a clip is a directory holding four artifacts while a preview
is a single MP4. Every rename works on both; the deletion in the ``placing``
phase is the one place that has to tell them apart.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from content_engine.domain.exceptions import ContentEngineError
from content_engine.utils.json import read_json, write_json

__all__ = [
    "PHASE_MOVING_ASIDE",
    "PHASE_PLACING",
    "PHASE_RESTORING",
    "ROLLBACK_SCHEMA_VERSION",
    "PublicationLayout",
    "publish",
    "resolve_pending",
]

#: Bumped whenever the journal changes shape. A journal this build cannot read
#: is refused rather than guessed at, because guessing decides which files get
#: deleted.
ROLLBACK_SCHEMA_VERSION = 1

#: How far the operation has got, and therefore what undoing it may touch. The
#: journal carries exactly this, because every phase forbids something the
#: previous one required.
#:
#: ``moving_aside``  Part of the previous set may still be in the published
#:                   directory and nothing new has been placed. The undo moves
#:                   items back out of the backup and **deletes nothing**.
#:
#: ``placing``       Every item of the previous set is in the backup, so
#:                   anything publishable in the directory belongs to the
#:                   attempt that failed. The undo **deletes those first**, and
#:                   then advances to ``restoring``.
#:
#: ``restoring``     The deletion is over and items are being moved back, so the
#:                   directory now holds recovered ones. The undo **must never
#:                   delete**: it only moves back whatever is still in the
#:                   backup.
#:
#: The third phase is not a refinement, it is the fix for a data loss.
#: ``placing`` and ``restoring`` are indistinguishable from the directory
#: contents alone -- both leave publishable items sitting in it -- so a restore
#: interrupted half-way through moving items back used to be resumed as though
#: the directory still held new ones, and the resumed undo deleted the very
#: items it had just recovered. They were gone from the backup too, having
#: already left it. Recording the transition is what makes a resume safe.
PHASE_MOVING_ASIDE = "moving_aside"
PHASE_PLACING = "placing"
PHASE_RESTORING = "restoring"

_PHASES = frozenset({PHASE_MOVING_ASIDE, PHASE_PLACING, PHASE_RESTORING})


@dataclass(frozen=True)
class PublicationLayout:
    """Everything one stage's publication does differently from another's.

    The protocol above is identical for previews and clips. What is not
    identical is what the directories are called, which entries publication owns
    -- an operator's stray notes must survive a republication -- what a stage
    calls its output when it has to explain a failure, and which exception type
    the caller has to catch.
    """

    #: Recorded rather than used here: the protocol never touches the staging
    #: directory, because what gets staged and how is the stage's own business.
    #: It is on the layout so one object describes the whole of a stage's
    #: publication, which is what a reader comparing two stages wants.
    staging_dirname: str
    rollback_dirname: str
    journal_filename: str
    #: Which entries in the published directory belong to this stage. Anything
    #: else is left exactly where it is, moved aside neither on the way in nor
    #: on the way out.
    owns: Callable[[Path], bool]
    #: Builds the stage's own rollback exception from a finished message.
    error: Callable[[str], ContentEngineError]
    #: The command that finishes a pending restore, named in the error so the
    #: operator is told what to do rather than only what happened.
    command: str
    #: How the stage refers to what it publishes: ("preview set", "previews").
    set_noun: str
    plural_noun: str

    def rollback(self, directory: Path) -> Path:
        return directory.joinpath(self.rollback_dirname)


def _write_journal(layout: PublicationLayout, rollback: Path, phase: str) -> None:
    """Record how far publication has got, atomically."""
    write_json(
        rollback.joinpath(layout.journal_filename),
        {"schema_version": ROLLBACK_SCHEMA_VERSION, "phase": phase},
    )


def _read_journal(layout: PublicationLayout, rollback: Path) -> str:
    """The phase a pending backup was left in, or a refusal.

    Every failure to read this is a refusal rather than a default. The phase
    decides whether the undo deletes items from the published directory, so
    guessing it wrong deletes the wrong ones -- and a backup nobody can
    interpret is exactly the case where doing nothing is right.
    """
    path = rollback.joinpath(layout.journal_filename)
    if not path.is_file():
        raise layout.error(
            f"{rollback} holds a backup of a previous {layout.set_noun} but no "
            f"{layout.journal_filename}, so how far the publication got cannot be "
            "established and restoring it automatically could delete the wrong files. It is "
            f"left untouched: the files in that directory are the previous {layout.plural_noun} "
            "and can be moved back by hand."
        )
    try:
        payload = read_json(path)
    except Exception as error:  # noqa: BLE001 - any unreadable journal is one refusal
        raise layout.error(
            f"{path} cannot be read ({error}), so the pending backup in {rollback} is left "
            f"untouched. The files in it are the previous {layout.plural_noun}."
        ) from error
    if not isinstance(payload, dict):
        raise layout.error(f"{path} does not contain a rollback journal.")
    if payload.get("schema_version") != ROLLBACK_SCHEMA_VERSION:
        raise layout.error(
            f"{path} declares rollback journal schema {payload.get('schema_version')!r}; this "
            f"build understands {ROLLBACK_SCHEMA_VERSION}. The backup in {rollback} is left "
            "untouched."
        )
    phase = payload.get("phase")
    if phase not in _PHASES:
        raise layout.error(
            f"{path} names publication phase {phase!r}, which this build does not know how "
            f"to undo. The backup in {rollback} is left untouched."
        )
    # Narrowed by the membership test; restated for the type checker rather than
    # cast, because a cast would also silence a real change to the phase set.
    return str(phase)


def _held(layout: PublicationLayout, rollback: Path) -> list[Path]:
    """What the backup is holding, the journal excepted."""
    return [entry for entry in sorted(rollback.iterdir()) if entry.name != layout.journal_filename]


def _remove(path: Path) -> None:
    """Delete one published item, whether it is a file or a directory."""
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _restore(layout: PublicationLayout, directory: Path, rollback: Path, phase: str) -> None:
    """Put a saved set back, and delete the backup only if all of it went back.

    Safe to call again on a restore that stopped part-way, which is the whole
    reason the phase is recorded. Two invariants do that work.

    **The deletion happens once, and the journal says when it is over.** In
    ``placing`` the publishable items in the directory belong to the attempt
    that failed, so they are removed; the moment that finishes, ``restoring`` is
    written, *before* the first item is moved back. Every later resume reads
    ``restoring`` and deletes nothing, so an item already recovered cannot be
    mistaken for one the failed publication left behind -- which is precisely
    how an earlier version of this function lost the files it had just restored.

    **Moving back is idempotent.** Each move takes one item out of the backup,
    so a repeated call simply continues with whatever is left. Nothing is copied
    and nothing is compared: an item is in the backup or it is in the directory,
    never neither.

    If writing ``restoring`` fails, the phase on disk is still ``placing`` and
    nothing has moved: the previous set is complete in the backup, and a later
    resume re-runs the deletion -- which now finds nothing to delete -- and
    tries the transition again.

    The ``rmtree`` is the only place a backup is discarded here, and it is
    reached only after every move has succeeded.
    """
    if phase == PHASE_PLACING:
        for path in sorted(directory.iterdir()):
            if layout.owns(path):
                _remove(path)
        # The order of these two statements is the fix. Recording the transition
        # before the first move is what makes the next resume able to tell a
        # recovered item from a leftover one.
        _write_journal(layout, rollback, PHASE_RESTORING)
    for saved in _held(layout, rollback):
        saved.replace(directory.joinpath(saved.name))
    shutil.rmtree(rollback)


def _stranded(
    layout: PublicationLayout,
    directory: Path,
    rollback: Path,
    failure: BaseException,
    restore_failure: OSError,
) -> ContentEngineError:
    """The error for a publication that failed and could not be undone.

    It has one job beyond reporting: to say where the data is. The operator is
    being told that the published directory is incomplete *and* that nothing has
    been lost, and neither half of that is useful without the path.
    """
    saved = sorted(entry.name for entry in _held(layout, rollback))
    return layout.error(
        f"The {layout.command} publication in {directory} failed ({failure}), and undoing it "
        f"failed too ({restore_failure}). Nothing has been lost: {len(saved)} file(s) of the "
        f"previous set are held in {layout.rollback_dirname} inside that directory "
        f"({', '.join(saved) or 'none'}), and that backup is not deleted. The directory is "
        f"incomplete until the restore finishes; the next `{layout.command}` run completes it, "
        "or the files can be moved back by hand."
    )


@contextmanager
def publish(layout: PublicationLayout, directory: Path) -> Iterator[None]:
    """Replace the published set with a new one, durably.

    The caller places the new set inside the ``with`` body: the whole previous
    set has been moved aside by the time the body runs, and any exception out of
    it triggers the restore. Nothing about *what* is published is decided here,
    which is what lets one implementation serve a stage that publishes files and
    a stage that publishes directories.

    A pre-existing backup is refused rather than overwritten. It is the only
    copy of something, and ``resolve_pending`` is what deals with it -- callers
    run that first, so reaching this means a caller skipped the step or a
    resolution has just failed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    rollback = layout.rollback(directory)
    if rollback.exists():
        raise layout.error(
            f"A previous publication left a backup in {rollback} that has not been restored, "
            "so a new one cannot start without discarding it. Resolve it first: the next "
            f"`{layout.command}` run finishes the restore, or the files can be moved back by "
            "hand."
        )
    rollback.mkdir(parents=True)
    try:
        _write_journal(layout, rollback, PHASE_MOVING_ASIDE)
    except OSError:
        # Provably empty: the journal is the first thing written and nothing has
        # been moved, so there is nothing here to lose.
        shutil.rmtree(rollback, ignore_errors=True)
        raise

    phase = PHASE_MOVING_ASIDE
    try:
        for path in sorted(directory.iterdir()):
            if layout.owns(path):
                path.replace(rollback.joinpath(path.name))
        _write_journal(layout, rollback, PHASE_PLACING)
        phase = PHASE_PLACING
        yield
    except BaseException as failure:
        # The phase is taken from this frame rather than read back off disk. It
        # is the same information, and a read here could fail at the one moment
        # the undo must not be prevented from starting.
        try:
            _restore(layout, directory, rollback, phase)
        except OSError as restore_failure:
            raise _stranded(layout, directory, rollback, failure, restore_failure) from failure
        raise
    # Only now, with every item of the new set in place: the previous one is no
    # longer needed.
    shutil.rmtree(rollback)


def resolve_pending(layout: PublicationLayout, directory: Path) -> str | None:
    """Finish a restore an earlier failure could not, or refuse to touch it.

    Returns a description when something was restored, and None when there was
    nothing pending. Raises when the backup exists but cannot be resolved
    deterministically, in which case it is left exactly as it was.

    This is what makes the durability guarantee more than a promise: a stranded
    backup is not something an operator has to unpick by hand, it is something
    the next invocation of the same command finishes.
    """
    rollback = layout.rollback(directory)
    if not rollback.is_dir():
        return None

    held = _held(layout, rollback)
    if not held and not rollback.joinpath(layout.journal_filename).is_file():
        # An empty directory with no journal holds nothing recoverable, so
        # removing it is not discarding anything. This is the one case where a
        # pre-existing backup directory may be deleted without being read.
        shutil.rmtree(rollback, ignore_errors=True)
        return None

    phase = _read_journal(layout, rollback)
    try:
        _restore(layout, directory, rollback, phase)
    except OSError as restore_failure:
        raise _stranded(
            layout,
            directory,
            rollback,
            RuntimeError("an earlier publication left this backup"),
            restore_failure,
        ) from restore_failure
    return (
        f"restored {len(held)} file(s) of the previous {layout.set_noun} from a backup an "
        "earlier run could not put back"
    )
