"""Unit tests against a scripted transport: no network, no API key."""
from __future__ import annotations

import asyncio
import io
import re
from datetime import datetime, timezone

import httpx
import pytest

import scriptivox
from scriptivox import (
    API_ERROR_CODES,
    ERROR_CLASS_BY_CODE,
    TRANSCRIPTION_ERROR_CODES,
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    InternalError,
    InvalidApiKeyError,
    InvalidRequestError,
    MaintenanceError,
    NotFoundError,
    PayloadTooLargeError,
    PaymentRequiredError,
    RateLimitError,
    RateLimitExceededError,
    Scriptivox,
    ScriptivoxError,
    ServiceUnavailableError,
    TranscriptionFailedError,
    TranscriptionNotFoundError,
    UploadFailedError,
    WaitTimeoutError,
    ZeroBalanceError,
    safe_filename,
)

from .conftest import BASE, api_error, job, ok

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def test_constructor_needs_a_key_and_reads_env(monkeypatch, mock):
    monkeypatch.delenv("SCRIPTIVOX_API_KEY", raising=False)
    with pytest.raises(ScriptivoxError):
        Scriptivox()
    monkeypatch.setenv("SCRIPTIVOX_API_KEY", "sk_live_env")
    monkeypatch.setenv("SCRIPTIVOX_BASE_URL", BASE + "/v1/")
    c = Scriptivox(http_client=httpx.Client(transport=httpx.MockTransport(mock.handler)))
    assert c.base_url == BASE
    mock.reply(ok({"balance_cents": 1}))
    c.balance()
    assert mock.requests[0].headers["authorization"] == "sk_live_env"
    assert mock.requests[0].path == "/v1/balance"


def test_transcribe_url_body_headers_and_idempotency_key(mock):
    mock.reply(job("j1", "created"))
    out = mock.client().transcribe(url="https://example.com/a.mp3", language="en", diarize=True, speaker_count=2)
    assert out == {"id": "j1", "status": "created"}
    r = mock.requests[0]
    assert (r.method, r.path) == ("POST", "/v1/transcribe")
    assert r.json == {"language": "en", "diarize": True, "speaker_count": 2, "url": "https://example.com/a.mp3"}
    assert r.headers["content-type"] == "application/json"
    assert r.headers["authorization"] == "sk_live_test"
    assert UUID_RE.match(r.headers["idempotency-key"])
    assert r.headers["user-agent"].startswith(f"scriptivox-python/{scriptivox.__version__} python/")


def test_transcribe_needs_exactly_one_source(mock):
    c = mock.client()
    with pytest.raises(TypeError):
        c.transcribe(language="en")
    with pytest.raises(TypeError):
        c.transcribe(url="https://x/a.mp3", upload_id="u")


def test_retried_submit_reuses_the_key(mock):
    mock.reply(api_error(500, "INTERNAL_ERROR"), job("j2", "created"))
    assert mock.client().transcribe(url="https://example.com/a.mp3")["id"] == "j2"
    assert len(mock.requests) == 2
    assert mock.requests[0].headers["idempotency-key"] == mock.requests[1].headers["idempotency-key"]


def test_caller_key_is_sent(mock):
    mock.reply(job("j3", "created"))
    mock.client().transcribe(upload_id="u1", idempotency_key="order-42")
    assert mock.requests[0].headers["idempotency-key"] == "order-42"
    assert mock.requests[0].json == {"upload_id": "u1"}


def test_429_with_retry_after_is_waited_out(mock):
    mock.reply(api_error(429, "RATE_LIMIT_EXCEEDED", headers={"retry-after": "1"}), ok({"balance_cents": 5}))
    assert mock.client().balance()["balance_cents"] == 5
    assert mock.requests[1].at - mock.requests[0].at >= 0.95


def test_429_without_retry_after_is_raised_at_once(mock):
    mock.reply(api_error(429, "RATE_LIMIT_EXCEEDED", "25 jobs in progress", in_flight=25, limit=25))
    with pytest.raises(RateLimitExceededError) as e:
        mock.client().transcribe(url="https://example.com/a.mp3")
    assert isinstance(e.value, RateLimitError)
    assert e.value.retry_after is None
    assert e.value.error["in_flight"] == 25 and e.value.error["limit"] == 25
    assert len(mock.requests) == 1


