# InferenceIQ

A **prompt token-optimizer for Claude Code**. It makes you save prompts 

You run Claude Code through a local **proxy** that strips filler from your prompts and
forwards everything else byte-for-byte to Anthropic. A separate **dashboard** tallies the
tokens and dollars saved.

- [Quick start](#quick-start)
- [Lifecycle reference](#lifecycle-reference)
- [The dashboard](#the-dashboard)
- [Configuration](#configuration)
- [Manual use (no launcher)](#manual-use-no-launcher)
- [How it works](#how-it-works)

---

## Architecture

<img width="991" height="197" alt="InferenceIQ architecture" src="https://github.com/user-attachments/assets/62d89474-ef4e-4294-b00a-617690a42d5f" />

| Component | Role |
|---|---|
| `core-engine/optimize.py` | mechanical filler-strip + token counting + reporting; also a CLI |
| `core-engine/router.py` | deterministic intent → model routing (Haiku/Sonnet/Opus) |
| `core-engine/semcache.py` | 3-layer semantic cache (non-agentic, text-only) |
| `core-engine/calibrate.py` | same-prompt brevity gauge (reply-trimming on vs off) |
| `proxy/intercept.py` | optimizing reverse proxy, port **:8082** |
| `dashboard/collector.py` | standalone metrics dashboard, port **:8088** |

---

## Quick start

The proxy optimizes your prompts and forwards byte-for-byte to Anthropic; the dashboard
tallies the savings. They have **independent lifecycles** — bring up whichever you want.

### 1. Start the dashboard (optional, but recommended)

```bash
./dash start         # collector on :8088 — see your savings at http://localhost:8088
```

> The dashboard outlives proxy restarts so its tally keeps compounding. Skip this step and
> the proxy still optimizes — you just won't have a live view. See [The dashboard](#the-dashboard).

### 2. Start the proxy

```bash
./iq-lite start      # no Docker — runs the proxy on :8082 in a local venv
# or
./iq start           # Docker — proxy :8082 (includes the semantic cache)
```

> Both launchers start **only the proxy**. `start` just picks where the proxy *reports* —
> it never brings the collector up (that's `./dash`, step 1).

### 3. Launch Claude Code through it

```bash
./iq-lite            # or ./iq  — routes Claude through the proxy if it's up
```

You'll see one of these at launch:

- `⚡ Optimized with FortiInferenceIQ` — proxy is up, traffic routes through it
- `○ Plain Claude Mode` — proxy not running, plain Claude (no optimization)

### 4. Stop everything when done

```bash
./iq-lite stop       # or ./iq stop  — stops the proxy
./dash stop          # stops the dashboard (independent — stop it whenever)
```

### TL;DR — full stack in three lines

```bash
./dash start         # dashboard  :8088
./iq-lite start      # proxy      :8082   (or ./iq start for Docker)
./iq-lite            # Claude Code through the proxy   (or ./iq)
```

---

## Lifecycle reference

Both launchers **never auto-start the proxy** — you own its lifecycle. Each exposes the
same two control verbs (`start` / `stop`); a bare invocation just launches Claude Code and
routes through the proxy **only if it's already up**.

| Action | `iq-lite` (venv, no Docker) | `iq` (Docker) |
|---|---|---|
| **Start** the proxy | `./iq-lite start` | `./iq start` |
| **Launch** Claude through it | `./iq-lite [claude args]` | `./iq [claude args]` |
| **Stop** the proxy | `./iq-lite stop` | `./iq stop` |
| What `start` brings up | proxy `:8082` only | proxy `:8082` only (dashboard is separate) |
| What `stop` does | kills the saved PID (`.iq-lite.pid`), falls back to `pkill` on uvicorn | `docker compose down` |
| Semantic cache | off | on |

There is **no `restart` or `status` verb** — to restart, run `stop` then `start`.

> **Don't mix the two launchers on the same `:8082`.** If `iq-lite` is already bound to the
> port, `iq start` (or vice-versa) will collide — `stop` the running one first.

### Check whether the proxy is running

```bash
curl -fsS -o /dev/null http://localhost:8082/dashboard && echo up || echo down
```

- **iq-lite** also writes its PID to `.iq-lite.pid` and logs to `.iq-lite.log`:
  ```bash
  cat .iq-lite.pid                     # PID of the running proxy
  tail -f .iq-lite.log                 # follow proxy output
  ```
- **iq** runs under Docker — inspect it with compose:
  ```bash
  docker compose ps                    # is the intercept service up?
  docker compose logs -f intercept     # follow proxy output
  ```

---

## The dashboard

The proxy and the dashboard have **independent lifecycles**. Neither `iq` nor `iq-lite`
brings the collector up or down — `start` only picks where the proxy *reports*; `stop` only
kills the proxy. The collector (`dashboard/collector.py`, `:8088`) is meant to outlive proxy
restarts so its `tally.json` keeps compounding.

Manage a **local** collector with `./dash` (same pattern as `iq-lite` — PID file, on-demand
venv, no Docker):

```bash
./dash start    # start the collector on :8088 (backgrounded)
./dash stop     # stop it
./dash status   # is it up? where's the log?
```

| | detail |
|---|---|
| What `start` brings up | collector `:8088` only (reuses iq-lite's `.venv-iq`) |
| What `stop` does | kills the saved PID (`.dash.pid`), falls back to `pkill` on uvicorn |
| Port override | `IQ_DASH_PORT=9000 ./dash start` |
| Log | `.dash.log` |

Open **http://localhost:8088** once it's up. Stopping the proxy never affects it, and
vice-versa — that's deliberate.

> Pointing the proxy at a **remote** collector? Then `./dash` doesn't apply — the collector
> lives on that box. See [Configuration](#configuration).

---

## Configuration

The dashboard location lives in **`.env`**. To report to a remote collector, edit the one
line and recreate the container (a plain `restart` won't pick up env changes):

```bash
# .env
DASHBOARD_URL=http://192.168.1.50:8088     # ← your collector host:8088
```
```bash
docker compose up -d intercept             # recreate to apply
```

**Local Docker viewing is the exception.** A container's `localhost` is *not* your machine,
so the report target and the browser URL must differ — set these two (they override
`DASHBOARD_URL`):

```bash
# .env  — view the dashboard on this Mac
INFERENCEIQ_DASHBOARD=http://host.docker.internal:8088   # container → host collector
DASHBOARD_PUBLIC_URL=http://localhost:8088               # browser opens this
```

Comment those two out to fall back to the single `DASHBOARD_URL`.

---

## Manual use (no launcher)

The launchers just set one env var. You can do it yourself for a single command:

```bash
ANTHROPIC_BASE_URL=http://localhost:8082 claude
```

That routes every API call from that one `claude` command through the proxy. It touches
**no global config** — nothing else on your machine changes.

---

## How it works

InferenceIQ has **one shared core** and **thin surfaces** around it:

- **Core engine** (`core-engine/`) — the deterministic text compressor (`optimize.py`),
  model routing (`router.py`), and the semantic cache (`semcache.py`).
- **Proxy** (`proxy/intercept.py`, `:8082`) — the in-path surface. On each `POST
  /v1/messages` it optimizes **only the last user turn** (skips `tool_result` turns),
  optionally appends a brevity directive (`CONCISE=1`), and forwards everything else
  byte-for-byte to `api.anthropic.com`. Streaming passes straight through.
- **Dashboard** (`dashboard/collector.py`, `:8088`) — a standalone monitor. Every surface
  POSTs to its `/api/record`; it renders the aggregate with a per-machine breakdown.

**Safe for Claude Code:** the proxy never synthesizes or caches agentic responses, never
drops messages, and only ever touches genuine typed prose in the last user turn — keeping
Anthropic's prompt cache (and its ~90% discount) intact.

Two launchers wrap the proxy:

- **`iq-lite`** — Docker-free. First `start` creates `.venv-iq` (fastapi / uvicorn / httpx).
  Semantic cache is off; optimize + CONCISE + model routing still work.
- **`iq`** — Docker. Includes the semantic cache. The dashboard is **not** part of the
  compose stack — run a local collector with `./dash start` or point at a remote one.

See [`CLAUDE.md`](CLAUDE.md) for the full architecture, safety invariants, and file map.
