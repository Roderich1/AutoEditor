class ContentEngineError(Exception):
    """Base exception for expected application failures."""


class ConfigurationError(ContentEngineError):
    pass


class InvalidMediaError(ContentEngineError):
    pass


class NoAudioStreamError(InvalidMediaError):
    pass


class TranscriptionError(ContentEngineError):
    pass


class AnalysisError(ContentEngineError):
    pass


class InvalidCandidateError(ContentEngineError):
    pass


class RenderError(ContentEngineError):
    pass


class ExternalProviderError(ContentEngineError):
    pass