def test_long_retry_after_is_not_waited(mock):
    mock.reply(api_error(429, "RATE_LIMIT_EXCEEDED", headers={"retry-after": "600"}))
    with pytest.raises(RateLimitExceededError) as e:
        mock.client().balance()
    assert e.value.retry_after == 600
    assert len(mock.requests) == 1


def test_typed_errors(mock):
    mock.reply(api_error(402, "ZERO_BALANCE", "Balance is $0 or below. Please deposit funds before transcribing."))
    with pytest.raises(ZeroBalanceError) as e:
        mock.client().transcribe(url="https://example.com/a.mp3")
    err = e.value
    assert isinstance(err, PaymentRequiredError) and isinstance(err, APIError) and isinstance(err, ScriptivoxError)
    assert (err.status, err.code, err.request_id) == (402, "ZERO_BALANCE", "req_0123456789abcdef")
    assert err.docs_url == "https://platform.scriptivox.com/docs/api-reference#error-codes"
    assert re.search(r"deposit funds.*\[ZERO_BALANCE, request req_0123456789abcdef\]", str(err))
    assert err.message.startswith("Balance is $0")
    assert len(mock.requests) == 1

    mock.reply(api_error(401, "INVALID_API_KEY"))
    with pytest.raises(InvalidApiKeyError) as e401:
        mock.client().balance()
    assert isinstance(e401.value, AuthenticationError)

    mock.reply(api_error(404, "TRANSCRIPTION_NOT_FOUND"))
    with pytest.raises(TranscriptionNotFoundError) as e404:
        mock.client().get("nope")
    assert isinstance(e404.value, NotFoundError)

    mock.reply(api_error(404, "NOT_FOUND"))
    with pytest.raises(NotFoundError) as eroute:
        mock.client().get("nope")
    assert type(eroute.value) is NotFoundError and eroute.value.code == "NOT_FOUND"


def test_every_code_has_a_class():
    assert len(API_ERROR_CODES) == 26 and len(TRANSCRIPTION_ERROR_CODES) == 13
    for code in API_ERROR_CODES:
        assert issubclass(ERROR_CLASS_BY_CODE[code], APIError), code


def test_unknown_code_and_non_json_body(mock):
    mock.reply(*[api_error(503, "SOMETHING_NEW")] * 3)
    with pytest.raises(ServiceUnavailableError) as e:
        mock.client().balance()
    assert e.value.code == "SOMETHING_NEW"
    assert len(mock.requests) == 3

    mock.requests.clear()
    mock.reply(httpx.Response(413, content=b"<html><body>413 Request Entity Too Large</body></html>", headers={"content-type": "text/html", "x-request-id": "req_feedfeedfeedfeed"}))
    with pytest.raises(PayloadTooLargeError) as e413:
        mock.client().transcribe(url="https://example.com/a.mp3")
    assert e413.value.code == "UNKNOWN_ERROR"
    assert e413.value.request_id == "req_feedfeedfeedfeed"
    assert "HTTP 413: <html>" in str(e413.value)


def test_no_retry_for_400_or_non_idempotent_post(mock):
    mock.reply(api_error(400, "INVALID_REQUEST"))
    with pytest.raises(InvalidRequestError):
        mock.client().transcribe(url="https://example.com/a.mp3", speaker_count=2)
    assert len(mock.requests) == 1
    mock.requests.clear()
    mock.reply(api_error(500, "INTERNAL_ERROR"))
    with pytest.raises(InternalError):
        mock.client().feedback("j1", rating=5)
    assert len(mock.requests) == 1


def test_locked_key_and_maintenance_are_retried(mock):
    mock.reply(api_error(409, "IDEMPOTENCY_KEY_LOCKED", headers={"retry-after": "0"}), job("j4", "created"))
    assert mock.client().transcribe(url="https://example.com/a.mp3")["id"] == "j4"
    assert len(mock.requests) == 2
    mock.requests.clear()
    mock.reply(api_error(503, "MAINTENANCE", headers={"retry-after": "0"}), ok({"delivered": True}))
    assert mock.client().test_webhook("https://example.com/hook")["delivered"] is True
    assert mock.requests[0].json == {"url": "https://example.com/hook"}
    assert len(mock.requests) == 2
    mock.reply(api_error(503, "MAINTENANCE", headers={"retry-after": "0"}))
    with pytest.raises(MaintenanceError):
        mock.client(max_retries=0).balance()


