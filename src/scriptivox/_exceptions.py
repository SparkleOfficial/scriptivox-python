"""Exceptions. Everything the SDK raises extends ScriptivoxError.

    ScriptivoxError
    ├── APIError                        the API answered with an error (status, code, request_id)
    │   ├── BadRequestError (400)       ├── InvalidRequestError, InvalidFilenameError, ... (one per error.code,
    │   ├── AuthenticationError (401)   │   generated in _error_codes.py from the OpenAPI document)
    │   ├── PaymentRequiredError (402)  ├── InsufficientBalanceError, ZeroBalanceError
    │   ├── ... one class per HTTP status the API uses
    │   └── RateLimitError (429)        └── RateLimitExceededError
    ├── APIConnectionError              no answer: DNS, refused, reset (APITimeoutError: no answer in time)
    ├── UploadFailedError               the file PUT to the presigned upload URL failed
    ├── TranscriptionFailedError        wait(): the job ended with status "failed" (code = the job's error.code)
    ├── WaitTimeoutError                wait(): the job was still running when the timeout passed
    └── WebhookVerificationError        verify_webhook(): bad signature, old timestamp, missing header
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Type


class ScriptivoxError(Exception):
    """Base class of every error the SDK raises."""


class APIError(ScriptivoxError):
    """The API answered with an HTTP error. Branch on ``code`` (stable) or the class; never on the message."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        request_id: Optional[str] = None,
        docs_url: Optional[str] = None,
        retry_after: Optional[float] = None,
        error: Optional[Dict[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        ref = ", ".join(x for x in (code, f"request {request_id}" if request_id else None) if x)
        super().__init__(f"{message} [{ref}]")
        #: HTTP status.
        self.status = status
        #: The API's machine-readable ``error.code``, e.g. "ZERO_BALANCE"; "UNKNOWN_ERROR" when the body had none.
        self.code = code
        #: The server's message (without the bracketed code and request id).
        self.message = message
        #: ``X-Request-Id`` / ``error.request_id`` (``req_`` + 16 hex). Quote it to support.
        self.request_id = request_id
        self.docs_url = docs_url
        #: Seconds from the ``Retry-After`` header, when the answer had one.
        self.retry_after = retry_after
        #: The whole ``error`` object (extra fields such as ``in_flight`` and ``limit`` live here).
        self.error: Dict[str, Any] = dict(error or {})
        #: Response headers, lower-case names.
        self.headers: Dict[str, str] = {k.lower(): v for k, v in (headers or {}).items()}


class BadRequestError(APIError):
    pass


class AuthenticationError(APIError):
    pass


class PaymentRequiredError(APIError):
    pass


class PermissionDeniedError(APIError):
    pass


class NotFoundError(APIError):
    pass


class MethodNotAllowedError(APIError):
    pass


class ConflictError(APIError):
    pass


class GoneError(APIError):
    pass


class PayloadTooLargeError(APIError):
    pass


class UnsupportedMediaTypeError(APIError):
    pass


class UnprocessableEntityError(APIError):
    pass


class RateLimitError(APIError):
    pass


class InternalServerError(APIError):
    pass


class ServiceUnavailableError(APIError):
    pass


_CATEGORY_BY_STATUS: Dict[int, Type[APIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    402: PaymentRequiredError,
    403: PermissionDeniedError,
    404: NotFoundError,
    405: MethodNotAllowedError,
    409: ConflictError,
    410: GoneError,
    413: PayloadTooLargeError,
    415: UnsupportedMediaTypeError,
    422: UnprocessableEntityError,
    429: RateLimitError,
    503: ServiceUnavailableError,
}


def category_for_status(status: int) -> Type[APIError]:
    """The category class for an HTTP status, used when a code is not one the SDK knows."""
    if status in _CATEGORY_BY_STATUS:
        return _CATEGORY_BY_STATUS[status]
    return InternalServerError if status >= 500 else APIError


class APIConnectionError(ScriptivoxError):
    """No HTTP answer at all: DNS failure, connection refused or reset."""


class APITimeoutError(APIConnectionError):
    """No answer within the request timeout."""


class UploadFailedError(ScriptivoxError):
    """The PUT of the file bytes to the presigned upload URL failed."""

    def __init__(self, message: str, *, status: Optional[int] = None, body: Optional[str] = None) -> None:
        super().__init__(message)
        #: HTTP status from the storage server, when it answered.
        self.status = status
        #: The start of the storage server's answer (often XML).
        self.body = body


class TranscriptionFailedError(ScriptivoxError):
    """wait(): the job ended with ``status: "failed"``. Failed jobs cost nothing.

    ``code`` is the job's ``error.code`` (URL_NOT_ACCESSIBLE, INVALID_MEDIA_FORMAT, CANCELLED, ...);
    ``transcription`` is the job.
    """

    def __init__(self, transcription: Mapping[str, Any]) -> None:
        error = transcription.get("error") or {}
        self.code: str = error.get("code") or "UNKNOWN_ERROR"
        self.transcription_id: str = transcription.get("id", "")
        self.transcription: Dict[str, Any] = dict(transcription)
        message = error.get("message") or "The transcription failed."
        super().__init__(f"Transcription {self.transcription_id} failed: {message} [{self.code}]")


class WaitTimeoutError(ScriptivoxError):
    """wait(): the timeout passed while the job was still running. The job carries on; wait again later."""

    def __init__(self, transcription: Mapping[str, Any], timeout: float) -> None:
        self.transcription_id: str = transcription.get("id", "")
        #: The last status seen.
        self.transcription: Dict[str, Any] = dict(transcription)
        super().__init__(
            f'Transcription {self.transcription_id} is still "{transcription.get("status")}" after {round(timeout)} s. '
            "It carries on running; call wait() again or get() later."
        )


class WebhookVerificationError(ScriptivoxError):
    """verify_webhook(): not a genuine, fresh Scriptivox webhook. Answer such a request with 400.

    ``reason`` is one of: missing_header, invalid_timestamp, timestamp_outside_tolerance,
    signature_mismatch, invalid_json.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
