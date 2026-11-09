# Scriptivox Python SDK

The official Python client for the [Scriptivox transcription API](https://platform.scriptivox.com/docs/quickstart). Upload a file or pass a link, wait for the transcript, export SRT, VTT or text, list your jobs and verify webhooks. Sync and asyncio clients, typed responses and one exception class per error code.

- Python 3.9 or newer. One dependency: [httpx](https://www.python-httpx.org/).
- Every submit carries an `Idempotency-Key`, so a retried call never starts a second job.
- Large files are streamed from disk, never read into memory.

## Install

```bash
pip install scriptivox
```

## Quickstart

Create an API key on the [API Keys page](https://platform.scriptivox.com/platform/keys) and add balance on the [Billing page](https://platform.scriptivox.com/platform/billing).

```python
from scriptivox import Scriptivox

client = Scriptivox()  # reads SCRIPTIVOX_API_KEY, or pass api_key="sk_live_..."

job = client.transcribe(file="meeting.mp3", language="en")
done = client.wait(job["id"])
print(done["result"]["full_transcript"])
```

From a public link (direct file links, Google Drive, Dropbox and OneDrive share links):

```python
job = client.transcribe(url="https://example.com/podcast.mp3", diarize=True, speaker_count=2)
```

Pass `language` when you know it: it is faster and more accurate than auto-detection. Omit it to auto-detect.

## Captions and plain text

```python
srt = client.get(job["id"], format="srt", max_words=4)
vtt = client.get(job["id"], format="vtt", include_speakers=True)
text = client.get(job["id"], format="text", include_timestamps=True)
```

`format="json"` (the default) returns the whole job. The caption options are `max_words`, `max_chars`, `max_duration`, `sentence_aware`, `include_speakers`, `strip_chars` and, for text, `include_timestamps`. See [caption export](https://platform.scriptivox.com/docs/api-reference#get-result).

## Upload with progress

```python
def show(p):
    print(f"{p.percent:5.1f}%  {p.loaded}/{p.total} bytes")

upload = client.upload("lecture.mp4", on_progress=show)
job = client.transcribe(upload_id=upload["upload_id"])
# or in one call:
job = client.transcribe(file="lecture.mp4", on_upload_progress=show)
```

`file` can be a path, `bytes`, or a binary file object (`open(path, "rb")`). For bytes or a stream without a name, pass `filename="audio.mp3"`. A non-seekable stream also needs `size=`. Filenames are made ASCII-safe for you (`Réunion.mp3` is sent as `Reunion.mp3`).

## Waiting

`wait(id)` polls until the job ends. It waits 2 s between polls at first, growing to 10 s, and honours `Retry-After` when rate-limited. It returns the completed job. A job that fails raises `TranscriptionFailedError`, whose `code` is the job's `error.code`:

```python
from scriptivox import TranscriptionFailedError

try:
    done = client.wait(job["id"], timeout=3600)
except TranscriptionFailedError as e:
    print(e.code, e.transcription["error"]["message"])  # e.g. URL_NOT_ACCESSIBLE
```

Failed and cancelled jobs cost nothing. Prefer webhooks to polling for long files.

## Listing, cancelling, deleting, balance

```python
for t in client.list(status="completed"):      # follows the cursor page by page
    print(t["id"], t["audio_duration_seconds"])

client.cancel(job_id)    # before transcription starts; not charged
client.delete(job_id)    # a finished job and its transcript
b = client.balance()
print(b["available_cents"], b["auto_topup"]["enabled"])
```

`list()` takes `status`, `from_`, `to` (datetime or ISO string), `limit` (page size) and `order`. Items leave out `result`; `get(id)` has it.

## Webhooks

Pass `webhook_url` when you submit, and check every request with `verify_webhook`. It needs the **raw** body, your key's webhook signing secret (`whsec_…`, on the API Keys page) and the request headers. It refuses a bad signature and a timestamp more than 5 minutes old.

```python
import os
from fastapi import FastAPI, Request, Response
from scriptivox import verify_webhook, WebhookVerificationError

app = FastAPI()

@app.post("/scriptivox")
async def scriptivox_webhook(request: Request):
    try:
        event = verify_webhook(await request.body(), request.headers, os.environ["SCRIPTIVOX_WEBHOOK_SECRET"])
    except WebhookVerificationError:
        return Response(status_code=400)
    delivery_id = request.headers["x-scriptivox-delivery"]  # the same on every retry: skip ones you have handled
    if event["event"] == "transcription.completed":
        print(event["transcription_id"], event["result"]["full_transcript"][:80])
    elif event["event"] == "transcription.failed":
        print(event["transcription_id"], event["error"]["code"])
    return Response(status_code=200)
```

Flask: `verify_webhook(request.get_data(), request.headers, secret)`. Django: `verify_webhook(request.body, request.headers, secret)`.

`client.test_webhook("https://your.server/scriptivox")` sends a signed `webhook.test` event and tells you what your server answered. `client.resend_webhook(job_id)` sends a finished job's final event again.

## Errors

Every error extends `ScriptivoxError`. When the API answers with an error you get an `APIError` subclass named after its `error.code`, under a class for its HTTP status:

```python
from scriptivox import APIError, PaymentRequiredError, RateLimitExceededError, ZeroBalanceError

try:
    client.transcribe(url="https://example.com/a.mp3")
except ZeroBalanceError:
    print("Add funds on the Billing page")
except RateLimitExceededError as e:
    print("Too many jobs in progress:", e.error.get("in_flight"), "of", e.error.get("limit"))
except APIError as e:
    print(e.status, e.code, e.message, e.request_id)  # quote request_id to support
```

| Class | When |
| --- | --- |
| `BadRequestError` (400): `InvalidRequestError`, `InvalidFilenameError`, `InvalidMediaFormatError`, … | The request is wrong; fix it, do not retry |
| `AuthenticationError` (401): `InvalidApiKeyError`, `ApiKeyRevokedError` | The key is missing, wrong or revoked |
| `PaymentRequiredError` (402): `InsufficientBalanceError`, `ZeroBalanceError` | Add balance |
| `NotFoundError` (404): `TranscriptionNotFoundError`, `UploadNotFoundError` | Wrong id |
| `ConflictError` (409), `IdempotencyKeyLockedError` | Not allowed in the job's current state |
| `RateLimitError` (429): `RateLimitExceededError` | Too many requests, or too many jobs in progress |
| `InternalServerError` (5xx), `ServiceUnavailableError` (503): `MaintenanceError` | Our side; safe to retry |
| `APIConnectionError`, `APITimeoutError` | No answer |
| `UploadFailedError` | The file upload itself failed |
| `TranscriptionFailedError` | `wait()`: the job ended failed |
| `WaitTimeoutError` | `wait()`: still running at the timeout |
| `WebhookVerificationError` | `verify_webhook()`: not a genuine, fresh webhook |

The full list of codes is in the [API reference](https://platform.scriptivox.com/docs/api-reference#error-codes).

## Retries, timeouts and idempotency

- Requests that are safe to repeat (reads, deletes, cancels and submits with an `Idempotency-Key`) are retried up to 2 times after a dropped connection or a 5xx, waiting longer before each try (0.5 s, 1 s, 2 s, …).
- A `429` or `503` with `Retry-After` is retried after that many seconds, if it is 60 s or less. A `429` without `Retry-After` (the jobs-in-progress cap) is raised at once.
- `upload()` and `transcribe()` send a new random `Idempotency-Key` per call, reused on the SDK's own retries. Pass `idempotency_key=` (for example your own record id) to make a retry after a crash safe too.

```python
client = Scriptivox(max_retries=4, timeout=30.0)  # timeout per request, in seconds
```

## asyncio

```python
import asyncio
from scriptivox import AsyncScriptivox

async def main():
    async with AsyncScriptivox() as client:
        job = await client.transcribe(file="meeting.mp3", language="en")
        done = await client.wait(job["id"])
        async for t in client.list(status="completed"):
            print(t["id"])

asyncio.run(main())
```

## Configuration

| Setting | Argument | Environment variable | Default |
| --- | --- | --- | --- |
| API key | `api_key` | `SCRIPTIVOX_API_KEY` | none (required) |
| Base URL | `base_url` | `SCRIPTIVOX_BASE_URL` | `https://api.scriptivox.com` |
| Retries | `max_retries` | | 2 |
| Request timeout (s) | `timeout` | | 60 |
| Your own httpx client | `http_client` | | a new `httpx.Client` |

Responses are plain dicts, typed with `TypedDict`s generated from the [OpenAPI document](https://platform.scriptivox.com/openapi.json) (`scriptivox.Transcription`, `scriptivox.Balance`, …).

## Development

```bash
python scripts/generate.py            # regenerate _types.py and _error_codes.py from openapi/openapi.json
python scripts/generate.py --fetch    # refresh the snapshot from platform.scriptivox.com first
pip install -e '.[dev]' && pytest     # unit tests, no network

# integration tests against a test deployment
SCRIPTIVOX_API_KEY=sk_live_... SCRIPTIVOX_BASE_URL=https://api-test.scriptivox.com pytest tests/test_integration.py
```

## License

MIT
