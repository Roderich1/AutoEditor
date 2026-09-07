"""CE-040: absolute transcript words become clip-local subtitle cues.

The builder is the one place where a timestamp changes meaning, so these tests
are mostly about arithmetic and boundaries rather than about text. A word that
straddles the clip edge, a word that lands exactly on it, a gap that should
break a cue and a rounding step that must not collapse one -- each of those is a
way a subtitle drifts out of sync with the picture, and none of them is visible
in a passing render.
"""

from __future__ import annotations

import pytest

from content_engine.domain.models import TranscriptWord
from content_engine.domain.subtitles import (
    DEFAULT_SUBTITLE_RULES,
    SubtitleRules,
    build_cues,
)


def word(text: str, start: float, end: float) -> TranscriptWord:
    return TranscriptWord(word=text, start=start, end=end, probability=0.9)


def spoken(texts: list[str], start: float = 0.0, step: float = 0.4) -> list[TranscriptWord]:
    """Evenly spaced words, one every ``step`` seconds with no gap between them."""
    return [
        word(text, start + index * step, start + (index + 1) * step)
        for index, text in enumerate(texts)
    ]


class TestWordSelection:
    def test_words_outside_the_interval_are_not_selected(self) -> None:
        words = [word("antes", 0.0, 1.0), word("dentro", 12.0, 12.5), word("despues", 30.0, 31.0)]

        cues = build_cues(words, 10.0, 20.0)

        assert [cue.text for cue in cues] == ["dentro"]

    def test_a_word_that_straddles_the_start_is_clamped(self) -> None:
        cues = build_cues([word("cruzando", 9.0, 11.0)], 10.0, 20.0)

        assert len(cues) == 1
        assert cues[0].start == pytest.approx(0.0)
        assert cues[0].end == pytest.approx(1.0)

    def test_a_word_that_straddles_the_end_is_clamped(self) -> None:
        cues = build_cues([word("cruzando", 19.0, 25.0)], 10.0, 20.0)

        assert cues[0].start == pytest.approx(9.0)
        assert cues[0].end == pytest.approx(10.0)

    def test_a_word_ending_exactly_at_the_start_is_excluded(self) -> None:
        """A zero-length intersection is not an intersection."""
        assert build_cues([word("justo", 8.0, 10.0)], 10.0, 20.0) == []

    def test_a_word_starting_exactly_at_the_end_is_excluded(self) -> None:
        assert build_cues([word("justo", 20.0, 22.0)], 10.0, 20.0) == []

    def test_a_word_covering_the_whole_clip_is_clamped_to_it(self) -> None:
        cues = build_cues([word("larga", 0.0, 100.0)], 10.0, 20.0)

        assert cues[0].start == pytest.approx(0.0)
        assert cues[0].end == pytest.approx(10.0)

    def test_whitespace_only_words_are_dropped(self) -> None:
        words = [word("  ", 10.0, 10.4), word("real", 10.5, 11.0)]

        assert [cue.text for cue in build_cues(words, 10.0, 20.0)] == ["real"]

    def test_no_words_in_the_interval_produce_no_cues(self) -> None:
        """Silence is a legitimate clip, not a failure."""
        assert build_cues([word("lejos", 0.0, 1.0)], 10.0, 20.0) == []

    def test_an_empty_word_list_produces_no_cues(self) -> None:
        assert build_cues([], 10.0, 20.0) == []


class TestLocalTimes:
    def test_times_are_absolute_minus_the_clip_start(self) -> None:
        cues = build_cues(spoken(["uno", "dos"], start=100.0), 100.0, 110.0)

        assert cues[0].start == pytest.approx(0.0)
        assert cues[0].end == pytest.approx(0.8)

    def test_every_cue_is_inside_the_clip_and_ordered(self) -> None:
        words = spoken([f"w{index}" for index in range(40)], start=5.0, step=0.5)

        cues = build_cues(words, 5.0, 25.0)

        assert cues
        previous_end = 0.0
        for cue in cues:
            assert cue.start >= 0.0
            assert cue.start >= previous_end - 1e-9
            assert cue.end > cue.start
            assert cue.end <= 20.0 + 1e-9
            previous_end = cue.end

    def test_cue_indices_are_contiguous_from_one(self) -> None:
        cues = build_cues(spoken([f"w{index}" for index in range(30)]), 0.0, 20.0)

        assert [cue.index for cue in cues] == list(range(1, len(cues) + 1))


