class WikiBuilderError(Exception):
    """Base error with a concise user-facing message."""


class ValidationError(WikiBuilderError):
    """Input, generated data, or enrichment failed validation."""


class UnsafeOutputError(WikiBuilderError):
    """The configured output directory is not safe to replace."""


class ProviderError(WikiBuilderError):
    """The configured enrichment API failed."""
