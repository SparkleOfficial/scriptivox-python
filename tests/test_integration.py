"""Integration tests against a real Scriptivox API (TEST: https://api-test.scriptivox.com).

    SCRIPTIVOX_API_KEY        a key with a little balance (required; every test skips without it)
    SCRIPTIVOX_BASE_URL       default https://api-test.scriptivox.com (refuses the production URL)
    SCRIPTIVOX_EMPTY_API_KEY  optional: a key on an account with no balance (real 402 check)
    SCRIPTIVOX_TEST_GPU=1     optional: also run one real transcription of tests/fixtures/speech-en.mp3

Without SCRIPTIVOX_TEST_GPU the jobs used here all fail at the file check (a link to a JSON document),
which costs $0 and never reaches a GPU.
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path

import pytest

from scriptivox import (
    TRANSCRIPTION_ERROR_CODES,
    AsyncScriptivox,
    AuthenticationError,
    ConflictError,
    InvalidApiKeyError,
    InvalidMediaFormatError,
    InvalidRequestError,
    Scriptivox,
    TranscriptionFailedError,
    TranscriptionNotFoundError,
    ZeroBalanceError,
)

KEY = os.environ.get("SCRIPTIVOX_API_KEY")
BASE = os.environ.get("SCRIPTIVOX_BASE_URL") or "https://api-test.scriptivox.com"
EMPTY_KEY = os.environ.get("SCRIPTIVOX_EMPTY_API_KEY")
GPU = os.environ.get("SCRIPTIVOX_TEST_GPU") == "1"
if re.search(r"//api\.scriptivox\.com", BASE):
    raise RuntimeError("Integration tests refuse the production API; point SCRIPTIVOX_BASE_URL at TEST.")

pytestmark = pytest.mark.skipif(not KEY, reason="SCRIPTIVOX_API_KEY not set")
REQ_ID = re.compile(r"^req_[0-9a-f]{16}$")
NOT_MEDIA = "https://httpbin.org/json"
SLOW = "https://httpbin.org/delay/10"
HOOK = "https://httpbin.org/post"
SPEECH = Path(__file__).parent / "fixtures" / "speech-en.mp3"
STATE: dict = {"made": []}


def log(*a):
    print("   ·", *a)


@pytest.fixture(scope="module")
def client():
    with Scriptivox(KEY, base_url=BASE) as c:
        yield c
    if STATE["made"]:
        print(f"\n   · TEST jobs made by this run: {', '.join(STATE['made'])}")


def test_balance(client):
    b = client.balance()
    log("balance", b)
    for k in ("balance_cents", "reserved_cents", "available_cents", "price_per_hour_cents"):
        assert isinstance(b[k], (int, float)), k
    assert isinstance(b["auto_topup"]["enabled"], bool)
    assert b["available_cents"] > 0


def test_wrong_key():
    with Scriptivox("sk_live_" + "0" * 48, base_url=BASE) as bad, pytest.raises(InvalidApiKeyError) as e:
        bad.balance()
    log(type(e.value).__name__, e.value.status, e.value.code, e.value.request_id)
    assert isinstance(e.value, AuthenticationError) and e.value.status == 401
    assert REQ_ID.match(e.value.request_id) and e.value.headers["x-request-id"] == e.value.request_id


def test_unknown_job(client):
    with pytest.raises(TranscriptionNotFoundError) as e:
        client.get(str(uuid.uuid4()))
    log(type(e.value).__name__, e.value.status, e.value.code, e.value.request_id)
    assert REQ_ID.match(e.value.request_id)


def test_bad_submit(client):
    with pytest.raises(InvalidRequestError) as e:
        client.transcribe(url=NOT_MEDIA, speaker_count=2)
    log(type(e.value).__name__, e.value.status, e.value.code, e.value.request_id, "-", e.value.message)
    assert REQ_ID.match(e.value.request_id)


@pytest.mark.skipif(not EMPTY_KEY, reason="SCRIPTIVOX_EMPTY_API_KEY not set")
def test_zero_balance():
    with Scriptivox(EMPTY_KEY, base_url=BASE) as empty, pytest.raises(ZeroBalanceError) as e:
        empty.transcribe(url=NOT_MEDIA)
    log(type(e.value).__name__, e.value.status, e.value.code, e.value.request_id)
    assert e.value.status == 402


def test_upload_unsupported_extension(client, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("not audio")
    with pytest.raises(InvalidMediaFormatError) as e:
        client.upload(f)
    log(type(e.value).__name__, e.value.status, e.value.code, e.value.request_id)


def test_upload_with_progress(client):
    progress = []
    up = client.upload(SPEECH, on_progress=progress.append)
    log("upload", up["upload_id"], up["filename"], up["size"], "bytes; progress calls", len(progress), "last", progress[-1].percent)
    assert re.match(r"^[0-9a-f-]{36}$", up["upload_id"])
    assert progress[-1].percent == 100.0


def test_url_flow_failure_is_free(client):
    job = client.transcribe(url=NOT_MEDIA, language="en", webhook_url=HOOK)
    STATE["made"].append(job["id"])
    assert job["status"] == "created"
    statuses = []
    with pytest.raises(TranscriptionFailedError) as e:
        client.wait(job["id"], poll_interval=1.0, timeout=180, on_poll=lambda t: statuses.append(t["status"]))
    log("polled", " → ".join(statuses), "|", type(e.value).__name__, e.value.code, "-", e.value.transcription["error"]["message"])
    assert e.value.code in TRANSCRIPTION_ERROR_CODES
    assert e.value.code in ("INVALID_MEDIA_FORMAT", "URL_NOT_ACCESSIBLE")
    assert (e.value.transcription.get("cost_cents") or 0) == 0
    STATE["failed_id"] = job["id"]


def test_same_idempotency_key_same_job(client):
    key = f"p9-sdk-py-{uuid.uuid4()}"
    a = client.transcribe(url=NOT_MEDIA, idempotency_key=key)
    b = client.transcribe(url=NOT_MEDIA, idempotency_key=key)
    STATE["made"].append(a["id"])
    log("first", a["id"], "second", b["id"])
    assert a["id"] == b["id"]


def test_cancel(client):
    job = client.transcribe(url=SLOW)
    STATE["made"].append(job["id"])
    c1 = client.cancel(job["id"])
    log("cancel", c1)
    assert c1["status"] == "failed" and c1["error"]["code"] == "CANCELLED"
    assert client.cancel(job["id"])["error"]["code"] == "CANCELLED"
    with pytest.raises(TranscriptionFailedError) as e:
        client.wait(job["id"], poll_interval=0.5)
    assert e.value.code == "CANCELLED"


def test_resend_and_test_webhook(client):
    resent = client.resend_webhook(STATE["failed_id"])
    log("resend", {k: resent.get(k) for k in ("event", "delivered", "response_code", "attempt", "status")})
    assert resent["event"] == "transcription.failed" and resent["delivered"] is True
    tested = client.test_webhook(HOOK)
    log("test", {k: tested.get(k) for k in ("event", "delivered", "response_code")})
    assert tested["event"] == "webhook.test" and tested["delivered"] is True


def test_list_iterates_the_cursor(client):
    ids = []
    for t in client.list(limit=2, status="failed"):
        assert t["status"] == "failed" and "result" not in t
        ids.append(t["id"])
        if len(ids) >= 7:
            break
    log("listed", len(ids), "failed jobs")
    assert len(ids) >= 3 and len(set(ids)) == len(ids)


def test_async_list_and_balance():
    async def run():
        async with AsyncScriptivox(KEY, base_url=BASE) as c:
            b = await c.balance()
            ids = []
            async for t in c.list(limit=2, status="failed"):
                ids.append(t["id"])
                if len(ids) >= 5:
                    break
            return b, ids

    b, ids = asyncio.run(run())
    log("async: available", b["available_cents"], "cents; listed", len(ids))
    assert len(ids) >= 3 and len(set(ids)) == len(ids)


def test_delete_then_not_found(client):
    fid = STATE["failed_id"]
    client.delete(fid)
    client.delete(fid)
    with pytest.raises(TranscriptionNotFoundError) as e:
        client.get(fid)
    log("after delete:", type(e.value).__name__, e.value.code)


def test_delete_running_job_conflicts(client):
    job = client.transcribe(url=SLOW)
    STATE["made"].append(job["id"])
    with pytest.raises(ConflictError) as e:
        client.delete(job["id"])
    log(type(e.value).__name__, e.value.status, e.value.code, "-", e.value.message)
    try:
        client.cancel(job["id"])
    except Exception:
        pass


@pytest.mark.skipif(not GPU, reason="SCRIPTIVOX_TEST_GPU is not 1")
def test_gpu_transcribe_file_wait_export(client):
    last = []
    job = client.transcribe(file=SPEECH, language="en", on_upload_progress=lambda p: last.append(p.percent))
    STATE["made"].append(job["id"])
    log("created", job["id"], "upload progress", last[-1])
    statuses = []
    done = client.wait(job["id"], on_poll=lambda t: statuses.append(t["status"]))
    log("polled", " → ".join(statuses))
    log("transcript:", repr(done["result"]["full_transcript"]))
    log("duration", done.get("audio_duration_seconds"), "s, cost", done.get("cost_cents"), "cents")
    assert re.search(r"scriptivox|software|development|upload|caption", done["result"]["full_transcript"].lower())
    srt = client.get(job["id"], format="srt", max_words=3)
    vtt = client.get(job["id"], format="vtt")
    text = client.get(job["id"], format="text", include_timestamps=True)
    log("srt first cue:", repr(srt.split("\n\n")[0]))
    log("vtt head:", repr(vtt[:40]))
    log("text:", repr(text[:80]))
    assert re.match(r"^1\r?\n\d\d:\d\d:\d\d,\d{3} --> ", srt)
    assert vtt.startswith("WEBVTT")
    assert re.match(r"^\[\d\d:\d\d\]", text)
