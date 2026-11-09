"""Webhook signature check. The signing formula (platform.scriptivox.com/docs/webhooks)::

    signature = hex(HMAC-SHA256(secret, f"{X-Scriptivox-Timestamp}.{raw body}"))

where ``secret`` is the key's whole ``whsec_…`` string. A timestamp more than 5 minutes from now is refused.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Iterable, Mapping, Optional, Tuple, Union, cast

from ._exceptions import WebhookVerificationError
from ._types import WebhookTest, WebhookTranscriptionCompleted, WebhookTranscriptionDownloading, WebhookTranscriptionFailed, WebhookTranscriptionProcessing

SIGNATURE_HEADER = "X-Scriptivox-Signature"
TIMESTAMP_HEADER = "X-Scriptivox-Timestamp"
DELIVERY_HEADER = "X-Scriptivox-Delivery"
ATTEMPT_HEADER = "X-Scriptivox-Attempt"
#: Seconds a webhook's timestamp may be away from now.
DEFAULT_TOLERANCE = 300

#: Every event body a webhook can carry; switch on ``event``. Answer 2xx to event types you do not know.
WebhookEvent = Union[
    WebhookTranscriptionProcessing,
    WebhookTranscriptionCompleted,
    WebhookTranscriptionFailed,
    WebhookTranscriptionDownloading,
    WebhookTest,
]

HeadersLike = Union[Mapping[str, Any], Iterable[Tuple[str, str]]]


def read_header(headers: HeadersLike, name: str) -> Optional[str]:
    """Case-insensitive lookup in a dict, httpx/Starlette/Django/Flask headers, or a list of pairs."""
    wanted = name.lower()
    items = headers.items() if hasattr(headers, "items") else headers
    for key, value in cast(Iterable[Tuple[str, Any]], items):
        if isinstance(key, bytes):
            key = key.decode("latin-1")
        if key.lower() != wanted or value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        return None if value is None else str(value)
    return None


def _body_bytes(raw_body: Union[bytes, bytearray, memoryview, str]) -> bytes:
    if isinstance(raw_body, str):
        return raw_body.encode("utf-8")
    if isinstance(raw_body, (bytes, bytearray, memoryview)):
        return bytes(raw_body)
    raise TypeError(
        "verify_webhook needs the raw request body (bytes or str), not parsed JSON: "
        "re-serialising changes the bytes and the signature."
    )


def compute_signature(raw_body: Union[bytes, bytearray, memoryview, str], timestamp: str, secret: str) -> str:
    """The signature the API would send for this body and timestamp."""
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8"), hashlib.sha256)
    mac.update(_body_bytes(raw_body))
    return mac.hexdigest()


def verify_webhook(
    raw_body: Union[bytes, bytearray, memoryview, str],
    headers: HeadersLike,
    secret: str,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: Optional[float] = None,
) -> WebhookEvent:
    """Check that a webhook request came from Scriptivox and is fresh, then return its parsed event.

    Pass the body exactly as received (``await request.body()``, ``request.get_data()``,
    ``request.body``), never re-serialised JSON. Raises WebhookVerificationError; answer such a
    request with 400. Deduplicate on the X-Scriptivox-Delivery header: an event can arrive twice.
    """
    body = _body_bytes(raw_body)
    if not isinstance(secret, str) or not secret:
        raise TypeError("verify_webhook needs the key's webhook signing secret (whsec_…).")
    signature = read_header(headers, SIGNATURE_HEADER)
    timestamp = read_header(headers, TIMESTAMP_HEADER)
    if not signature or not timestamp:
        raise WebhookVerificationError("missing_header", "Missing X-Scriptivox-Signature or X-Scriptivox-Timestamp header.")
    timestamp = timestamp.strip()
    if not timestamp.isdigit():
        raise WebhookVerificationError("invalid_timestamp", "X-Scriptivox-Timestamp is not a Unix time in seconds.")
    current = time.time() if now is None else now
    if abs(int(current) - int(timestamp)) > tolerance:
        raise WebhookVerificationError(
            "timestamp_outside_tolerance",
            f"The webhook's timestamp is more than {tolerance} s from now: refused as a possible replay.",
        )
    expected = compute_signature(body, timestamp, secret)
    if not hmac.compare_digest(expected.encode("ascii"), signature.strip().lower().encode("utf-8")):
        raise WebhookVerificationError("signature_mismatch", "The webhook signature does not match: wrong secret, or the body was changed.")
    try:
        return cast(WebhookEvent, json.loads(body.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        raise WebhookVerificationError("invalid_json", "The webhook body is not JSON.") from None
