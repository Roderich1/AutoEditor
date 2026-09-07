"""CE-043 to CE-045: the FFmpeg invocation is built by a pure function.

Asserted element by element rather than as a joined string. ``run_command``
takes a sequence and never a shell string (ADR-007), so the only way a path, a
topic or a transcript could reach a shell is if this function put it there --
and the only way to know it did not is to look at every element.

The filter graphs get the same treatment. "Fills 9:16 without stretching" is a
claim about a specific chain of filters, and a test that only checks the output
dimensions would pass on a graph that squashed the picture to fit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from content_engine.config import load_settings
from content_engine.domain.enums import RenderPreset
from content_engine.domain.render_rules import (
    RENDER_OUTPUT_LABEL,
    render_arguments,
    render_filter_complex,
    render_stage_config,
    render_stage_config_sha256,
    require_plain_filter_name,
)
from content_engine.domain.renders import SUBTITLES_ASS_FILENAME, RenderStageConfig


def config(**overrides: object) -> RenderStageConfig:
    """The packaged render configuration, optionally with a field replaced.

    Rebuilt through ``model_validate`` rather than ``model_copy``: a copy skips
    every validator, so a test asking whether an odd width is refused would pass
    against a model that never looked.
    """
    settings = load_settings()
    built = render_stage_config(settings.render)
    if not overrides:
        return built
    return RenderStageConfig.model_validate(built.model_dump(mode="json") | dict(overrides))


class TestStageConfig:
    def test_it_records_what_the_profile_asked_for(self) -> None:
        built = config()

        assert (built.width, built.height) == (1080, 1920)
        assert built.preset is RenderPreset.VERTICAL_BLUR
        assert built.video_codec == "libx264"
        assert built.crf == 20
        assert built.audio_bitrate == "192k"
        assert built.burn_subtitles is True

    def test_it_records_the_encoder_name_and_the_probe_name_separately(self) -> None:
        """libx264 muxes as h264; conflating the two makes verification a tautology."""
        built = config()

        assert built.video_codec == "libx264"
        assert built.expected_video_codec == "h264"

    def test_it_records_the_subtitle_rules_and_style(self) -> None:
        built = config()

        assert built.subtitles.max_words_per_cue == 8
        assert built.subtitles.max_lines == 2
        assert built.subtitles.font_name
        assert built.subtitles.source == "words"

    def test_it_records_every_version_that_changes_the_bytes(self) -> None:
        built = config()

        for field in (
            "render_rules_version",
            "argument_version",
            "subtitle_rules_version",
            "index_schema_version",
            "metadata_schema_version",
            "candidates_schema_version",
            "decisions_schema_version",
        ):
            assert getattr(built, field) >= 1

    def test_the_digest_is_the_same_for_two_independently_built_configurations(self) -> None:
        """Hashing one object twice would pass for any pure function.

        The property that matters is portability: two configurations built
        separately from the same profile must hash identically, because that
        digest is what a later run compares against the manifest -- on another
        machine, in another process.
        """
        first = config()
        second = config()

        assert first is not second, "the two must really be separate objects"
        assert render_stage_config_sha256(first) == render_stage_config_sha256(second)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("crf", 21),
            ("preset", RenderPreset.VERTICAL_CROP.value),
            ("burn_subtitles", False),
            ("encoder_preset", "slow"),
            ("blur_sigma", 12.0),
            ("width", 720),
        ],
    )
    def test_any_changed_setting_changes_the_digest(self, field: str, value: object) -> None:
        assert render_stage_config_sha256(config()) != render_stage_config_sha256(
            config(**{field: value})
        )

    def test_a_changed_subtitle_rule_changes_the_digest(self) -> None:
        """Nested, so a digest over the top level alone would miss it."""
        built = config()
        payload = built.model_dump(mode="json")
        payload["subtitles"]["max_words_per_cue"] = 5

        assert render_stage_config_sha256(built) != render_stage_config_sha256(
            RenderStageConfig.model_validate(payload)
        )

    def test_odd_dimensions_are_refused(self) -> None:
        with pytest.raises(ValueError, match="even"):
            config(width=1081)


class TestVerticalBlur:
    def test_the_background_fills_the_frame_and_is_blurred(self) -> None:
        graph = render_filter_complex(config(), None)

        assert "force_original_aspect_ratio=increase" in graph
        assert "crop=1080:1920" in graph
        assert "gblur=sigma=" in graph

    def test_the_foreground_fits_whole_inside_the_frame(self) -> None:
        graph = render_filter_complex(config(), None)

        assert "force_original_aspect_ratio=decrease" in graph

    def test_the_foreground_is_centred_over_the_background(self) -> None:
        graph = render_filter_complex(config(), None)

        assert "overlay=x=(W-w)/2:y=(H-h)/2" in graph

    def test_the_source_is_split_rather_than_decoded_twice(self) -> None:
        assert "split=2" in render_filter_complex(config(), None)

    def test_pixels_are_square(self) -> None:
        assert "setsar=1" in render_filter_complex(config(), None)

    def test_nothing_scales_to_a_fixed_size_without_preserving_the_aspect_ratio(self) -> None:
        """A bare ``scale=1080:1920`` is exactly the stretch this preset exists to avoid."""
        graph = render_filter_complex(config(), None)

        assert "scale=1080:1920," not in graph
        assert "scale=1080:1920[" not in graph

    def test_every_intermediate_size_stays_even(self) -> None:
        """yuv420p cannot represent an odd dimension, so an odd scale would fail."""
        graph = render_filter_complex(config(), None)

        assert graph.count("force_divisible_by=2") == 2

    def test_the_graph_ends_on_the_mapped_label(self) -> None:
        assert render_filter_complex(config(), None).endswith(f"[{RENDER_OUTPUT_LABEL}]")


class TestVerticalCrop:
    def test_it_fills_the_frame_and_crops_the_centre(self) -> None:
        graph = render_filter_complex(config(preset=RenderPreset.VERTICAL_CROP), None)

        assert "force_original_aspect_ratio=increase" in graph
        assert "crop=1080:1920" in graph

    def test_it_does_not_blur_or_overlay(self) -> None:
        graph = render_filter_complex(config(preset=RenderPreset.VERTICAL_CROP), None)

        assert "gblur" not in graph
        assert "overlay" not in graph
        assert "split" not in graph

    def test_pixels_are_square(self) -> None:
        assert "setsar=1" in render_filter_complex(config(preset=RenderPreset.VERTICAL_CROP), None)

    def test_the_graph_ends_on_the_mapped_label(self) -> None:
        graph = render_filter_complex(config(preset=RenderPreset.VERTICAL_CROP), None)

        assert graph.endswith(f"[{RENDER_OUTPUT_LABEL}]")


class TestSubtitleBurn:
    def test_no_subtitle_path_means_no_ass_filter(self) -> None:
        assert "ass=" not in render_filter_complex(config(), None)

    @pytest.mark.parametrize("preset", [RenderPreset.VERTICAL_BLUR, RenderPreset.VERTICAL_CROP])
    def test_a_subtitle_name_is_burned_last(self, preset: RenderPreset) -> None:
        graph = render_filter_complex(config(preset=preset), SUBTITLES_ASS_FILENAME)

        burn = graph.rsplit(",", 1)[-1]
        assert burn.startswith("ass=")
        assert burn.endswith(f"[{RENDER_OUTPUT_LABEL}]")


class TestTheSubtitleNameInTheGraph:
    """The filtergraph carries a bare filename, never a path. ADR-035.

    An earlier version escaped an absolute path into the graph and had a unit
    test that compared the escaped string. The string looked right and FFmpeg
    still opened the wrong file, because libavfilter unescapes a filter option
    value twice: a quote that survives the graph parser is read as a quote again
    by the option parser, so ``codex it's ñ`` was opened as ``codex its ñ``.

    A string comparison could never have caught that, which is why the real
    assertion now lives in ``tests/integration/test_render_pipeline.py`` and
    asks FFmpeg. What is left here is that nothing but a plain filename can get
    into the graph at all.
    """

    def test_the_graph_carries_the_bare_filename(self) -> None:
        graph = render_filter_complex(config(), SUBTITLES_ASS_FILENAME)

        assert f"ass={SUBTITLES_ASS_FILENAME}" in graph

    def test_no_separator_or_path_character_reaches_the_graph(self) -> None:
        graph = render_filter_complex(config(), SUBTITLES_ASS_FILENAME)
        burn = graph.rsplit(",", 1)[-1]

        assert burn == f"ass={SUBTITLES_ASS_FILENAME}[{RENDER_OUTPUT_LABEL}]"
        for character in ("/", "\\", ":", "'", '"'):
            assert character not in burn

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "sub titles.ass",
            "it's.ass",
            "a:b.ass",
            "a,b.ass",
            "a;b.ass",
            "a[b].ass",
            "/tmp/subtitles.ass",
            "C:/tmp/subtitles.ass",
            r"..\subtitles.ass",
            "ñ.ass",
        ],
    )
    def test_anything_but_a_plain_name_is_refused(self, name: str) -> None:
        with pytest.raises(ValueError, match="not safe"):
            require_plain_filter_name(name)

    def test_the_name_the_engine_produces_is_accepted(self) -> None:
        assert require_plain_filter_name(SUBTITLES_ASS_FILENAME) == SUBTITLES_ASS_FILENAME

    def test_a_graph_built_with_an_unsafe_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not safe"):
            render_filter_complex(config(), "it's.ass")


class TestArguments:
    def test_the_whole_invocation(self, tmp_path: Path) -> None:
        source = tmp_path.joinpath("source.mp4")
        output = tmp_path.joinpath("clip.mp4")

        arguments = render_arguments(source, 12.5, 30.25, None, output, config())

        assert arguments == [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "12.500",
            "-i",
            str(source),
            "-t",
            "30.250",
            "-filter_complex",
            render_filter_complex(config(), None),
            "-map",
            f"[{RENDER_OUTPUT_LABEL}]",
            "-map",
            "0:a:0",
            "-sn",
            "-dn",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(output),
        ]

    def test_the_seek_precedes_the_input_and_the_limit_follows_it(self, tmp_path: Path) -> None:
        """``-ss`` before ``-i`` seeks; after it, FFmpeg decodes the whole file first."""
        arguments = render_arguments(
            tmp_path.joinpath("s.mp4"), 900.0, 30.0, None, tmp_path.joinpath("o.mp4"), config()
        )

        assert arguments.index("-ss") < arguments.index("-i") < arguments.index("-t")

    def test_timestamps_are_formatted_rather_than_repr(self, tmp_path: Path) -> None:
        arguments = render_arguments(
            tmp_path.joinpath("s.mp4"),
            0.1 + 0.2,
            1 / 3,
            None,
            tmp_path.joinpath("o.mp4"),
            config(),
        )

        assert arguments[arguments.index("-ss") + 1] == "0.300"
        assert arguments[arguments.index("-t") + 1] == "0.333"

    def test_only_one_video_and_one_audio_stream_are_carried(self, tmp_path: Path) -> None:
        arguments = render_arguments(
            tmp_path.joinpath("s.mp4"), 0.0, 1.0, None, tmp_path.joinpath("o.mp4"), config()
        )

        assert "-sn" in arguments
        assert "-dn" in arguments
        assert arguments.count("-map") == 2

    def test_every_argument_is_a_string(self, tmp_path: Path) -> None:
        arguments = render_arguments(
            tmp_path.joinpath("s.mp4"), 0.0, 1.0, None, tmp_path.joinpath("o.mp4"), config()
        )

        assert all(isinstance(argument, str) for argument in arguments)

    def test_no_argument_is_a_shell_construct(self, tmp_path: Path) -> None:
        arguments = render_arguments(
            tmp_path.joinpath("s.mp4"),
            0.0,
            1.0,
            SUBTITLES_ASS_FILENAME,
            tmp_path.joinpath("o.mp4"),
            config(),
        )

        joined = " ".join(arguments)
        for dangerous in ("&&", "||", "|", ">", "<", "$(", "`"):
            assert dangerous not in joined

    def test_the_subtitle_name_appears_only_inside_the_filter_graph(self, tmp_path: Path) -> None:
        arguments = render_arguments(
            tmp_path.joinpath("s.mp4"),
            0.0,
            1.0,
            SUBTITLES_ASS_FILENAME,
            tmp_path.joinpath("o.mp4"),
            config(),
        )

        holders = [argument for argument in arguments if SUBTITLES_ASS_FILENAME in argument]
        assert len(holders) == 1
        assert holders[0] == arguments[arguments.index("-filter_complex") + 1]

    def test_no_directory_of_the_subtitles_appears_anywhere(self, tmp_path: Path) -> None:
        """The whole point of ADR-035: the path is not in the command at all."""
        arguments = render_arguments(
            tmp_path.joinpath("s.mp4"),
            0.0,
            1.0,
            SUBTITLES_ASS_FILENAME,
            tmp_path.joinpath("clip_x", "clip.mp4"),
            config(),
        )

        graph = arguments[arguments.index("-filter_complex") + 1]
        assert "clip_x" not in graph
        assert str(tmp_path) not in graph

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_timestamp_is_refused(self, value: float, tmp_path: Path) -> None:
        source = tmp_path.joinpath("s.mp4")
        output = tmp_path.joinpath("o.mp4")
        settings = config()

        with pytest.raises(ValueError, match="finite"):
            render_arguments(source, value, 1.0, None, output, settings)

    def test_a_non_positive_duration_is_refused(self, tmp_path: Path) -> None:
        source = tmp_path.joinpath("s.mp4")
        output = tmp_path.joinpath("o.mp4")
        settings = config()

        with pytest.raises(ValueError, match="positive"):
            render_arguments(source, 0.0, 0.0, None, output, settings)

    def test_a_negative_start_is_refused(self, tmp_path: Path) -> None:
        source = tmp_path.joinpath("s.mp4")
        output = tmp_path.joinpath("o.mp4")
        settings = config()

        with pytest.raises(ValueError, match="negative"):
            render_arguments(source, -1.0, 1.0, None, output, settings)
