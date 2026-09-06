"""Publishing a preview set is all-or-nothing (CE-034).

Generation was already transactional: everything is encoded and probed in a
staging directory, so a failed encode never touched the published set. The
*publication* was not. It removed stale previews, then replaced files one at a
time, then wrote the two artifacts -- so a failure anywhere after the first
`unlink` or the first `replace` left the directory holding a mixture of two
runs, with `index.json` describing neither.

That is the worst possible outcome for this stage. A failed `--force` is
supposed to be free: the previous previews are still there and still verifiable.
A half-published set destroys work that took twenty-six seconds to produce and
leaves nothing that can be verified at all.

Every test here injects a failure at one specific step of publication and then
demands two things of the previous set: byte-identical files, and a successful
`verify_previews` against the fingerprint recorded before the attempt. The
second is what makes the first meaningful -- files that merely still exist are
not the same as a set a later run will accept.
"""

from __future__ import annotations

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
    STAGING_DIRNAME,
    PreviewPlan,
    PreviewService,
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
        if ROLLBACK_DIRNAME in str(target) and STAGING_DIRNAME not in str(self):
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
