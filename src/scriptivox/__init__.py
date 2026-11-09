"""Official Python SDK for the Scriptivox transcription API.

    from scriptivox import Scriptivox

    client = Scriptivox()  # reads SCRIPTIVOX_API_KEY
    job = client.transcribe(file="meeting.mp3", language="en")
    done = client.wait(job["id"])
    print(done["result"]["full_transcript"])
"""
from ._async_client import AsyncScriptivox
from ._client import Scriptivox, UploadResult
from ._common import DEFAULT_BASE_URL, UploadProgress, safe_filename
from ._error_codes import *  # noqa: F401,F403  (one exception class per error.code)
from ._error_codes import API_ERROR_CODES, ERROR_CLASS_BY_CODE, HTTP_STATUS_BY_CODE, TRANSCRIPTION_ERROR_CODES
from ._error_codes import __all__ as _code_classes
from ._exceptions import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    GoneError,
    InternalServerError,
    MethodNotAllowedError,
    NotFoundError,
    PayloadTooLargeError,
    PaymentRequiredError,
    PermissionDeniedError,
    RateLimitError,
    ScriptivoxError,
    ServiceUnavailableError,
    TranscriptionFailedError,
    UnprocessableEntityError,
    UnsupportedMediaTypeError,
    UploadFailedError,
    WaitTimeoutError,
    WebhookVerificationError,
)
from ._types import (
    AutoTopup,
    Balance,
    FeedbackRequest,
    FeedbackResult,
    Transcription,
    TranscriptionCancelled,
    TranscriptionCreated,
    TranscriptionError,
    TranscriptionPage,
    TranscriptionResult,
    TranscriptionStatus,
    TranscriptionSummary,
    Upload,
    Utterance,
    WebhookSendResult,
    WebhookTest,
    WebhookTranscriptionCompleted,
    WebhookTranscriptionDownloading,
    WebhookTranscriptionFailed,
    WebhookTranscriptionProcessing,
    Word,
)
from ._version import __version__
from ._webhooks import (
    ATTEMPT_HEADER,
    DEFAULT_TOLERANCE,
    DELIVERY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookEvent,
    compute_signature,
    verify_webhook,
)

__all__ = [
    "Scriptivox",
    "AsyncScriptivox",
    "verify_webhook",
    "compute_signature",
    "safe_filename",
    "UploadProgress",
    "UploadResult",
    "DEFAULT_BASE_URL",
    "DEFAULT_TOLERANCE",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "DELIVERY_HEADER",
    "ATTEMPT_HEADER",
    "WebhookEvent",
    "ScriptivoxError",
    "APIError",
    "APIConnectionError",
    "APITimeoutError",
    "BadRequestError",
    "AuthenticationError",
    "PaymentRequiredError",
    "PermissionDeniedError",
    "NotFoundError",
    "MethodNotAllowedError",
    "ConflictError",
    "GoneError",
    "PayloadTooLargeError",
    "UnsupportedMediaTypeError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    "ServiceUnavailableError",
    "UploadFailedError",
    "TranscriptionFailedError",
    "WaitTimeoutError",
    "WebhookVerificationError",
    "AutoTopup",
    "Balance",
    "FeedbackRequest",
    "FeedbackResult",
    "Transcription",
    "TranscriptionCancelled",
    "TranscriptionCreated",
    "TranscriptionError",
    "TranscriptionPage",
    "TranscriptionResult",
    "TranscriptionStatus",
    "TranscriptionSummary",
    "Upload",
    "Utterance",
    "WebhookSendResult",
    "WebhookTest",
    "WebhookTranscriptionCompleted",
    "WebhookTranscriptionDownloading",
    "WebhookTranscriptionFailed",
    "WebhookTranscriptionProcessing",
    "Word",
    "__version__",
    *_code_classes,
]

# Tracebacks and reprs name the public module (scriptivox.ZeroBalanceError), not the private file it lives in.
for _name in __all__:
    _obj = globals().get(_name)
    if isinstance(_obj, type) and getattr(_obj, "__module__", "").startswith("scriptivox._"):
        try:
            _obj.__module__ = __name__
        except (AttributeError, TypeError):  # pragma: no cover
            pass
del _name, _obj
