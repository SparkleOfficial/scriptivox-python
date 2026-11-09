"""The synchronous client."""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, Iterator, List, Mapping, Optional, Union, cast, overload

import httpx

from . import _common as c
from ._exceptions import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    RateLimitError,
    ScriptivoxError,
    TranscriptionFailedError,
    UploadFailedError,
    WaitTimeoutError,
)
from ._types import (
    Balance,
    FeedbackResult,
    Transcription,
    TranscriptionCancelled,
    TranscriptionCreated,
    TranscriptionPage,
    TranscriptionStatus,
    TranscriptionSummary,
    Upload,
    WebhookSendResult,
)
from ._webhooks import HeadersLike, WebhookEvent, verify_webhook

from typing import Literal, TypedDict

_MISSING: Any = object()


class UploadResult(TypedDict):
    upload_id: str
    #: The filename the API stored (after making it ASCII-safe).
    filename: str
    #: Bytes uploaded.
    size: int


def _resolve_key(api_key: Optional[str]) -> str:
    key = api_key or os.environ.get("SCRIPTIVOX_API_KEY")
    if not key:
        raise ScriptivoxError("No API key: pass api_key= or set the SCRIPTIVOX_API_KEY environment variable.")
    return key


class Scriptivox:
    """The Scriptivox API client (synchronous). For asyncio use AsyncScriptivox.

    >>> client = Scriptivox()  # reads SCRIPTIVOX_API_KEY
    >>> job = client.transcribe(file="meeting.mp3", language="en")
    >>> done = client.wait(job["id"])
    >>> print(done["result"]["full_transcript"])
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        max_retries: int = c.DEFAULT_MAX_RETRIES,
        timeout: float = c.DEFAULT_TIMEOUT,
        http_client: Optional[httpx.Client] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._api_key = _resolve_key(api_key)
        self.base_url = c.normalize_base_url(base_url or os.environ.get("SCRIPTIVOX_BASE_URL") or c.DEFAULT_BASE_URL)
        self.max_retries = max_retries
        self.timeout = timeout
        self._default_headers = dict(default_headers or {})
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(follow_redirects=False)

    # ─── lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "Scriptivox":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ─── transport ────────────────────────────────────────────────────────────

    def _send(
        self,
        method: str,
        path: str,
        *,
        idempotent: bool,
        query: Optional[Mapping[str, Any]] = None,
        body: Any = _MISSING,
        idempotency_key: Optional[str] = None,
        text: bool = False,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> Any:
        retries = self.max_retries if max_retries is None else max_retries
        headers: Dict[str, str] = {
            **self._default_headers,
            "Authorization": self._api_key,
            "Accept": "text/plain, text/vtt, application/json" if text else "application/json",
            "User-Agent": c.user_agent(),
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        kwargs: Dict[str, Any] = {}
        if body is not _MISSING:
            kwargs["json"] = body
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    method,
                    self.base_url + path,
                    params=c.build_query(query or {}),
                    headers=headers,
                    timeout=self.timeout if timeout is None else timeout,
                    **kwargs,
                )
            except httpx.TimeoutException as exc:
                err: Exception = APITimeoutError(f"No answer from {method} {path} in time.")
                err.__cause__ = exc
            except httpx.TransportError as exc:
                err = APIConnectionError(f"Could not reach the Scriptivox API ({method} {path}): {exc}")
                err.__cause__ = exc
            else:
                if response.is_success:
                    if text:
                        return response.text
                    if not response.content:
                        return None
                    try:
                        return response.json()
                    except ValueError:
                        raise APIError(
                            status=response.status_code,
                            code="INVALID_RESPONSE",
                            message=f"The API answered {method} {path} with a body that is not JSON.",
                            request_id=response.headers.get("x-request-id"),
                            headers=response.headers,
                        ) from None
                api_err = c.error_from_response(response.status_code, response.text, response.headers)
                delay = c.retry_delay_for(api_err, idempotent, attempt) if attempt < retries else None
                if delay is None:
                    raise api_err
                time.sleep(delay)
                attempt += 1
                continue
            if idempotent and attempt < retries:
                time.sleep(c.backoff(attempt))
                attempt += 1
                continue
            raise err

    def _put(self, upload_url: str, headers: Mapping[str, str], source: c.FileSource, on_progress: Optional[c.ProgressCallback]) -> None:
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    "PUT",
                    upload_url,
                    content=c.counted(source, on_progress),
                    headers={**headers, "Content-Length": str(source.size), "User-Agent": c.user_agent()},
                    timeout=c.UPLOAD_IDLE_TIMEOUT,
                )
                if response.is_success:
                    return
                err: Exception = UploadFailedError(
                    f"The file upload was refused with HTTP {response.status_code}.",
                    status=response.status_code,
                    body=response.text[:2048],
                )
                transient = response.status_code >= 500
            except httpx.TimeoutException as exc:
                err = APITimeoutError("The upload stalled.")
                err.__cause__ = exc
                transient = True
            except httpx.TransportError as exc:
                err = APIConnectionError(f"The upload connection failed: {exc}")
                err.__cause__ = exc
                transient = True
            if not transient or not source.repeatable or attempt >= self.max_retries:
                raise err
            time.sleep(c.backoff(attempt))
            attempt += 1

    # ─── upload and transcribe ────────────────────────────────────────────────

    def upload(
        self,
        file: c.FileInput,
        *,
        filename: Optional[str] = None,
        size: Optional[int] = None,
        on_progress: Optional[c.ProgressCallback] = None,
        idempotency_key: Optional[str] = None,
    ) -> UploadResult:
        """Upload a local file (POST /v1/upload, then a streamed PUT to the presigned URL).

        ``file`` is a path, bytes, or a binary file object. Large files are streamed, never read
        into memory. Returns the ``upload_id`` for ``transcribe(upload_id=...)``.
        """
        source = c.resolve_source(file, size)
        raw_name = filename or source.name
        if not raw_name:
            raise ValueError('Uploading bytes or a stream needs a filename with an extension: pass filename="audio.mp3".')
        safe = c.safe_filename(raw_name)
        upload = cast(
            Upload,
            self._send(
                "POST",
                "/v1/upload",
                body={"filename": safe},
                idempotency_key=idempotency_key or str(uuid.uuid4()),
                idempotent=True,
            ),
        )
        if upload.get("method", "PUT").upper() != "PUT":
            raise ScriptivoxError(f"The API asked for an upload method this SDK does not know: {upload.get('method')}.")
        self._put(upload["upload_url"], upload.get("headers") or {}, source, on_progress)
        return {"upload_id": upload["upload_id"], "filename": safe, "size": source.size}

    def transcribe(
        self,
        *,
        url: Optional[str] = None,
        file: Optional[c.FileInput] = None,
        upload_id: Optional[str] = None,
        language: Optional[str] = None,
        diarize: Optional[bool] = None,
        speaker_count: Optional[int] = None,
        align: Optional[bool] = None,
        webhook_url: Optional[str] = None,
        filename: Optional[str] = None,
        size: Optional[int] = None,
        on_upload_progress: Optional[c.ProgressCallback] = None,
        idempotency_key: Optional[str] = None,
    ) -> TranscriptionCreated:
        """Start a transcription from a public ``url``, a local ``file`` (uploaded first) or an earlier
        ``upload_id``. Returns as soon as the job is accepted (status "created"); call wait(id) next.

        Every call sends an Idempotency-Key (a new UUID unless you pass one), so the SDK's own retries
        never create a second billable job. Omit ``language`` to auto-detect.
        """
        c.check_one_source(url, file, upload_id)
        if file is not None:
            upload_id = self.upload(file, filename=filename, size=size, on_progress=on_upload_progress)["upload_id"]
        body = c.transcribe_body(
            url,
            upload_id,
            {"language": language, "diarize": diarize, "speaker_count": speaker_count, "align": align, "webhook_url": webhook_url},
        )
        return cast(
            TranscriptionCreated,
            self._send("POST", "/v1/transcribe", body=body, idempotency_key=idempotency_key or str(uuid.uuid4()), idempotent=True),
        )

    # ─── read ─────────────────────────────────────────────────────────────────

    @overload
    def get(self, transcription_id: str) -> Transcription: ...

    @overload
    def get(self, transcription_id: str, *, format: Literal["json"]) -> Transcription: ...

    @overload
    def get(
        self,
        transcription_id: str,
        *,
        format: Literal["srt", "vtt", "text"],
        max_words: Optional[int] = None,
        max_chars: Optional[int] = None,
        max_duration: Optional[int] = None,
        sentence_aware: Optional[bool] = None,
        include_speakers: Union[bool, Literal["auto", "true", "false"], None] = None,
        strip_chars: Optional[str] = None,
        include_timestamps: Optional[bool] = None,
    ) -> str: ...

    def get(
        self,
        transcription_id: str,
        *,
        format: str = "json",
        max_words: Optional[int] = None,
        max_chars: Optional[int] = None,
        max_duration: Optional[int] = None,
        sentence_aware: Optional[bool] = None,
        include_speakers: Union[bool, str, None] = None,
        strip_chars: Optional[str] = None,
        include_timestamps: Optional[bool] = None,
    ) -> Union[Transcription, str]:
        """GET /v1/transcribe/{id}: the job (format "json"), or the SRT / VTT / text file as a string."""
        query = c.export_query(
            format,
            {
                "max_words": max_words,
                "max_chars": max_chars,
                "max_duration": max_duration,
                "sentence_aware": sentence_aware,
                "include_speakers": include_speakers,
                "strip_chars": strip_chars,
                "include_timestamps": include_timestamps,
            },
        )
        return cast(
            Union[Transcription, str],
            self._send("GET", f"/v1/transcribe/{c.path_id(transcription_id)}", query=query, idempotent=True, text=format != "json"),
        )

    def wait(
        self,
        transcription_id: str,
        *,
        timeout: float = c.DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = c.DEFAULT_POLL_INTERVAL,
        max_poll_interval: float = c.DEFAULT_MAX_POLL_INTERVAL,
        on_poll: Optional[Any] = None,
    ) -> Transcription:
        """Poll until the job ends and return it completed. A failed job raises TranscriptionFailedError
        (its ``code`` is the job's error.code). The wait between polls starts at 2 s and grows to 10 s;
        a 429 waits out its Retry-After. ``timeout`` is in seconds (default 2 hours)."""
        deadline = c.monotonic() + timeout
        interval = min(poll_interval, max_poll_interval)
        while True:
            try:
                job: Transcription = self.get(transcription_id)
            except RateLimitError as exc:
                if exc.retry_after is not None and c.monotonic() + exc.retry_after < deadline:
                    time.sleep(exc.retry_after)
                    continue
                raise
            if on_poll:
                on_poll(job)
            if job["status"] == "completed":
                return job
            if job["status"] == "failed":
                raise TranscriptionFailedError(job)
            if c.monotonic() + interval > deadline:
                raise WaitTimeoutError(job, timeout)
            time.sleep(interval)
            interval = min(interval * 1.5, max_poll_interval)

    def list_page(
        self,
        *,
        status: Optional[TranscriptionStatus] = None,
        from_: Optional[Any] = None,
        to: Optional[Any] = None,
        limit: Optional[int] = None,
        order: Optional[Literal["desc", "asc"]] = None,
        cursor: Optional[str] = None,
    ) -> TranscriptionPage:
        """One page of GET /v1/transcriptions. Most callers want list(), which follows the cursor."""
        query = {"status": status, "from": from_, "to": to, "limit": limit, "order": order, "cursor": cursor}
        return cast(TranscriptionPage, self._send("GET", "/v1/transcriptions", query=query, idempotent=True))

    def list(
        self,
        *,
        status: Optional[TranscriptionStatus] = None,
        from_: Optional[Any] = None,
        to: Optional[Any] = None,
        limit: Optional[int] = None,
        order: Optional[Literal["desc", "asc"]] = None,
        cursor: Optional[str] = None,
    ) -> Iterator[TranscriptionSummary]:
        """Every matching transcription, newest first, fetched page by page as you iterate.
        ``from_`` and ``to`` take a datetime or an ISO string. Items omit ``result``; get(id) has it."""
        while True:
            page = self.list_page(status=status, from_=from_, to=to, limit=limit, order=order, cursor=cursor)
            yield from page["items"]
            cursor = page.get("next_cursor")
            if not cursor:
                return

    # ─── change ───────────────────────────────────────────────────────────────

    def cancel(self, transcription_id: str) -> TranscriptionCancelled:
        """Stop a job before it starts transcribing (created, downloading or pending). You are not charged."""
        return cast(TranscriptionCancelled, self._send("POST", f"/v1/transcribe/{c.path_id(transcription_id)}/cancel", idempotent=True))

    def delete(self, transcription_id: str) -> None:
        """Delete a finished job and its transcript. Twice is fine; a job still running raises ConflictError (cancel it first)."""
        self._send("DELETE", f"/v1/transcribe/{c.path_id(transcription_id)}", idempotent=True)

    def balance(self) -> Balance:
        """Balance, reserved and available amounts in cents, the price per hour and the auto top-up settings."""
        return cast(Balance, self._send("GET", "/v1/balance", idempotent=True))

    # ─── webhooks and feedback ────────────────────────────────────────────────

    def resend_webhook(self, transcription_id: str) -> WebhookSendResult:
        """Send a finished job's final webhook again now, with the same X-Scriptivox-Delivery id."""
        return cast(WebhookSendResult, self._send("POST", f"/v1/transcribe/{c.path_id(transcription_id)}/webhook/resend", idempotent=False))

    def test_webhook(self, url: Optional[str] = None) -> WebhookSendResult:
        """Send a signed ``webhook.test`` event to ``url`` (or your default webhook URL) and report the answer."""
        return cast(WebhookSendResult, self._send("POST", "/v1/webhooks/test", body={} if url is None else {"url": url}, idempotent=False))

    def feedback(
        self,
        transcription_id: str,
        *,
        rating: Optional[int] = None,
        topics: Optional[List[str]] = None,
        comment: Optional[str] = None,
        their_words: Optional[str] = None,
        problem: Optional[bool] = None,
        what_happened: Optional[str] = None,
    ) -> FeedbackResult:
        """Rate a completed job (1–5) and/or report a problem with any job."""
        body = {
            k: v
            for k, v in {"rating": rating, "topics": topics, "comment": comment, "their_words": their_words, "problem": problem, "what_happened": what_happened}.items()
            if v is not None
        }
        return cast(FeedbackResult, self._send("POST", f"/v1/transcribe/{c.path_id(transcription_id)}/feedback", body=body, idempotent=False))

    @staticmethod
    def verify_webhook(raw_body: Union[bytes, str], headers: HeadersLike, secret: str, *, tolerance: int = 300, now: Optional[float] = None) -> WebhookEvent:
        """Same as the module-level verify_webhook()."""
        return verify_webhook(raw_body, headers, secret, tolerance=tolerance, now=now)
