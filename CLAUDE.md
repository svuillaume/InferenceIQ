# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

InferenceIQ is a **prompt token-optimizer for Claude / Claude Code**. It makes prompts cheaper
to send and replies cheaper to receive — *without changing meaning* — and shows the savings on a
live dashboard. It has **one shared core** and **four thin surfaces** around it:

- **Core engines**
  - `optimize.py` — mechanical, offline, deterministic text compressor (regex rules that drop
    filler / swap verbose phrases / tidy whitespace). Also exact token counting via Anthropic's
    `count_tokens`, the shared `est()` estimate, and the stdlib `report()` that POSTs each run to
    the dashboard (tagged with this machine's `host`/`user`). It's also a CLI.
- **Surfaces (the "doers")**
  - **CLI** — `./optimize.py "…"`.
  - **Claude Code hook** — `.claude/hooks/optimize_prompt.py`, a `UserPromptSubmit` hook. Single
    auto mode: injects a tighter equivalent phrasing **and** a brevity directive as
    `additionalContext` (it cannot replace typed text, so the input cut is advisory/measured; the
    output control is real). Never blocks; fails open.
  - **Intercept proxy** — `intercept.py` (:8082). Point `ANTHROPIC_BASE_URL` at it. On each
    `POST /v1/messages` it optimizes **only the last user turn** (skips `tool_result` turns),
    optionally appends a brevity directive (`CONCISE=1`), and forwards everything else
    **byte-for-byte** to `api.anthropic.com`. Streaming passes straight through.
- **Monitor (view-only)**
  - `dashboard/collector.py` (:8088) — a **standalone** FastAPI collector. It imports nothing
    from the rest of the repo; every surface POSTs to its `/api/record`, and it renders the
    aggregate (`/api/stats`). Because it's self-contained it can run on a **remote** box and
    collect from **many machines at once** — each event carries a `host` tag and the dashboard
    shows a per-machine breakdown. The proxy's `/dashboard` route just redirects here.

`iq` is a launcher that brings up the compose stack and runs `claude` with `ANTHROPIC_BASE_URL`
pointed at the proxy (scoped to that one command — it changes no global config).

## Run / develop

```bash
# Full stack (collector :8088 + proxy :8082) in Docker:
docker compose up -d --build
#   CONCISE=1 is on by default in compose; set CONCISE=0 to disable reply-trimming.

# Standalone collector only (e.g. deploy on a remote host):
cd dashboard && uvicorn collector:app --host 0.0.0.0 --port 8088

# CLI (local; exact counts need ANTHROPIC_API_KEY):
./engines/optimize.py "Hey could you please just clean this up?"

# Drive Claude Code through the proxy:
./iq                                      # or: ANTHROPIC_BASE_URL=http://localhost:8082 claude
```

- Dashboard / stats: `http://3.96.147.26:8088` (auto-refreshes `/api/stats` every 2s).
- Ports and tuning live as module-level globals / env vars at the top of each file — no config
  file. Editing a server file requires a restart (`docker compose restart`, or rebuild since the
  images `COPY` the source).
- Reporting target: `INFERENCEIQ_DASHBOARD` (default `http://3.96.147.26:8088`; `http://dashboard:8088`
  inside compose; `off` disables). Set it to a remote collector's URL to report across machines.

## Architecture notes

- **Two report paths to the same collector.** `optimize.report()` is **stdlib-only** (`urllib`),
  because the CLI and hook run under whatever `python3` the user has (often no `httpx`). The proxy
  uses async `httpx`. Both POST the same `/api/record` shape and both attach `host`/`user`.
- **`est()` lives in `optimize.py`** and is imported by the proxy and the hook (one source of
  truth for the chars/4 estimate). Exact counts come from `optimize.count_tokens()`.
- **Single-process, in-memory dashboard.** `TALLY` resets on restart; no lock, no persistence —
  a prototype, not horizontally scalable.

## Critical safety invariants (the proxy fronts an agentic, tool-using client)

These are load-bearing — the project was **rewritten** away from an earlier caching proxy
(`proxy.py`, since deleted) that broke Claude Code by synthesizing responses, dropping messages,
and mutating arbitrary turns. Do not reintroduce any of that. "Safe for Claude Code" means:

- **Never synthesize or cache responses.** No fake turns; never strip a `tool_use`.
- **Never drop messages.** Dropping a leading message can orphan a `tool_result` from its
  `tool_use` → API 400.
- **Only ever touch the last user turn, and only genuine typed prose.** Skip any turn carrying a
  `tool_result`. This keeps Anthropic's **prompt cache** intact (system / tools / history are
  byte-identical), preserving the ~90% cache discount — mutating the cached prefix would cost far
  more than filler-stripping saves.
