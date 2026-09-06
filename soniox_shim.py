"""
soniox-shim — OpenAI-compatible /v1/audio/transcriptions endpoint backed by Soniox.

Spokenly (or any client that speaks the OpenAI transcription API) points at this
service; it translates the single synchronous OpenAI call into Soniox's async
job flow:

    POST /v1/files                          upload audio
    POST /v1/transcriptions                 create job
    GET  /v1/transcriptions/{id}            poll until completed
    GET  /v1/transcriptions/{id}/transcript fetch text
    DELETE both                             clean up, after the client is served

Async, not the real-time WebSocket: Soniox paces a real-time stream at wall
clock speed, so it costs ~0.9x the recording's length no matter how fast you
push the bytes, while an async job is near-constant (about 2-3s) regardless of length.
Measured with tests/bench.py — rerun it before trusting this comment.

Run (docker):
    cp secrets.sops.yaml.example secrets.sops.yaml && sops -e -i secrets.sops.yaml
    make up

Run (bare):
    export SONIOX_API_KEY=...
    uvicorn soniox_shim:app --host 127.0.0.1 --port 8756
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import statistics
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

# Regional domains follow api.<region>.soniox.com; the bare host is US.
SONIOX_BASE = os.environ.get("SONIOX_BASE_URL", "https://api.soniox.com")


def _batch_model(name: str) -> str:
    """A real-time model id in the async flow is a common slip; translate it."""
    if name.startswith("stt-rt-"):
        return "stt-async-" + name[len("stt-rt-") :]
    return name


# Soniox rev their model names often (preview -> v3 -> v4 -> v5).
# Check soniox.com/docs/stt/models and override with SONIOX_MODEL if needed.
DEFAULT_MODEL = _batch_model(os.environ.get("SONIOX_MODEL", "stt-async-v5"))

# Language hints materially improve accuracy on mixed speech.
LANGUAGE_HINTS = [
    lang.strip()
    for lang in os.environ.get("SONIOX_LANGUAGE_HINTS", "ru,en").split(",")
    if lang.strip()
]

# Domain vocabulary. Soniox takes a structured object, not a blob of text:
# `terms` pins spelling and casing of proper nouns, `general` sets the subject.
# Ceiling is 8000 tokens (~10000 chars) for the whole object.
CONTEXT_TERMS = [
    term.strip()
    for term in os.environ.get("SONIOX_CONTEXT_TERMS", "").split(",")
    if term.strip()
]
CONTEXT_DOMAIN = os.environ.get("SONIOX_CONTEXT_DOMAIN", "").strip()


def _context() -> dict[str, Any] | None:
    context: dict[str, Any] = {}
    if CONTEXT_DOMAIN:
        context["general"] = [{"key": "domain", "value": CONTEXT_DOMAIN}]
    if CONTEXT_TERMS:
        context["terms"] = CONTEXT_TERMS
    return context or None

# Jobs finish in ~1s; poll tight, the request is blocked on this either way.
POLL_INTERVAL = float(os.environ.get("SONIOX_POLL_INTERVAL", "0.1"))
POLL_TIMEOUT = float(os.environ.get("SONIOX_POLL_TIMEOUT", "300"))

# Async batch price per audio hour, per soniox.com/pricing. Only used by /stats.
PRICE_PER_HOUR = float(os.environ.get("SONIOX_PRICE_PER_HOUR", "0.10"))

USAGE_LOG = Path(os.environ.get("SONIOX_USAGE_LOG", "/data/usage.jsonl"))

# A dictation is a few hundred KB; anything far larger is a mistake or an abuse.
# The real ceiling belongs on the reverse proxy — by the time this check runs the
# body has already been received — but it keeps junk from reaching Soniox.
MAX_UPLOAD_BYTES = int(os.environ.get("SHIM_MAX_UPLOAD_BYTES", str(64 * 1024 * 1024)))

# Shared secret the client must present. Unset means "trust whoever reaches me",
# which is only safe while the socket is bound to 127.0.0.1. Set it before the
# service is reachable from anywhere else — a phone, a LAN, an ingress.
AUTH_TOKEN = os.environ.get("SHIM_AUTH_TOKEN", "").strip()

# One pooled client for the process: a cold TLS handshake to Soniox costs ~0.4s,
# which is a quarter of a dictation's total latency if paid on every request.
_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient(
        base_url=SONIOX_BASE,
        timeout=httpx.Timeout(60.0, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300.0),
    )
    # Warm the pool so the first dictation of the day is not the slow one.
    try:
        await _client.get("/v1/models", timeout=5.0)
    except httpx.HTTPError:
        pass

    if not AUTH_TOKEN:
        logging.getLogger("uvicorn.error").warning(
            "SHIM_AUTH_TOKEN is not set: every caller who can reach this port is "
            "served on your Soniox key. Safe on 127.0.0.1, not anywhere else."
        )
    yield
    await _client.aclose()


app = FastAPI(title="soniox-shim", lifespan=lifespan)

# Endpoints a probe or a client legitimately calls without credentials.
PUBLIC_PATHS = {"/health", "/v1/models"}


@app.middleware("http")
async def gate(request: Request, call_next):
    """Authenticate and size-check before FastAPI parses the body.

    Multipart is parsed before the endpoint runs, so a check inside the handler
    happens only after the upload has already been received and spooled to disk —
    an unauthenticated caller could fill the disk and still get a 401.
    """
    path = request.url.path

    if AUTH_TOKEN and path not in PUBLIC_PATHS and not _token_ok(request):
        return JSONResponse({"detail": "Invalid token"}, status_code=401)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"detail": f"Body larger than {MAX_UPLOAD_BYTES} bytes"}, status_code=413
        )

    return await call_next(request)


def _presented(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def _token_ok(request: Request) -> bool:
    """Compare as bytes: compare_digest raises TypeError on non-ASCII strings,
    and Starlette hands us the header decoded as latin-1, so any byte above 127
    in a caller's token would crash the request instead of rejecting it."""
    presented = _presented(request).encode("utf-8", "surrogateescape")
    return secrets.compare_digest(presented, AUTH_TOKEN.encode("utf-8"))


