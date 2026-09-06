"""Normalization and validation rules for provider transcription output.

The distinction this module draws is deliberate. Whitespace, empty segments and
differences small enough to be floating-point noise are normalized and counted.
Anything larger is a real disagreement between the provider and the audio, and
is rejected rather than quietly repaired: a transcript that silently absorbed a
thirty second offset would poison every downstream stage.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime

from content_engine.domain.exceptions import TranscriptionError
from content_engine.domain.models import (
    TRANSCRIPT_SCHEMA_VERSION,
    NormalizationReport,
    RawSegment,
    RawTranscription,
    ResolvedHardware,
    Transcript,
    TranscriptionOptions,
    TranscriptionStageConfig,
    TranscriptSegment,
    TranscriptWord,
)
from content_engine.utils.hashing import sha256_bytes

#: Largest correction attributable to floating point and provider rounding.
TIMESTAMP_TOLERANCE_SECONDS = 0.05
#: Largest accepted disagreement between the real audio duration and the one the
#: transcriber declares. Bigger means they were not looking at the same audio.
DURATION_TOLERANCE_SECONDS = 0.5
#: Bumped whenever these rules change, so fingerprints and reports stay comparable.
NORMALIZATION_RULES_VERSION = 1

#: Guards the comparisons themselves against binary floating point, so a gap of
#: exactly the tolerance is corrected rather than rejected by representation error.
_COMPARISON_EPSILON = 1e-9

_MAX_NOTES = 20


@dataclass
class _Report:
    clamped_segment_bounds: int = 0
    clamped_word_bounds: int = 0
    dropped_empty_segments: int = 0
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        if len(self.notes) < _MAX_NOTES:
            self.notes.append(message)
        elif len(self.notes) == _MAX_NOTES:
            self.notes.append("further normalizations omitted")

    def to_model(self) -> NormalizationReport:
        return NormalizationReport(
            rules_version=NORMALIZATION_RULES_VERSION,
            tolerance_seconds=TIMESTAMP_TOLERANCE_SECONDS,
            clamped_segment_bounds=self.clamped_segment_bounds,
            clamped_word_bounds=self.clamped_word_bounds,
            dropped_empty_segments=self.dropped_empty_segments,
            notes=self.notes,
        )


def _require_finite(value: float, label: str) -> float:
    """Refuse NaN and the infinities before any comparison touches them.

    Every ordering test against NaN is false, so a NaN start would slip past the
    tolerance checks and be silently clamped to the bound it was compared with.
    A provider that emits one is not describing a position in the audio, and the
    output is refused rather than repaired.
    """
    if not math.isfinite(value):
        raise TranscriptionError(
            f"{label}: {value} is not a finite number, so it cannot describe a "
            "position in the audio. The provider output is refused."
        )
    return value


def _reject(message: str) -> None:
    raise TranscriptionError(
        f"{message}. The difference exceeds the {TIMESTAMP_TOLERANCE_SECONDS}s "
        "normalization tolerance, so the output is rejected instead of corrected."
    )


def _pull_up(value: float, floor: float, label: str, report: _Report, counter: str) -> float:
    """Raise ``value`` to ``floor`` when the gap is noise, reject when it is not."""
    if value >= floor:
        return value
    if floor - value > TIMESTAMP_TOLERANCE_SECONDS + _COMPARISON_EPSILON:
        _reject(f"{label}: {value} is before {floor}")
    setattr(report, counter, getattr(report, counter) + 1)
    report.note(f"{label}: raised {value} to {floor}")
    return floor


def _pull_down(value: float, ceiling: float, label: str, report: _Report, counter: str) -> float:
    """Lower ``value`` to ``ceiling`` when the gap is noise, reject when it is not."""
    if value <= ceiling:
        return value
    if value - ceiling > TIMESTAMP_TOLERANCE_SECONDS + _COMPARISON_EPSILON:
        _reject(f"{label}: {value} is beyond {ceiling}")
    setattr(report, counter, getattr(report, counter) + 1)
    report.note(f"{label}: lowered {value} to {ceiling}")
    return ceiling


def _normalize_words(
    segment: RawSegment,
    start: float,
    end: float,
    index: int,
    report: _Report,
) -> list[TranscriptWord]:
    words: list[TranscriptWord] = []
    previous_start = start
    for position, raw_word in enumerate(segment.words):
        label = f"segment {index} word {position}"
        _require_finite(raw_word.start, f"{label} start")
        _require_finite(raw_word.end, f"{label} end")
        if raw_word.probability is not None:
            _require_finite(raw_word.probability, f"{label} probability")
        word_start = _pull_up(raw_word.start, previous_start, label, report, "clamped_word_bounds")
        word_start = _pull_down(word_start, end, label, report, "clamped_word_bounds")
        word_end = _pull_up(raw_word.end, word_start, label, report, "clamped_word_bounds")
        word_end = _pull_down(word_end, end, label, report, "clamped_word_bounds")
        words.append(
            TranscriptWord(
                word=" ".join(str(raw_word.word).split()),
                start=word_start,
                end=word_end,
                probability=raw_word.probability,
            )
        )
        previous_start = word_start
    return words


def normalize_transcription(
    raw: RawTranscription,
    audio_duration_seconds: float,
    created_at: datetime,
) -> tuple[Transcript, NormalizationReport]:
    """Turn provider output into a validated transcript, or refuse it."""
    _require_finite(audio_duration_seconds, "audio duration")
    if audio_duration_seconds <= 0:
        raise TranscriptionError(f"Audio duration must be positive, got {audio_duration_seconds}")

    if raw.language_probability is not None:
        _require_finite(raw.language_probability, "language probability")

    declared = raw.declared_duration_seconds
    if declared is not None:
        _require_finite(declared, "declared duration")
    if declared is not None and (
        abs(declared - audio_duration_seconds) > DURATION_TOLERANCE_SECONDS + _COMPARISON_EPSILON
    ):
        raise TranscriptionError(
            f"The transcriber declared {declared}s of audio but the file measures "
            f"{audio_duration_seconds}s. The difference exceeds "
            f"{DURATION_TOLERANCE_SECONDS}s, so the transcript is not trusted."
        )

    report = _Report()
    segments: list[TranscriptSegment] = []
    previous_start = 0.0

    for position, raw_segment in enumerate(raw.segments):
        label = f"segment {position}"
        _require_finite(raw_segment.start, f"{label} start")
        _require_finite(raw_segment.end, f"{label} end")
        start = _pull_up(raw_segment.start, previous_start, label, report, "clamped_segment_bounds")
        start = _pull_down(start, audio_duration_seconds, label, report, "clamped_segment_bounds")
        end = _pull_up(raw_segment.end, start, label, report, "clamped_segment_bounds")
        end = _pull_down(end, audio_duration_seconds, label, report, "clamped_segment_bounds")

        text = " ".join(str(raw_segment.text).split())
        if not text:
            report.dropped_empty_segments += 1
            report.note(f"{label}: dropped, no text")
            previous_start = start
            continue

        segments.append(
            TranscriptSegment(
                index=len(segments),
                start=start,
                end=end,
                text=text,
                words=_normalize_words(raw_segment, start, end, position, report),
            )
        )
        previous_start = start

    transcript = Transcript(
        language=raw.language,
        language_probability=raw.language_probability,
        duration_seconds=audio_duration_seconds,
        declared_duration_seconds=declared,
        segments=segments,
        model=raw.model,
        created_at=created_at,
    )
    return transcript, report.to_model()


def stage_config(
    options: TranscriptionOptions, hardware: ResolvedHardware
) -> TranscriptionStageConfig:
    """Convert a request plus the hardware it resolved to into the stage record.

    This is the only conversion from ``TranscriptionOptions`` and
    ``ResolvedHardware`` into the shape that both identifies and explains a
    transcription, so the artifact on disk and the fingerprint cannot be built
    from different pictures of the same run.
    """
    return TranscriptionStageConfig(
        provider=options.provider,
        model=options.model,
        beam_size=options.beam_size,
        word_timestamps=options.word_timestamps,
        vad_filter=options.vad_filter,
        device_requested=options.device,
        device_resolved=hardware.device,
        compute_type_requested=options.compute_type,
        compute_type_resolved=hardware.compute_type,
        normalization_version=NORMALIZATION_RULES_VERSION,
    )


def _canonical(payload: dict[str, object]) -> str:
    """One serialization for every hash in this module: sorted, compact, strict."""
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def stage_config_sha256(config: TranscriptionStageConfig) -> str:
    """Hash the stage configuration so a manifest can address that exact artifact.

    Independent of how the artifact is laid out on disk, so reformatting the JSON
    does not invalidate a run.
    """
    return sha256_bytes(_canonical(config.model_dump(mode="json")).encode("utf-8"))


def transcription_fingerprint(audio_sha256: str, config: TranscriptionStageConfig) -> str:
    """Identify the real inputs and conditions of one transcription.

    This is not ``config_sha256``. The configuration hash is portable: the same
    logical experiment hashes identically on any machine. This fingerprint is
    deliberately not portable, because ``device = "auto"`` resolves to CPU/int8
    on one machine and CUDA/float16 on another, and those two runs do not
    produce the same transcript. Two executions with different resolved hardware
    must not be treated as interchangeable.

    It is derived from ``TranscriptionStageConfig`` and nothing else, so the
    readable artifact a run keeps on disk and the digest that decides reuse are
    the same statement twice. Recomputing this from the stored artifact is what
    lets the reuse path prove the two still agree.
    """
    payload: dict[str, object] = dict(config.model_dump(mode="json"))
    payload["audio_sha256"] = audio_sha256
    payload["transcript_schema_version"] = TRANSCRIPT_SCHEMA_VERSION
    return sha256_bytes(_canonical(payload).encode("utf-8"))
