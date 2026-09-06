"""Stand-in for api.soniox.com, just enough to exercise the shim end to end.

MOCK_FAIL=1 makes every job fail, which is how the cleanup-on-error path is
tested: the shim must still delete the file and the job.
"""

import os

from fastapi import FastAPI, Request, UploadFile, File

app = FastAPI()

FAIL = os.environ.get("MOCK_FAIL") == "1"

state = {
    "uploaded_bytes": 0,
    "job_payload": None,
    "auth": None,
    "polls": 0,
    "deleted": [],
}

WORDS = "тестовая расшифровка через Nomad и Terraform".split(" ")


@app.post("/v1/files")
async def upload(file: UploadFile = File(...)):
    state["uploaded_bytes"] = len(await file.read())
    return {"id": "file_test"}


@app.post("/v1/transcriptions")
async def create(request: Request):
    state["job_payload"] = await request.json()
    # Which credential actually reached Soniox — the shim must never forward a
    # client's bearer token as the API key.
    state["auth"] = request.headers.get("authorization")
    return {"id": "job_test"}


@app.get("/v1/transcriptions/{job_id}")
async def poll(job_id: str):
    state["polls"] += 1
    if FAIL:
        return {"status": "error", "error_message": "synthetic failure"}
    # Report progress twice so the polling loop is actually exercised.
    if state["polls"] <= 2:
        return {"status": "processing"}
    # Deliberately longer than the last token ends: the shim must bill the audio,
    # not the speech.
    return {"status": "completed", "audio_duration_ms": 3000}


@app.get("/v1/transcriptions/{job_id}/transcript")
async def transcript(job_id: str):
    return {
        "text": " ".join(WORDS),
        "tokens": [
            {"text": w, "end_ms": (i + 1) * 400} for i, w in enumerate(WORDS)
        ],
    }


@app.delete("/v1/transcriptions/{job_id}")
async def del_job(job_id: str):
    state["deleted"].append(f"job:{job_id}")
    return {"ok": True}


@app.delete("/v1/files/{file_id}")
async def del_file(file_id: str):
    state["deleted"].append(f"file:{file_id}")
    return {"ok": True}


@app.get("/_state")
async def dump():
    return state


@app.get("/v1/models")
async def models():
    return {"models": []}