def _resolve_key(request: Request) -> str:
    """Authenticate the caller, then decide which Soniox key to bill."""
    env_key = os.environ.get("SONIOX_API_KEY", "").strip()

    if AUTH_TOKEN:
        # The middleware already verified the bearer. It is a password, never a
        # Soniox key, so it is never forwarded upstream.
        if not env_key:
            raise HTTPException(500, "SHIM_AUTH_TOKEN is set but SONIOX_API_KEY is missing")
        return env_key

    # The server's own key wins. Dictation clients demand *some* key in their
    # settings, and whatever the user typed there is not a Soniox key — sending
    # it upstream would just earn a confusing 401 from Soniox.
    if env_key:
        return env_key

    # No key configured: fall back to whatever the client presented, so the shim
    # is still usable as a pure translator with a per-request key.
    candidate = _presented(request)
    if candidate:
        return candidate
    raise HTTPException(401, "No Soniox API key in Authorization header or SONIOX_API_KEY")


async def _upload(auth: dict[str, str], filename: str, blob: bytes) -> str:
    resp = await _client.post(
        "/v1/files",
        headers=auth,
        files={"file": (filename or "audio.wav", blob, "application/octet-stream")},
    )
    resp.raise_for_status()
    return _json(resp)["id"]


async def _create_job(auth: dict[str, str], file_id: str, model: str, language: str | None) -> str:
    payload: dict[str, Any] = {"model": model, "file_id": file_id}

    hints = [language] if language else LANGUAGE_HINTS
    if hints:
        payload["language_hints"] = hints
    context = _context()
    if context:
        payload["context"] = context

    resp = await _client.post("/v1/transcriptions", headers=auth, json=payload)
    resp.raise_for_status()
    return _json(resp)["id"]


async def _await_completion(auth: dict[str, str], job_id: str) -> int:
    """Waits for the job and returns the audio duration in ms, as Soniox measured it."""
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT
    while True:
        resp = await _client.get(f"/v1/transcriptions/{job_id}", headers=auth)
        resp.raise_for_status()
        body = _json(resp)
        status = body.get("status")

        if status == "completed":
            # Not the last token's end_ms: that stops at the last word and drops
            # the trailing silence, which understates real dictations by ~20%.
            return int(body.get("audio_duration_ms") or 0)
        if status == "error":
            raise HTTPException(502, f"Soniox job failed: {body.get('error_message', 'unknown')}")
        if asyncio.get_event_loop().time() > deadline:
            raise HTTPException(504, f"Soniox job {job_id} timed out")

        await asyncio.sleep(POLL_INTERVAL)


def _json(resp: httpx.Response) -> dict[str, Any]:
    """A 200 that is not JSON means something between us and Soniox answered."""
    try:
        return resp.json()
    except ValueError as exc:
        raise HTTPException(502, "Soniox returned a non-JSON response") from exc


