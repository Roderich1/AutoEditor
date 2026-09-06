from enum import StrEnum


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
