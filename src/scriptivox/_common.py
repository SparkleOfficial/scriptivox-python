"""Logic shared by the sync and async clients: retry rules, error mapping, filenames, file sources."""
from __future__ import annotations

import email.utils
import io
import json
import os
import platform
import random
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, Mapping, NamedTuple, Optional, Union

from ._error_codes import ERROR_CLASS_BY_CODE
from ._exceptions import APIError, category_for_status
from ._version import __version__

DEFAULT_BASE_URL = "https://api.scriptivox.com"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2
#: A Retry-After longer than this is not waited out automatically; the error is raised instead.
MAX_RETRY_AFTER = 60.0
DEFAULT_WAIT_TIMEOUT = 2 * 60 * 60.0
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_MAX_POLL_INTERVAL = 10.0
#: Per socket operation during the file upload: no byte moving for this long ends that try.
UPLOAD_IDLE_TIMEOUT = 120.0
CHUNK = 1024 * 1024
MAX_FILENAME = 255
TRANSCRIPTION_OPTION_KEYS = ("language", "diarize", "speaker_count", "align", "webhook_url")


class UploadProgress(NamedTuple):
    """Passed to ``on_progress`` as the file's bytes are sent."""

    loaded: int
    total: int
    #: 0–100.
    percent: float


FileInput = Union[str, "os.PathLike[str]", bytes, bytearray, memoryview, io.IOBase, Any]
ProgressCallback = Callable[[UploadProgress], None]


def normalize_base_url(url: str) -> str:
    url = url.rstrip("/")
    return url[: -len("/v1")] if url.endswith("/v1") else url


def user_agent() -> str:
    try:
        import httpx

        httpx_version = httpx.__version__
    except Exception:  # pragma: no cover
        httpx_version = "unknown"
    return f"scriptivox-python/{__version__} python/{platform.python_version()} httpx/{httpx_version}"


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Retry-After as seconds: delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def backoff(attempt: int) -> float:
    """Exponential backoff with jitter: 0.5 s, 1 s, 2 s, … up to 8 s, ±25 %."""
    return float(min(0.5 * 2.0**attempt, 8.0) * (0.75 + random.random() * 0.5))


def error_from_response(status: int, text: str, headers: Mapping[str, str]) -> APIError:
    """The typed error for an error answer. Handles a non-JSON body (e.g. an HTML 413 from the network layer)."""
    lower = {k.lower(): v for k, v in headers.items()}
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    error = parsed.get("error") if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict) else None
    code = error.get("code") if error and isinstance(error.get("code"), str) else "UNKNOWN_ERROR"
    snippet = " ".join(text.split())[:200]
    message = error.get("message") if error and isinstance(error.get("message"), str) else f"HTTP {status}" + (f": {snippet}" if snippet else "")
    request_id = (error.get("request_id") if error and isinstance(error.get("request_id"), str) else None) or lower.get("x-request-id")
    retry_after = parse_retry_after(lower.get("retry-after"))
    if retry_after is None and isinstance(parsed, dict) and isinstance(parsed.get("retry_after"), (int, float)):
        retry_after = float(parsed["retry_after"])
    cls = ERROR_CLASS_BY_CODE.get(code) or category_for_status(status)
    return cls(
        status=status,
        code=code,
        message=message,
        request_id=request_id,
        docs_url=error.get("docs_url") if error and isinstance(error.get("docs_url"), str) else None,
        retry_after=retry_after,
        error=error,
        headers=lower,
    )


def retry_delay_for(err: APIError, idempotent: bool, attempt: int) -> Optional[float]:
    """Whether an error answer is worth another try, and after how many seconds.

    - 429 or 503 with Retry-After (rate limit, maintenance): refused before any work, so retried whatever
      the method, after Retry-After (up to MAX_RETRY_AFTER). A 429 without Retry-After is the
      jobs-in-progress cap: retrying at once cannot help.
    - 409 IDEMPOTENCY_KEY_LOCKED: the first request with this key is still running; retry shortly.
    - 408 and other 5xx: only when the request is safe to repeat.
    """
    ra = err.retry_after
    if err.status in (429, 503):
        if ra is not None:
            return ra if ra <= MAX_RETRY_AFTER else None
        if err.status == 429:
            return None
    if err.code == "IDEMPOTENCY_KEY_LOCKED":
        return ra if ra is not None and ra <= MAX_RETRY_AFTER else backoff(attempt)
    if idempotent and (err.status == 408 or err.status >= 500):
        return ra if ra is not None and ra <= MAX_RETRY_AFTER else backoff(attempt)
    return None


def query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return str(value.astimezone(timezone.utc).isoformat()).replace("+00:00", "Z")
    return str(value)


def build_query(params: Mapping[str, Any]) -> Dict[str, str]:
    return {k: query_value(v) for k, v in params.items() if v is not None}


