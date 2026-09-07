"""Publishing a set of clips never loses one.

The protocol is the one the preview stage paid for and
``test_preview_publication.py`` still proves in full. What is new here is that a
published item is a **directory** of four artifacts rather than a single file,
so the move aside, the deletion in the ``placing`` phase and the move back all
have to work on directories -- and the deletion is the one step that has to tell
a directory from a file at all.

The two promises are the same, and the distinction between them is the point
rather than a hedge:

- when publication fails and the restore succeeds, the previous set is
  **byte-identical and still verifies**;
- when the restore *itself* fails, the guarantee is **durability, not
  atomicity**. Every artifact stays reachable in ``clips/`` or in
  ``clips/.rollback/``, the backup survives, and a later invocation finishes the
  job.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.adapters.media.render import FFmpegClipRenderer
from content_engine.domain.exceptions import RenderError
from content_engine.domain.render_rules import RENDER_INDEX_FILENAME
from content_engine.services import publication, render_service
from content_engine.services.render_service import (
    ROLLBACK_DIRNAME,
    ROLLBACK_JOURNAL,
    RenderPlan,
    RenderService,
    resolve_pending_rollback,
    verify_clips,
)
from tests.conftest import FakeMedia
from tests.unit.test_render_service import GENERATED_AT, LATER, plan_for


@pytest.fixture
def media(monkeypatch: pytest.MonkeyPatch) -> FakeMedia:
    return FakeMedia(width=1080, height=1920).install(monkeypatch)


def service() -> RenderService:
    return RenderService(FFmpegClipRenderer(), FFprobeAdapter())


def snapshot(directory: Path) -> dict[str, bytes]:
    """Every published file, keyed by its path relative to the clips directory."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and ROLLBACK_DIRNAME not in path.parts
    }


def recoverable(directory: Path) -> dict[str, bytes]:
    """Everything still reachable, wherever it is.

    The union of the published directory and the backup, which is what the
    durability guarantee is about: a file is in one or the other, never neither.
    Keyed by basename because a file in the backup is flat and the same file in
    place is nested inside its clip directory.
    """
    held: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name == ROLLBACK_JOURNAL:
            continue
        # Keyed by what the item *is* rather than by where it currently sits: a
        # clip directory keeps its own name whether it is published or held in
        # the backup, and the two stage artifacts are identified by their
        # filename alone, because their parent is `clips` in one place and
        # `.rollback` in the other.
        parent = path.parent.name
        held[f"{parent}/{path.name}" if parent.startswith("clip_") else path.name] = (
            path.read_bytes()
        )
    return held


@pytest.fixture
def published(media: FakeMedia, tmp_path: Path) -> tuple[Path, RenderPlan, str, str]:
    directory = tmp_path.joinpath("clips")
    plan = plan_for(tmp_path, count=2)
    outcome = service().generate(plan, directory, GENERATED_AT)
    return directory, plan, outcome.fingerprint, outcome.stage_config_sha256


def fail_on_write(name: str) -> Callable[[Path, Any], None]:
    real = publication.write_json

    def refuse(path: Path, value: Any) -> None:
        if path.name == name:
            raise OSError(f"synthetic failure writing {name}")
        real(path, value)

    return refuse


def fail_on_restore(nth: int, *, persistent: bool = False) -> Callable[..., None]:
    """Refuse the nth rename that moves something *out* of the backup.

    The ``.tmp`` exclusion is not incidental. ``write_json`` writes atomically
    through ``rollback.json.tmp`` and renames it into place, and that rename is
    also out of the backup directory -- counting it would make the journal write
    itself the failure the test thinks it placed on a restore.
    """
    real = Path.replace
    seen = {"count": 0}

    def replace(self: Path, target: Any) -> Any:
        if ROLLBACK_DIRNAME in str(self) and self.suffix != ".tmp":
            seen["count"] += 1
            if seen["count"] == nth or (persistent and seen["count"] >= nth):
                raise OSError(f"synthetic restore failure on move {seen['count']}")
        return real(self, target)

    return replace


