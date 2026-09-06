"""Time both Soniox transports on the same audio, phase by phase.

Runs inside the shim container so it inherits SONIOX_API_KEY from the compose
env file: no key ever has to be printed or passed on a command line.

    docker compose run --rm -v "$PWD/tests:/app/tests:ro" -v /some/audio:/audio:ro \
        --entrypoint python soniox-shim /app/tests/bench.py /audio/speech.wav
"""

import asyncio
import json
import os
import statistics
import sys
import time
import wave

import httpx
import websockets

KEY = os.environ["SONIOX_API_KEY"]
WS_URL = os.environ.get("SONIOX_WS_URL", "wss://stt-rt.soniox.com/transcribe-websocket")
REST = os.environ.get("SONIOX_BASE_URL", "https://api.soniox.com")
HINTS = ["ru", "en"]
CHUNK = int(os.environ.get("SONIOX_CHUNK_BYTES", str(64 * 1024)))


def duration_s(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


async def bench_ws(blob: bytes) -> dict:
    t0 = time.monotonic()
    marks = {}
    async with websockets.connect(WS_URL, max_size=None, ping_interval=None) as ws:
        marks["connect"] = time.monotonic() - t0
        await ws.send(json.dumps({
            "api_key": KEY, "model": "stt-rt-v5",
            "audio_format": "auto", "language_hints": HINTS,
        }))
        for off in range(0, len(blob), CHUNK):
            await ws.send(blob[off : off + CHUNK])
        await ws.send("")
        marks["audio_sent"] = time.monotonic() - t0

        first = None
        parts = []
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("error_code"):
                raise SystemExit(f"soniox error: {msg}")
            for tok in msg.get("tokens", []):
                if first is None:
                    first = time.monotonic() - t0
                if tok.get("is_final"):
                    parts.append(tok.get("text", ""))
            if msg.get("finished"):
                break
    marks["first_token"] = first
    marks["total"] = time.monotonic() - t0
    marks["text"] = "".join(parts).strip()
    return marks


# Mirror the shim: one pooled, pre-warmed client, so the TLS handshake is not
# charged to every measurement.
_pool: httpx.AsyncClient | None = None


async def pool() -> httpx.AsyncClient:
    global _pool
    if _pool is None:
        _pool = httpx.AsyncClient(
            base_url=REST,
            headers={"Authorization": f"Bearer {KEY}"},
            timeout=120,
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300.0),
        )
        await _pool.get("/v1/models")
    return _pool


async def bench_async(blob: bytes) -> dict:
    t0 = time.monotonic()
    marks = {}
    c = await pool()
    if True:
        r = await c.post("/v1/files", files={"file": ("a.wav", blob, "application/octet-stream")})
        r.raise_for_status()
        file_id = r.json()["id"]
        marks["upload"] = time.monotonic() - t0

        r = await c.post("/v1/transcriptions", json={
            "model": "stt-async-v5", "file_id": file_id, "language_hints": HINTS,
        })
        r.raise_for_status()
        job = r.json()["id"]
        marks["job_created"] = time.monotonic() - t0

        while True:
            r = await c.get(f"/v1/transcriptions/{job}")
            r.raise_for_status()
            status = r.json().get("status")
            if status == "completed":
                break
            if status == "error":
                raise SystemExit(f"job failed: {r.json()}")
            await asyncio.sleep(0.35)
        marks["job_done"] = time.monotonic() - t0

        r = await c.get(f"/v1/transcriptions/{job}/transcript")
        r.raise_for_status()
        marks["text"] = (r.json().get("text") or "").strip()
        marks["total"] = time.monotonic() - t0

        await c.delete(f"/v1/transcriptions/{job}")
        await c.delete(f"/v1/files/{file_id}")
    return marks


async def main() -> None:
    runs = int(os.environ.get("BENCH_RUNS", "3"))
    for path in sys.argv[1:]:
        blob = open(path, "rb").read()
        secs = duration_s(path)
        print(f"\n=== {path}: {secs:.1f}s of audio, {len(blob)} bytes, {runs} runs ===")

        for name, fn in (("websocket", bench_ws), ("async rest", bench_async)):
            totals = []
            sample = None
            for _ in range(runs):
                m = await fn(blob)
                totals.append(m["total"])
                sample = m
            med = statistics.median(totals)
            print(f"{name:>10}: median {med:6.2f}s  (x{med / secs:.2f} of audio length)")
            detail = {k: round(v, 2) for k, v in sample.items()
                      if k != "text" and isinstance(v, float)}
            print(f"{'':>10}  phases {detail}")
            print(f"{'':>10}  text   {sample['text'][:70]!r}")


asyncio.run(main())