def test_wait_polls_until_completed(mock):
    mock.reply(job("w1", "created"), job("w1", "processing"), job("w1", "processing"), job("w1", "completed", result={"full_transcript": "hi"}))
    seen = []
    done = mock.client().wait("w1", poll_interval=0.02, max_poll_interval=0.04, on_poll=lambda t: seen.append(t["status"]))
    assert done["result"]["full_transcript"] == "hi"
    assert seen == ["created", "processing", "processing", "completed"]
    gaps = [b.at - a.at for a, b in zip(mock.requests, mock.requests[1:])]
    assert gaps[0] >= 0.015 and gaps[2] >= 0.035


def test_wait_failed_raises(mock):
    mock.reply(job("w2", "failed", error={"code": "URL_NOT_ACCESSIBLE", "message": "The URL could not be fetched (HTTP 403)."}))
    with pytest.raises(TranscriptionFailedError) as e:
        mock.client().wait("w2")
    assert e.value.code == "URL_NOT_ACCESSIBLE" and e.value.transcription_id == "w2"
    assert "HTTP 403" in str(e.value)


def test_wait_timeout(mock):
    mock.always(job("w3", "processing"))
    with pytest.raises(WaitTimeoutError) as e:
        mock.client().wait("w3", timeout=0.12, poll_interval=0.05)
    assert e.value.transcription["status"] == "processing"


def test_wait_honours_retry_after(mock):
    mock.reply(job("w4", "processing"), api_error(429, "RATE_LIMIT_EXCEEDED", headers={"retry-after": "1"}), job("w4", "completed", result={"full_transcript": "ok"}))
    assert mock.client(max_retries=0).wait("w4", poll_interval=0.01)["result"]["full_transcript"] == "ok"
    assert mock.requests[2].at - mock.requests[1].at >= 0.95


def test_get_json_and_exports(mock):
    c = mock.client()
    mock.reply(job("g1", "completed", result={"full_transcript": "x"}))
    assert c.get("g1")["result"]["full_transcript"] == "x"
    assert mock.requests[0].query == {}
    mock.reply(ok("1\n00:00:00,000 --> 00:00:01,000\nHi\n"))
    srt = c.get("g1", format="srt", max_words=3, sentence_aware=False, include_speakers=True, strip_chars=",.")
    assert "-->" in srt
    assert mock.requests[1].query == {"format": "srt", "max_words": "3", "sentence_aware": "false", "include_speakers": "true", "strip_chars": ",."}
    mock.reply(ok("[00:00] Hi"))
    c.get("g1", format="text", include_timestamps=True)
    assert mock.requests[2].query == {"format": "text", "include_timestamps": "true"}


def test_list_follows_the_cursor(mock):
    mock.reply(
        ok({"items": [{"id": "a", "status": "completed"}, {"id": "b", "status": "completed"}], "next_cursor": "c1"}),
        ok({"items": [{"id": "c", "status": "failed"}], "next_cursor": "c2"}),
        ok({"items": [{"id": "d", "status": "completed"}], "next_cursor": None}),
    )
    ids = [t["id"] for t in mock.client().list(limit=2, from_=datetime(2026, 11, 1, tzinfo=timezone.utc), order="desc")]
    assert ids == ["a", "b", "c", "d"]
    assert [r.query.get("cursor") for r in mock.requests] == [None, "c1", "c2"]
    assert mock.requests[0].query["from"] == "2026-11-01T00:00:00Z"
    assert mock.requests[0].query["limit"] == "2"


