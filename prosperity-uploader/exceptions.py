"""Custom exceptions for the prosperity uploader."""


class ProsperityUploaderError(Exception):
    """Base exception."""


class AuthenticationError(ProsperityUploaderError):
    """Token is missing, expired, or rejected (401/403)."""


class RateLimitError(ProsperityUploaderError):
    """Server returned 429 Too Many Requests."""

    def __init__(self, message: str = "Rate limited", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class UploadError(ProsperityUploaderError):
    """Upload failed."""


class SubmissionNotFoundError(ProsperityUploaderError):
    """Could not locate the submission after upload."""


class SubmissionTimeoutError(ProsperityUploaderError):
    """Polling timed out waiting for submission to complete."""


class GraphError(ProsperityUploaderError):
    """Failed to retrieve graph/artifact URL."""


class ArtifactDownloadError(ProsperityUploaderError):
    """Failed to download the signed S3 artifact."""


class ArtifactParseError(ProsperityUploaderError):
    """Failed to parse the artifact JSON."""


class ConfigError(ProsperityUploaderError):
    """Configuration error."""
