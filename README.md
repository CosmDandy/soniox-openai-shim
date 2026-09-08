# soniox-openai-shim

[![Open in GitHub Codespaces][codespaces]](https://codespaces.new/CosmDandy/soniox-openai-shim)

[![build][build]](https://github.com/CosmDandy/soniox-openai-shim/actions/workflows/image.yaml) [![scorecard][scorecard]](https://scorecard.dev/viewer/?uri=github.com/CosmDandy/soniox-openai-shim) [![SLSA][SLSA]](https://slsa.dev) [![ghcr.io][ghcr.io]](https://github.com/CosmDandy/soniox-openai-shim/pkgs/container/soniox-openai-shim) [![license][license]](LICENSE)

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
| `SONIOX_BASE_URL` | `https://api.soniox.com` | Regional endpoint; EU is `https://api.eu.soniox.com`. Keys are region-bound: a US key gets 401 from the EU endpoint, so changing region means issuing a key in that region's console. |
| `SONIOX_POLL_INTERVAL` | `0.1` | Job polling step, seconds. |
| `SONIOX_POLL_TIMEOUT` | `300` | Ceiling on one dictation, seconds. |
| `SONIOX_USAGE_LOG` | `/data/usage.jsonl` | Where usage entries are appended. |
| `SHIM_DAILY_LIMIT_MINUTES` | `0` | Ceiling on audio billed per UTC day; `0` disables it. Past the cap transcription answers 429 while `/stats` keeps working. This is the only measure that limits what a leaked token can cost you, rather than the odds of it leaking. |
| `SHIM_MAX_UPLOAD_BYTES` | `67108864` | Bodies declaring more than this are refused with 413 before being read. A client that lies about `Content-Length` still gets through, so keep a limit on the reverse proxy — the deploy example sets one. |

### No term list

Soniox accepts a `context` object that pins the spelling of proper nouns, and the shim
deliberately does not send one. It is billed as input text tokens on *every* request, at
$3.50 per million against $1.50 for audio: a 1100-character vocabulary came to 524 tokens a
request, which was 72% of a month's bill — more than the audio and the transcript combined.

What it bought was not measurable. One user's history holds 7124 dictations transcribed by
Soniox with no vocabulary and 382 through this shim with a 76-term one; the same technical
names appear 5.8 times per 10 000 characters in the first and 5.9 in the second. If a word you
say constantly does come back wrong, `_create_job` is four lines from sending a `context`
again — but price it first.

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

Every rejection is logged with the caller's address, taken from `X-Forwarded-For` — behind a
reverse proxy the socket only ever shows the proxy, so without that header a burst of probes is
indistinguishable from your own typo on a new device. Pair it with `SHIM_DAILY_LIMIT_MINUTES`:
the log tells you something is wrong, the cap decides how much it can cost before you notice.

```bash
openssl rand -base64 32
```

`deploy/docker-compose.yaml` is an example for a host already running Traefik: no published
ports, TLS and routing from the reverse proxy, a rate limit, a 64 MiB body cap at the edge, and
the token required rather than optional.

## Usage and cost

`GET /stats` reports dictations, minutes of audio and median latency from the log on the
`usage` volume, and the money from Soniox's own `/v1/usage/summary` for the calendar month so
far — cost to date, cost today, and what that works out to per audio-hour.

The money is not computed here on purpose. Soniox bills per token, at rates that differ by
model and by what the token is, so a local estimate from an average price per hour is a guess;
the one this endpoint used to print was low by a factor of four.

The bill is small either way. Dictation with no term list runs about $0.11 per audio-hour, so
six months of one user's history — 10 850 dictations, 77.5 hours — comes to roughly $8.50. That
history is uneven: a median day is 23 minutes, the busiest single day 213 minutes.

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

[codespaces]: https://github.com/codespaces/badge.svg
[build]: https://img.shields.io/github/actions/workflow/status/CosmDandy/soniox-openai-shim/image.yaml?branch=master&style=flat&label=build&labelColor=21262d&logo=githubactions&logoColor=8b949e
[scorecard]: https://img.shields.io/ossf-scorecard/github.com/CosmDandy/soniox-openai-shim?style=flat&label=scorecard&labelColor=21262d
[SLSA]: https://img.shields.io/badge/SLSA-3-7828dc?style=flat&labelColor=21262d&logo=data%3Aimage%2Fpng%3Bbase64%2CiVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAMAAAAolt3jAAAABGdBTUEAALGPC%2FxhBQAAACBjSFJNAAB6JgAAgIQAAPoAAACA6AAAdTAAAOpgAAA6mAAAF3CculE8AAABMlBMVEXvMQDvMADwMQDwMADwMADvMADvMADwMADwMQDvMQDvMQDwMADwMADvMADwMADwMADwMQDvMQDvMQDwMQDvMQDwMQDwMADwMADwMQDwMADwMADvMADvMQDvMQDwMADwMQDwMADvMQDwMADwMQDwMADwMADwMADwMADwMADwMADvMQDvMQDwMADwMQDwMADvMQDvMQDwMADvMQDvMQDwMADwMQDwMQDwMQDvMQDwMADvMADwMADwMQDvMQDwMADwMQDwMQDwMQDwMQDvMQDvMQDvMADwMADvMADvMADvMADwMQDwMQDvMADvMQDvMQDvMADvMADvMQDwMQDvMQDvMADvMADvMADvMQDwMQDvMQDvMQDvMADvMADwMADvMQDvMQDvMQDvMADwMADwMQDwMAAAAAA%2FHoSwAAAAY3RSTlMpsvneQlQrU%2FLQSWzvM5DzmzeF9Pi%2BN6vvrk9HuP3asTaPgkVFmO3rUrMjqvL6d0LLTVjI%2FPuMQNSGOWa%2F6YU8zNuDLihJ0e6aMGzl8s2IT7b6lIFkRj1mtvQ0eJW95rG0%2BSid59x%2FAAAAAWJLR0Rltd2InwAAAAlwSFlzAAAOwwAADsMBx2%2BoZAAAAAd0SU1FB%2BYHGg0tGLrTaD4AAACqSURBVAjXY2BgZEqGAGYWVjYGdg4oj5OLm4eRgZcvBcThFxAUEk4WYRAVE09OlpCUkpaRTU6WY0iWV1BUUlZRVQMqUddgSE7W1NLS1gFp0NXTB3KTDQyNjE2Sk03NzC1A3GR1SytrG1s7e4dkBogtjk7OLq5uyTCuu4enl3cyhOvj66fvHxAIEmYICg4JDQuPiAQrEmGIio6JjZOFOjSegSHBBMpOToxPAgCJfDZC%2Fm2KHgAAACV0RVh0ZGF0ZTpjcmVhdGUAMjAyMi0wNy0yNlQxMzo0NToyNCswMDowMC8AywoAAAAldEVYdGRhdGU6bW9kaWZ5ADIwMjItMDctMjZUMTM6NDU6MjQrMDA6MDBeXXO2AAAAGXRFWHRTb2Z0d2FyZQB3d3cuaW5rc2NhcGUub3Jnm%2B48GgAAAABJRU5ErkJggg%3D%3D
[ghcr.io]: https://img.shields.io/badge/ghcr.io-soniox--openai--shim-00a8c8?style=flat&labelColor=21262d&logo=docker&logoColor=8b949e
[license]: https://img.shields.io/github/license/CosmDandy/soniox-openai-shim?style=flat&label=license&labelColor=21262d&color=484f58&logo=opensourceinitiative&logoColor=8b949e