def test_methods_and_paths(mock):
    mock.reply(
        ok({"id": "x", "status": "failed", "error": {"code": "CANCELLED", "message": "Cancelled"}, "released_cents": 0}),
        httpx.Response(204),
        ok({"balance_cents": 100, "reserved_cents": 0, "available_cents": 100, "price_per_hour_cents": 15, "auto_topup": {"enabled": False, "threshold_cents": 500, "amount_cents": 2000}}),
        ok({"delivered": True}),
        ok({"transcription_id": "x", "rating_saved": True, "problem_report": None}),
    )
    c = mock.client()
    assert c.cancel("x")["error"]["code"] == "CANCELLED"
    assert c.delete("x") is None
    assert c.balance()["auto_topup"]["enabled"] is False
    c.resend_webhook("x")
    c.feedback("x", rating=5)
    assert [f"{r.method} {r.path}" for r in mock.requests] == [
        "POST /v1/transcribe/x/cancel",
        "DELETE /v1/transcribe/x",
        "GET /v1/balance",
        "POST /v1/transcribe/x/webhook/resend",
        "POST /v1/transcribe/x/feedback",
    ]
    assert "idempotency-key" not in mock.requests[0].headers
    assert mock.requests[4].json == {"rating": 5}


def test_timeouts_and_connection_errors(mock):
    mock.always("timeout")
    with pytest.raises(APITimeoutError):
        mock.client(max_retries=1).balance()
    assert len(mock.requests) == 2
    mock.requests.clear()
    mock.always("refused")
    with pytest.raises(APIConnectionError):
        mock.client(max_retries=0).feedback("x", rating=1)
    assert len(mock.requests) == 1


# ─── upload ──────────────────────────────────────────────────────────────────

def upload_flow(put=None):
    return [
        ok({"upload_id": "up-1", "upload_url": "https://storage.example.test/put?sig=abc", "expires_in": 3600, "method": "PUT", "headers": {"Content-Type": "audio/mpeg"}}),
        put or httpx.Response(200),
    ]


def test_upload_path_streams_with_progress_and_no_api_key(mock, tmp_path):
    data = bytes([7]) * (3 * 1024 * 1024 + 123)
    f = tmp_path / "Réunion d’équipe.mp3"
    f.write_bytes(data)
    mock.reply(*upload_flow())
    progress = []
    out = mock.client().upload(str(f), on_progress=progress.append)
    assert out == {"upload_id": "up-1", "filename": "Reunion d_equipe.mp3", "size": len(data)}
    presign, put = mock.requests
    assert presign.json == {"filename": "Reunion d_equipe.mp3"}
    assert UUID_RE.match(presign.headers["idempotency-key"])
    assert put.method == "PUT" and put.host == "storage.example.test" and put.query == {"sig": "abc"}
    assert put.headers["content-type"] == "audio/mpeg"
    assert put.headers["content-length"] == str(len(data))
    assert "transfer-encoding" not in put.headers
    assert "authorization" not in put.headers
    assert put.body == data
    assert len(progress) >= 4
    assert progress[-1] == (len(data), len(data), 100.0)
    assert all(b.loaded > a.loaded for a, b in zip(progress, progress[1:]))


def test_upload_inputs(mock, tmp_path):
    c = mock.client()
    with pytest.raises(ValueError, match="filename"):
        c.upload(b"abc")
    with pytest.raises(ValueError, match="extension"):
        c.upload(b"abc", filename="noext")
    mock.reply(*upload_flow())
    c.upload(bytearray(b"bytes!"), filename="clip.wav")
    assert mock.requests[1].body == b"bytes!"
    mock.requests.clear()
    p = tmp_path / "x.ogg"
    p.write_bytes(b"0123456789")
    with open(p, "rb") as fh:
        fh.read(2)
        mock.reply(*upload_flow())
        out = c.upload(fh)
    assert out["filename"] == "x.ogg" and out["size"] == 8
    assert mock.requests[1].body == b"23456789"

    class OneShot(io.RawIOBase):
        def __init__(self):
            self.parts = [b"ab", b"cd"]

        def readable(self):
            return True

        def read(self, n=-1):
            return self.parts.pop(0) if self.parts else b""

    with pytest.raises(ValueError, match="size"):
        c.upload(OneShot(), filename="s.ogg")
    mock.requests.clear()
    mock.reply(*upload_flow())
    c.upload(OneShot(), filename="s.ogg", size=4)
    assert mock.requests[1].body == b"abcd"


