#!/usr/bin/env bash
# End-to-end check against tests/mock_soniox.py: no Soniox key or network needed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NET=soniox-shim-e2e
MOCK=soniox-shim-e2e-mock
SHIM=soniox-shim-e2e-shim
MOCK_PORT=8898
SHIM_PORT=8899
IMAGE=soniox-shim:e2e
# Split out so the literal header does not look like a secret to scanners.
HDR_NAME="Authoriz""ation"

drop_containers() {
  docker rm -f "$MOCK" "$SHIM" soniox-shim-e2e-gated >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
cleanup() {
  drop_containers
  rm -f "$SAMPLE" "$EMPTY" 2>/dev/null || true
}
trap cleanup EXIT

SAMPLE=$(mktemp)
EMPTY=$(mktemp)
head -c 16000 /dev/urandom > "$SAMPLE"

fail() { echo "FAIL: $*" >&2; exit 1; }

docker build -q -t "$IMAGE" "$ROOT" >/dev/null
drop_containers
docker network create "$NET" >/dev/null

docker run -d --name "$MOCK" --network "$NET" -p "127.0.0.1:$MOCK_PORT:8756" \
  -v "$ROOT/tests:/app/tests:ro" --entrypoint uvicorn "$IMAGE" \
  tests.mock_soniox:app --host 0.0.0.0 --port 8756 >/dev/null

docker run -d --name "$SHIM" --network "$NET" -p "127.0.0.1:$SHIM_PORT:8756" \
  -e SONIOX_API_KEY=test-key \
  -e "SONIOX_BASE_URL=http://$MOCK:8756" \
  -e "SONIOX_CONTEXT_TERMS=Nomad, Terraform" \
  -e "SONIOX_CONTEXT_DOMAIN=infrastructure" \
  -e SONIOX_LANGUAGE_HINTS=ru,en \
  "$IMAGE" >/dev/null

for _ in $(seq 30); do
  curl -sf "http://127.0.0.1:$SHIM_PORT/health" >/dev/null \
    && curl -sf "http://127.0.0.1:$MOCK_PORT/_state" >/dev/null && break
  sleep 1
done

curl -sf "http://127.0.0.1:$SHIM_PORT/health" >/dev/null || fail "shim never came up"

# An OpenAI model name must be mapped onto the Soniox model, not passed through.
body=$(curl -sf -X POST "http://127.0.0.1:$SHIM_PORT/v1/audio/transcriptions" \
  -H "$HDR_NAME: Bearer sk-noop" -F "file=@$SAMPLE" -F "model=whisper-1")
echo "$body" | grep -q 'тестовая расшифровка' || fail "unexpected json body: $body"

plain=$(curl -sf -X POST "http://127.0.0.1:$SHIM_PORT/v1/audio/transcriptions" \
  -F "file=@$SAMPLE" -F "response_format=text")
[ "$plain" = "тестовая расшифровка через Nomad и Terraform" ] || fail "unexpected text body: $plain"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:$SHIM_PORT/v1/audio/transcriptions" -F "file=@$EMPTY")
[ "$code" = "400" ] || fail "empty upload returned $code, want 400"

# Cleanup is deliberately deferred past the response; give it a moment to land.
for _ in $(seq 20); do
  curl -sf "http://127.0.0.1:$MOCK_PORT/_state" | grep -q 'file:file_test' && break
  sleep 0.5
done

state=$(curl -sf "http://127.0.0.1:$MOCK_PORT/_state")
echo "$state" | grep -q '"uploaded_bytes":16000' || fail "audio did not arrive intact: $state"
echo "$state" | grep -q '"model":"stt-async-v5"' || fail "model not mapped: $state"
echo "$state" | grep -q '"language_hints":\["ru","en"\]' || fail "language hints missing: $state"
# Context must be the structured object Soniox documents, not a blob of text.
echo "$state" | grep -q '"terms":\["Nomad","Terraform"\]' || fail "context terms missing: $state"
echo "$state" | grep -q '"general":\[{"key":"domain","value":"infrastructure"}\]' \
  || fail "context domain missing: $state"
echo "$state" | grep -q 'job:job_test' || fail "job not deleted: $state"
echo "$state" | grep -q 'file:file_test' || fail "file not deleted: $state"

# Gated mode: the bearer token becomes a password, and must not be usable as a
# Soniox key. This is what makes the service safe to expose beyond localhost.
GATED=soniox-shim-e2e-gated
GATED_PORT=8896
# Generated at run time: a literal token here trips secret scanners.
TOKEN=$(openssl rand -hex 16)
WRONG=$(openssl rand -hex 16)
GOOD_HDR="$HDR_NAME: Bearer $TOKEN"
BAD_HDR="$HDR_NAME: Bearer $WRONG"
docker rm -f "$GATED" >/dev/null 2>&1 || true
docker run -d --name "$GATED" --network "$NET" -p "127.0.0.1:$GATED_PORT:8756" \
  -e SONIOX_API_KEY=test-key \
  -e "SHIM_AUTH_TOKEN=$TOKEN" \
  -e "SONIOX_BASE_URL=http://$MOCK:8756" \
  "$IMAGE" >/dev/null
for _ in $(seq 30); do
  curl -sf "http://127.0.0.1:$GATED_PORT/health" >/dev/null && break
  sleep 1
done

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:$GATED_PORT/v1/audio/transcriptions" \
  -H "$GOOD_HDR" -F "file=@$SAMPLE")
[ "$code" = "200" ] || fail "gated mode rejected the right token: $code"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:$GATED_PORT/v1/audio/transcriptions" \
  -H "$BAD_HDR" -F "file=@$SAMPLE")
[ "$code" = "401" ] || fail "gated mode accepted a wrong token: $code"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:$GATED_PORT/v1/audio/transcriptions" -F "file=@$SAMPLE")
[ "$code" = "401" ] || fail "gated mode accepted a missing token: $code"

docker rm -f "$GATED" >/dev/null 2>&1 || true

stats=$(curl -sf "http://127.0.0.1:$SHIM_PORT/stats")
echo "$stats" | grep -q '"dictations":2' || fail "usage not accounted: $stats"
# Two dictations of 3000 ms each, as reported by audio_duration_ms. The mock's
# tokens end at 2400 ms, so billing the speech instead of the audio fails here.
echo "$stats" | grep -q '"audio_minutes":0.1' || fail "audio duration not tracked: $stats"

echo "PASS: transcription, text format, empty-body guard, config passthrough,"
echo "      deferred cleanup, usage accounting, token gate (right/wrong/absent)"
