from enum import StrEnum
from types import MappingProxyType


class RunStatus(StrEnum):
    CREATED = "CREATED"
    INSPECTED = "INSPECTED"
    AUDIO_READY = "AUDIO_READY"
    TRANSCRIBED = "TRANSCRIBED"
    ANALYZED = "ANALYZED"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    REVIEWED = "REVIEWED"
    RENDERED = "RENDERED"
    COMPLETED = "COMPLETED"
    FAILED_INSPECT = "FAILED_INSPECT"
    FAILED_AUDIO = "FAILED_AUDIO"
    FAILED_TRANSCRIPTION = "FAILED_TRANSCRIPTION"
    FAILED_ANALYSIS = "FAILED_ANALYSIS"
    FAILED_RENDER = "FAILED_RENDER"


class RunStage(StrEnum):
    """Pipeline stage a run can be executing or can have failed in."""

    INSPECT = "inspect"
    AUDIO = "audio"
    TRANSCRIPTION = "transcription"
    ANALYSIS = "analysis"
    RENDER = "render"


class TranscriptionProvider(StrEnum):
    FASTER_WHISPER = "faster-whisper"


class Device(StrEnum):
    AUTO = "auto"
    CPU = "cpu"
    CUDA = "cuda"


class ComputeType(StrEnum):
    AUTO = "auto"
    INT8 = "int8"
    INT8_FLOAT16 = "int8_float16"
    INT8_BFLOAT16 = "int8_bfloat16"
    FLOAT16 = "float16"
    BFLOAT16 = "bfloat16"
    FLOAT32 = "float32"


class AnalysisProvider(StrEnum):
    """ADR-019. One member on purpose: a configuration may only name a provider
    this build has an adapter for, or is committed to building."""

    GEMINI = "gemini"


class RenderPreset(StrEnum):
    VERTICAL_BLUR = "vertical_blur"
    VERTICAL_CROP = "vertical_crop"


class ClipCategory(StrEnum):
    """The kinds of moment worth clipping from a technical recording.

    A closed set on purpose: an open category field would let the model invent
    labels that no downstream metric could group by, and category performance is
    one of the evaluation signals the project is built to measure.
    """

    PROBLEM_SOLUTION = "problem_solution"
    ERROR_LEARNING = "error_learning"
    QUICK_TUTORIAL = "quick_tutorial"
    EXPLANATION = "explanation"
    DISCOVERY = "discovery"
    OPINION = "opinion"
    RESULT = "result"
    BEFORE_AFTER = "before_after"
    TIP = "tip"
    STORY = "story"
    DEMONSTRATION = "demonstration"


class CandidateStatus(StrEnum):
    """What became of a candidate the analyzer proposed.

    Nothing is deleted. A candidate that failed validation or lost a duplicate
    comparison is kept with the reason, because the rate at which the model
    produces each is the measurement that tells us whether the prompt is working.
    """

    SUGGESTED = "suggested"
    REJECTED = "rejected"
    DEDUPLICATED = "deduplicated"


class RejectionReason(StrEnum):
    """Why a proposal did not end up in the final list.

    The first group is CE-030: refused before scoring, so the record is an
    InvalidCandidate with no interval of its own. The second group is everything
    that was scored first and dropped afterwards, so those records are complete
    ValidatedCandidates.
    """

    INVALID_INTERVAL = "invalid_interval"
    OUTSIDE_CHUNK = "outside_chunk"
    END_BEYOND_SOURCE = "end_beyond_source"
    UNGROUNDED = "ungrounded"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"

    BELOW_MIN_SCORE = "below_min_score"
    DUPLICATE = "duplicate"
    #: Survived every rule and was still cut by the max_candidates ceiling.
    NOT_IN_TOP_N = "not_in_top_n"


#: Reasons CE-030 can reach. They are decided by looking at the proposal and the
#: chunk alone, before any score exists, so a record citing one of these never
#: got far enough to have an interval, a boundary or a total of its own.
PRE_SCORING_REASONS: frozenset[RejectionReason] = frozenset(
    {
        RejectionReason.INVALID_INTERVAL,
        RejectionReason.OUTSIDE_CHUNK,
        RejectionReason.END_BEYOND_SOURCE,
        RejectionReason.UNGROUNDED,
        RejectionReason.TOO_SHORT,
        RejectionReason.TOO_LONG,
    }
)

#: The three outcomes that require a score to have been computed first, one per
#: stage of the deterministic pipeline. Named individually because each is the
#: only reason its stage can produce, and code that means "the duplicate one"
#: should say so rather than index a set.
BELOW_SCORE_REASON = RejectionReason.BELOW_MIN_SCORE
DEDUPE_REASON = RejectionReason.DUPLICATE
TOP_N_REASON = RejectionReason.NOT_IN_TOP_N

POST_SCORING_REASONS: frozenset[RejectionReason] = frozenset(
    {BELOW_SCORE_REASON, DEDUPE_REASON, TOP_N_REASON}
)

#: The reasons each terminal status may carry, and the whole of them. A status
#: absent from this mapping carries no reason at all, which is why SUGGESTED is
#: not a key. Kept here rather than in the models so that adding a rejection
#: reason forces a decision about which phase and which status owns it.
TERMINAL_REASONS: MappingProxyType[CandidateStatus, frozenset[RejectionReason]] = MappingProxyType(
    {
        CandidateStatus.REJECTED: frozenset({BELOW_SCORE_REASON, TOP_N_REASON}),
        CandidateStatus.DEDUPLICATED: frozenset({DEDUPE_REASON}),
    }
)


class BoundaryAnchor(StrEnum):
    """What a snapped boundary was moved onto, or that it was left alone."""

    SEGMENT_START = "segment_start"
    SEGMENT_END = "segment_end"
    WORD_START = "word_start"
    WORD_END = "word_end"
    UNCHANGED = "unchanged"
