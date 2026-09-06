"""CE-035 to CE-039 at the command boundary: `content-engine review RUN_ID`.

Every test here drives the real prompt loop through stdin. What is being held is
that the session never invents a decision, never loses one, and never lets the
manifest claim more than the person actually decided.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner, Result

from content_engine import cli
from content_engine.config import Settings
from content_engine.domain.enums import EditorialReason, RunStage, RunStatus
from content_engine.domain.exceptions import (
    EXIT_INVALID_INPUT,
    EXIT_SUCCESS,
)
from content_engine.domain.preview_rules import preview_filename
from content_engine.domain.review import DECISIONS_SCHEMA_VERSION
from content_engine.services.review_service import DECISIONS_FILENAME
from tests.conftest import Analysed, FakeMedia, Harness, analyse, cli_output

runner = CliRunner()


@pytest.fixture
def previewed(analysed: Analysed) -> Analysed:
    result = runner.invoke(cli.app, ["preview", analysed.run_id])
    assert result.exit_code == EXIT_SUCCESS, cli_output(result)
    assert analysed.manifest()["status"] == RunStatus.READY_FOR_REVIEW
    return analysed


def review(run_id: str, keys: str = "", *arguments: str) -> Result:
    return runner.invoke(cli.app, ["review", run_id, *arguments], input=keys)


def decisions_of(run: Analysed) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = run.decisions()["decisions"]
    return payload


class TestGuards:
    def test_an_unknown_run_is_refused(self, media: FakeMedia, settings: Settings) -> None:
        result = review("nope")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "Run not found" in cli_output(result)

    def test_a_run_without_previews_is_refused(self, analysed: Analysed) -> None:
        result = review(analysed.run_id, "a\n")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "preview" in cli_output(result).lower()
        assert analysed.manifest()["status"] == RunStatus.ANALYZED

    def test_a_run_that_has_not_been_analysed_is_refused(
        self, harness: Harness, media: FakeMedia
    ) -> None:
        result = review(harness.run_id, "a\n")
        assert result.exit_code == EXIT_INVALID_INPUT

    def test_a_missing_preview_file_is_refused(self, previewed: Analysed) -> None:
        next(iter(previewed.previews.glob("*.mp4"))).unlink()
        result = review(previewed.run_id, "a\n")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert not previewed.review.joinpath(DECISIONS_FILENAME).exists()


class TestPresentation:
    def test_every_required_field_is_shown(self, previewed: Analysed) -> None:
        candidate = previewed.candidates()[0]
        output = cli_output(review(previewed.run_id, "q\n"))
        assert candidate["id"] in output
        assert str(candidate["topic"]) in output
        assert str(candidate["category"]) in output
        assert str(candidate["hook"]) in output
        assert str(candidate["summary"]) in output
        assert str(candidate["reason"]) in output
        assert f"{float(candidate['total_score']):.2f}" in output
        assert preview_filename(str(candidate["id"])) in output

    def test_the_component_scores_are_shown(self, previewed: Analysed) -> None:
        scores = previewed.candidates()[0]["scores"]
        assert isinstance(scores, dict)
        output = cli_output(review(previewed.run_id, "q\n")).lower()
        for label in ("hook", "value", "context", "clarity", "engagement", "relevance"):
            assert label in output
        for value in scores.values():
            assert str(value) in output

    def test_position_and_rank_are_shown(self, previewed: Analysed) -> None:
        output = cli_output(review(previewed.run_id, "q\n"))
        assert "1/2" in output
        assert "rank 1" in output.lower()

    def test_the_interval_and_duration_are_shown(self, previewed: Analysed) -> None:
        candidate = previewed.candidates()[0]
        output = cli_output(review(previewed.run_id, "q\n"))
        assert f"{float(candidate['start']):.2f}" in output
        assert f"{float(candidate['end']):.2f}" in output
        assert f"{float(candidate['duration']):.2f}" in output

    def test_the_action_menu_is_shown(self, previewed: Analysed) -> None:
        output = cli_output(review(previewed.run_id, "q\n"))
        for key in ("[A]", "[R]", "[E]", "[S]", "[Q]"):
            assert key in output

    @pytest.mark.parametrize("columns", ["40", "200"])
    def test_the_session_works_at_any_terminal_width(
        self, previewed: Analysed, monkeypatch: pytest.MonkeyPatch, columns: str
    ) -> None:
        monkeypatch.setenv("COLUMNS", columns)
        result = review(previewed.run_id, "a\na\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert previewed.manifest()["status"] == RunStatus.REVIEWED


class TestApprove:
    def test_an_approval_is_persisted_immediately(self, previewed: Analysed) -> None:
        candidate = previewed.candidates()[0]
        review(previewed.run_id, "a\nq\n")
        decisions = decisions_of(previewed)
        assert len(decisions) == 1
        assert decisions[0]["candidate_id"] == candidate["id"]
        assert decisions[0]["decision"] == "approved"
        assert decisions[0]["final_start"] == candidate["start"]
        assert decisions[0]["final_end"] == candidate["end"]

    def test_the_original_bounds_are_preserved(self, previewed: Analysed) -> None:
        candidate = previewed.candidates()[0]
        review(previewed.run_id, "a\nq\n")
        decision = decisions_of(previewed)[0]
        assert decision["original_start"] == candidate["start"]
        assert decision["original_end"] == candidate["end"]

    def test_an_approval_carries_no_rejection_reason(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\nq\n")
        assert "reason" not in decisions_of(previewed)[0]

    def test_the_timestamp_is_utc(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\nq\n")
        assert str(decisions_of(previewed)[0]["reviewed_at"]).endswith("Z")

    def test_a_lowercase_and_an_uppercase_key_both_work(self, previewed: Analysed) -> None:
        result = review(previewed.run_id, "A\na\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert len(decisions_of(previewed)) == 2


class TestReject:
    def test_a_rejection_without_a_reason_is_allowed(self, previewed: Analysed) -> None:
        review(previewed.run_id, "r\n\nq\n")
        decision = decisions_of(previewed)[0]
        assert decision["decision"] == "rejected"
        assert decision["reason"] is None

    def test_a_rejection_has_no_final_bounds(self, previewed: Analysed) -> None:
        review(previewed.run_id, "r\n\nq\n")
        decision = decisions_of(previewed)[0]
        assert "final_start" not in decision
        assert "final_end" not in decision

    def test_a_reason_can_be_chosen_by_name(self, previewed: Analysed) -> None:
        review(previewed.run_id, "r\nweak_hook\nq\n")
        assert decisions_of(previewed)[0]["reason"] == EditorialReason.WEAK_HOOK.value

    def test_a_reason_can_be_chosen_by_number(self, previewed: Analysed) -> None:
        review(previewed.run_id, "r\n1\nq\n")
        assert decisions_of(previewed)[0]["reason"] == list(EditorialReason)[0].value

    def test_an_unknown_reason_is_rejected_and_asked_again(self, previewed: Analysed) -> None:
        result = review(previewed.run_id, "r\nnot_funny\nduplicate\nq\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "not_funny" in cli_output(result)
        assert decisions_of(previewed)[0]["reason"] == EditorialReason.DUPLICATE.value

    def test_an_over_long_detail_is_asked_again_rather_than_crashing(
        self, previewed: Analysed
    ) -> None:
        """The model caps a detail at 2000 characters; the loop must handle that.

        A ValidationError escaping the prompt reached the CLI's last-resort
        handler and exited 1 as an unexpected internal error, after the session
        had already saved an earlier decision -- so the reviewer saw a crash and
        no indication of whether their work had survived.
        """
        keys = "\n".join(["a", "r", "other", "x" * 2001, "se corta el audio"]) + "\n"
        result = review(previewed.run_id, keys)
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        output = cli_output(result).lower()
        assert "unexpected error" not in output
        assert "2000" in output
        decisions = decisions_of(previewed)
        assert len(decisions) == 2, "the approval taken before the bad input must survive"
        assert decisions[0]["decision"] == "approved"
        assert decisions[1]["detail"] == "se corta el audio"

    def test_a_detail_of_exactly_the_limit_is_accepted(self, previewed: Analysed) -> None:
        limit = "y" * 2000
        result = review(previewed.run_id, "\n".join(["r", "other", limit, "q"]) + "\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert decisions_of(previewed)[0]["detail"] == limit

    def test_other_asks_for_a_detail(self, previewed: Analysed) -> None:
        review(previewed.run_id, "r\nother\nthe audio clips\nq\n")
        decision = decisions_of(previewed)[0]
        assert decision["reason"] == EditorialReason.OTHER.value
        assert decision["detail"] == "the audio clips"

    def test_a_blank_detail_for_other_is_asked_again(self, previewed: Analysed) -> None:
        result = review(previewed.run_id, "r\nother\n\nthe audio clips\nq\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert decisions_of(previewed)[0]["detail"] == "the audio clips"

    def test_a_named_reason_does_not_ask_for_a_detail(self, previewed: Analysed) -> None:
        """The next line is consumed by the next candidate, not by a detail prompt."""
        result = review(previewed.run_id, "r\nduplicate\na\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        kinds = [decision["decision"] for decision in decisions_of(previewed)]
        assert kinds == ["rejected", "approved"]


class TestEdit:
    def test_an_edit_records_both_intervals(self, previewed: Analysed) -> None:
        candidate = previewed.candidates()[0]
        start = float(candidate["start"]) + 1.5
        end = float(candidate["end"]) - 1.5
        review(previewed.run_id, f"e\n{start}\n{end}\nq\n")
        decision = decisions_of(previewed)[0]
        assert decision["decision"] == "edited"
        assert decision["original_start"] == candidate["start"]
        assert decision["original_end"] == candidate["end"]
        assert decision["final_start"] == start
        assert decision["final_end"] == end

    def test_a_blank_answer_keeps_the_current_bound(self, previewed: Analysed) -> None:
        candidate = previewed.candidates()[0]
        end = float(candidate["end"]) - 2.0
        review(previewed.run_id, f"e\n\n{end}\nq\n")
        decision = decisions_of(previewed)[0]
        assert decision["final_start"] == candidate["start"]
        assert decision["final_end"] == end

    def test_an_edit_that_changes_nothing_is_asked_again(self, previewed: Analysed) -> None:
        candidate = previewed.candidates()[0]
        end = float(candidate["end"]) - 2.0
        result = review(previewed.run_id, f"e\n\n\n\n{end}\nq\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "differ" in cli_output(result).lower()
        assert decisions_of(previewed)[0]["final_end"] == end

    @pytest.mark.parametrize("bad", ["abc", "nan", "inf", "-5"])
    def test_an_unparseable_or_impossible_bound_is_asked_again(
        self, previewed: Analysed, bad: str
    ) -> None:
        candidate = previewed.candidates()[0]
        end = float(candidate["end"]) - 2.0
        result = review(previewed.run_id, f"e\n{bad}\n\n{end}\nq\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert decisions_of(previewed)[0]["final_end"] == end

    def test_an_inverted_interval_is_asked_again(self, previewed: Analysed) -> None:
        candidate = previewed.candidates()[0]
        end = float(candidate["end"]) - 2.0
        result = review(
            previewed.run_id, f"e\n{candidate['end']}\n{candidate['start']}\n\n{end}\nq\n"
        )
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert decisions_of(previewed)[0]["final_end"] == end

    def test_an_edit_beyond_the_source_is_asked_again(self, previewed: Analysed) -> None:
        candidate = previewed.candidates()[0]
        end = float(candidate["end"]) - 2.0
        result = review(previewed.run_id, f"e\n\n99999\n\n{end}\nq\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "source" in cli_output(result).lower()
        assert decisions_of(previewed)[0]["final_end"] == end

    def test_an_edit_shorter_than_the_analyzer_minimum_is_accepted(
        self, previewed: Analysed
    ) -> None:
        candidate = previewed.candidates()[0]
        start = float(candidate["start"])
        result = review(previewed.run_id, f"e\n\n{start + 3.0}\nq\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert decisions_of(previewed)[0]["final_end"] == start + 3.0


class TestSkipQuitAndInterruption:
    def test_skip_records_no_decision(self, previewed: Analysed) -> None:
        result = review(previewed.run_id, "s\ns\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert not previewed.review.joinpath(DECISIONS_FILENAME).exists()

    def test_skip_leaves_the_run_short_of_reviewed(self, previewed: Analysed) -> None:
        review(previewed.run_id, "s\ns\n")
        assert previewed.manifest()["status"] == RunStatus.READY_FOR_REVIEW

    def test_quit_keeps_what_was_already_decided(self, previewed: Analysed) -> None:
        result = review(previewed.run_id, "a\nq\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert len(decisions_of(previewed)) == 1
        assert previewed.manifest()["status"] == RunStatus.READY_FOR_REVIEW

    def test_quit_is_not_a_failure(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\nq\n")
        manifest = previewed.manifest()
        assert manifest["failure"] is None
        assert manifest["status"] != RunStatus.FAILED_REVIEW

    def test_end_of_input_keeps_what_was_decided(self, previewed: Analysed) -> None:
        result = review(previewed.run_id, "a\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert len(decisions_of(previewed)) == 1
        assert previewed.manifest()["status"] == RunStatus.READY_FOR_REVIEW

    def test_end_of_input_with_nothing_decided_writes_nothing(self, previewed: Analysed) -> None:
        result = review(previewed.run_id, "")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert not previewed.review.joinpath(DECISIONS_FILENAME).exists()

    def test_a_keyboard_interrupt_keeps_what_was_decided(
        self, previewed: Analysed, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answers = iter(["a"])

        def interrupt(*_: object) -> str:
            try:
                return next(answers)
            except StopIteration:
                raise KeyboardInterrupt from None

        monkeypatch.setattr(cli, "_read_line", interrupt)
        result = review(previewed.run_id)
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert len(decisions_of(previewed)) == 1
        assert previewed.manifest()["status"] == RunStatus.READY_FOR_REVIEW
        assert previewed.manifest()["failure"] is None

    def test_an_unknown_key_is_asked_again(self, previewed: Analysed) -> None:
        result = review(previewed.run_id, "x\na\na\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "x" in cli_output(result).lower()
        assert len(decisions_of(previewed)) == 2


class TestCompletion:
    def test_deciding_everything_reaches_reviewed(self, previewed: Analysed) -> None:
        result = review(previewed.run_id, "a\nr\n\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert previewed.manifest()["status"] == RunStatus.REVIEWED

    def test_the_review_stage_is_recorded(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\na\n")
        record = previewed.manifest()["stages"][RunStage.REVIEW.value]
        assert len(record["fingerprint"]) == 64
        assert record["schema_version"] == DECISIONS_SCHEMA_VERSION

    def test_the_effective_configuration_is_written(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\na\n")
        assert previewed.review.joinpath("config.effective.json").is_file()

    def test_it_summarises_the_session(self, previewed: Analysed) -> None:
        output = cli_output(review(previewed.run_id, "a\nr\n\n"))
        assert "1 approved" in output
        assert "1 rejected" in output

    def test_a_finished_review_is_not_asked_again(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\na\n")
        before = previewed.decisions()
        result = review(previewed.run_id, "a\na\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "already" in cli_output(result).lower()
        assert previewed.decisions() == before

    def test_nothing_is_rewritten_when_the_review_is_complete(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\na\n")
        before = previewed.review.joinpath(DECISIONS_FILENAME).read_bytes()
        manifest = previewed.run_path.joinpath("manifest.json").read_bytes()
        review(previewed.run_id, "")
        assert previewed.review.joinpath(DECISIONS_FILENAME).read_bytes() == before
        assert previewed.run_path.joinpath("manifest.json").read_bytes() == manifest


class TestResume:
    def test_a_later_session_only_asks_the_pending_candidates(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\nq\n")
        first = previewed.candidates()[0]
        second = previewed.candidates()[1]
        output = cli_output(review(previewed.run_id, "q\n"))
        assert str(second["id"]) in output
        assert str(first["id"]) not in output

    def test_a_skipped_candidate_comes_back(self, previewed: Analysed) -> None:
        review(previewed.run_id, "s\nq\n")
        output = cli_output(review(previewed.run_id, "q\n"))
        assert str(previewed.candidates()[0]["id"]) in output

    def test_the_session_reports_what_is_left(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\nq\n")
        assert "1 of 2" in cli_output(review(previewed.run_id, "q\n"))

    def test_resuming_completes_the_run(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\nq\n")
        result = review(previewed.run_id, "a\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert previewed.manifest()["status"] == RunStatus.REVIEWED
        assert len(decisions_of(previewed)) == 2


class TestIntegrityOfExistingDecisions:
    def test_decisions_from_another_analysis_are_refused(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\nq\n")
        path = previewed.review.joinpath(DECISIONS_FILENAME)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["analysis_fingerprint"] = "z" * 64
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = review(previewed.run_id, "a\n")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "--force" in cli_output(result)

    def test_corrupt_decisions_are_refused_rather_than_reinterpreted(
        self, previewed: Analysed
    ) -> None:
        review(previewed.run_id, "a\nq\n")
        previewed.review.joinpath(DECISIONS_FILENAME).write_text("{oops", encoding="utf-8")
        result = review(previewed.run_id, "a\n")
        assert result.exit_code == EXIT_INVALID_INPUT

    def test_a_refusal_does_not_touch_the_file(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\nq\n")
        path = previewed.review.joinpath(DECISIONS_FILENAME)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["analysis_fingerprint"] = "z" * 64
        broken = json.dumps(payload)
        path.write_text(broken, encoding="utf-8")
        review(previewed.run_id, "a\n")
        assert path.read_text(encoding="utf-8") == broken

    def test_a_decision_for_a_candidate_that_no_longer_exists_is_refused(
        self, previewed: Analysed
    ) -> None:
        review(previewed.run_id, "a\nq\n")
        path = previewed.review.joinpath(DECISIONS_FILENAME)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["decisions"][0]["candidate_id"] = "cand_ghost"
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = review(previewed.run_id, "a\n")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "cand_ghost" in cli_output(result)


class TestForce:
    def test_it_warns_before_discarding_decisions(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\nq\n")
        output = cli_output(review(previewed.run_id, "q\n", "--force"))
        assert "1" in output
        assert "discard" in output.lower()

    def test_it_starts_the_session_again_from_the_first_candidate(
        self, previewed: Analysed
    ) -> None:
        review(previewed.run_id, "a\nq\n")
        output = cli_output(review(previewed.run_id, "q\n", "--force"))
        assert str(previewed.candidates()[0]["id"]) in output

    def test_it_replaces_the_previous_decisions(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\na\n")
        review(previewed.run_id, "r\n\nr\n\n", "--force")
        kinds = [decision["decision"] for decision in decisions_of(previewed)]
        assert kinds == ["rejected", "rejected"]

    def test_it_reopens_a_reviewed_run(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\na\n")
        assert previewed.manifest()["status"] == RunStatus.REVIEWED
        review(previewed.run_id, "q\n", "--force")
        assert previewed.manifest()["status"] == RunStatus.READY_FOR_REVIEW

    def test_abandoning_a_forced_session_does_not_strand_the_run(self, previewed: Analysed) -> None:
        """--force reopens the review; quitting before deciding must not trap it.

        The decisions on disk are still the complete set, because --force writes
        nothing until the first new decision. The run is READY_FOR_REVIEW
        because it was reopened. A later ordinary session therefore finds a
        finished review over a run whose status does not say so, and has to
        settle it rather than reporting "already reviewed" while the run stays
        open forever.
        """
        review(previewed.run_id, "a\na\n")
        assert previewed.manifest()["status"] == RunStatus.REVIEWED
        finished = previewed.decisions()
        stage = previewed.manifest()["stages"][RunStage.REVIEW.value]

        review(previewed.run_id, "q\n", "--force")
        assert previewed.manifest()["status"] == RunStatus.READY_FOR_REVIEW
        assert previewed.decisions() == finished, "an abandoned --force must keep the decisions"

        result = review(previewed.run_id, "")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert previewed.manifest()["status"] == RunStatus.REVIEWED
        assert previewed.decisions() == finished
        recovered = previewed.manifest()["stages"][RunStage.REVIEW.value]
        assert recovered["fingerprint"] == stage["fingerprint"]
        assert recovered["stage_config_sha256"] == stage["stage_config_sha256"]
        assert recovered["schema_version"] == stage["schema_version"]

    def test_the_recovery_says_what_it_settled(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\na\n")
        review(previewed.run_id, "q\n", "--force")
        output = cli_output(review(previewed.run_id, "")).lower()
        assert "recovered" in output
        assert str(RunStatus.READY_FOR_REVIEW).lower() in output
        assert str(RunStatus.REVIEWED).lower() in output

    def test_a_reopened_run_whose_decisions_were_removed_stays_open(
        self, previewed: Analysed
    ) -> None:
        """Recovery settles a complete set. An absent one is a session to run."""
        review(previewed.run_id, "a\na\n")
        review(previewed.run_id, "q\n", "--force")
        previewed.review.joinpath(DECISIONS_FILENAME).unlink()
        result = review(previewed.run_id, "q\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert previewed.manifest()["status"] == RunStatus.READY_FOR_REVIEW

    def test_a_reopened_run_with_incoherent_decisions_is_refused_not_recovered(
        self, previewed: Analysed
    ) -> None:
        review(previewed.run_id, "a\na\n")
        review(previewed.run_id, "q\n", "--force")
        path = previewed.review.joinpath(DECISIONS_FILENAME)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["analysis_fingerprint"] = "z" * 64
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = review(previewed.run_id, "")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert previewed.manifest()["status"] == RunStatus.READY_FOR_REVIEW

    def test_a_partly_decided_reopened_run_is_not_recovered(self, previewed: Analysed) -> None:
        """Only a complete set settles the run; one pending candidate does not."""
        review(previewed.run_id, "a\na\n")
        review(previewed.run_id, "r\n\nq\n", "--force")
        assert len(decisions_of(previewed)) == 1
        result = review(previewed.run_id, "q\n")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert previewed.manifest()["status"] == RunStatus.READY_FOR_REVIEW

    def test_it_recovers_from_corrupt_decisions(self, previewed: Analysed) -> None:
        review(previewed.run_id, "a\nq\n")
        previewed.review.joinpath(DECISIONS_FILENAME).write_text("{oops", encoding="utf-8")
        result = review(previewed.run_id, "a\na\n", "--force")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert len(decisions_of(previewed)) == 2


class TestZeroCandidates:
    @pytest.fixture
    def nothing_selected(self, harness: Harness, media: FakeMedia) -> Analysed:
        from content_engine.adapters.analysis.fixture_analyzer import FixtureBatch
        from tests.conftest import analysis_fixture, weak_candidate, write_fixture

        write_fixture(
            harness.fixture_path,
            analysis_fixture(
                [
                    FixtureBatch(
                        chunk_id="chunk_0000",
                        raw_response='{"candidates": []}',
                        candidates=[weak_candidate(10.0, 39.0)],
                    )
                ]
            ),
        )
        assert analyse(harness).exit_code == EXIT_SUCCESS
        run = Analysed(harness, media)
        assert runner.invoke(cli.app, ["preview", run.run_id]).exit_code == EXIT_SUCCESS
        return run

    def test_there_is_nothing_to_decide_and_the_run_is_reviewed(
        self, nothing_selected: Analysed
    ) -> None:
        result = review(nothing_selected.run_id, "")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "no candidates" in cli_output(result).lower()
        assert nothing_selected.manifest()["status"] == RunStatus.REVIEWED

    def test_an_empty_decision_collection_is_written(self, nothing_selected: Analysed) -> None:
        review(nothing_selected.run_id, "")
        assert decisions_of(nothing_selected) == []
