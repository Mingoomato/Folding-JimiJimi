class LocalRuntimeError(Exception):
    """Base error raised by the local runtime."""


class EventConflictError(LocalRuntimeError):
    """The event conflicts with the active source state."""


class SourceAccessError(LocalRuntimeError):
    """The source path is missing or outside the configured roots."""


class ConversionError(LocalRuntimeError):
    """The doc2md service failed or returned an invalid result."""


class BuildActivationError(LocalRuntimeError):
    """A candidate build was generated but was not activated."""