class TestASuccessfulRepublication:
    def test_the_new_set_replaces_the_old_one_completely(
        self, published: tuple[Path, RenderPlan, str, str], media: FakeMedia, tmp_path: Path
    ) -> None:
        directory, _, _, _ = published
        smaller = plan_for(tmp_path, count=1)

        outcome = service().generate(smaller, directory, LATER)

        published_dirs = sorted(
            path.name
            for path in directory.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
        assert published_dirs == [outcome.index.clips[0].directory]

    def test_no_backup_survives(self, published: tuple[Path, RenderPlan, str, str]) -> None:
        directory, plan, _, _ = published

        service().generate(plan, directory, LATER)

        assert not directory.joinpath(ROLLBACK_DIRNAME).exists()


class TestAFailedPublication:
    def test_the_previous_set_comes_back_byte_identical(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        monkeypatch.setattr(render_service, "write_json", fail_on_write(RENDER_INDEX_FILENAME))

        with pytest.raises(OSError, match=RENDER_INDEX_FILENAME):
            service().generate(plan, directory, LATER)

        assert snapshot(directory) == before

    def test_and_the_recovered_set_still_verifies(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Files that merely exist are not the same as a set a later run accepts."""
        directory, plan, fingerprint, digest = published
        monkeypatch.setattr(render_service, "write_json", fail_on_write(RENDER_INDEX_FILENAME))
        with pytest.raises(OSError):
            service().generate(plan, directory, LATER)
        monkeypatch.undo()

        assert verify_clips(directory, fingerprint, digest, plan).clips

    def test_a_failure_while_moving_aside_keeps_everything(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        real = Path.replace
        seen = {"count": 0}

        def refuse_second_move_aside(self: Path, target: Any) -> Any:
            if ROLLBACK_DIRNAME in str(target) and render_service.STAGING_DIRNAME not in str(self):
                seen["count"] += 1
                if seen["count"] == 2:
                    raise OSError("synthetic move-aside failure")
            return real(self, target)

        monkeypatch.setattr(Path, "replace", refuse_second_move_aside)

        with pytest.raises(OSError, match="move-aside"):
            service().generate(plan, directory, LATER)
        monkeypatch.undo()

        assert snapshot(directory) == before
        assert verify_clips(directory, fingerprint, digest, plan).clips

    def test_the_backup_is_deleted_once_the_restore_finishes(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        monkeypatch.setattr(render_service, "write_json", fail_on_write(RENDER_INDEX_FILENAME))

        with pytest.raises(OSError):
            service().generate(plan, directory, LATER)

        assert not directory.joinpath(ROLLBACK_DIRNAME).exists()


class TestAFailedRestore:
    def test_nothing_is_lost_when_the_restore_itself_fails(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        held = recoverable(directory)
        monkeypatch.setattr(render_service, "write_json", fail_on_write(RENDER_INDEX_FILENAME))
        monkeypatch.setattr(Path, "replace", fail_on_restore(1, persistent=True))

        with pytest.raises(RenderError, match=ROLLBACK_DIRNAME):
            service().generate(plan, directory, LATER)
        monkeypatch.undo()

        after = recoverable(directory)
        assert set(held) <= set(after)
        for name, payload in held.items():
            assert after[name] == payload

    def test_the_backup_is_not_deleted(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        monkeypatch.setattr(render_service, "write_json", fail_on_write(RENDER_INDEX_FILENAME))
        monkeypatch.setattr(Path, "replace", fail_on_restore(1, persistent=True))

        with pytest.raises(RenderError):
            service().generate(plan, directory, LATER)
        monkeypatch.undo()

        assert directory.joinpath(ROLLBACK_DIRNAME).is_dir()

    def test_the_error_names_where_the_data_is(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        monkeypatch.setattr(render_service, "write_json", fail_on_write(RENDER_INDEX_FILENAME))
        monkeypatch.setattr(Path, "replace", fail_on_restore(1, persistent=True))

        with pytest.raises(RenderError) as raised:
            service().generate(plan, directory, LATER)
        monkeypatch.undo()

        assert ROLLBACK_DIRNAME in str(raised.value)
        assert "Nothing has been lost" in str(raised.value)


class TestResuming:
    def test_a_later_invocation_finishes_the_restore(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        monkeypatch.setattr(render_service, "write_json", fail_on_write(RENDER_INDEX_FILENAME))
        monkeypatch.setattr(Path, "replace", fail_on_restore(1, persistent=True))
        with pytest.raises(RenderError):
            service().generate(plan, directory, LATER)
        monkeypatch.undo()

        restored = resolve_pending_rollback(directory)

        assert restored is not None
        assert snapshot(directory) == before
        assert verify_clips(directory, fingerprint, digest, plan).clips

    @pytest.mark.parametrize("position", [1, 2])
    def test_a_restore_interrupted_at_each_position_resumes_whole(
        self,
        published: tuple[Path, RenderPlan, str, str],
        monkeypatch: pytest.MonkeyPatch,
        position: int,
    ) -> None:
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        monkeypatch.setattr(render_service, "write_json", fail_on_write(RENDER_INDEX_FILENAME))
        monkeypatch.setattr(Path, "replace", fail_on_restore(position, persistent=True))
        with pytest.raises(RenderError):
            service().generate(plan, directory, LATER)
        monkeypatch.undo()

        resolve_pending_rollback(directory)

        assert snapshot(directory) == before
        assert verify_clips(directory, fingerprint, digest, plan).clips

    def test_a_resume_never_deletes_what_it_has_already_recovered(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect the third journal phase exists for, on directories.

        A restore that stopped after moving one clip directory back leaves the
        published directory holding a recovered item. Resuming as though it were
        a leftover from the failed publication would delete exactly what was
        just rescued -- and it is gone from the backup, having already left it.
        """
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        monkeypatch.setattr(render_service, "write_json", fail_on_write(RENDER_INDEX_FILENAME))
        monkeypatch.setattr(Path, "replace", fail_on_restore(2, persistent=True))
        with pytest.raises(RenderError):
            service().generate(plan, directory, LATER)
        monkeypatch.undo()
        journal = json.loads(
            directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL).read_text("utf-8")
        )
        assert journal["phase"] == "restoring"

        resolve_pending_rollback(directory)

        assert snapshot(directory) == before
        assert verify_clips(directory, fingerprint, digest, plan).clips

    def test_resuming_twice_is_safe(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        monkeypatch.setattr(render_service, "write_json", fail_on_write(RENDER_INDEX_FILENAME))
        monkeypatch.setattr(Path, "replace", fail_on_restore(1, persistent=True))
        with pytest.raises(RenderError):
            service().generate(plan, directory, LATER)
        monkeypatch.undo()

        resolve_pending_rollback(directory)
        assert resolve_pending_rollback(directory) is None

        assert snapshot(directory) == before
        assert verify_clips(directory, fingerprint, digest, plan).clips

    def test_nothing_pending_is_reported_as_nothing(self, tmp_path: Path) -> None:
        assert resolve_pending_rollback(tmp_path.joinpath("clips")) is None

    def test_an_empty_backup_with_no_journal_is_removed(self, tmp_path: Path) -> None:
        directory = tmp_path.joinpath("clips")
        directory.joinpath(ROLLBACK_DIRNAME).mkdir(parents=True)

        assert resolve_pending_rollback(directory) is None
        assert not directory.joinpath(ROLLBACK_DIRNAME).exists()


class TestARefusedBackup:
    def strand(
        self,
        directory: Path,
        plan: RenderPlan,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(render_service, "write_json", fail_on_write(RENDER_INDEX_FILENAME))
        monkeypatch.setattr(Path, "replace", fail_on_restore(1, persistent=True))
        with pytest.raises(RenderError):
            service().generate(plan, directory, LATER)
        monkeypatch.undo()

    def test_a_backup_with_no_journal_is_left_untouched(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        self.strand(directory, plan, monkeypatch)
        held = recoverable(directory)
        directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL).unlink()

        with pytest.raises(RenderError, match=ROLLBACK_JOURNAL):
            resolve_pending_rollback(directory)

        assert recoverable(directory) == held

    def test_a_journal_of_another_schema_is_left_untouched(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        self.strand(directory, plan, monkeypatch)
        held = recoverable(directory)
        directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL).write_text(
            json.dumps({"schema_version": 99, "phase": "placing"}), encoding="utf-8"
        )

        with pytest.raises(RenderError, match="schema"):
            resolve_pending_rollback(directory)

        assert recoverable(directory) == held

    def test_a_journal_naming_an_unknown_phase_is_left_untouched(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        self.strand(directory, plan, monkeypatch)
        held = recoverable(directory)
        directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL).write_text(
            json.dumps({"schema_version": 1, "phase": "halfway"}), encoding="utf-8"
        )

        with pytest.raises(RenderError, match="halfway"):
            resolve_pending_rollback(directory)

        assert recoverable(directory) == held

    def test_an_unreadable_journal_is_left_untouched(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        self.strand(directory, plan, monkeypatch)
        held = recoverable(directory)
        directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL).write_text(
            "not json at all", encoding="utf-8"
        )

        with pytest.raises(RenderError, match="cannot be read"):
            resolve_pending_rollback(directory)

        assert recoverable(directory) == held

    def test_a_journal_holding_a_list_is_left_untouched(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        self.strand(directory, plan, monkeypatch)
        directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL).write_text("[]", encoding="utf-8")

        with pytest.raises(RenderError, match="rollback journal"):
            resolve_pending_rollback(directory)

    def test_a_new_publication_refuses_to_start_over_a_pending_backup(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, _, _ = published
        self.strand(directory, plan, monkeypatch)
        directory.joinpath(ROLLBACK_DIRNAME, ROLLBACK_JOURNAL).unlink()

        with pytest.raises(RenderError, match=ROLLBACK_JOURNAL):
            service().generate(plan, directory, LATER)


class TestTheJournalWriteItself:
    def test_a_journal_that_cannot_be_written_leaves_no_backup_behind(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The journal precedes every move, so failing it has nothing to strand."""
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        monkeypatch.setattr(publication, "write_json", fail_on_write(ROLLBACK_JOURNAL))

        with pytest.raises(OSError, match=ROLLBACK_JOURNAL):
            service().generate(plan, directory, LATER)
        monkeypatch.undo()

        assert not directory.joinpath(ROLLBACK_DIRNAME).exists()
        assert snapshot(directory) == before
        assert verify_clips(directory, fingerprint, digest, plan).clips


class TestOwnership:
    def test_a_directory_that_is_not_a_clip_is_never_moved_aside(
        self, published: tuple[Path, RenderPlan, str, str]
    ) -> None:
        directory, plan, _, _ = published
        stray = directory.joinpath("operator-notes")
        stray.mkdir()
        stray.joinpath("note.txt").write_text("keep me", encoding="utf-8")

        service().generate(plan, directory, LATER)

        assert stray.joinpath("note.txt").read_text(encoding="utf-8") == "keep me"

    def test_a_stale_clip_directory_is_deleted_on_republication(
        self, published: tuple[Path, RenderPlan, str, str], tmp_path: Path
    ) -> None:
        """Deleting a *directory* is the one step generalisation had to change."""
        directory, _, _, _ = published
        smaller = plan_for(tmp_path, count=1)
        kept = smaller.target.clips[0].candidate.id

        service().generate(smaller, directory, LATER)

        remaining = sorted(
            path.name
            for path in directory.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
        assert remaining == [f"clip_{kept}"]
        assert not directory.joinpath(".rollback").exists()

    def test_an_interrupted_publication_deletes_only_the_new_clip_directories(
        self, published: tuple[Path, RenderPlan, str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory, plan, fingerprint, digest = published
        before = snapshot(directory)
        monkeypatch.setattr(render_service, "write_json", fail_on_write(RENDER_INDEX_FILENAME))

        with pytest.raises(OSError):
            service().generate(plan, directory, LATER)
        monkeypatch.undo()

        assert snapshot(directory) == before
        # Every surviving file belongs to a clip directory or is one of the two
        # stage artifacts: the failed attempt's directories were deleted whole
        # and none of the previous set's files were left behind beside them.
        for name in snapshot(directory):
            assert name.startswith("clip_") or "/" not in name
        assert verify_clips(directory, fingerprint, digest, plan).clips
