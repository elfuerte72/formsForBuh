"""Domain-level error taxonomy.

Services translate SDK-specific exceptions into these types so pipelines can
handle a single, stable taxonomy.
"""


class AppError(Exception):
    """Base class for all domain errors raised from services/pipelines."""


class FileDownloadError(AppError):
    """Failure downloading a file from the URL supplied in the webhook payload."""


class UnsupportedFileTypeError(AppError):
    """File was downloaded but its media type is not PDF / image/*."""


class VisionExtractionError(AppError):
    """Claude Vision call failed or returned a malformed response."""


class RateLimitExceededError(VisionExtractionError):
    """Anthropic rate limit / quota persisted through all retries."""


class SheetsAppendError(AppError):
    """Failure appending a row to the Google Sheet."""


class SheetsReadError(AppError):
    """Failure reading rows from the Google Sheet."""


class OneCParseError(AppError):
    """Failure parsing a 1С export (xls/xlsx/csv)."""


class DriveUploadError(AppError):
    """Failure archiving the original scan to Google Drive.

    Treated as a *soft* failure in the upload pipeline: the Sheets row is still
    written (without a link) and the form warns — the scan can be re-attached
    later. The registry row is the primary value.
    """
