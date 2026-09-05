"""Expected application failures and the process exit codes they map to.

Exit codes follow the V0 specification: 0 success, 1 unknown, 2 configuration,
3 invalid input or media, 4 transcription, 5 analysis, 6 render.
"""

EXIT_SUCCESS = 0
EXIT_UNKNOWN = 1
EXIT_CONFIGURATION = 2
EXIT_INVALID_INPUT = 3
EXIT_TRANSCRIPTION = 4
EXIT_ANALYSIS = 5
EXIT_RENDER = 6


class ContentEngineError(Exception):
    """Base exception for expected application failures."""

    exit_code: int = EXIT_UNKNOWN
    title: str = "Error"


class ConfigurationError(ContentEngineError):
    exit_code = EXIT_CONFIGURATION
    title = "Configuration error"


class ExternalToolNotFoundError(ConfigurationError):
    """FFmpeg or ffprobe is missing from PATH: an environment problem."""

    title = "Missing external tool"


class ExternalToolError(ContentEngineError):
    """An external tool ran and reported failure.

    Adapters translate this into the failure that matters to the caller, such as
    InvalidMediaError for a file ffprobe cannot read. It should not reach the CLI.
    """

    title = "External tool failed"


class InvalidMediaError(ContentEngineError):
    exit_code = EXIT_INVALID_INPUT
    title = "Invalid media"


class NoAudioStreamError(InvalidMediaError):
    title = "Missing audio stream"


class AudioExtractionError(ContentEngineError):
    exit_code = EXIT_INVALID_INPUT
    title = "Audio extraction failed"


class InvalidRunIdError(ContentEngineError):
    exit_code = EXIT_INVALID_INPUT
    title = "Invalid run identifier"


class RunNotFoundError(ContentEngineError):
    exit_code = EXIT_INVALID_INPUT
    title = "Run not found"


class InvalidRunStateError(ContentEngineError):
    exit_code = EXIT_INVALID_INPUT
    title = "Invalid run state transition"


class CorruptArtifactError(ContentEngineError):
    exit_code = EXIT_INVALID_INPUT
    title = "Corrupt run artifact"


class UnsupportedSchemaVersionError(CorruptArtifactError):
    title = "Unsupported artifact schema"


class IncompatibleArtifactError(ContentEngineError):
    exit_code = EXIT_INVALID_INPUT
    title = "Incompatible artifact"


class TranscriptionError(ContentEngineError):
    exit_code = EXIT_TRANSCRIPTION
    title = "Transcription failed"


class AnalysisError(ContentEngineError):
    exit_code = EXIT_ANALYSIS
    title = "Analysis failed"


class ExternalProviderError(ContentEngineError):
    exit_code = EXIT_ANALYSIS
    title = "External provider failed"


class InvalidCandidateError(ContentEngineError):
    exit_code = EXIT_ANALYSIS
    title = "Invalid candidate"


class RenderError(ContentEngineError):
    exit_code = EXIT_RENDER
    title = "Render failed"
