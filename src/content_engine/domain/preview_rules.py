"""Identity and command construction for the preview stage (CE-034).

Pure functions over domain types. No I/O, no clock, no subprocess: this module
decides *what* FFmpeg will be asked to do and *whether* an existing preview set
may be reused, and the adapter beside it does the asking.

The argument list is built here rather than in the adapter for one reason: it is
the part a test can assert element by element. ``run_command`` takes a sequence
and never a shell string (ADR-007), so the only way a transcript, a topic or a
filename could reach a shell is if this function put it there. Keeping the
construction pure makes that a property the suite checks on every run instead of
a convention someone has to remember.

Two digests, following the shape transcription and analysis established
(ADR-017 and ADR-024):

``preview_stage_config_sha256``  the digest of ``previews/config.effective.json``,
                                 which ties the manifest to a readable artifact.
``preview_fingerprint``          the digest of the whole stage: the analysis it
                                 was cut from, the index of what was produced,
                                 and the configuration that produced it. It
                                 covers the *outputs* for the same reason the
                                 analysis fingerprint does -- the decision it
                                 makes is "may these files be reused", and a
                                 digest over inputs alone cannot notice that one
                                 of the files was replaced.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from content_engine.domain.candidates import (
    CANDIDATES_SCHEMA_VERSION,
    TIME_EPSILON,
    ValidatedCandidate,
)
from content_engine.domain.previews import (
    PREVIEW_DURATION_TOLERANCE_SECONDS,
    PREVIEW_FILENAME_TEMPLATE,
    PREVIEW_INDEX_SCHEMA_VERSION,
    PREVIEW_RULES_VERSION,
    PreviewIndex,
    PreviewStageConfig,
    preview_filename,
)
from content_engine.utils.canonical import canonical_sha256

__all__ = [
    "PREVIEW_ARGUMENT_VERSION",
    "PREVIEW_DURATION_TOLERANCE_SECONDS",
    "PREVIEW_FILENAME_TEMPLATE",
    "PREVIEW_FINGERPRINT_VERSION",
    "PREVIEW_INDEX_FILENAME",
    "PREVIEW_RULES_VERSION",
    "PREVIEW_STAGE_CONFIG_FILENAME",
    "preview_arguments",
    "preview_coherence_problem",
    "preview_filename",
    "preview_fingerprint",
    "preview_stage_config",
    "preview_stage_config_sha256",
    "preview_video_filter",
]

#: Bumped whenever the argument list changes, even if the policy did not. The
#: same intent expressed with different arguments produces different bytes, and
#: reuse compares bytes.
PREVIEW_ARGUMENT_VERSION = 1
#: Bumped whenever the fingerprint payload changes shape. Every recorded
#: fingerprint stops matching when it does, which is the intended effect.
PREVIEW_FINGERPRINT_VERSION = 1

PREVIEW_INDEX_FILENAME = "index.json"
PREVIEW_STAGE_CONFIG_FILENAME = "config.effective.json"

#: ADR-028. Encoder policy for a proxy nobody publishes: cheap, fast, and lossy
#: enough that watching thirty of them costs seconds rather than minutes.
#: `veryfast` and CRF 30 are chosen for encode time, not for looking good.
VIDEO_CODEC = "libx264"
ENCODER_PRESET = "veryfast"
CRF = 30
PIXEL_FORMAT = "yuv420p"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "96k"
AUDIO_SAMPLE_RATE = 44100
AUDIO_CHANNELS = 2

#: The whole frame is kept. A preview exists so a person can judge whether the
#: moment is worth clipping, and cropping to fill a vertical frame would hide
#: the terminal output that is usually the point of a technical recording.
#: CE-043 and CE-044 own the final presets; this is not one of them.
SCALING = "fit_pad"
PAD_COLOR = "black"

#: What ffprobe reports for the codecs above. libx264 muxes as h264, so the
#: encoder name and the verification name are deliberately separate values.
EXPECTED_VIDEO_CODEC = "h264"
EXPECTED_AUDIO_CODEC = "aac"


def preview_stage_config(width: int, height: int) -> PreviewStageConfig:
    """The effective configuration of one preview stage, in readable form."""
    return PreviewStageConfig(
        width=width,
        height=height,
        scaling=SCALING,
        pad_color=PAD_COLOR,
        video_codec=VIDEO_CODEC,
        encoder_preset=ENCODER_PRESET,
        crf=CRF,
        pixel_format=PIXEL_FORMAT,
        audio_codec=AUDIO_CODEC,
        audio_bitrate=AUDIO_BITRATE,
        audio_sample_rate=AUDIO_SAMPLE_RATE,
        audio_channels=AUDIO_CHANNELS,
        expected_video_codec=EXPECTED_VIDEO_CODEC,
        expected_audio_codec=EXPECTED_AUDIO_CODEC,
        duration_tolerance_seconds=PREVIEW_DURATION_TOLERANCE_SECONDS,
        preview_rules_version=PREVIEW_RULES_VERSION,
        argument_version=PREVIEW_ARGUMENT_VERSION,
        index_schema_version=PREVIEW_INDEX_SCHEMA_VERSION,
        candidates_schema_version=CANDIDATES_SCHEMA_VERSION,
    )


def preview_video_filter(config: PreviewStageConfig) -> str:
    """Fit the source frame inside the target and pad the rest.

    ``force_original_aspect_ratio=decrease`` scales until the frame fits in both
    dimensions, ``pad`` centres it, and ``setsar=1`` writes square pixels so no
    player stretches it back. A plain ``scale=w:h`` would distort every source
    whose aspect ratio is not 9:16, which is all of them.
    """
    return (
        f"scale={config.width}:{config.height}:force_original_aspect_ratio=decrease,"
        f"pad={config.width}:{config.height}:(ow-iw)/2:(oh-ih)/2:color={config.pad_color},"
        "setsar=1"
    )


def preview_arguments(
    source: Path,
    start: float,
    duration: float,
    output: Path,
    config: PreviewStageConfig,
) -> list[str]:
    """The exact FFmpeg invocation for one preview.

    ``-ss`` goes before ``-i`` so FFmpeg seeks rather than decoding the whole
    file up to the interval, and ``-t`` goes after it so the limit applies to
    what is written rather than to what is read. On a two-hour recording that
    ordering is the difference between seconds and minutes per preview.

    Timestamps are formatted to milliseconds. Handing FFmpeg a bare repr would
    make the command line depend on float formatting, and the command line is
    part of what the argument version promises to keep stable.
    """
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
        # One video and one audio stream, and nothing else. A source carrying
        # a subtitle or data track must not have it copied into a proxy.
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-sn",
        "-dn",
        "-vf",
        preview_video_filter(config),
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


def preview_stage_config_sha256(config: PreviewStageConfig) -> str:
    """Digest of the stage configuration exactly as it is written to disk."""
    return canonical_sha256(config.model_dump(mode="json"))


def preview_fingerprint(
    analysis_fingerprint: str,
    index: PreviewIndex,
    config: PreviewStageConfig,
) -> str:
    """The integrity of one preview execution, the produced files included.

    The index holds a digest of every file, so hashing the index hashes the
    output. Three parts and no chosen subset: every field left out would be a
    field a later run trusts without evidence.
    """
    return canonical_sha256(
        {
            "version": PREVIEW_FINGERPRINT_VERSION,
            "analysis_fingerprint": analysis_fingerprint,
            "index": index.model_dump(mode="json"),
            "config": config.model_dump(mode="json"),
        }
    )


def preview_coherence_problem(
    index: PreviewIndex,
    config: PreviewStageConfig,
    analysis_fingerprint: str,
    source_sha256: str,
    candidates: Sequence[ValidatedCandidate],
) -> str | None:
    """The first way a preview set contradicts the run it claims to describe.

    The fingerprint proves the index and the configuration were written
    together. It cannot prove they describe *this* run: a previews directory
    copied from another experiment would rebuild its own fingerprint perfectly
    and show a reviewer the wrong video.

    Returns a description rather than raising, because the caller decides what
    kind of failure it is: producing an incoherent set is a render bug, finding
    one on disk is an incompatible artifact.
    """
    if index.analysis_fingerprint != analysis_fingerprint:
        return (
            f"the previews were cut from analysis {index.analysis_fingerprint[:12]}, but the "
            f"run holds {analysis_fingerprint[:12]}"
        )
    if index.source_sha256 != source_sha256:
        return (
            f"the previews were cut from source {index.source_sha256[:12]}, but the run holds "
            f"{source_sha256[:12]}"
        )
    if (index.width, index.height) != (config.width, config.height):
        return (
            f"the index holds {index.width}x{index.height} previews and the stage "
            f"configuration asks for {config.width}x{config.height} dimensions"
        )
    if index.rules_version != config.preview_rules_version:
        return (
            f"the index names preview rules {index.rules_version} and the stage configuration "
            f"names {config.preview_rules_version}"
        )
    if config.candidates_schema_version != CANDIDATES_SCHEMA_VERSION:
        return (
            f"the previews were cut from candidate schema {config.candidates_schema_version}; "
            f"this build produces {CANDIDATES_SCHEMA_VERSION}"
        )

    recorded = index.by_candidate
    for candidate in candidates:
        entry = recorded.get(candidate.id)
        if entry is None:
            return f"candidate {candidate.id} has no preview in the index"
        if (
            abs(entry.start - candidate.start) > TIME_EPSILON
            or abs(entry.end - candidate.end) > TIME_EPSILON
        ):
            return (
                f"the preview for {candidate.id} covers the interval "
                f"[{entry.start}, {entry.end}] and the candidate is "
                f"[{candidate.start}, {candidate.end}]"
            )
        if entry.rank != candidate.rank:
            return (
                f"the preview for {candidate.id} is ranked {entry.rank} and the candidate is "
                f"ranked {candidate.rank}"
            )
    extra = sorted(set(recorded) - {candidate.id for candidate in candidates})
    if extra:
        return f"the index holds previews for candidates that were not selected: {extra}"
    return None
