"""CE-041 and CE-042: cues become SRT and ASS documents.

Two exports of one authoritative list of cues. Everything here is about what a
player will read back: the rounding step that turns seconds into milliseconds or
centiseconds, the characters each format gives structural meaning to, and the
fact that both documents must still describe the same clip afterwards.
"""

from __future__ import annotations

import pytest

from content_engine.domain.subtitles import (
    ASS_STYLE_NAME,
    DEFAULT_ASS_STYLE,
    AssStyle,
    SubtitleCue,
    ass_text,
    ass_timestamp,
    read_ass_events,
    read_srt_events,
    render_ass,
    render_srt,
    srt_timestamp,
)


def cue(index: int, start: float, end: float, *lines: str) -> SubtitleCue:
    return SubtitleCue(index=index, start=start, end=end, lines=list(lines or ("texto",)))


class TestSrtTimestamps:
    @pytest.mark.parametrize(
        ("milliseconds", "expected"),
        [
            (0, "00:00:00,000"),
            (1, "00:00:00,001"),
            (999, "00:00:00,999"),
            (1000, "00:00:01,000"),
            (61_001, "00:01:01,001"),
            (3_600_000, "01:00:00,000"),
            (3_661_123, "01:01:01,123"),
        ],
    )
    def test_formatting(self, milliseconds: int, expected: str) -> None:
        assert srt_timestamp(milliseconds) == expected

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.0005, 1),
            (0.0015, 2),
            (0.0025, 3),
            (1.2345, 1235),
            (1.2344, 1234),
        ],
    )
    def test_rounding_is_half_up_rather_than_half_even(self, seconds: float, expected: int) -> None:
        """``round()`` would answer 0, 2, 2 for the first three: half-even."""
        from content_engine.domain.subtitles import to_milliseconds

        assert to_milliseconds(seconds) == expected


class TestAssTimestamps:
    @pytest.mark.parametrize(
        ("centiseconds", "expected"),
        [
            (0, "0:00:00.00"),
            (1, "0:00:00.01"),
            (99, "0:00:00.99"),
            (100, "0:00:01.00"),
            (360_000, "1:00:00.00"),
            (366_112, "1:01:01.12"),
        ],
    )
    def test_formatting(self, centiseconds: int, expected: str) -> None:
        assert ass_timestamp(centiseconds) == expected

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0.005, 1), (0.015, 2), (0.025, 3), (1.234, 123), (1.236, 124)],
    )
    def test_rounding_is_half_up(self, seconds: float, expected: int) -> None:
        from content_engine.domain.subtitles import to_centiseconds

        assert to_centiseconds(seconds) == expected


class TestSrtDocument:
    def test_cues_are_numbered_from_one(self) -> None:
        text = render_srt([cue(1, 0.0, 1.0, "uno"), cue(2, 2.0, 3.0, "dos")])

        assert text.splitlines()[0] == "1"
        assert text.splitlines()[4] == "2"

    def test_the_shape_of_one_cue(self) -> None:
        text = render_srt([cue(1, 1.5, 2.25, "primera", "segunda")])

        assert text == "1\n00:00:01,500 --> 00:00:02,250\nprimera\nsegunda\n"

    def test_cues_are_separated_by_a_blank_line(self) -> None:
        text = render_srt([cue(1, 0.0, 1.0, "uno"), cue(2, 2.0, 3.0, "dos")])

        assert "\n\nuno" not in text
        assert text.count("\n\n") == 1

    def test_no_cues_produce_an_empty_document(self) -> None:
        assert render_srt([]) == ""

    def test_the_document_ends_with_exactly_one_newline(self) -> None:
        text = render_srt([cue(1, 0.0, 1.0, "uno")])

        assert text.endswith("\n")
        assert not text.endswith("\n\n")

    def test_no_carriage_returns_are_emitted(self) -> None:
        assert "\r" not in render_srt([cue(1, 0.0, 1.0, "uno")])

    def test_unicode_is_written_as_itself(self) -> None:
        text = render_srt([cue(1, 0.0, 1.0, "configuración en español ñ")])

        assert "configuración en español ñ" in text
        assert text.encode("utf-8").decode("utf-8") == text


class TestAssDocument:
    def test_the_document_declares_the_play_resolution(self) -> None:
        text = render_ass([cue(1, 0.0, 1.0, "uno")], 1080, 1920)

        assert "PlayResX: 1080" in text
        assert "PlayResY: 1920" in text

    def test_the_document_has_the_three_required_sections(self) -> None:
        text = render_ass([cue(1, 0.0, 1.0, "uno")], 1080, 1920)

        assert "[Script Info]" in text
        assert "[V4+ Styles]" in text
        assert "[Events]" in text

    def test_a_dialogue_line_names_the_style_and_carries_the_text(self) -> None:
        text = render_ass([cue(1, 1.5, 2.25, "primera", "segunda")], 1080, 1920)

        line = next(row for row in text.splitlines() if row.startswith("Dialogue:"))
        assert line == (
            f"Dialogue: 0,0:00:01.50,0:00:02.25,{ASS_STYLE_NAME},,0,0,0,,primera\\Nsegunda"
        )

    def test_lines_are_joined_with_a_hard_break(self) -> None:
        text = render_ass([cue(1, 0.0, 1.0, "a", "b")], 1080, 1920)

        assert "a\\Nb" in text

    def test_no_cues_still_produce_a_valid_document(self) -> None:
        text = render_ass([], 1080, 1920)

        assert "[Events]" in text
        assert "Dialogue:" not in text

    def test_the_style_is_bottom_anchored_inside_the_safe_area(self) -> None:
        text = render_ass([cue(1, 0.0, 1.0, "uno")], 1080, 1920)

        style = next(row for row in text.splitlines() if row.startswith("Style:"))
        fields = style.removeprefix("Style: ").split(",")
        assert fields[0] == ASS_STYLE_NAME
        # Alignment 2 is bottom-centre in the V4+ (numpad) convention.
        assert fields[18] == "2"
        assert int(fields[21]) >= 200

    def test_the_style_is_a_parameter(self) -> None:
        style = AssStyle(
            font_name="Verdana",
            font_size=48,
            primary_colour="&H00FFFFFF",
            outline_colour="&H00101010",
            back_colour="&HA0000000",
            bold=True,
            outline=3.0,
            shadow=1.0,
            alignment=2,
            margin_horizontal=60,
            margin_vertical=240,
        )

        text = render_ass([cue(1, 0.0, 1.0, "uno")], 1080, 1920, style)

        assert "Verdana" in text
        assert ",48," in text

    def test_the_default_style_is_the_one_used_without_an_argument(self) -> None:
        assert render_ass([cue(1, 0.0, 1.0, "x")], 1080, 1920) == render_ass(
            [cue(1, 0.0, 1.0, "x")], 1080, 1920, DEFAULT_ASS_STYLE
        )

    def test_no_carriage_returns_are_emitted(self) -> None:
        assert "\r" not in render_ass([cue(1, 0.0, 1.0, "uno")], 1080, 1920)


