# soniox-openai-shim

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/CosmDandy/soniox-openai-shim)

An OpenAI-compatible `/v1/audio/transcriptions` endpoint backed by [Soniox](https://soniox.com).

Dictation clients that speak the OpenAI transcription API — Spokenly, among others — send one
synchronous request and expect text back. Soniox has no such endpoint: it wants an async job
flow. This shim sits between them and translates:

```
client ──OpenAI multipart──▶ shim ──▶ POST /v1/files            upload
                                 ──▶ POST /v1/transcriptions    create job
                                 ──▶ GET  …/{id}                poll
                                 ──▶ GET  …/{id}/transcript     fetch
                                 ◀── {"text": "…"}
```

Audio and the finished job are deleted from your Soniox account after every request, once the
client already has its answer.

Useful if your client dropped native Soniox support — Spokenly removed API-key support in
2.18.12 (2026-04-02) — but you still want Soniox quality on your own key.

## Quick start

```bash
cp secrets.sops.yaml.example secrets.sops.yaml   # then encrypt, or just export the key
export SONIOX_API_KEY=...
docker compose up -d --build
curl -s http://127.0.0.1:8756/health
```

Point the client at `http://127.0.0.1:8756/v1` as an OpenAI-compatible provider. Whatever the
client has in its API-key field is ignored while `SONIOX_API_KEY` is set on the server, so put
any placeholder there — `sk-noop` is as good as anything. A model name that does not start
with `stt-` is replaced by the configured Soniox model; an `stt-rt-*` id is translated to its
async twin.

With [sops](https://github.com/getsops/sops) for the key, the bundled Makefile wraps the same
thing: `make key` to edit it, `make up` to start, `make test`, `make stats`, `make bench`.

## Configuration

Everything except the secret lives in `environment:` in `compose.yaml`; the key comes from
`secrets.sops.yaml`.

| Variable | Default | Purpose |
|---|---|---|
| `SONIOX_API_KEY` | — | Your key from console.soniox.com. |
| `SHIM_AUTH_TOKEN` | unset | Shared secret required from callers. See *Exposing it* below. |
| `SONIOX_MODEL` | `stt-async-v5` | Soniox rename models often; a real-time id is translated to its async twin. |
| `SONIOX_LANGUAGE_HINTS` | `ru,en` | Language hints — the lever for code-switching accuracy. |
| `SONIOX_CONTEXT_DOMAIN` | unset | Subject of the speech, sets the frame. |
| `SONIOX_CONTEXT_TERMS` | unset | Comma-separated vocabulary. The single biggest accuracy lever. |
| `SONIOX_BASE_URL` | `https://api.soniox.com` | Regional endpoint; EU is `https://api.eu.soniox.com`. Keys are region-bound: a US key gets 401 from the EU endpoint, so changing region means issuing a key in that region's console. |
| `SONIOX_POLL_INTERVAL` | `0.1` | Job polling step, seconds. |
| `SONIOX_POLL_TIMEOUT` | `300` | Ceiling on one dictation, seconds. |
| `SONIOX_PRICE_PER_HOUR` | `0.10` | Price stamped on each entry when it is written; `/stats` only sums what was recorded. |
| `SONIOX_USAGE_LOG` | `/data/usage.jsonl` | Where usage entries are appended. |
| `SHIM_MAX_UPLOAD_BYTES` | `67108864` | Bodies declaring more than this are refused with 413 before being read. A client that lies about `Content-Length` still gets through, so keep a limit on the reverse proxy — the deploy example sets one. |

### The term list matters

Soniox takes `context` as a structured object: `general` frames the subject, `terms` pins the
spelling and casing of proper nouns. The ceiling is 8000 tokens (~10000 characters), so there
is room for a large vocabulary.

The difference is measurable. On the same recording, terms present in the list came back
correct, while missing ones turned into "Victory Matrix" for VictoriaMetrics and "CIVITFS"
for SeaweedFS. Fill it with the words you actually say.

## Why async, and not the real-time WebSocket

Soniox offers a real-time model that accepts pre-recorded audio faster than real time, which
sounds like the faster option. It is not: the service paces the stream at wall-clock speed, so
a result arrives roughly when the recording would have finished playing. Async jobs take
near-constant time instead.

Measured against the live API, median of three runs:

| Audio length | WebSocket | Async |
|---|---|---|
| 4.7 s | 4.40 s | 2.89 s |
| 12.6 s | 11.50 s | 3.29 s |
| 25.7 s | 23.36 s | 4.10 s |

Break-even is around 2.5 seconds of speech; everything longer is faster over async. Reproduce
with `tests/bench.py`, which times both transports phase by phase.

End-to-end latency through the shim is 2.3–2.7 s, near-constant in the length of the dictation.
Roughly one second of that is ours and ~1.85 s is Soniox computing. What is already squeezed:
the HTTP connection pool is kept warm (paying for a cold connection on every dictation cost an
extra 0.83 s in the upload phase), cleanup runs after the response instead of before it, and
polling is tightened to 100 ms.

## Exposing it beyond localhost

By default the shim has no authentication: with `SHIM_AUTH_TOKEN` unset, anyone who can reach
the port is served on your Soniox key. That is only safe while the socket is bound to
`127.0.0.1`, and the service says so in its log on every start.

Set `SHIM_AUTH_TOKEN` before the service is reachable from anywhere else. It then becomes a
password, checked in constant time before the request body is parsed at all — an unauthenticated
caller cannot make the service spool an upload to disk. `/stats` sits behind the same gate, since
it reports how much you dictate and what it costs; `/health` and `/v1/models` stay open, because
a healthcheck and a client's provider probe need them.

The secret is never forwarded upstream: Soniox always sees the server's own key. The test suite
asserts both halves of that, along with rejection of a wrong, absent or non-ASCII bearer.

```bash
openssl rand -base64 32
```

`deploy/docker-compose.yaml` is an example for a host already running Traefik: no published
ports, TLS and routing from the reverse proxy, a rate limit, a 64 MiB body cap at the edge, and
the token required rather than optional.

## Usage and cost

`GET /stats` reports dictations, minutes of audio, estimated cost and median latency, persisted
on the `usage` volume.

At $0.10 per audio-hour, a heavy dictation habit is cheap: six months of one user's history —
10 850 dictations, 77.5 hours — works out to $7.75, about $1.30 a month. That history is
uneven: a median day is 23 minutes, while the busiest single day was 213 minutes and would
have cost 36 cents.

## Tests

`./tests/e2e.sh` runs the whole path against a stub Soniox in a throwaway Docker network: no API
key and no network access to Soniox required. It checks transcription, the text response format,
the empty-upload guard and the size ceiling, that config reaches the API in the documented shape,
that the file and the job are deleted afterwards — including when the job fails, where a
background task would have been dropped — that usage is billed from the audio duration rather
than the last spoken word, that the token gate accepts the right token while rejecting a wrong
or absent one — including a non-ASCII one, which a naive constant-time compare would crash on —
that `/stats` is behind that gate while the health probe stays open, and that the shared secret
never reaches Soniox in place of the API key.

## Releases

Images are published to `ghcr.io/cosmdandy/soniox-openai-shim` on every push to
`master` as `master`, `latest` and `sha-<short>`, and on a version tag as the
bare semver. Deployments should pin the semver — `latest` moves under you. Images are built
for `linux/amd64` only; on another architecture build locally with `make up`.

Cutting a release is one command; the tag is what triggers the versioned build:

```bash
git tag -a v0.2.0 -m "v0.2.0 — what changed"
git push origin v0.2.0
```

Commits follow conventional commits, so the history reads as a changelog until
there is reason to generate one.

## Licence

MIT.