async def _fetch_transcript(auth: dict[str, str], job_id: str) -> str:
    resp = await _client.get(f"/v1/transcriptions/{job_id}/transcript", headers=auth)
    resp.raise_for_status()
    body = _json(resp)

    if isinstance(body.get("text"), str):
        return body["text"].strip()
    # Older shapes return token lists instead of a joined string.
    tokens = body.get("tokens") or []
    return "".join(tok.get("text", "") for tok in tokens).strip()


async def _cleanup(auth: dict[str, str], job_id: str | None, file_id: str | None) -> None:
    """Best-effort: don't leave audio in the account after dictation. On the happy
    path this runs after the response so the user never waits for housekeeping;
    on a failure it runs inline, because background tasks attached to a response
    that was never returned are dropped."""
    for path in (f"/v1/transcriptions/{job_id}" if job_id else None,
                 f"/v1/files/{file_id}" if file_id else None):
        if not path:
            continue
        try:
            # Short timeout: whatever just failed may well hang this call too.
            await _client.delete(path, headers=auth, timeout=5.0)
        except httpx.HTTPError:
            pass


def _record_usage(audio_ms: int, latency_ms: int) -> None:
    """Append one line per dictation so /stats can answer 'what is this costing me'."""
    entry = {
        "ts": time.time(),
        "audio_ms": audio_ms,
        "latency_ms": latency_ms,
        "cost_usd": round(audio_ms / 3_600_000 * PRICE_PER_HOUR, 6),
    }
    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG.open("a") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        # Usage accounting must never cost you a dictation.
        pass


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "model": DEFAULT_MODEL, "language_hints": LANGUAGE_HINTS}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    """Some clients probe this before letting you save the endpoint."""
    return {
        "object": "list",
        "data": [{"id": DEFAULT_MODEL, "object": "model", "owned_by": "soniox"}],
    }


@app.get("/stats")
async def stats() -> dict[str, Any]:
    """Running total of what has been dictated, and what it costs."""
    count = 0
    audio_ms = 0
    cost = 0.0
    latencies: list[int] = []

    if USAGE_LOG.exists():
        for line in USAGE_LOG.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # A line torn by a crash must not take the whole report down.
                continue
            count += 1
            audio_ms += entry["audio_ms"]
            cost += entry["cost_usd"]
            latencies.append(entry["latency_ms"])

    return {
        "dictations": count,
        "audio_minutes": round(audio_ms / 60_000, 2),
        "cost_usd": round(cost, 4),
        "median_latency_ms": round(statistics.median(latencies)) if latencies else None,
        "price_per_hour_usd": PRICE_PER_HOUR,
    }


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    model: str = Form(DEFAULT_MODEL),
    language: str | None = Form(None),
    response_format: str = Form("json"),
):
    api_key = _resolve_key(request)
    auth = {"Authorization": f"Bearer {api_key}"}
    blob = await file.read()
    if not blob:
        raise HTTPException(400, "Empty audio upload")
    if len(blob) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Audio larger than {MAX_UPLOAD_BYTES} bytes")

    # Clients pass OpenAI model names; map anything unknown onto the Soniox model.
    soniox_model = _batch_model(model) if model.startswith("stt-") else DEFAULT_MODEL

    started = time.monotonic()
    job_id: str | None = None
    file_id: str | None = None

    try:
        file_id = await _upload(auth, file.filename or "audio.wav", blob)
        job_id = await _create_job(auth, file_id, soniox_model, language)
        audio_ms = await _await_completion(auth, job_id)
        text = await _fetch_transcript(auth, job_id)
    except httpx.HTTPStatusError as exc:
        await _cleanup(auth, job_id, file_id)
        detail = exc.response.text[:500]
        raise HTTPException(exc.response.status_code, f"Soniox API error: {detail}") from exc
    except httpx.HTTPError as exc:
        # Timeouts, DNS failures, connection resets: no response to quote.
        await _cleanup(auth, job_id, file_id)
        raise HTTPException(502, f"Soniox unreachable: {type(exc).__name__}") from exc
    except Exception:
        # Job failures and the poll timeout raise HTTPException from inside the
        # try block; they must not skip the cleanup either.
        await _cleanup(auth, job_id, file_id)
        raise

    background.add_task(_cleanup, auth, job_id, file_id)
    _record_usage(audio_ms, int((time.monotonic() - started) * 1000))

    if response_format == "text":
        return PlainTextResponse(text)
    return JSONResponse({"text": text})