class TestGrouping:
    def test_a_cue_holds_at_most_the_configured_word_count(self) -> None:
        cues = build_cues(spoken([f"w{index}" for index in range(24)]), 0.0, 20.0)

        assert cues
        for cue in cues:
            assert len(cue.text.split()) <= DEFAULT_SUBTITLE_RULES.max_words_per_cue

    def test_a_cue_never_exceeds_two_lines(self) -> None:
        cues = build_cues(spoken(["palabra"] * 24), 0.0, 20.0)

        for cue in cues:
            assert 1 <= len(cue.lines) <= DEFAULT_SUBTITLE_RULES.max_lines

    def test_a_sentence_end_breaks_the_cue_even_when_it_is_short(self) -> None:
        cues = build_cues(spoken(["hola.", "adios"]), 0.0, 20.0)

        assert [cue.text for cue in cues] == ["hola.", "adios"]

    def test_a_question_mark_breaks_the_cue(self) -> None:
        cues = build_cues(spoken(["listo?", "vamos"]), 0.0, 20.0)

        assert [cue.text for cue in cues] == ["listo?", "vamos"]

    def test_a_pause_breaks_the_cue(self) -> None:
        words = [word("antes", 0.0, 0.4), word("despues", 2.0, 2.4)]

        cues = build_cues(words, 0.0, 20.0)

        assert [cue.text for cue in cues] == ["antes", "despues"]

    def test_a_gap_shorter_than_the_pause_does_not_break_the_cue(self) -> None:
        words = [word("antes", 0.0, 0.4), word("despues", 0.6, 1.0)]

        assert [cue.text for cue in build_cues(words, 0.0, 20.0)] == ["antes despues"]

    def test_a_comma_breaks_only_once_the_cue_is_long_enough(self) -> None:
        early = build_cues(spoken(["uno,", "dos", "tres"]), 0.0, 20.0)
        assert [cue.text for cue in early] == ["uno, dos tres"]

        late = build_cues(spoken(["a", "b", "c", "d", "e", "f,", "g", "h"]), 0.0, 20.0)
        assert [cue.text for cue in late] == ["a b c d e f,", "g h"]

    def test_a_cue_never_exceeds_the_character_budget(self) -> None:
        rules = DEFAULT_SUBTITLE_RULES
        budget = rules.max_lines * rules.max_chars_per_line
        cues = build_cues(spoken(["doceletras12"] * 12), 0.0, 20.0)

        assert cues
        for cue in cues:
            assert len(cue.text) <= budget

    def test_a_single_word_longer_than_a_line_is_kept_whole(self) -> None:
        """Breaking a word would corrupt the text; overflowing one line will not."""
        long_word = "x" * 90
        cues = build_cues([word(long_word, 0.0, 1.0)], 0.0, 20.0)

        assert [cue.text for cue in cues] == [long_word]
        assert cues[0].lines == [long_word]


class TestLineWrapping:
    def test_a_short_cue_is_one_line(self) -> None:
        cues = build_cues(spoken(["hola", "mundo"]), 0.0, 20.0)

        assert cues[0].lines == ["hola mundo"]

    def test_a_long_cue_is_split_into_two_balanced_lines(self) -> None:
        cues = build_cues(spoken(["doceletras12"] * 6), 0.0, 20.0)

        assert len(cues) == 1
        assert len(cues[0].lines) == 2
        first, second = cues[0].lines
        assert first.split() + second.split() == ["doceletras12"] * 6
        assert abs(len(first) - len(second)) <= 12

    def test_neither_line_exceeds_the_limit_when_it_can_be_avoided(self) -> None:
        cues = build_cues(spoken(["doceletras12"] * 6), 0.0, 20.0)

        for line in cues[0].lines:
            assert len(line) <= DEFAULT_SUBTITLE_RULES.max_chars_per_line


class TestMinimumDuration:
    def test_a_zero_length_word_becomes_a_visible_cue(self) -> None:
        cues = build_cues([word("instante", 5.0, 5.0)], 0.0, 20.0)

        assert len(cues) == 1
        assert cues[0].end - cues[0].start >= DEFAULT_SUBTITLE_RULES.min_cue_seconds

    def test_extension_never_reaches_past_the_clip(self) -> None:
        cues = build_cues([word("final", 19.99, 20.0)], 0.0, 20.0)

        assert cues[0].end <= 20.0

    def test_extension_never_overlaps_the_following_cue(self) -> None:
        words = [word("a.", 0.0, 0.01), word("b", 0.05, 0.06), word("c", 5.0, 5.4)]

        cues = build_cues(words, 0.0, 20.0)

        for earlier, later in zip(cues, cues[1:], strict=False):
            assert earlier.end <= later.start + 1e-9

    def test_a_cue_that_cannot_be_made_visible_is_dropped(self) -> None:
        """A clip too short to show anything produces no cue rather than a zero-length one."""
        assert build_cues([word("x", 0.0, 0.0)], 0.0, 0.0001) == []


class TestCustomRules:
    def test_the_rules_are_parameters_rather_than_constants(self) -> None:
        rules = SubtitleRules(
            max_words_per_cue=2,
            min_words_before_soft_break=2,
            max_lines=2,
            max_chars_per_line=42,
            pause_seconds=0.6,
            min_cue_seconds=0.2,
        )

        cues = build_cues(spoken(["a", "b", "c", "d"]), 0.0, 20.0, rules)

        assert [cue.text for cue in cues] == ["a b", "c d"]

    @pytest.mark.parametrize("value", [0, -1])
    def test_a_non_positive_word_budget_is_refused(self, value: int) -> None:
        with pytest.raises(ValueError, match="max_words_per_cue"):
            SubtitleRules(
                max_words_per_cue=value,
                min_words_before_soft_break=6,
                max_lines=2,
                max_chars_per_line=42,
                pause_seconds=0.6,
                min_cue_seconds=0.2,
            )


class TestRefusals:
    def test_an_inverted_interval_is_refused(self) -> None:
        with pytest.raises(ValueError, match="ends at"):
            build_cues([word("x", 1.0, 2.0)], 20.0, 10.0)

    def test_a_negative_start_is_refused(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            build_cues([word("x", 1.0, 2.0)], -1.0, 10.0)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_bound_is_refused(self, value: float) -> None:
        with pytest.raises(ValueError, match="finite"):
            build_cues([word("x", 1.0, 2.0)], 0.0, value)