def test_upload_403_not_retried_500_retried(mock, tmp_path):
    p = tmp_path / "a.mp3"
    p.write_bytes(b"hello")
    mock.reply(*upload_flow(httpx.Response(403, content=b"<Error><Code>AccessDenied</Code></Error>")))
    with pytest.raises(UploadFailedError) as e:
        mock.client().upload(str(p))
    assert e.value.status == 403 and "AccessDenied" in e.value.body
    assert len(mock.requests) == 2
    mock.requests.clear()
    mock.reply(*upload_flow(httpx.Response(500, content=b"oops")), httpx.Response(200))
    assert mock.client().upload(p)["upload_id"] == "up-1"
    assert len(mock.requests) == 3 and mock.requests[2].body == b"hello"


def test_transcribe_file_uploads_then_submits(mock, tmp_path):
    p = tmp_path / "b.m4a"
    p.write_bytes(bytes(2048))
    mock.reply(*upload_flow(), job("t9", "created"))
    seen = []
    out = mock.client().transcribe(file=p, language="en", on_upload_progress=lambda pr: seen.append(pr.percent))
    assert out["id"] == "t9"
    assert mock.requests[2].json == {"language": "en", "upload_id": "up-1"}
    assert mock.requests[0].headers["idempotency-key"] != mock.requests[2].headers["idempotency-key"]
    assert seen[-1] == 100.0


def test_safe_filename():
    assert safe_filename("C:\\Users\\me\\Café “Mix”.mp3") == "Cafe _Mix_.mp3"
    assert safe_filename("a<b>\"c'`.wav") == "a_b__c__.wav"
    long = safe_filename("x" * 300 + ".flac")
    assert len(long) == 255 and long.endswith(".flac")
    with pytest.raises(ValueError):
        safe_filename("README")


# ─── asyncio client ──────────────────────────────────────────────────────────

def test_async_client_end_to_end(mock, tmp_path):
    p = tmp_path / "c.mp3"
    p.write_bytes(bytes(1500))
    mock.reply(
        *upload_flow(),
        job("a1", "created"),
        job("a1", "processing"),
        api_error(500, "INTERNAL_ERROR"),
        job("a1", "completed", result={"full_transcript": "async ok"}),
        ok("WEBVTT\n\n00:00.000 --> 00:01.000\nasync ok\n", headers={"content-type": "text/vtt"}),
        ok({"items": [{"id": "a1", "status": "completed"}], "next_cursor": "n"}),
        ok({"items": [{"id": "a0", "status": "completed"}], "next_cursor": None}),
        api_error(402, "ZERO_BALANCE"),
    )

    async def run():
        async with mock.aclient() as c:
            progress = []
            created = await c.transcribe(file=str(p), on_upload_progress=progress.append)
            done = await c.wait(created["id"], poll_interval=0.01)
            vtt = await c.get(created["id"], format="vtt")
            ids = [t["id"] async for t in c.list(limit=1)]
            with pytest.raises(ZeroBalanceError):
                await c.transcribe(url="https://example.com/a.mp3")
            return progress, done, vtt, ids

    progress, done, vtt, ids = asyncio.run(run())
    assert progress[-1].percent == 100.0
    assert done["result"]["full_transcript"] == "async ok"
    assert vtt.startswith("WEBVTT")
    assert ids == ["a1", "a0"]
    assert mock.requests[1].body == bytes(1500)
    assert mock.requests[1].headers["content-length"] == "1500"
    assert "transfer-encoding" not in mock.requests[1].headers
    assert mock.requests[2].json == {"upload_id": "up-1"}


def test_generated_types_resolve_at_runtime():
    """Every generated annotation evaluates (a bad literal such as Literal[true] would raise NameError)."""
    import typing

    from scriptivox import _types

    checked = 0
    for name in _types.__all__:
        obj = getattr(_types, name)
        if isinstance(obj, type):
            typing.get_type_hints(obj)
            checked += 1
    assert checked >= 25
    assert typing.get_type_hints(_types.WebhookTest)["test"] == typing.Literal[True]
