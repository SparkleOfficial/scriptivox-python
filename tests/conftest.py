"""A scripted httpx transport that records every request (sync and async)."""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional, Union
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from scriptivox import AsyncScriptivox, Scriptivox

BASE = "https://api.example.test"


class Recorded:
    def __init__(self, request: httpx.Request, body: bytes) -> None:
        self.method = request.method
        parts = urlsplit(str(request.url))
        self.url = str(request.url)
        self.host = parts.netloc
        self.path = parts.path
        self.query = dict(parse_qsl(parts.query, keep_blank_values=True))
        self.headers = request.headers
        self.body = body
        self.at = time.monotonic()

    @property
    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


Reply = Union[httpx.Response, Callable[[Recorded], httpx.Response], str]


class Mock:
    def __init__(self) -> None:
        self.requests: List[Recorded] = []
        self.queue: List[Reply] = []
        self.fallback: Optional[Reply] = None

    def reply(self, *replies: Reply) -> "Mock":
        self.queue.extend(replies)
        return self

    def always(self, reply: Reply) -> "Mock":
        self.fallback = reply
        return self

    def _answer(self, rec: Recorded) -> httpx.Response:
        self.requests.append(rec)
        r = self.queue.pop(0) if self.queue else self.fallback
        if r is None:
            return httpx.Response(599, text="no reply queued")
        if r == "timeout":
            raise httpx.ReadTimeout("timed out")
        if r == "refused":
            raise httpx.ConnectError("refused")
        if callable(r):
            r = r(rec)
        r.headers.setdefault("x-request-id", "req_00000000000000aa")
        return r

    def handler(self, request: httpx.Request) -> httpx.Response:
        return self._answer(Recorded(request, request.read()))

    async def ahandler(self, request: httpx.Request) -> httpx.Response:
        return self._answer(Recorded(request, await request.aread()))

    def client(self, **kw: Any) -> Scriptivox:
        return Scriptivox(api_key="sk_live_test", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(self.handler)), **kw)

    def aclient(self, **kw: Any) -> AsyncScriptivox:
        return AsyncScriptivox(api_key="sk_live_test", base_url=BASE, http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.ahandler)), **kw)


def ok(body: Any = None, status: int = 200, headers: Optional[Dict[str, str]] = None) -> httpx.Response:
    if isinstance(body, (str, bytes)):
        return httpx.Response(status, content=body if isinstance(body, bytes) else body.encode(), headers=headers or {"content-type": "text/plain; charset=utf-8"})
    return httpx.Response(status, json=body, headers=headers or {}) if body is not None else httpx.Response(status, headers=headers or {})


def api_error(status: int, code: str, message: Optional[str] = None, headers: Optional[Dict[str, str]] = None, **extra: Any) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"code": code, "message": message or code, "docs_url": "https://platform.scriptivox.com/docs/api-reference#error-codes", "request_id": "req_0123456789abcdef", **extra}},
        headers=headers or {},
    )


def job(id: str, status: str, **extra: Any) -> httpx.Response:
    return ok({"id": id, "status": status, **extra})


@pytest.fixture
def mock() -> Mock:
    return Mock()