class TestAssTextSafety:
    """ASS gives ``{``, ``}`` and ``\\`` structural meaning inside a Text field.

    There is no portable escape for them across libass and VSFilter, so they are
    transliterated under a versioned rule rather than passed through. The JSON
    transcript stays the authoritative text.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("{color}", "(color)"),
            ("C:\\Users", "C:/Users"),
            ("a{b}c\\d", "a(b)c/d"),
            ("sin nada especial", "sin nada especial"),
        ],
    )
    def test_structural_characters_are_transliterated(self, raw: str, expected: str) -> None:
        assert ass_text(raw) == expected

    def test_an_override_block_cannot_reach_the_renderer(self) -> None:
        text = render_ass([cue(1, 0.0, 1.0, "{\\an8}arriba")], 1080, 1920)

        assert "{\\an8}" not in text
        assert "(/an8)arriba" in text

    def test_a_dialogue_line_cannot_be_split_by_the_text(self) -> None:
        """Line breaks are removed by the builder; the exporter proves it again."""
        text = render_ass([cue(1, 0.0, 1.0, "una linea")], 1080, 1920)

        dialogues = [row for row in text.splitlines() if row.startswith("Dialogue:")]
        assert len(dialogues) == 1


class TestParsingBack:
    """CE-046 reads the finished files rather than trusting what was written."""

    def test_srt_round_trips(self) -> None:
        cues = [cue(1, 0.0, 1.25, "uno"), cue(2, 2.5, 3.75, "dos", "tres")]

        events = read_srt_events(render_srt(cues))

        assert [event.start_ms for event in events] == [0, 2500]
        assert [event.end_ms for event in events] == [1250, 3750]
        assert [event.text for event in events] == ["uno", "dos\ntres"]

    def test_ass_round_trips(self) -> None:
        cues = [cue(1, 0.0, 1.25, "uno"), cue(2, 2.5, 3.75, "dos", "tres")]

        events = read_ass_events(render_ass(cues, 1080, 1920))

        assert [event.start_ms for event in events] == [0, 2500]
        assert [event.end_ms for event in events] == [1250, 3750]
        assert [event.text for event in events] == ["uno", "dos\ntres"]

    def test_an_empty_srt_parses_as_no_events(self) -> None:
        assert read_srt_events("") == []

    def test_an_ass_document_with_no_dialogue_parses_as_no_events(self) -> None:
        assert read_ass_events(render_ass([], 1080, 1920)) == []

    @pytest.mark.parametrize(
        "damaged",
        [
            "1\nnot a timestamp\ntexto\n",
            "no-number\n00:00:00,000 --> 00:00:01,000\ntexto\n",
            "1\n00:00:00,000 --> 00:00:01,000\n",
            "1\n00:00:01,000 --> 00:00:00,000\ntexto\n",
        ],
    )
    def test_a_damaged_srt_is_refused(self, damaged: str) -> None:
        with pytest.raises(ValueError, match="SRT"):
            read_srt_events(damaged)

    @pytest.mark.parametrize(
        "damaged",
        [
            "[Events]\nDialogue: 0,nonsense,0:00:01.00,Default,,0,0,0,,texto\n",
            "[Events]\nDialogue: 0,0:00:01.00\n",
            "[Events]\nDialogue: 0,0:00:02.00,0:00:01.00,Default,,0,0,0,,texto\n",
        ],
    )
    def test_a_damaged_ass_is_refused(self, damaged: str) -> None:
        with pytest.raises(ValueError, match="ASS"):
            read_ass_events(damaged)


class TestCueModel:
    def test_a_cue_ending_before_it_starts_is_refused(self) -> None:
        with pytest.raises(ValueError, match="ends at"):
            SubtitleCue(index=1, start=2.0, end=1.0, lines=["texto"])

    def test_a_cue_with_no_lines_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            SubtitleCue(index=1, start=0.0, end=1.0, lines=[])

    def test_a_cue_with_a_blank_line_is_refused(self) -> None:
        with pytest.raises(ValueError, match="blank"):
            SubtitleCue(index=1, start=0.0, end=1.0, lines=["   "])

    def test_a_cue_line_holding_a_break_is_refused(self) -> None:
        with pytest.raises(ValueError, match="line break"):
            SubtitleCue(index=1, start=0.0, end=1.0, lines=["a\nb"])

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_bound_is_refused(self, value: float) -> None:
        with pytest.raises(ValueError):
            SubtitleCue(index=1, start=0.0, end=value, lines=["texto"])
