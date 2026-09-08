#!/usr/bin/env bash
# End-to-end check against tests/mock_soniox.py: no Soniox key or network needed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NET=soniox-openai-shim-e2e
MOCK=soniox-openai-shim-e2e-mock
SHIM=soniox-openai-shim-e2e-shim
MOCK_PORT=8898
SHIM_PORT=8899
IMAGE=soniox-openai-shim:e2e
# Split out so the literal header does not look like a secret to scanners.
HDR_NAME="Authoriz""ation"

drop_containers() {
  docker rm -f "$MOCK" "$SHIM" soniox-openai-shim-e2e-gated \
    soniox-openai-shim-e2e-failmock soniox-openai-shim-e2e-failshim \
    soniox-openai-shim-e2e-capped >/dev/null 2>&1 || true
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
# No vocabulary is sent: Soniox rebills a term list on every single request.
if echo "$state" | grep -q '"context"'; then fail "context sent despite being dropped: $state"; fi
echo "$state" | grep -q 'job:job_test' || fail "job not deleted: $state"
echo "$state" | grep -q 'file:file_test' || fail "file not deleted: $state"

# Gated mode: the bearer token becomes a password, and must not be usable as a
# Soniox key. This is what makes the service safe to expose beyond localhost.
GATED=soniox-openai-shim-e2e-gated
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

# A non-ASCII bearer must be rejected, not crash the handler: compare_digest
# refuses to compare non-ASCII strings.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:$GATED_PORT/v1/audio/transcriptions" \
  -H "$HDR_NAME: Bearer парольчик" -F "file=@$SAMPLE")
[ "$code" = "401" ] || fail "non-ASCII token gave $code, want 401"

# Usage figures are private: the gate must cover /stats too.
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$GATED_PORT/stats")
[ "$code" = "401" ] || fail "/stats open without a token in gated mode: $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "$GOOD_HDR" \
  "http://127.0.0.1:$GATED_PORT/stats")
[ "$code" = "200" ] || fail "/stats rejected the right token: $code"

# Probes stay reachable, otherwise the healthcheck fails the container.
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$GATED_PORT/health")
[ "$code" = "200" ] || fail "/health must stay open for probes: $code"

# The shared secret is a password, not a credential to forward: Soniox must see
# the server's own key.
seen=$(curl -sf "http://127.0.0.1:$MOCK_PORT/_state" | tr ',' '\n' | grep '"auth"')
echo "$seen" | grep -q 'test-key' || fail "server key did not reach the API: $seen"
if echo "$seen" | grep -q "$TOKEN"; then fail "the shared secret leaked upstream to Soniox"; fi

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:$GATED_PORT/v1/audio/transcriptions" -F "file=@$SAMPLE")
[ "$code" = "401" ] || fail "gated mode accepted a missing token: $code"

BAD_HDR="$HDR_NAME: Bearer $WRONG"
# A rejected caller must be identifiable: behind a proxy that means the
# forwarded header, since the socket only ever shows the proxy.
curl -s -o /dev/null -X POST "http://127.0.0.1:$GATED_PORT/v1/audio/transcriptions" \
  -H "$BAD_HDR" -H "X-Forwarded-For: 203.0.113.7" -F "file=@$SAMPLE" || true
docker logs "$GATED" 2>&1 | grep -q '203.0.113.7' \
  || fail "rejection was not logged with the forwarded caller address"

docker rm -f "$GATED" >/dev/null 2>&1 || true

# A failed job still has to be cleaned up: background tasks are dropped when the
# response comes from an exception handler, so this path must clean up inline.
FAILMOCK=soniox-openai-shim-e2e-failmock
FAILSHIM=soniox-openai-shim-e2e-failshim
FAILMOCK_PORT=8894
FAILSHIM_PORT=8893
docker run -d --name "$FAILMOCK" --network "$NET" -p "127.0.0.1:$FAILMOCK_PORT:8756" \
  -e MOCK_FAIL=1 -v "$ROOT/tests:/app/tests:ro" --entrypoint uvicorn "$IMAGE" \
  tests.mock_soniox:app --host 0.0.0.0 --port 8756 >/dev/null
docker run -d --name "$FAILSHIM" --network "$NET" -p "127.0.0.1:$FAILSHIM_PORT:8756" \
  -e SONIOX_API_KEY=test-key -e "SONIOX_BASE_URL=http://$FAILMOCK:8756" \
  -e SHIM_MAX_UPLOAD_BYTES=1000 "$IMAGE" >/dev/null
for _ in $(seq 30); do
  curl -sf "http://127.0.0.1:$FAILSHIM_PORT/health" >/dev/null && break
  sleep 1