- **Mechanical rules are meaning-preserving and guarded** (e.g. won't drop "just" when it means
  "only"). The `CONCISE` directive trims *padding* (preamble/filler/repetition), never substance,
  and savings are **measured** from the real output-token count, not assumed.
- **Counting and the LLM rewrite always hit `api.anthropic.com` directly**, never a local proxy,
  even when `ANTHROPIC_BASE_URL` is set.

## File map

```
engines/                           the shared core (importable modules; CLI lives here too)
  optimize.py                      mechanical core + CLI; est(); host-tagged report() (privacy-gated)
  router.py                        deterministic intent → model routing (Haiku/Sonnet/Opus); no API call
  semcache.py                      3-layer semantic cache (exact + fastembed vector + LLM fallback); non-agentic only
proxy/                             the in-path proxy surface
  intercept.py                     ⚡ Auto proxy (:8082): cache + optimize + CONCISE + model routing; imports ../engines
  Dockerfile                       proxy image (copies engines/ + proxy/intercept.py; PYTHONPATH=/app/engines)
  requirements-proxy.txt           proxy image deps only (fastapi · uvicorn · httpx — no anthropic)
dashboard/                         the standalone monitor surface
  collector.py                     monitor (:8088): per-host, models-used, routing; modern UI
  Dockerfile                       slim collector image (fastapi/uvicorn + tzdata)
  requirements.txt                 fastapi · uvicorn · tzdata
.claude/hooks/optimize_prompt.py   UserPromptSubmit hook (single auto mode; injects context; never blocks)
compose.yml                        intercept service (build: proxy/Dockerfile, context = repo root); reports to remote collector
requirements.txt                   full local/CLI set (fastapi · uvicorn · httpx · anthropic)
iq                                 launcher: compose up + claude via the proxy
demo.sh                            drives sample prompts through the proxy to populate the dashboard
```

Layout: **engines/** (shared core) · **proxy/** (in-path surface) · **dashboard/** (monitor). The
proxy imports the engine modules from `../engines` (a `sys.path` shim locally; `PYTHONPATH=/app/engines`
in the image). The hook resolves `optimize.py` from `engines/` too (`OPTIMIZER_DIR` or auto-detect).

## Model routing & privacy (added)

- **`router.py`** is deterministic (keyword + length, no LLM call). Routing is **on by default**
  (`ROUTE_MODELS=on`; `advise` reports only, `off` disables), and it **never touches agentic
  requests** (tools present or a `tool_result` turn) — Claude Code keeps its requested model.
  Default Sonnet when intent is unclear; never silently downgrade real work to Haiku.
- **Privacy:** reporters send **counts + host only** by default; `IQ_REPORT_TEXT=1` opts into
  sending prompt text. The collector **never stores** before/after regardless. The dashboard
  timezone is non-blocking: `IQ_TZ`/`TZ` if set, else public-IP lookup in a daemon thread.
- **Semantic cache (`semcache.py`)** — this revives the mechanism that got `proxy.py` deleted, so it
  is fenced by hard invariants; do NOT loosen them:
  1. The proxy **bypasses the cache entirely** when `tools` are present or the last user turn is a
     `tool_result` (all real Claude Code traffic) — it is never served a synthesized answer.
  2. Only **pure-text** replies are stored (`stop_reason` end_turn/stop_sequence, no `tool_use`).
  3. Namespaced by **system-prompt hash** → never serve across a different system instruction.
  4. **Fail-open:** if the fastembed model can't load, every lookup is a clean miss (plain proxy).
  On by default (`CACHE_ENABLED=1`); 50MB store, fp16 embeddings, gzip responses, dedup ≥0.97,
  hybrid LRU+freq eviction, pluggable numpy/hnsw/faiss. The model is local ONNX (no PyTorch).
