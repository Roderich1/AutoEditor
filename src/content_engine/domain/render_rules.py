"""Identity and command construction for the render stage (CE-043 to CE-046).

Pure functions over domain types. No I/O, no clock, no subprocess: this module
decides *what* FFmpeg will be asked to do and *whether* an existing set of clips
may be reused, and the adapter beside it does the asking.

The argument list is built here rather than in the adapter for one reason: it is
the part a test can assert element by element. ``run_command`` takes a sequence
and never a shell string (ADR-007), so the only way a transcript, a topic or a
filename could reach a shell is if this function put it there. Keeping the
construction pure makes that a property the suite checks on every run instead of
a convention someone has to remember.

The filter graph is the one place in the engine where a path becomes part of a
string an external tool parses, because ``ass=`` takes its file as an option
value rather than as an argument of its own. ``escape_filter_path`` is therefore
a security boundary and not a formatting helper, and it has its own tests.

Two digests, following the shape the other three stages established (ADR-017,
ADR-024, ADR-031):

``render_stage_config_sha256``  the digest of ``clips/config.effective.json``,
                                which ties the manifest to a readable artifact
                                and answers "was this rendered under the same
                                policy".
``render_fingerprint``          the digest of the whole stage: what it was built
                                from, what was produced, and the policy that
                                produced it. It covers the *outputs* for the
                                same reason the analysis and preview
                                fingerprints do -- the decision it makes is "may
                                these files be reused", and a digest over inputs
                                alone cannot notice that one of them was
                                replaced.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite
from pathlib import Path, PurePath

from content_engine.config import RenderSettings
from content_engine.domain.candidates import (
    CANDIDATES_SCHEMA_VERSION,
    TIME_EPSILON,
)
from content_engine.domain.enums import RenderPreset
from content_engine.domain.render_targets import RenderTarget, RenderTargetClip
from content_engine.domain.renders import (
    CLIP_METADATA_SCHEMA_VERSION,
    EXPECTED_SAMPLE_ASPECT_RATIO,
    RENDER_DURATION_TOLERANCE_SECONDS,
    RENDER_INDEX_SCHEMA_VERSION,
    RENDER_RULES_VERSION,
    RenderIndex,
    RenderStageConfig,
    RenderSubtitleConfig,
)
from content_engine.domain.review import DECISIONS_SCHEMA_VERSION
from content_engine.domain.subtitles import (
    DEFAULT_ASS_STYLE,
    DEFAULT_SUBTITLE_RULES,
    SUBTITLE_RULES_VERSION,
    AssStyle,
    SubtitleRules,
)
from content_engine.utils.canonical import canonical_sha256

__all__ = [
    "RENDER_ARGUMENT_VERSION",
    "RENDER_FINGERPRINT_VERSION",
    "RENDER_INDEX_FILENAME",
    "RENDER_OUTPUT_LABEL",
    "RENDER_STAGE_CONFIG_FILENAME",
    "escape_filter_path",
    "render_arguments",
    "render_coherence_problem",
    "render_filter_complex",
    "render_fingerprint",
    "ass_style",
    "render_stage_config",
    "render_stage_config_sha256",
    "subtitle_rules",
]

#: Bumped whenever the argument list changes, even if the policy did not. The
#: same intent expressed with different arguments produces different bytes, and
#: reuse compares bytes.
RENDER_ARGUMENT_VERSION = 1
#: Bumped whenever the fingerprint payload changes shape. Every recorded
#: fingerprint stops matching when it does, which is the intended effect.
RENDER_FINGERPRINT_VERSION = 1

RENDER_INDEX_FILENAME = "index.json"
RENDER_STAGE_CONFIG_FILENAME = "config.effective.json"

#: The label the finished video leaves the filter graph on, and the one thing
#: ``-map`` is pointed at. Prefixed so it cannot collide with a label FFmpeg
#: generates for an unlabelled chain.
RENDER_OUTPUT_LABEL = "ce_out"
_BACKGROUND_LABEL = "ce_bg"
_FOREGROUND_LABEL = "ce_fg"
_BLURRED_LABEL = "ce_back"
_FITTED_LABEL = "ce_front"

#: ADR-032. Stage constants, not profile keys. ``[render]`` is what the
#: specification fixes and what an operator chooses between experiments; these
#: describe how the picture is composed, and ADR-028's reasoning applies -- a new
#: key in ``[render]`` changes ``config_sha256`` and therefore the logical
#: identity of every run that already exists.
BLUR_SIGMA = 24.0
PIXEL_FORMAT = "yuv420p"
AUDIO_SAMPLE_RATE = 44100
AUDIO_CHANNELS = 2
#: What the cues are built from. A transcript with no word timestamps is refused
#: rather than approximated from segments, and this names the contract that
#: refusal enforces.
SUBTITLE_SOURCE = "words"

#: What ffprobe reports for the codecs above. libx264 muxes as h264, so the
#: encoder name and the verification name are deliberately separate values.
EXPECTED_VIDEO_CODEC = "h264"
EXPECTED_AUDIO_CODEC = "aac"


def render_stage_config(settings: RenderSettings) -> RenderStageConfig:
    """The effective configuration of one render stage, in readable form."""
    return RenderStageConfig(
        width=settings.width,
        height=settings.height,
        preset=settings.preset,
        burn_subtitles=settings.burn_subtitles,
        video_codec=settings.video_codec,
        encoder_preset=settings.encoder_preset,
        crf=settings.crf,
        pixel_format=PIXEL_FORMAT,
        blur_sigma=BLUR_SIGMA,
        audio_codec=settings.audio_codec,
        audio_bitrate=settings.audio_bitrate,
        audio_sample_rate=AUDIO_SAMPLE_RATE,
        audio_channels=AUDIO_CHANNELS,
        expected_video_codec=EXPECTED_VIDEO_CODEC,
        expected_audio_codec=EXPECTED_AUDIO_CODEC,
        expected_sample_aspect_ratio=EXPECTED_SAMPLE_ASPECT_RATIO,
        duration_tolerance_seconds=RENDER_DURATION_TOLERANCE_SECONDS,
        subtitles=RenderSubtitleConfig(
            source=SUBTITLE_SOURCE,
            max_words_per_cue=DEFAULT_SUBTITLE_RULES.max_words_per_cue,
            min_words_before_soft_break=DEFAULT_SUBTITLE_RULES.min_words_before_soft_break,
            max_lines=DEFAULT_SUBTITLE_RULES.max_lines,
            max_chars_per_line=DEFAULT_SUBTITLE_RULES.max_chars_per_line,
            pause_seconds=DEFAULT_SUBTITLE_RULES.pause_seconds,
            min_cue_seconds=DEFAULT_SUBTITLE_RULES.min_cue_seconds,
            font_name=DEFAULT_ASS_STYLE.font_name,
            font_size=DEFAULT_ASS_STYLE.font_size,
            primary_colour=DEFAULT_ASS_STYLE.primary_colour,
            outline_colour=DEFAULT_ASS_STYLE.outline_colour,
            back_colour=DEFAULT_ASS_STYLE.back_colour,
            bold=DEFAULT_ASS_STYLE.bold,
            outline=DEFAULT_ASS_STYLE.outline,
            shadow=DEFAULT_ASS_STYLE.shadow,
            alignment=DEFAULT_ASS_STYLE.alignment,
            margin_horizontal=DEFAULT_ASS_STYLE.margin_horizontal,
            margin_vertical=DEFAULT_ASS_STYLE.margin_vertical,
        ),
        render_rules_version=RENDER_RULES_VERSION,
        argument_version=RENDER_ARGUMENT_VERSION,
        subtitle_rules_version=SUBTITLE_RULES_VERSION,
        index_schema_version=RENDER_INDEX_SCHEMA_VERSION,
        metadata_schema_version=CLIP_METADATA_SCHEMA_VERSION,
        candidates_schema_version=CANDIDATES_SCHEMA_VERSION,
        decisions_schema_version=DECISIONS_SCHEMA_VERSION,
    )


def subtitle_rules(config: RenderStageConfig) -> SubtitleRules:
    """The cue rules a recorded stage configuration describes.

    The builder is driven from the artifact rather than from the module
    constants, so a set of clips and the configuration written beside them
    cannot describe two different policies -- and so a future run reading an
    older configuration back would build the cues that configuration names.
    """
    return SubtitleRules(
        max_words_per_cue=config.subtitles.max_words_per_cue,
        min_words_before_soft_break=config.subtitles.min_words_before_soft_break,
        max_lines=config.subtitles.max_lines,
        max_chars_per_line=config.subtitles.max_chars_per_line,
        pause_seconds=config.subtitles.pause_seconds,
        min_cue_seconds=config.subtitles.min_cue_seconds,
    )


def ass_style(config: RenderStageConfig) -> AssStyle:
    """The caption style a recorded stage configuration describes."""
    return AssStyle(
        font_name=config.subtitles.font_name,
        font_size=config.subtitles.font_size,
        primary_colour=config.subtitles.primary_colour,
        outline_colour=config.subtitles.outline_colour,
        back_colour=config.subtitles.back_colour,
        bold=config.subtitles.bold,
        outline=config.subtitles.outline,
        shadow=config.subtitles.shadow,
        alignment=config.subtitles.alignment,
        margin_horizontal=config.subtitles.margin_horizontal,
        margin_vertical=config.subtitles.margin_vertical,
    )


def escape_filter_path(path: PurePath) -> str:
    """Quote a path so libavfilter reads it back unchanged.

    A filtergraph is unescaped twice on the way to a filter's option, and a
    Windows path trips over both levels.

    At the *graph* level ``,`` ``;`` ``[`` ``]`` separate chains and ``'``
    quotes; inside single quotes none of them is special and no backslash is
    consumed. At the *option* level ``:`` separates one option from the next and
    ``=`` separates a name from its value, and a backslash escapes either.

    So: backslashes become forward slashes, which FFmpeg accepts on Windows and
    which removes the whole question of what a backslash means here; every
    ``:`` -- the drive letter's included -- is escaped for the option parser;
    and the result is wrapped in single quotes for the graph parser. A literal
    quote in the path closes, escapes and reopens, which is the only way the
    format offers.

    The escaping is deliberately not "replace the characters that broke last
    time": both levels are handled, in the order they are applied, so a path
    holding a comma is protected by the quotes rather than by luck.
    """
    text = str(path).replace("\\", "/").replace(":", "\\:")
    return "'" + text.replace("'", "'\\''") + "'"


def _blur_chains(config: RenderStageConfig) -> list[str]:
    """CE-043. A blurred fill behind the whole, undistorted frame.

    ``split`` decodes once and uses the frame twice. The background is scaled up
    until it covers 9:16 in both dimensions and the excess is cropped away, so
    it always fills; the foreground is scaled *down* until it fits in both, so
    nothing leaves it. Neither uses a bare ``scale=w:h``, which would stretch
    every source that is not already 9:16 -- which is all of them.

    ``force_divisible_by=2`` is not cosmetic. ``yuv420p`` subsamples chroma by
    two in each direction and cannot represent an odd dimension, so a source of
    odd width fitted into the frame would produce an intermediate size the
    format cannot hold and FFmpeg would refuse the graph.
    """
    width, height = config.width, config.height
    return [
        f"[0:v]split=2[{_BACKGROUND_LABEL}][{_FOREGROUND_LABEL}]",
        f"[{_BACKGROUND_LABEL}]scale={width}:{height}:force_original_aspect_ratio=increase"
        f":force_divisible_by=2,crop={width}:{height},"
        f"gblur=sigma={_number(config.blur_sigma)}[{_BLURRED_LABEL}]",
        f"[{_FOREGROUND_LABEL}]scale={width}:{height}:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2[{_FITTED_LABEL}]",
        f"[{_BLURRED_LABEL}][{_FITTED_LABEL}]overlay=x=(W-w)/2:y=(H-h)/2,setsar=1",
    ]


def _crop_chains(config: RenderStageConfig) -> list[str]:
    """CE-044. Scale until the frame is covered, then take the centre."""
    width, height = config.width, config.height
    return [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase"
        f":force_divisible_by=2,crop={width}:{height},setsar=1"
    ]


def render_filter_complex(config: RenderStageConfig, subtitles: PurePath | None) -> str:
    """The whole filter graph, ending on the label ``-map`` is pointed at.

    Subtitles are burned last, after the composition and after ``setsar``, so
    the caption is drawn in output pixels at the size the ASS document declares
    rather than scaled along with the picture.

    ``subtitles`` is None when nothing is to be burned -- either because the
    profile says so or because there is nothing to draw. The distinction is the
    caller's to make; this function only reports what it was given.
    """
    chains = (
        _blur_chains(config)
        if config.preset is RenderPreset.VERTICAL_BLUR
        else _crop_chains(config)
    )
    last = chains[-1]
    if subtitles is not None:
        last = f"{last},ass={escape_filter_path(subtitles)}"
    chains[-1] = f"{last}[{RENDER_OUTPUT_LABEL}]"
    return ";".join(chains)


def _number(value: float) -> str:
    """Write a filter number without a trailing ``.0`` where it is a whole one."""
    return str(int(value)) if float(value).is_integer() else str(value)


def render_arguments(
    source: Path,
    start: float,
    duration: float,
    subtitles: Path | None,
    output: Path,
    config: RenderStageConfig,
) -> list[str]:
    """CE-045. The exact FFmpeg invocation for one clip.

    ``-ss`` goes before ``-i`` so FFmpeg seeks rather than decoding the whole
    file up to the interval, and ``-t`` goes after it so the limit applies to
    what is written rather than to what is read. On a 35-minute recording that
    ordering is the difference between seconds and minutes per clip. Seeking on
    the input also rebases the output timestamps to zero, which is what makes
    the clip-local subtitle times line up with the picture.

    Timestamps are formatted to milliseconds. Handing FFmpeg a bare ``repr``
    would make the command line depend on float formatting, and the command line
    is part of what the argument version promises to keep stable.
    """
    for name, value in (("start", start), ("duration", duration)):
        if not isfinite(value):
            raise ValueError(f"{name} must be a finite number, got {value}")
    if start < 0:
        raise ValueError(f"start is negative ({start})")
    if duration <= 0:
        raise ValueError(f"duration must be positive, got {duration}")
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        "-filter_complex",
        render_filter_complex(config, subtitles),
        # One video and one audio stream, and nothing else. A source carrying a
        # subtitle or data track must not have it copied into the clip -- the
        # subtitles this stage produces are the ones it built.
        "-map",
        f"[{RENDER_OUTPUT_LABEL}]",
        "-map",
        "0:a:0",
        "-sn",
        "-dn",
        "-c:v",
        config.video_codec,
        "-preset",
        config.encoder_preset,
        "-crf",
        str(config.crf),
        "-pix_fmt",
        config.pixel_format,
        "-c:a",
        config.audio_codec,
        "-b:a",
        config.audio_bitrate,
        "-ar",
        str(config.audio_sample_rate),
        "-ac",
        str(config.audio_channels),
        "-movflags",
        "+faststart",
        str(output),
    ]


def render_stage_config_sha256(config: RenderStageConfig) -> str:
    """Digest of the stage configuration exactly as it is written to disk."""
    return canonical_sha256(config.model_dump(mode="json"))


def render_fingerprint(index: RenderIndex, config: RenderStageConfig) -> str:
    """The integrity of one render execution, the produced files included.

    Everything the specification asks a render fingerprint to cover is inside
    these two objects and is covered whole rather than field by field. The index
    carries the source digest, the transcript digest, the analysis fingerprint,
    the review fingerprint, the digest of the decision file, every final
    interval and a digest of every artifact; the configuration carries the
    preset, the encoder settings, the subtitle rules and style and every schema,
    rules and argument version.

    Two parts and no chosen subset: every field left out would be a field a
    later run trusts with no evidence.
    """
    return canonical_sha256(
        {
            "version": RENDER_FINGERPRINT_VERSION,
            "index": index.model_dump(mode="json"),
            "config": config.model_dump(mode="json"),
        }
    )


def _identity_problem(index: RenderIndex, expected: RenderTarget) -> str | None:
    for label, recorded, wanted in (
        ("analysis", index.analysis_fingerprint, expected.analysis_fingerprint),
        ("review", index.review_fingerprint, expected.review_fingerprint),
        ("decision file", index.decisions_sha256, expected.decisions_sha256),
        ("transcript", index.transcript_sha256, expected.transcript_sha256),
        ("source", index.source_sha256, expected.source_sha256),
    ):
        if recorded != wanted:
            return (
                f"the clips were rendered from {label} {recorded[:12]}, but the run holds "
                f"{wanted[:12]}"
            )
    return None


def _policy_problem(index: RenderIndex, config: RenderStageConfig) -> str | None:
    if (index.width, index.height) != (config.width, config.height):
        return (
            f"the index holds {index.width}x{index.height} clips and the stage configuration "
            f"asks for {config.width}x{config.height}"
        )
    if index.preset is not config.preset:
        return (
            f"the index names preset {index.preset} and the stage configuration names "
            f"{config.preset}"
        )
    if index.burn_subtitles != config.burn_subtitles:
        return (
            f"the index was rendered with burn_subtitles={index.burn_subtitles} and the stage "
            f"configuration asks for {config.burn_subtitles}"
        )
    if index.rules_version != config.render_rules_version:
        return (
            f"the index names render rules {index.rules_version} and the stage configuration "
            f"names {config.render_rules_version}"
        )
    if config.candidates_schema_version != CANDIDATES_SCHEMA_VERSION:
        return (
            f"the clips were cut from candidate schema {config.candidates_schema_version}; "
            f"this build produces {CANDIDATES_SCHEMA_VERSION}"
        )
    if config.decisions_schema_version != DECISIONS_SCHEMA_VERSION:
        return (
            f"the clips were cut from decision schema {config.decisions_schema_version}; this "
            f"build produces {DECISIONS_SCHEMA_VERSION}"
        )
    return None


def _shortlist_problem(index: RenderIndex, targets: Sequence[RenderTargetClip]) -> str | None:
    recorded = index.by_candidate
    for target in targets:
        entry = recorded.get(target.candidate.id)
        if entry is None:
            return f"candidate {target.candidate.id} was kept in review but has no clip"
        if entry.decision is not target.decision:
            return (
                f"the clip for {target.candidate.id} records a {entry.decision} decision and "
                f"the review recorded {target.decision}"
            )
        if (
            abs(entry.start - target.start) > TIME_EPSILON
            or abs(entry.end - target.end) > TIME_EPSILON
        ):
            return (
                f"the clip for {target.candidate.id} covers [{entry.start}, {entry.end}] and "
                f"the decision approved [{target.start}, {target.end}]"
            )
        if entry.rank != target.candidate.rank:
            return (
                f"the clip for {target.candidate.id} is ranked {entry.rank} and the candidate "
                f"is ranked {target.candidate.rank}"
            )
    extra = sorted(set(recorded) - {target.candidate.id for target in targets})
    if extra:
        return (
            f"the index holds clips for candidates this review did not keep: {extra}. A "
            "rejected candidate must not have one."
        )
    return None


def render_coherence_problem(
    index: RenderIndex,
    config: RenderStageConfig,
    expected: RenderTarget,
) -> str | None:
    """The first way a set of clips contradicts the run it claims to describe.

    The fingerprint proves the index and the configuration were written
    together. It cannot prove they describe *this* run: a clips directory copied
    from another experiment would rebuild its own fingerprint perfectly and hand
    somebody a video of the wrong material under the right name.

    Returns a description rather than raising, because the caller decides what
    kind of failure it is: producing an incoherent set is a render bug, finding
    one on disk is an incompatible artifact.
    """
    return (
        _identity_problem(index, expected)
        or _policy_problem(index, config)
        or _shortlist_problem(index, expected.clips)
    )
