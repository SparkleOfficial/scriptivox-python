"""verify_webhook against fixed vectors (one from Python's hmac, one captured from the API's own sender)."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx
import pytest

from scriptivox import Scriptivox, WebhookVerificationError, compute_signature, verify_webhook

FIXTURES = Path(__file__).parent / "fixtures"
VECTORS = [
    (f.name, v)
    for f in sorted(FIXTURES.glob("webhook-vectors*.json"))
    for v in json.loads(f.read_text(encoding="utf-8"))["vectors"]
]


def reason(fn) -> str:
    with pytest.raises(WebhookVerificationError) as e:
        fn()
    return e.value.reason


def test_fixtures_present():
    assert len(VECTORS) >= 2


@pytest.mark.parametrize("name,v", VECTORS, ids=[n for n, _ in VECTORS])
def test_valid_signature_in_every_header_shape(name, v):
    now = int(v["timestamp"]) + 10
    headers = {"X-Scriptivox-Signature": v["signature"], "X-Scriptivox-Timestamp": v["timestamp"]}
    event = verify_webhook(v["body"], headers, v["secret"], now=now)
    assert event == json.loads(v["body"])
    assert verify_webhook(v["body"].encode("utf-8"), headers, v["secret"], now=now) == event
    assert verify_webhook(v["body"], httpx.Headers(headers), v["secret"], now=now) == event
    assert verify_webhook(v["body"], {k.lower(): [val] for k, val in headers.items()}, v["secret"], now=now) == event
    assert verify_webhook(v["body"], list(headers.items()), v["secret"], now=now) == event
    assert verify_webhook(v["body"], [(k.lower().encode(), val.encode()) for k, val in headers.items()], v["secret"], now=now) == event
    assert compute_signature(v["body"], v["timestamp"], v["secret"]) == v["signature"]
    assert Scriptivox(api_key="sk_live_x").verify_webhook(v["body"], headers, v["secret"], now=now) == event


@pytest.mark.parametrize("name,v", VECTORS, ids=[n for n, _ in VECTORS])
def test_tampering_fails(name, v):
    now = int(v["timestamp"]) + 10
    headers = {"X-Scriptivox-Signature": v["signature"], "X-Scriptivox-Timestamp": v["timestamp"]}
    assert reason(lambda: verify_webhook(v["body"].replace("completed", "c0mpleted"), headers, v["secret"], now=now)) == "signature_mismatch"
    assert reason(lambda: verify_webhook(v["body"], headers, v["secret"] + "x", now=now)) == "signature_mismatch"
    assert reason(lambda: verify_webhook(v["body"], headers, v["secret"].replace("whsec_", ""), now=now)) == "signature_mismatch"
    reserialised = json.dumps(json.loads(v["body"]), indent=2)
    assert reason(lambda: verify_webhook(reserialised, headers, v["secret"], now=now)) == "signature_mismatch"
    with pytest.raises(TypeError):
        verify_webhook(json.loads(v["body"]), headers, v["secret"], now=now)


@pytest.mark.parametrize("name,v", VECTORS, ids=[n for n, _ in VECTORS])
def test_five_minute_window(name, v):
    ts = int(v["timestamp"])
    headers = {"X-Scriptivox-Signature": v["signature"], "X-Scriptivox-Timestamp": v["timestamp"]}
    verify_webhook(v["body"], headers, v["secret"], now=ts + 300)
    verify_webhook(v["body"], headers, v["secret"], now=ts - 300)
    assert reason(lambda: verify_webhook(v["body"], headers, v["secret"], now=ts + 301)) == "timestamp_outside_tolerance"
    assert reason(lambda: verify_webhook(v["body"], headers, v["secret"], now=ts - 301)) == "timestamp_outside_tolerance"
    verify_webhook(v["body"], headers, v["secret"], now=ts + 900, tolerance=1000)


def test_missing_and_malformed_headers():
    secret = "whsec_" + "ab" * 32
    body = '{"event":"webhook.test","test":true,"message":"hi"}'
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    assert verify_webhook(body, {"x-scriptivox-signature": sig, "x-scriptivox-timestamp": ts}, secret)["event"] == "webhook.test"
    assert verify_webhook(body, {"x-scriptivox-signature": sig.upper(), "x-scriptivox-timestamp": ts}, secret)["event"] == "webhook.test"
    assert reason(lambda: verify_webhook(body, {"x-scriptivox-timestamp": ts}, secret)) == "missing_header"
    assert reason(lambda: verify_webhook(body, {"x-scriptivox-signature": sig}, secret)) == "missing_header"
    assert reason(lambda: verify_webhook(body, {"x-scriptivox-signature": sig, "x-scriptivox-timestamp": "12a"}, secret)) == "invalid_timestamp"
    assert reason(lambda: verify_webhook(body, {"x-scriptivox-signature": "abc", "x-scriptivox-timestamp": ts}, secret)) == "signature_mismatch"
    assert reason(lambda: verify_webhook(body, {"x-scriptivox-signature": "é" * 64, "x-scriptivox-timestamp": ts}, secret)) == "signature_mismatch"
    with pytest.raises(TypeError):
        verify_webhook(body, {"x-scriptivox-signature": sig, "x-scriptivox-timestamp": ts}, "")
    not_json = "not json"
    sig2 = hmac.new(secret.encode(), f"{ts}.{not_json}".encode(), hashlib.sha256).hexdigest()
    assert reason(lambda: verify_webhook(not_json, {"x-scriptivox-signature": sig2, "x-scriptivox-timestamp": ts}, secret)) == "invalid_json"
