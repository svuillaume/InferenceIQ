# InferenceIQ — Prompt Token Optimizer and Saver for Claude Code

Makes Claude prompts cheaper to send and replies cheaper to receive — **without changing meaning** —
and shows the savings on a live dashboard (UI brand: **FortiInferenceIQ**).

## How it works

- Strips filler from your prompt (meaning-preserving) and **nudges shorter replies** — the big lever,
  since output tokens cost ~5× input.
- Optionally **routes** simple requests to cheaper models and serves a **semantic cache** for repeated
  non-agentic queries.
- Every surface reports token + $ savings to **one dashboard** (tagged per machine).

You apply it two ways: a **Claude Code hook/plugin** (works on any login, incl. Pro/Max OAuth) and an
in-path **proxy** (API-key only; measures real output savings). It never breaks Claude Code's tool loop.

## Components

| Path | What it is |
|---|---|
| `core-engine/optimize.py` | mechanical filler-strip + token counting + reporting; also a CLI |
| `core-engine/router.py` | deterministic intent → model routing (Haiku/Sonnet/Opus) |
| `core-engine/semcache.py` | 3-layer semantic cache (non-agentic, text-only) |
| `core-engine/calibrate.py` | same-prompt brevity gauge (reply-trimming on vs off) |
| `proxy/intercept.py` | optimizing reverse proxy, port **:8082** |
| `dashboard/collector.py` | standalone metrics dashboard, port **:8088** |
| `.claude/hooks/optimize_prompt.py` | `UserPromptSubmit` hook (shipped as a plugin) |
| `iq` | launcher: starts the proxy and runs Claude Code through it |

## Run — proxy + Claude Code

Needs an **API key** (a Pro/Max OAuth login bypasses the proxy — use the plugin instead).

```bash
docker compose up -d --build     # start the proxy on :8082
./iq                             # run Claude Code through the proxy
docker compose down              # stop
```

- Turn reply-trimming off: `CONCISE=0 docker compose up -d intercept` (on by default).
- Report to a dashboard: `INFERENCEIQ_DASHBOARD=http://<host>:8088 docker compose up -d intercept`.

## Install — dashboard only (standalone)

Depends on nothing else; one instance collects from many machines and **persists totals across
restarts** via a volume.

```bash
cd dashboard
docker build -t iq-dashboard .
docker run -d --name iq-dashboard -p 8088:8088 -v iq_data:/data --restart unless-stopped iq-dashboard
```

- Open: `http://<host>:8088` (simple before/after view) · `http://<host>:8088/full` (detailed).
- Reset counters: `curl -XPOST http://<host>:8088/api/reset`
- Redeploy without losing totals: rebuild, `docker stop iq-dashboard && docker rm iq-dashboard`,
  then re-run with the **same** `-v iq_data:/data`.

## Install — Claude Code plugin (the hook, no API key)

In Claude Code:

```
/plugin marketplace add svuillaume/InferenceIQ
/plugin install inferenceiq@inferenceiq
```

Auto-shortens your prompts and nudges shorter replies on every prompt; never blocks; works on any
login. (Takes effect next session.)

## CLI (optional)

```bash
./core-engine/optimize.py "Hey could you please just clean this up?"
```

## How the before/after numbers are measured

- **Input** — filler removed by the optimizer (after = billed input).
- **Output** — **median** reply length with trimming **on** (`CONCISE=1`) vs **off** (`CONCISE=0`),
  over the concise replies served. For a true same-prompt baseline:
  `ANTHROPIC_API_KEY=… ./core-engine/calibrate.py`.
- All from Anthropic's real `usage` object — not estimates.

## Configuration (proxy env)

`CONCISE` (1/0 reply-trimming) · `ROUTE_MODELS` (on/advise/off) · `CACHE_ENABLED` (1/0) ·
`INFERENCEIQ_DASHBOARD` (where to report; `off` disables) · `IQ_TOKEN` (shared secret for a
token-protected collector) · `IQ_PERSIST_PATH` (dashboard persistence file, default `/data/tally.json`
in the image).

See [CLAUDE.md](CLAUDE.md) for architecture/safety invariants and [roadmap.md](roadmap.md) for what's
applied vs planned.