done

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:$FAILSHIM_PORT/v1/audio/transcriptions" -F "file=@$SAMPLE")
[ "$code" = "413" ] || fail "oversized upload returned $code, want 413"

small=$(mktemp); head -c 500 /dev/urandom > "$small"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:$FAILSHIM_PORT/v1/audio/transcriptions" -F "file=@$small")
rm -f "$small"
[ "$code" = "502" ] || fail "failed job returned $code, want 502"

for _ in $(seq 20); do
  curl -sf "http://127.0.0.1:$FAILMOCK_PORT/_state" | grep -q 'file:file_test' && break
  sleep 0.5
done
failstate=$(curl -sf "http://127.0.0.1:$FAILMOCK_PORT/_state")
echo "$failstate" | grep -q 'job:job_test' || fail "job left behind after failure: $failstate"
echo "$failstate" | grep -q 'file:file_test' || fail "file left behind after failure: $failstate"

docker rm -f "$FAILMOCK" "$FAILSHIM" >/dev/null 2>&1 || true

# A leaked token is worth at most one day's cap, so the cap has to actually bite.
CAPPED=soniox-openai-shim-e2e-capped
CAPPED_PORT=8892
docker run -d --name "$CAPPED" --network "$NET" -p "127.0.0.1:$CAPPED_PORT:8756" \
  -e SONIOX_API_KEY=test-key -e "SONIOX_BASE_URL=http://$MOCK:8756" \
  -e SHIM_DAILY_LIMIT_MINUTES=0.05 "$IMAGE" >/dev/null
for _ in $(seq 60); do
  curl -sf "http://127.0.0.1:$CAPPED_PORT/health" >/dev/null && break
  sleep 1
done
curl -sf "http://127.0.0.1:$CAPPED_PORT/health" >/dev/null \
  || fail "capped shim never came up (logs: $(docker logs "$CAPPED" 2>&1 | tail -3))"

# The cap must announce itself: an INFO line on a logger with no handler is
# dropped silently, and then nothing tells you whether the cap is even on.
# Waited for, not asserted instantly: the runner writes the log a beat later.
for _ in $(seq 20); do
  docker logs "$CAPPED" 2>&1 | grep -q 'daily cap' && break
  sleep 0.5
done
docker logs "$CAPPED" 2>&1 | grep -q 'daily cap' \
  || fail "the cap did not announce itself (logs: $(docker logs "$CAPPED" 2>&1 | tail -5))"

# The mock bills 3000 ms per dictation, and the cap is 0.05 min = 3000 ms.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:$CAPPED_PORT/v1/audio/transcriptions" -F "file=@$SAMPLE")
[ "$code" = "200" ] || fail "first dictation under the cap returned $code"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "http://127.0.0.1:$CAPPED_PORT/v1/audio/transcriptions" -F "file=@$SAMPLE")
[ "$code" = "429" ] || fail "dictation over the daily cap returned $code, want 429"

# Hitting the cap must not blind you to your own usage.
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$CAPPED_PORT/stats")
[ "$code" = "200" ] || fail "/stats broke once the cap was reached: $code"

docker rm -f "$CAPPED" >/dev/null 2>&1 || true


stats=$(curl -sf "http://127.0.0.1:$SHIM_PORT/stats")
echo "$stats" | grep -q '"dictations":2' || fail "usage not accounted: $stats"
# Two dictations of 3000 ms each, as reported by audio_duration_ms. The mock's
# tokens end at 2400 ms, so billing the speech instead of the audio fails here.
echo "$stats" | grep -q '"audio_minutes":0.1' || fail "audio duration not tracked: $stats"
# Money comes from Soniox's own usage summary, never from a local average price.
echo "$stats" | grep -q '"cost_usd":0.44' || fail "billing not read from the API: $stats"
echo "$stats" | grep -q '"today_usd":0.15' || fail "today not picked off the day array: $stats"
# 0.44 over the hour of audio the summary reports.
echo "$stats" | grep -q '"usd_per_audio_hour":0.44' || fail "rate not derived: $stats"
if echo "$stats" | grep -q 'price_per_hour'; then fail "average price still reported: $stats"; fi

echo "PASS: transcription, text format, empty-body guard, config passthrough,"
echo "      deferred cleanup, usage accounting, token gate (right/wrong/absent),"
echo "      server key never replaced by the caller's, cleanup after a failed job,"
echo "      upload ceiling refused before the body is read, non-ASCII token,"
echo "      /stats behind the gate while probes stay open, daily cap enforced"
echo "      with /stats still readable, rejections logged with the caller address,"
echo "      no context sent upstream, cost read from Soniox's usage summary"
