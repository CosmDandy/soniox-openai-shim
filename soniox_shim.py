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
push the bytes, while an async job is near-constant (~2s) regardless of length.
Measured with tests/bench.py — rerun it before trusting this comment.

Run (docker):
    cp .env.example .env && $EDITOR .env
    docker compose up -d --build

Run (bare):
    export SONIOX_API_KEY=...
    uvicorn soniox_shim:app --host 127.0.0.1 --port 8756
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
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
    yield
    await _client.aclose()


app = FastAPI(title="soniox-shim", lifespan=lifespan)


def _presented(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def _resolve_key(request: Request) -> str:
    """Authenticate the caller, then decide which Soniox key to bill."""
    env_key = os.environ.get("SONIOX_API_KEY", "").strip()

    if AUTH_TOKEN:
        # Gated mode: the bearer token is a password, never a Soniox key, so a
        # caller who guesses their way in cannot spend anything on our account.
        if not secrets.compare_digest(_presented(request), AUTH_TOKEN):
            raise HTTPException(401, "Invalid token")
        if not env_key:
            raise HTTPException(500, "SHIM_AUTH_TOKEN is set but SONIOX_API_KEY is missing")
        return env_key

    candidate = _presented(request)
    # Clients often send a dummy key when the real one lives server-side.
    if candidate and candidate not in {"sk-noop", "cant-be-empty", "none"}:
        return candidate
    if not env_key:
        raise HTTPException(401, "No Soniox API key in Authorization header or SONIOX_API_KEY")
    return env_key


async def _upload(auth: dict[str, str], filename: str, blob: bytes) -> str:
    resp = await _client.post(
        "/v1/files",
        headers=auth,
        files={"file": (filename or "audio.wav", blob, "application/octet-stream")},
    )
    resp.raise_for_status()
    return resp.json()["id"]


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
    return resp.json()["id"]


async def _await_completion(auth: dict[str, str], job_id: str) -> None:
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT
    while True:
        resp = await _client.get(f"/v1/transcriptions/{job_id}", headers=auth)
        resp.raise_for_status()
        body = resp.json()
        status = body.get("status")

        if status == "completed":
            return
        if status == "error":
            raise HTTPException(502, f"Soniox job failed: {body.get('error_message', 'unknown')}")
        if asyncio.get_event_loop().time() > deadline:
            raise HTTPException(504, f"Soniox job {job_id} timed out")

        await asyncio.sleep(POLL_INTERVAL)


async def _fetch_transcript(auth: dict[str, str], job_id: str) -> tuple[str, int]:
    """Returns (text, audio duration in ms)."""
    resp = await _client.get(f"/v1/transcriptions/{job_id}/transcript", headers=auth)
    resp.raise_for_status()
    body = resp.json()

    tokens = body.get("tokens") or []
    audio_ms = max((int(tok.get("end_ms") or 0) for tok in tokens), default=0)

    if isinstance(body.get("text"), str):
        return body["text"].strip(), audio_ms
    # Older shapes return token lists instead of a joined string.
    return "".join(tok.get("text", "") for tok in tokens).strip(), audio_ms


async def _cleanup(auth: dict[str, str], job_id: str | None, file_id: str | None) -> None:
    """Best-effort, and deliberately after the response: don't leave audio in the
    account after dictation, but don't make the user wait for the housekeeping."""
    for path in (f"/v1/transcriptions/{job_id}" if job_id else None,
                 f"/v1/files/{file_id}" if file_id else None):
        if not path:
            continue
        try:
            await _client.delete(path, headers=auth)
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
            entry = json.loads(line)
            count += 1
            audio_ms += entry["audio_ms"]
            cost += entry["cost_usd"]
            latencies.append(entry["latency_ms"])

    latencies.sort()
    return {
        "dictations": count,
        "audio_minutes": round(audio_ms / 60_000, 2),
        "cost_usd": round(cost, 4),
        "median_latency_ms": latencies[len(latencies) // 2] if latencies else None,
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

    # Clients pass OpenAI model names; map anything unknown onto the Soniox model.
    soniox_model = _batch_model(model) if model.startswith("stt-") else DEFAULT_MODEL

    started = time.monotonic()
    job_id: str | None = None
    file_id: str | None = None

    try:
        file_id = await _upload(auth, file.filename or "audio.wav", blob)
        job_id = await _create_job(auth, file_id, soniox_model, language)
        await _await_completion(auth, job_id)
        text, audio_ms = await _fetch_transcript(auth, job_id)
    except httpx.HTTPStatusError as exc:
        background.add_task(_cleanup, auth, job_id, file_id)
        detail = exc.response.text[:500]
        raise HTTPException(exc.response.status_code, f"Soniox API error: {detail}") from exc

    background.add_task(_cleanup, auth, job_id, file_id)
    _record_usage(audio_ms, int((time.monotonic() - started) * 1000))

    if response_format == "text":
        return PlainTextResponse(text)
    return JSONResponse({"text": text})