def safe_filename(name: str) -> str:
    """The filename the API accepts: printable ASCII, no path separators, none of < > " ' `, at most
    255 characters, with an extension. Accents are dropped (é → e), anything else becomes "_"."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    decomposed = unicodedata.normalize("NFKD", base)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    safe = "".join(c if 0x20 <= ord(c) <= 0x7E and c not in '<>"\'`' else "_" for c in stripped).strip()
    dot = safe.rfind(".")
    if dot <= 0 or dot == len(safe) - 1:
        raise ValueError(f'The filename "{name}" needs an extension such as .mp3 or .mp4: pass filename=.')
    if len(safe) > MAX_FILENAME:
        ext = safe[dot:]
        safe = safe[: MAX_FILENAME - len(ext)] + ext
    return safe


@dataclass
class FileSource:
    name: Optional[str]
    size: int
    repeatable: bool
    _open: Callable[[], Iterator[bytes]]
    _used: bool = False

    def chunks(self) -> Iterator[bytes]:
        if not self.repeatable:
            if self._used:
                raise ValueError("A stream can only be uploaded once.")
            self._used = True
        return self._open()


def resolve_source(file: Any, size: Optional[int] = None) -> FileSource:
    """Any accepted input as a sized, (where possible) re-readable byte source."""
    if isinstance(file, (str, os.PathLike)):
        path = os.fspath(file)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"{path} is not a file.")
        file_size = os.path.getsize(path)

        def read_path() -> Iterator[bytes]:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        return
                    yield chunk

        return FileSource(os.path.basename(path), file_size, True, read_path)

    if isinstance(file, (bytes, bytearray, memoryview)):
        data = bytes(file)

        def read_bytes() -> Iterator[bytes]:
            for i in range(0, len(data), CHUNK):
                yield data[i : i + CHUNK]

        return FileSource(None, len(data), True, read_bytes)

    if hasattr(file, "read"):
        raw_name = getattr(file, "name", None)
        name = os.path.basename(raw_name) if isinstance(raw_name, str) else None
        seekable = bool(getattr(file, "seekable", lambda: False)())
        start = file.tell() if seekable else 0
        if size is None:
            if not seekable:
                raise ValueError("Uploading a non-seekable stream needs its byte length: pass size=.")
            file.seek(0, io.SEEK_END)
            size = file.tell() - start
            file.seek(start)

        def read_stream() -> Iterator[bytes]:
            if seekable:
                file.seek(start)
            while True:
                chunk = file.read(CHUNK)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    raise TypeError("Open the file in binary mode ('rb').")
                yield chunk

        return FileSource(name, int(size), seekable, read_stream)

    raise TypeError("file must be a path, bytes, or a binary file object.")


def counted(source: FileSource, on_progress: Optional[ProgressCallback]) -> Iterator[bytes]:
    """The source's chunks, checking the byte count and reporting progress."""
    sent = 0
    for chunk in source.chunks():
        if sent + len(chunk) > source.size:
            raise ValueError(f"The source has more bytes than the size given ({source.size}).")
        yield chunk
        sent += len(chunk)
        if on_progress:
            on_progress(UploadProgress(sent, source.size, round(sent / source.size * 100, 1) if source.size else 100.0))
    if sent != source.size:
        raise ValueError(f"The source ended after {sent} of {source.size} bytes.")
    if source.size == 0 and on_progress:
        on_progress(UploadProgress(0, 0, 100.0))


def transcribe_body(
    url: Optional[str],
    upload_id: Optional[str],
    options: Mapping[str, Any],
) -> Dict[str, Any]:
    body: Dict[str, Any] = {k: options[k] for k in TRANSCRIPTION_OPTION_KEYS if options.get(k) is not None}
    if url is not None:
        body["url"] = url
    if upload_id is not None:
        body["upload_id"] = upload_id
    return body


def check_one_source(url: Any, file: Any, upload_id: Any) -> None:
    if sum(x is not None for x in (url, file, upload_id)) != 1:
        raise TypeError("transcribe() needs exactly one of url=, file= or upload_id=.")


def path_id(transcription_id: str) -> str:
    if not isinstance(transcription_id, str) or not transcription_id:
        raise TypeError("A transcription id is required.")
    from urllib.parse import quote

    return quote(transcription_id, safe="")


def export_query(fmt: str, options: Mapping[str, Any]) -> Dict[str, str]:
    if fmt == "json":
        return {}
    params: Dict[str, Any] = {"format": fmt}
    for key, value in options.items():
        if value is None:
            continue
        if key == "include_speakers" and isinstance(value, bool):
            value = "true" if value else "false"
        params[key] = value
    return build_query(params)


def monotonic() -> float:
    return time.monotonic()
