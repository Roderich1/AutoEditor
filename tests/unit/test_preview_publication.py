"""Publishing a preview set never loses one (CE-034).

Generation was already transactional: everything is encoded and probed in a
staging directory, so a failed encode never touched the published set. The
*publication* was not. It removed stale previews, then replaced files one at a
time, then wrote the two artifacts -- so a failure anywhere after the first
`unlink` or the first `replace` left the directory holding a mixture of two
runs, with `index.json` describing neither. That is worse than either set: the
previous previews were reusable and became unverifiable.

The first fix moved the whole published set aside and restored it on failure,
which made the ordinary failure atomic. It also introduced a worse defect,
because it deleted the backup unconditionally on the way out -- so a failure
*during the restore* destroyed the only remaining copy.

What the tests hold, therefore, is two different promises, and the distinction
is the point rather than a hedge:

- when publication fails and the restore succeeds, the previous set is
  **byte-identical and still passes `verify_previews`**. Files that merely exist
  are not the same as a set a later run will accept, which is why both are
  asserted;
- when the restore *itself* fails, the guarantee is **durability, not
  atomicity**. Every file stays reachable in `previews/` or in
  `previews/.rollback/`, the backup survives, and a later invocation finishes
  the job. `recoverable()` is that union and is what those tests assert against.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from content_engine.domain.candidates import CandidateCollection
from content_engine.domain.exceptions import RenderError
from content_engine.domain.preview_rules import (
    PREVIEW_INDEX_FILENAME,
    PREVIEW_STAGE_CONFIG_FILENAME,
    preview_filename,
    preview_stage_config,
)
from content_engine.services import preview_service
from content_engine.services.preview_service import (
    ROLLBACK_DIRNAME,
    ROLLBACK_JOURNAL,
    STAGING_DIRNAME,
    PreviewPlan,
    PreviewService,
    resolve_pending_rollback,
    verify_previews,
)
from tests.conftest import chunk_of, collect, raw_candidate, speech_transcript

GENERATED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64
SOURCE_SHA = "c" * 64


class WritingRenderer:
    """Always succeeds, and never twice with the same bytes.

    The generation marker is what makes a half-finished publication visible. A
    fake that re-encoded an unchanged interval to identical bytes would leave a
    partly replaced set looking exactly like a successful one, and every test
    below would pass over the defect they exist to catch.

    It is not a claim about x264. Measured on the real run, two encodes of the
    same source and arguments with the same FFmpeg build produced byte-identical
    output; what varies between them is only the `generated_at` in the index.
    The marker stands in for "these are different files", which is the property
    under test, rather than for encoder nondeterminism.
    """

    def __init__(self, generation: str = "first") -> None:
        self.generation = generation

    def render(
        self, source: Path, start: float, duration: float, output: Path, config: Any
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(
            f"preview:{self.generation}:{output.name}:{start:.3f}:{duration:.3f}".encode()
        )


class FakeProbe:
    def probe(self, input_path: Path) -> tuple[Any, dict[str, Any]]:
        from content_engine.domain.models import MediaInfo

        # The measured duration has to be close to the interval, and the
        # interval differs per candidate, so it is read back out of the bytes
        # the renderer wrote rather than fixed.
        declared = float(input_path.read_bytes().decode().rsplit(":", 1)[-1])
        return (
            MediaInfo(
                duration_seconds=declared,
                video_codec="h264",
                width=540,
                height=960,
                fps=30.0,
                audio_codec="aac",
                sample_rate=44100,
                channels=2,
                container="mov,mp4,m4a",
                file_size=input_path.stat().st_size,
            ),
            {},
        )


def service(generation: str = "first") -> PreviewService:
    return PreviewService(WritingRenderer(generation), FakeProbe())


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path.joinpath("source.mp4")
    path.write_bytes(b"source-video")
    return path


#: The published set is three previews on purpose. A shrinking shortlist then
#: still holds two, so the "fail on the second file" injections are reachable in
#: every scenario rather than silently not firing.
def three_candidates() -> CandidateCollection:
    return collect(
        chunk_of(speech_transcript()),
        [
            raw_candidate(10.0, 39.0),
            raw_candidate(60.0, 89.0, hook=88),
            raw_candidate(95.0, 118.0, hook=80),
        ],
    )


def four_candidates() -> CandidateCollection:
    return collect(
        chunk_of(speech_transcript()),
        [
            raw_candidate(10.0, 39.0),
            raw_candidate(60.0, 89.0, hook=88),
            raw_candidate(95.0, 118.0, hook=80),
            raw_candidate(39.5, 60.5, hook=75),
        ],
    )


def plan_for(source: Path, collection: CandidateCollection) -> PreviewPlan:
    return PreviewPlan(
        candidates=tuple(collection.candidates),
        config=preview_stage_config(width=540, height=960),
        analysis_fingerprint=FINGERPRINT,
        source_path=source,
        source_sha256=SOURCE_SHA,
        source_duration_seconds=collection.source_duration_seconds,
    )


def shorter(plan: PreviewPlan) -> PreviewPlan:
    """The same plan with the last candidate dropped."""
    return PreviewPlan(
        candidates=plan.candidates[:-1],
        config=plan.config,
        analysis_fingerprint=plan.analysis_fingerprint,
        source_path=plan.source_path,
        source_sha256=plan.source_sha256,
        source_duration_seconds=plan.source_duration_seconds,
    )


def snapshot(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file()}


@pytest.fixture
def published(tmp_path: Path, source: Path) -> tuple[Path, PreviewPlan, str, str]:
    """A verified set of three previews on disk, and the identity recorded for it."""
    directory = tmp_path.joinpath("previews")
    collection = three_candidates()
    assert len(collection.candidates) == 3, "the fixture must really select three"
    plan = plan_for(source, collection)
    outcome = service().generate(plan, directory, GENERATED_AT)
    return directory, plan, outcome.fingerprint, outcome.stage_config_sha256


def fail_on_write(target: str) -> Callable[..., None]:
    """Replace `write_json` so it refuses one artifact by name."""
    real = preview_service.write_json

    def guarded(path: Path, value: Any) -> None:
        if path.name == target:
            raise OSError(f"synthetic write failure for {target}")
        real(path, value)

    return guarded


def fail_on_replace(nth: int) -> Callable[..., Any]:
    """Refuse the nth move that places a staged preview into the directory."""
    real = Path.replace
    calls = {"count": 0}

    def guarded(self: Path, target: Any) -> Any:
        # Only publication moves are counted. The atomic `.tmp` rename inside
        # write_json also lands here, and failing that would be testing a
        # different thing.
        if self.suffix == ".mp4" and STAGING_DIRNAME in str(self):
            calls["count"] += 1
            if calls["count"] == nth:
                raise OSError(f"synthetic replace failure on move {nth}")
        return real(self, target)

    return guarded


def fail_on_restore(nth: int, *, persistent: bool = False) -> Callable[..., Any]:
    """Refuse the nth move that takes a saved file back out of `.rollback`.

    The restore is the last line of defence, so a failure here is the one that
    decides whether a bad moment costs data or only costs an error message.
    `persistent` keeps refusing, which is what a full disk or a revoked
    permission looks like: the restore cannot be completed now or on the next
    attempt, and the backup has to survive regardless.
    """
    real = Path.replace
    calls = {"count": 0}

    def guarded(self: Path, target: Any) -> Any:
        # Moves *out of* the rollback directory only. The move-aside goes the
        # other way, and write_json's atomic `.tmp` rename must not be counted.
        if ROLLBACK_DIRNAME in str(self) and self.suffix != ".tmp":
            calls["count"] += 1
            if calls["count"] == nth or (persistent and calls["count"] >= nth):
                raise OSError(f"synthetic restore failure on file {calls['count']}")
        return real(self, target)

    return guarded


def recoverable(directory: Path) -> dict[str, bytes]:
    """Every published file still reachable, wherever it currently lives.

    A file counts as safe whether it is back in `previews/` or still held in
    `.rollback`. That union is the real guarantee: publication may be left
    incomplete by a failure it cannot undo, but nothing may be *lost*.
    """
    found: dict[str, bytes] = {}
    rollback = directory.joinpath(ROLLBACK_DIRNAME)
    if rollback.is_dir():
        for path in sorted(rollback.iterdir()):
            if path.is_file() and path.name != ROLLBACK_JOURNAL:
                found[path.name] = path.read_bytes()
    for path in sorted(directory.iterdir()):
        if path.is_file():
            found[path.name] = path.read_bytes()
    return found


def fail_on_move_aside(nth: int) -> Callable[..., Any]:
    """Refuse the nth move that takes a published file out of the directory.

    This is the step that replaced the old in-place `unlink` of a stale
    preview: nothing is deleted in place any more, the whole published set is
    moved aside. A failure here is the most expensive moment of the operation,
    because part of the previous set has already left the directory.
    """
    real = Path.replace
    calls = {"count": 0}

    def guarded(self: Path, target: Any) -> Any:
        # `.tmp` is write_json's atomic rename, and the journal is written into
        # this same directory, so without the exclusion "the nth move-aside"
        # would mean the (n-1)th file.
        if (
            ROLLBACK_DIRNAME in str(target)
            and STAGING_DIRNAME not in str(self)
            and self.suffix != ".tmp"
        ):
            calls["count"] += 1
            if calls["count"] == nth:
                raise OSError(f"synthetic move-aside failure on file {nth}")
        return real(self, target)

    return guarded


#: Each entry installs one failure at one step of publication.
INJECTIONS: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "first-move-aside": lambda mp: mp.setattr(Path, "replace", fail_on_move_aside(1)),
    "second-move-aside": lambda mp: mp.setattr(Path, "replace", fail_on_move_aside(2)),
    "last-move-aside": lambda mp: mp.setattr(Path, "replace", fail_on_move_aside(5)),
    "first-mp4-move": lambda mp: mp.setattr(Path, "replace", fail_on_replace(1)),
    "second-mp4-move": lambda mp: mp.setattr(Path, "replace", fail_on_replace(2)),
    "stage-config-write": lambda mp: mp.setattr(
        preview_service, "write_json", fail_on_write(PREVIEW_STAGE_CONFIG_FILENAME)
    ),
    "index-write": lambda mp: mp.setattr(
        preview_service, "write_json", fail_on_write(PREVIEW_INDEX_FILENAME)
    ),
}


class TestPublicationIsAllOrNothing:
    @pytest.mark.parametrize("injection", sorted(INJECTIONS))
    @pytest.mark.parametrize("shortlist", ["same", "grown", "shrunk"])
    def test_a_failure_leaves_the_previous_set_byte_identical(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
        injection: str,
        shortlist: str,
        source: Path,
    ) -> None:
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        assert len(before) == 5, before.keys()

        # A shrinking shortlist is the dangerous case: a preview nobody wants
        # any more has to disappear, so under the old design the previous set
        # was already damaged before a single new file had been placed.
        grown = four_candidates()
        assert len(grown.candidates) == 4, "the grown scenario must really add one"
        regenerate = {
            "same": plan,
            "grown": plan_for(source, grown),
            "shrunk": shorter(plan),
        }[shortlist]
        INJECTIONS[injection](monkeypatch)

        engine = service("second")
        with pytest.raises((RenderError, OSError)):
            engine.generate(regenerate, directory, LATER)

        assert snapshot(directory) == before

    @pytest.mark.parametrize("injection", sorted(INJECTIONS))
    def test_the_previous_set_still_verifies_after_a_failure(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
        injection: str,
    ) -> None:
        """Surviving files are not enough: the set has to remain reusable."""
        directory, plan, fingerprint, digest = published
        INJECTIONS[injection](monkeypatch)

        engine = service("second")
        with pytest.raises((RenderError, OSError)):
            engine.generate(plan, directory, LATER)

        assert verify_previews(directory, fingerprint, digest, plan).previews

    def test_a_failure_while_the_old_set_is_being_moved_aside_restores_it(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The moment at which part of the previous set has left the directory.

        Nothing is deleted in place any more, so this step is what the old
        `unlink` of a stale preview became -- and it is where a failure costs
        most, because the directory is already incomplete when it happens.
        """
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        monkeypatch.setattr(Path, "replace", fail_on_move_aside(2))

        engine = service("second")
        smaller = shorter(plan)
        with pytest.raises((RenderError, OSError)):
            engine.generate(smaller, directory, LATER)

        assert snapshot(directory) == before
        assert verify_previews(directory, fingerprint, digest, plan).previews

    def test_a_shrinking_shortlist_that_fails_keeps_the_dropped_preview(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The preview the new set would drop must come back with the rest."""
        directory, plan, fingerprint, digest = published
        dropped = preview_filename(plan.candidates[-1].id)
        assert directory.joinpath(dropped).is_file()
        monkeypatch.setattr(preview_service, "write_json", fail_on_write(PREVIEW_INDEX_FILENAME))

        engine = service("second")
        smaller = shorter(plan)
        with pytest.raises((RenderError, OSError)):
            engine.generate(smaller, directory, LATER)

        assert directory.joinpath(dropped).is_file()
        assert verify_previews(directory, fingerprint, digest, plan).previews

    @pytest.mark.parametrize("injection", sorted(INJECTIONS))
    def test_no_temporary_directory_survives_a_failure(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
        injection: str,
    ) -> None:
        directory, plan, _, _ = published
        INJECTIONS[injection](monkeypatch)

        engine = service("second")
        with pytest.raises((RenderError, OSError)):
            engine.generate(plan, directory, LATER)

        assert [path.name for path in directory.iterdir() if path.is_dir()] == []
        assert list(directory.rglob("*.tmp")) == []

    def test_a_successful_publication_still_replaces_everything(
        self, published: tuple[Path, PreviewPlan, str, str], source: Path
    ) -> None:
        """The transaction must not have made the normal path a no-op."""
        directory, plan, _, _ = published
        grown = plan_for(source, four_candidates())
        outcome = service("second").generate(grown, directory, LATER)

        assert len(outcome.index.previews) == 4
        assert len(list(directory.glob("*.mp4"))) == 4
        assert verify_previews(
            directory, outcome.fingerprint, outcome.stage_config_sha256, grown
        ).previews

    def test_a_shrinking_shortlist_removes_the_stale_preview(
        self, published: tuple[Path, PreviewPlan, str, str]
    ) -> None:
        directory, plan, _, _ = published
        gone = preview_filename(plan.candidates[-1].id)
        smaller = shorter(plan)
        outcome = service("second").generate(smaller, directory, LATER)

        assert not directory.joinpath(gone).exists()
        assert len(list(directory.glob("*.mp4"))) == 2
        assert verify_previews(
            directory, outcome.fingerprint, outcome.stage_config_sha256, smaller
        ).previews


class TestFailuresDuringRestoration:
    """When the undo itself fails, nothing may be lost.

    The transaction can be defeated: a restore is a sequence of renames, and a
    rename can fail for reasons that have nothing to do with this program --
    a full disk, a revoked permission, a virus scanner holding a handle. What
    the design owes in that case is not atomicity, which it cannot deliver, but
    **durability**: every file of the previous set stays reachable in
    `previews/` or in `previews/.rollback/`, the backup is never deleted while
    incomplete, and a later invocation can finish the job deterministically.

    The defect these tests were written against did the opposite. A failing
    restore propagated out of `_publish`, and `generate`'s `finally` deleted
    `.rollback` unconditionally -- so the one copy of the previous set was
    removed on the way out and `previews/` was left empty.
    """

    def publication_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make the placement fail, which is what triggers a restore at all."""
        monkeypatch.setattr(preview_service, "write_json", fail_on_write(PREVIEW_INDEX_FILENAME))

    @pytest.mark.parametrize(("position", "nth"), [("first", 1), ("middle", 3), ("last", 5)])
    def test_a_failed_restore_loses_nothing(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
        position: str,
        nth: int,
    ) -> None:
        directory, plan, _, _ = published
        before = snapshot(directory)
        assert len(before) == 5, before.keys()

        self.publication_fails(monkeypatch)
        monkeypatch.setattr(Path, "replace", fail_on_restore(nth))
        engine = service("second")

        with pytest.raises((RenderError, OSError)):
            engine.generate(plan, directory, LATER)

        assert recoverable(directory) | before == recoverable(directory), (
            f"restoring the {position} file failed and the previous set was not "
            "left wholly reachable"
        )

    @pytest.mark.parametrize("shortlist", ["same", "grown", "shrunk"])
    def test_the_backup_survives_a_failed_restore(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
        source: Path,
        shortlist: str,
    ) -> None:
        directory, plan, _, _ = published
        before = snapshot(directory)
        grown = four_candidates()
        regenerate = {
            "same": plan,
            "grown": plan_for(source, grown),
            "shrunk": shorter(plan),
        }[shortlist]

        self.publication_fails(monkeypatch)
        monkeypatch.setattr(Path, "replace", fail_on_restore(2, persistent=True))
        engine = service("second")

        with pytest.raises((RenderError, OSError)):
            engine.generate(regenerate, directory, LATER)

        assert directory.joinpath(ROLLBACK_DIRNAME).is_dir(), "the backup was deleted"
        assert recoverable(directory) | before == recoverable(directory)

    def test_the_error_says_where_the_data_is(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An incomplete restore is only survivable if the operator is told."""
        directory, plan, _, _ = published
        self.publication_fails(monkeypatch)
        monkeypatch.setattr(Path, "replace", fail_on_restore(1, persistent=True))
        engine = service("second")

        with pytest.raises(RenderError) as raised:
            engine.generate(plan, directory, LATER)

        message = str(raised.value)
        assert ROLLBACK_DIRNAME in message
        assert str(directory) in message

    def test_a_transient_restore_failure_is_finished_by_the_next_run(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The next invocation resolves the pending backup before generating.

        This deliberately does **not** assert through `generate`. A second
        `generate` that publishes a fresh set successfully leaves a complete,
        verifiable directory whether or not the previous set was recovered, so
        it cannot tell recovery from replacement. `TestResumingAPartialRestore`
        calls `resolve_pending_rollback` on its own for exactly that reason;
        what is checked here is only that a pending backup does not block the
        next run.
        """
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)

        self.publication_fails(monkeypatch)
        monkeypatch.setattr(Path, "replace", fail_on_restore(2))
        engine = service("second")
        with pytest.raises((RenderError, OSError)):
            engine.generate(plan, directory, LATER)

        assert directory.joinpath(ROLLBACK_DIRNAME).is_dir(), "the backup was deleted"
        assert recoverable(directory) | before == recoverable(directory)

        monkeypatch.undo()
        engine = service("third")
        outcome = engine.generate(plan, directory, LATER)
        assert not directory.joinpath(ROLLBACK_DIRNAME).exists()
        assert verify_previews(
            directory, outcome.fingerprint, outcome.stage_config_sha256, plan
        ).previews
        del fingerprint, digest

    def test_a_persistent_restore_failure_never_loses_the_backup(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Three attempts, each failing, and the previous set is still all there."""
        directory, plan, _, _ = published
        before = snapshot(directory)

        self.publication_fails(monkeypatch)
        monkeypatch.setattr(Path, "replace", fail_on_restore(1, persistent=True))

        for _ in range(3):
            engine = service("second")
            with pytest.raises((RenderError, OSError)):
                engine.generate(plan, directory, LATER)
            assert directory.joinpath(ROLLBACK_DIRNAME).is_dir()
            assert recoverable(directory) | before == recoverable(directory)

    def test_a_pending_rollback_is_not_destroyed_by_the_next_publication(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pending backup is the only copy of something. It is never overwritten."""
        directory, plan, _, _ = published
        before = snapshot(directory)

        self.publication_fails(monkeypatch)
        monkeypatch.setattr(Path, "replace", fail_on_restore(1, persistent=True))
        engine = service("second")
        with pytest.raises((RenderError, OSError)):
            engine.generate(plan, directory, LATER)
        pending = snapshot(directory.joinpath(ROLLBACK_DIRNAME))
        assert pending, "the failed restore should have left a backup"

        # The next attempt still cannot restore, and must not silently discard
        # what it cannot put back.
        engine = service("third")
        with pytest.raises((RenderError, OSError)):
            engine.generate(plan, directory, LATER)
        assert recoverable(directory) | before == recoverable(directory)

    def test_a_rollback_with_no_journal_is_refused_untouched(
        self,
        published: tuple[Path, PreviewPlan, str, str],
    ) -> None:
        """An unreadable backup cannot be resolved deterministically, so it is left."""
        directory, plan, _, _ = published
        rollback = directory.joinpath(ROLLBACK_DIRNAME)
        rollback.mkdir()
        rollback.joinpath("candidate_cand_unknown.mp4").write_bytes(b"from an older build")
        stranded = snapshot(rollback)

        engine = service("second")
        with pytest.raises(RenderError, match=ROLLBACK_DIRNAME):
            engine.generate(plan, directory, LATER)

        assert snapshot(rollback) == stranded


class TestAPendingBackupIsInterpretedOrLeftAlone:
    """The journal decides what an interrupted publication meant.

    It carries one thing: how far publication had got. That is enough, because
    the two phases are disjoint -- nothing is placed until everything has been
    moved aside -- and it is *necessary*, because the two need opposite undo
    steps. In `moving_aside` the files in the previews directory are the
    previous set and must be kept; in `placing` they belong to the attempt that
    failed and must go. Guessing wrong deletes the wrong ones, so a journal this
    build cannot read is never guessed at.
    """

    @staticmethod
    def held_data(directory: Path) -> dict[str, bytes]:
        """The previous set inside a pending backup, journal excluded.

        The journal is deliberately corrupted by most tests here, so comparing
        it would be comparing the thing under test against itself.
        """
        rollback = directory.joinpath(ROLLBACK_DIRNAME)
        return {
            path.name: path.read_bytes()
            for path in sorted(rollback.iterdir())
            if path.is_file() and path.name != ROLLBACK_JOURNAL
        }

    def strand(
        self, directory: Path, monkeypatch: pytest.MonkeyPatch, plan: PreviewPlan
    ) -> dict[str, bytes]:
        """Leave a real pending backup behind, and return the data it holds."""
        monkeypatch.setattr(preview_service, "write_json", fail_on_write(PREVIEW_INDEX_FILENAME))
        monkeypatch.setattr(Path, "replace", fail_on_restore(1, persistent=True))
        engine = service("second")
        with pytest.raises(RenderError):
            engine.generate(plan, directory, LATER)
        monkeypatch.undo()
        data = self.held_data(directory)
        assert data, "the stranded backup should hold the previous set"
        return data

    def test_a_journal_naming_another_schema_is_refused(
        self, published: tuple[Path, PreviewPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        held = self.strand(directory, monkeypatch, plan)
        journal = directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL)
        payload = json.loads(journal.read_text(encoding="utf-8"))
        payload["schema_version"] = 99
        journal.write_text(json.dumps(payload), encoding="utf-8")

        engine = service("third")
        with pytest.raises(RenderError, match="schema"):
            engine.generate(plan, directory, LATER)
        assert self.held_data(directory) == held

    def test_a_journal_naming_an_unknown_phase_is_refused(
        self, published: tuple[Path, PreviewPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        held = self.strand(directory, monkeypatch, plan)
        journal = directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL)
        journal.write_text(json.dumps({"schema_version": 1, "phase": "halfway"}), encoding="utf-8")

        engine = service("third")
        with pytest.raises(RenderError, match="halfway"):
            engine.generate(plan, directory, LATER)
        assert self.held_data(directory) == held

    def test_an_unreadable_journal_is_refused(
        self, published: tuple[Path, PreviewPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        held = self.strand(directory, monkeypatch, plan)
        directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL).write_text(
            "{truncated", encoding="utf-8"
        )

        engine = service("third")
        with pytest.raises(RenderError, match="cannot be read"):
            engine.generate(plan, directory, LATER)
        assert self.held_data(directory) == held

    def test_a_journal_that_is_not_an_object_is_refused(
        self, published: tuple[Path, PreviewPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        held = self.strand(directory, monkeypatch, plan)
        directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL).write_text("[]", encoding="utf-8")

        engine = service("third")
        with pytest.raises(RenderError, match="rollback journal"):
            engine.generate(plan, directory, LATER)
        assert self.held_data(directory) == held

    def test_an_empty_backup_with_no_journal_is_cleared(
        self, published: tuple[Path, PreviewPlan, str, str]
    ) -> None:
        """It holds nothing recoverable, so removing it discards nothing.

        The only case where a pre-existing backup directory may go without being
        read, and it is checked to be empty first rather than assumed.
        """
        directory, plan, _, _ = published
        directory.joinpath(ROLLBACK_DIRNAME).mkdir()

        outcome = service("second").generate(plan, directory, LATER)
        assert not directory.joinpath(ROLLBACK_DIRNAME).exists()
        assert verify_previews(
            directory, outcome.fingerprint, outcome.stage_config_sha256, plan
        ).previews

    def test_publish_refuses_to_overwrite_a_backup_it_was_handed(
        self, published: tuple[Path, PreviewPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_publish` guards itself, so a caller that skipped the resolution stops.

        `generate` resolves a pending backup before publishing, so this branch is
        unreachable through it. It is still checked, because the thing it
        prevents is overwriting the only copy of somebody's previous set.
        """
        directory, plan, _, _ = published
        held = self.strand(directory, monkeypatch, plan)
        staging = directory.joinpath(STAGING_DIRNAME)
        staging.mkdir(exist_ok=True)
        index = read_index_from(directory.joinpath(ROLLBACK_DIRNAME))
        config = preview_stage_config(width=540, height=960)

        with pytest.raises(RenderError, match="has not been restored"):
            PreviewService._publish(directory, staging, index, config)
        assert self.held_data(directory) == held


class TestTheJournalWriteItself:
    def test_a_journal_that_cannot_be_written_leaves_no_backup_behind(
        self, published: tuple[Path, PreviewPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The journal is written first, so failing it has nothing to strand.

        This is the one place a backup directory is removed on a failure path,
        and it is sound only because it is provably empty: the journal precedes
        every move, so nothing of the previous set can be inside it yet.
        """
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        monkeypatch.setattr(preview_service, "write_json", fail_on_write(ROLLBACK_JOURNAL))

        engine = service("second")
        with pytest.raises(OSError, match=ROLLBACK_JOURNAL):
            engine.generate(plan, directory, LATER)

        assert not directory.joinpath(ROLLBACK_DIRNAME).exists()
        assert snapshot(directory) == before
        assert verify_previews(directory, fingerprint, digest, plan).previews


class TestTheMovingAsidePhase:
    """A backup stranded before anything was placed needs the opposite undo.

    This is the phase distinction the journal exists for. Here the previews
    directory still holds part of the previous set -- the files not yet moved --
    so the undo must move the saved ones back and delete nothing. Applying the
    `placing` undo instead would delete exactly the files that have no copy in
    the backup, which is the bug the phase flag prevents.
    """

    def test_a_restore_stranded_before_any_placement_keeps_everything(
        self, published: tuple[Path, PreviewPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)

        # Fail the second move-aside, so the phase is still `moving_aside`, and
        # fail the restore that follows.
        monkeypatch.setattr(Path, "replace", fail_on_move_aside_then_restore(2))
        engine = service("second")
        with pytest.raises(RenderError):
            engine.generate(plan, directory, LATER)

        journal = json.loads(
            directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL).read_text(encoding="utf-8")
        )
        assert journal["phase"] == "moving_aside"
        assert recoverable(directory) | before == recoverable(directory)

        monkeypatch.undo()
        outcome = service("third").generate(plan, directory, LATER)
        assert not directory.joinpath(ROLLBACK_DIRNAME).exists()
        assert verify_previews(
            directory, outcome.fingerprint, outcome.stage_config_sha256, plan
        ).previews
        del fingerprint, digest


def fail_on_move_aside_then_restore(nth: int) -> Callable[..., Any]:
    """Fail the nth move-aside, then fail the first restore move that follows.

    One injector rather than two, because the two failures have to happen in the
    same `Path.replace` patch: the restore is triggered by the move-aside
    failing, so both are moves and both must be intercepted.
    """
    real = Path.replace
    state = {"aside": 0, "restored": 0}

    def guarded(self: Path, target: Any) -> Any:
        # `.tmp` sources are write_json's own atomic rename, including the
        # journal's. Counting those as move-asides made this injector fire one
        # file early, before anything real had moved.
        if (
            ROLLBACK_DIRNAME in str(target)
            and STAGING_DIRNAME not in str(self)
            and self.suffix != ".tmp"
        ):
            state["aside"] += 1
            if state["aside"] == nth:
                raise OSError(f"synthetic move-aside failure on file {nth}")
        elif ROLLBACK_DIRNAME in str(self) and self.suffix != ".tmp":
            state["restored"] += 1
            raise OSError("synthetic restore failure")
        return real(self, target)

    return guarded


def read_index_from(directory: Path) -> Any:
    """The index saved inside a pending backup, for tests that need a real one."""
    from content_engine.domain.previews import PreviewIndex

    payload = json.loads(directory.joinpath(PREVIEW_INDEX_FILENAME).read_text(encoding="utf-8"))
    return PreviewIndex.model_validate(payload)


class TestResumingAPartialRestore:
    """A restore that stopped half-way is finished, not started again.

    This is the defect the phase protocol was missing. `PHASE_PLACING` means
    "the previews directory holds files from the failed publication", and its
    undo deletes them before moving the previous set back. That is correct the
    first time and catastrophic the second: once some of the previous set has
    been moved back, the directory no longer holds only new files, and re-running
    the same branch deletes the very files that were just recovered. The backup
    is then emptied of them too, because they had already left it.

    So the journal has to record that the deletion half is over.
    `PHASE_RESTORING` is that record: it is written after the last deletion and
    before the first move back, and its undo never deletes anything.

    Every test here resolves the backup by calling `resolve_pending_rollback`
    directly. Going through `generate` would publish a fresh set, and a fresh
    set is complete and verifiable whether or not anything was recovered -- it
    hides exactly the loss under test.
    """

    #: Five files are published: three previews plus the two artifacts. The
    #: restore therefore has five moves, and these are the second, a middle one
    #: and the last.
    POSITIONS = (2, 3, 5)

    def strand_mid_restore(
        self,
        directory: Path,
        monkeypatch: pytest.MonkeyPatch,
        plan: PreviewPlan,
        nth_restore: int,
    ) -> None:
        """Fail the publication, then fail the restore at `nth_restore`."""
        monkeypatch.setattr(preview_service, "write_json", fail_on_write(PREVIEW_INDEX_FILENAME))
        monkeypatch.setattr(Path, "replace", fail_on_restore(nth_restore, persistent=True))
        engine = service("second")
        with pytest.raises(RenderError):
            engine.generate(plan, directory, LATER)
        monkeypatch.undo()

    @pytest.mark.parametrize("nth", POSITIONS)
    @pytest.mark.parametrize("shortlist", ["same", "grown", "shrunk"])
    def test_resolving_restores_the_previous_set_exactly(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
        source: Path,
        nth: int,
        shortlist: str,
    ) -> None:
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        assert len(before) == 5, before.keys()

        failing = {
            "same": plan,
            "grown": plan_for(source, four_candidates()),
            "shrunk": shorter(plan),
        }[shortlist]
        self.strand_mid_restore(directory, monkeypatch, failing, nth)

        # Step 4 of the report: at this moment nothing has been lost yet.
        assert recoverable(directory) | before == recoverable(directory)

        resolve_pending_rollback(directory)

        assert snapshot(directory) == before, (
            "resolving a half-finished restore must put the previous set back "
            "byte for byte, not delete what had already been recovered"
        )
        assert not directory.joinpath(ROLLBACK_DIRNAME).exists()
        assert verify_previews(directory, fingerprint, digest, plan).previews

    @pytest.mark.parametrize("nth", POSITIONS)
    def test_the_backup_is_kept_until_the_last_file_is_back(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
        nth: int,
    ) -> None:
        """A resolution that fails again still leaves everything reachable."""
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        self.strand_mid_restore(directory, monkeypatch, plan, nth)

        monkeypatch.setattr(Path, "replace", fail_on_restore(1, persistent=True))
        with pytest.raises(RenderError):
            resolve_pending_rollback(directory)
        monkeypatch.undo()

        assert directory.joinpath(ROLLBACK_DIRNAME).is_dir()
        assert recoverable(directory) | before == recoverable(directory)

        resolve_pending_rollback(directory)
        assert snapshot(directory) == before
        assert verify_previews(directory, fingerprint, digest, plan).previews

    def test_several_failures_at_different_positions_still_recover(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Four invocations, each stopping somewhere different, then success.

        The counter restarts every time because each invocation installs its own
        injector, so "fail on the second move" means the second move *of what is
        left*. Between attempts the previous set must stay wholly reachable, and
        at the end it must be back exactly.
        """
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        self.strand_mid_restore(directory, monkeypatch, plan, 2)

        for position in (2, 1, 2):
            monkeypatch.setattr(Path, "replace", fail_on_restore(position, persistent=True))
            with pytest.raises(RenderError):
                resolve_pending_rollback(directory)
            monkeypatch.undo()
            assert directory.joinpath(ROLLBACK_DIRNAME).is_dir()
            assert recoverable(directory) | before == recoverable(directory)

        resolve_pending_rollback(directory)
        assert snapshot(directory) == before
        assert not directory.joinpath(ROLLBACK_DIRNAME).exists()
        assert verify_previews(directory, fingerprint, digest, plan).previews

    @pytest.mark.parametrize("nth", POSITIONS)
    def test_the_journal_records_that_deleting_is_over(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
        nth: int,
    ) -> None:
        """The phase must move off `placing` before the first file goes back.

        Without this the resumed undo cannot tell "the directory holds new
        files" from "the directory holds files I already recovered", and it
        deletes the second as though it were the first.
        """
        directory, plan, _, _ = published
        self.strand_mid_restore(directory, monkeypatch, plan, nth)

        journal = json.loads(
            directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL).read_text(encoding="utf-8")
        )
        assert journal["phase"] == "restoring"

    def test_a_failed_phase_transition_leaves_the_backup_complete(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The window between the last deletion and the first move back.

        If writing `restoring` fails, nothing has moved yet and the previous set
        is still whole in the backup. The phase on disk is still `placing`, so a
        resume re-runs the deletion -- finding nothing left to delete -- and
        tries the transition again. Repeating it has to be safe, because that is
        the only way out.
        """
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)

        real_write = preview_service.write_json

        def refuse_the_transition(path: Path, value: Any) -> None:
            if path.name == PREVIEW_INDEX_FILENAME:
                raise OSError("synthetic placement failure")
            if path.name == ROLLBACK_JOURNAL and value.get("phase") == "restoring":
                raise OSError("synthetic phase transition failure")
            real_write(path, value)

        monkeypatch.setattr(preview_service, "write_json", refuse_the_transition)
        engine = service("second")
        with pytest.raises(RenderError):
            engine.generate(plan, directory, LATER)
        monkeypatch.undo()

        rollback = directory.joinpath(ROLLBACK_DIRNAME)
        journal = json.loads(rollback.joinpath(ROLLBACK_JOURNAL).read_text(encoding="utf-8"))
        assert journal["phase"] == "placing", "the transition must not be recorded if it failed"
        held = {
            path.name: path.read_bytes()
            for path in sorted(rollback.iterdir())
            if path.is_file() and path.name != ROLLBACK_JOURNAL
        }
        assert held == before, "the previous set must still be whole in the backup"

        resolve_pending_rollback(directory)
        assert snapshot(directory) == before
        assert verify_previews(directory, fingerprint, digest, plan).previews

    def test_a_resumed_restore_deletes_nothing_from_the_previews_directory(
        self,
        published: tuple[Path, PreviewPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Asserted directly: no unlink of a published file during a resume."""
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        self.strand_mid_restore(directory, monkeypatch, plan, 3)
        recovered = sorted(path.name for path in directory.iterdir() if path.is_file())
        assert recovered, "two files should already be back"

        unlinked: list[str] = []
        real_unlink = Path.unlink

        def watched(self: Path, missing_ok: bool = False) -> None:
            if ROLLBACK_DIRNAME not in str(self):
                unlinked.append(self.name)
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", watched)
        resolve_pending_rollback(directory)
        monkeypatch.undo()

        assert unlinked == [], f"a resumed restore deleted {unlinked}"
        assert snapshot(directory) == before
        assert verify_previews(directory, fingerprint, digest, plan).previews
