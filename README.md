# InferenceIQ — Prompt Token Optimizer

Make prompts **cheaper to send**, replies **cheaper to receive**, and both **better at getting
good answers** — without changing what they mean. InferenceIQ shortens chatty prompt text,
can trim the model's reply, and tells you how to cut tokens.

It has **one shared core** and a few thin surfaces around it. Three of them *do* something to your
prompts — a **CLI**, a **Claude Code hook**, and an in-path **proxy** — and they all report into
**one standalone dashboard** that you can run locally or on a remote box to watch savings across
**every machine** at once.

```
   reporters (the "doers")                          monitor (view-only)
   ────────────────────────                         ───────────────────
   CLI        optimize.py                 ─┐
   Hook       .claude/hooks/optimize_*.py  ─┼──►  POST /api/record  ──►  dashboard/collector.py
   Proxy      intercept.py  (:8082)        ─┘        (host-tagged)            (:8088)
                                                                          ▲ deploy anywhere;
   every report carries this machine's host  ─────────────────────────────┘ many hosts → one view
```

The whole-input savings are small at *your* end, but the **reply** savings are large at the LLM
API end — output tokens cost ~5× input and usually dominate the bill.

---

## Table of contents
1. [Why this exists](#why-this-exists)
2. [Techniques used](#techniques-used)
3. [The engine](#the-engine)
4. [The surfaces — how you use it](#the-surfaces--how-you-use-it)
5. [Components — what each does and why](#components)
6. [Architecture](#architecture)
7. [How to run](#how-to-run)
8. [Configuration](#configuration)
9. [Design principles & safety](#design-principles--safety)
10. [Benefits, limits, and trade-offs](#benefits-limits-and-trade-offs)
11. [File map](#file-map)

---

## Why this exists

This project began as a token-saving **proxy** that sat in front of `api.anthropic.com` and
applied three tricks: a semantic response cache, filler stripping, and context compression. That
design was **broken for an agentic client like Claude Code**:

- the response cache returned text-only answers, which **stripped tool calls** and broke the agent
  loop;
- context compression **dropped messages**, orphaning `tool_result` from `tool_use` → HTTP 400s;
- filler stripping **silently mutated** prompts anywhere in the conversation.

---

## Techniques used

Every token-saving lever InferenceIQ actually applies, what it saves, and — crucially — **how the
saving is measured**. Where possible it's **real** (from Anthropic's `count_tokens` and the response
`usage` object), not an estimate.

| # | Technique | What it does | Typical saving | Measured | Where | Needs proxy/API key |
|---|---|---|---|---|---|---|
| 1 | **Mechanical input trim** | Deterministic regex strips filler ("please", "just basically"), swaps verbose phrases ("in order to"→"to"), tidies whitespace — meaning-preserving. | small (filler only) | **exact** (`count_tokens`) | `optimize.py` (CLI · hook · proxy) | hook/CLI: no |
| 2 | **CONCISE output control** | Appends a brevity directive to the last user turn → shorter replies. The big lever (output ≈5× input). | 40–60% of the reply | **real** (`usage.output_tokens`) | proxy (`CONCISE`) + hook | output on-wire: **proxy/key**; hook directive: no |
| 3 | **Intent model routing** | Deterministic keyword/length routing to Haiku / Sonnet / Opus (no extra API call). Agentic requests are never routed. | 30–40% | **real** — $ priced from the actual reply's input+output tokens × the price delta between the requested and served model | `router.py` (proxy) | **yes (key)** |
| 4 | **Semantic response cache** | 3-layer: exact hash → fastembed vector (cosine) → LLM fallback. A hit returns the stored answer with **no upstream call**. Non-agentic, text-only only. | avoids whole calls | hit-rate + calls-avoided | `semcache.py` (proxy) | **yes (key)** |
| 5 | **Prompt-cache preservation** | The proxy never mutates the cached prefix (system/tools/history), so Anthropic's prompt cache stays intact (~90% off cached tokens). | ~90% on cached tokens | **real** (`usage.cache_read_input_tokens`) | proxy invariant | **yes (key)** |
| 6 | **Exact token counting** | `count_tokens` (model-specific) — never `tiktoken`. Powers the savings numbers, before vs after. | — (measurement) | n/a | `optimize.py` | key for exact counts |
| 7 | **Host tagging + privacy gating** | Each report carries `host`/`user`; prompt text stays on-box by default (`IQ_REPORT_TEXT=0`). | — (observability) | n/a | all reporters → collector | no |
| 8 | **Advisory tips** | Surfaces *when* RAG, chunking, AST summaries, command distillation, search-before-reading, and prompt-cache ordering would help (can't auto-apply in a transparent proxy). | 60–99% *(if you build them)* | — | hook + roadmap | no |

**Measured with Anthropic's real data** (techniques 1–2, 5–6): exact `count_tokens` and the response
`usage` object (`input_tokens` / `output_tokens` / `cache_read_input_tokens` /
`cache_creation_input_tokens`). The dashboard's *Prompt-cache saved* and *Reply reduction* numbers are
real, not modeled. The **ROI** tab leads with a **live projection** — the real $ saved so far,
extrapolated to a month from the measured run-rate and the actual number of reporting machines
(no team model) — and keeps a separate **hypothetical-team** model (5/10/15+ devs) below, clearly
labelled as modeled. See **[roadmap.md](roadmap.md)** for what's applied vs planned vs
tool-wrapper-only, with Anthropic doc references.

### How the before/after numbers are measured (plain English)

The landing page (`/`) gauges tokens **before vs after** FortiInferenceIQ. Here's exactly what each
side means:

1. **Measured from Anthropic's real usage** — the numbers come from actual traffic and the response
   `usage` object, not synthetic tests or estimates.
2. **Input — filler removed by the optimizer (after = billed input).** Before the request is sent,
   the optimizer strips meaning-preserving filler (padding phrases, redundant whitespace). The
   trimmed version is what reaches the API and gets billed, so *after* = your effective input and
   *before* = after + the filler that was removed.
3. **Output — median reply length, reply-trimming on vs off.** Two modes are compared: trimming
   **ON** (`CONCISE=1`, the model is nudged to be concise) vs **OFF** (`CONCISE=0`, normal-length
   answers). We report the **median** reply length (the typical response, robust to the occasional
   very long answer that would skew a mean).
4. **Applied to the concise replies served.** The output saving is the per-reply median gap
   (off − on) multiplied by the number of concise replies actually served — not a hypothetical.
5. **`./core-engine/calibrate.py` for a same-prompt baseline.** Because normal operation runs
   everything with `CONCISE=1`, there's often no `CONCISE=0` baseline to compare against. This script
   sends the *same* prompts both ways (on and off), straight to the API, so the on-vs-off comparison
   is a true same-prompt measurement instead of an average across different prompts.

---

## The engine

| Engine | File | What it does | Cost | Determinism |
|---|---|---|---|---|
| **Mechanical** | `optimize.py` | Regex rules that drop filler ("please", "just basically"), swap verbose phrases ("in order to"→"to"), collapse whitespace — **meaning-preserving**, conservative. | Free, offline, instant | 100% deterministic |

Mechanical is safe, free, and predictable — it trims obvious filler without ever changing meaning.
It's the shared core: the CLI, the hook, and the proxy all run the same `optimize.py` rules.

---

## The surfaces — how you use it

Three surfaces *act* on prompts; the dashboard only *watches*.

| # | Surface | Kind | Who it's for | What happens | Where |
|---|---|---|---|---|---|
| **1** | **CLI** (`optimize.py`) | 🙋 you run it | Terminal / scripting | Shorten from the shell; prints before/after + savings; reports to the dashboard | `./core-engine/optimize.py "…"` |
| **2** | **Claude Code hook** | ⚡ automatic | Inside Claude Code | On submit, injects a tighter equivalent phrasing **and** a brevity directive as context (no confirmation, never blocks) | every CC session |
| **3** | **Intercept proxy** | ⚡ automatic | Terminal `claude` / any API client | Rewrites the **last user turn** on the wire, and (default `CONCISE=1`) trims the reply | `ANTHROPIC_BASE_URL=http://localhost:8082` |
| **4** | **Dashboard** | 👁 view-only | Anyone watching cost | A live page of savings across all sources and machines. View-only — **no prompt input**; the only controls are Settings (refresh/theme/timezone + a **Reset counters** button), a model-price selector, and the team-ROI view | `http://3.96.147.26:8088` |

**How to choose:** scripting / one-off cleanup → **1** · hands-off inside Claude Code → **2** ·
fully automatic on the wire (and reply-trimming) → **3** · just watching the numbers → **4**.

---

## Components

### `optimize.py` — the mechanical core + CLI
- **What:** a conservative, meaning-preserving text compressor. Rules live in a plain `RULES` list
  at the top (verbose→concise swaps, filler removal, whitespace cleanup, sentence
  re-capitalization). Also: exact token counting via Anthropic `count_tokens` (falls back to a
  labelled estimate without a key), the shared `est()` chars/4 estimate, and `report()` — a
  **stdlib-only** (`urllib`) POST of each run to the dashboard, **tagged with this machine's
  `host`/`user`**.
- **Why:** the safe baseline. No network to transform, no cost, no surprises — every change is
  printed so you can see exactly what it did and why. (It never routes token counting through a
  proxy, even if `ANTHROPIC_BASE_URL` is set.)
- **Limits:** small savings on already-tight prompts; can't restructure or reason about a prompt.
- **CLI:**
  ```bash
  ./core-engine/optimize.py "text"                                  # single prompt
  ./core-engine/optimize.py --copy "text"                           # also copy result to clipboard (macOS)
  ./core-engine/optimize.py --batch prompts.txt --out optimized.txt # many prompts → totals + file
  #   batch file: prompts separated by a line of ---, or one per line
  ```

### `intercept.py` — the optimizing proxy (the ⚡ Auto surface, :8082)
- **What:** a reverse proxy. On every `POST /v1/messages` it optimizes **only the last user turn**
  (and skips it entirely if that turn is a `tool_result`), then forwards everything else
  **byte-for-byte** to Anthropic. Streaming passes straight through. It reports each optimized turn
  — and the **real output-token count** of each reply — to the dashboard (host-tagged), and its
  `/dashboard` route **redirects** to the collector.
- **Output-side savings (`CONCISE`, on by default in compose):** appends a short brevity directive
  — *"Be brief. Lead with the direct answer in a few short sentences. Omit preamble, background,
  caveats, and closing summaries unless explicitly asked."* — to the **last user turn only**. This
  is the big lever: it shortens the **reply**, where tokens are most expensive. The dashboard
  measures actual output tokens and shows concise-vs-normal **% shorter**.
- **Why it's safe:** never breaks the agent loop; **protects the prompt cache** (the cached prefix
  — system, tools, history — is never altered, so the ~90% discount survives); the `CONCISE` nudge
  is also cache-safe (last turn only) and skips `tool_result` turns.
- **Limits:** input savings are small by design (only your new prose, only filler). `CONCISE` is
  **behavioral, not guaranteed** — a complex question can still produce a long answer, which is why
  the dashboard measures *real* output tokens instead of assuming a fixed saving.

### `.claude/hooks/optimize_prompt.py` — the Claude Code hook (⚡ Auto)
- **What:** a `UserPromptSubmit` hook with a **single auto mode**. On every prompt it (1) optimizes
  the text mechanically, (2) injects the **tighter equivalent phrasing** plus an **output-control
  brevity directive** as authoritative `additionalContext`, and (3) reports the saving to the
  dashboard. It **never blocks** — any failure passes the prompt through untouched.
- **Honest limit:** a Claude Code hook **cannot replace your typed text** (only block or add
  context). So the *input* saving here is advisory/measured; the **output** control is real (Claude
  follows the brevity directive). For on-the-wire input cuts too, route through the proxy (`./iq`).
- **Config:** `OPTIMIZER_DIR` (where `optimize.py` is; auto-detected), `CONCISE_NOTE` (override the
  directive), `INFERENCEIQ_DASHBOARD` (where to report; `off` disables).

### `dashboard/collector.py` — the standalone dashboard (👁 view-only, :8088)
- **What:** a self-contained FastAPI monitor. It **imports nothing from the rest of the repo** and
  calls no API — every other surface POSTs to **`/api/record`**, and the page renders the aggregate
  from **`/api/stats`**. Modern dark UI, auto-refresh (configurable in Settings, default **5s**).
  It takes **no prompt input** — the only interactive controls are Settings (refresh / theme /
  timezone + a **↺ Reset counters** button), a model-price selector, and the team-ROI view.
- **Shows:** total **$ saved** across four real levers — shorter prompts, shorter replies
  (`CONCISE`), **model-routing savings** (priced from real token counts against the cheaper model
  actually served), and Anthropic prompt-cache reads — plus **prompts handled**, **avg reply
  reduction %**, a **per-machine breakdown** (host tagging), **by source** (cli / hook / proxy /
  web), **models used** + **routing decisions**, **top mechanical rules** / **best-practice tips**,
  and a **live activity feed**.
- **Live vs Demo indicator:** the header pill reads **live** for real traffic and flips to **Demo**
  (amber) while `demo.sh` sends heartbeats, reverting to **live** ~12s after a demo ends.
- **Why standalone:** because it depends on nothing else, you can deploy just `dashboard/` on a
  remote box and have CLIs/hooks/proxies on many machines report to it — see below.
- **Endpoints:** `POST /api/record` (ingest — `kind` ∈ opt · rec · out · route · cache · cachestat
  · mode), `GET /api/stats`, `POST /api/reset` (zero all counters), `POST /api/tz` (set feed
  timezone), `GET /`.
- **Reset the counters from the CLI** (same as the Settings → ↺ Reset button) — zeroes every
  counter, chart, and feed across all sources/machines, and clears the persisted store so the
  reset survives a restart:
  ```bash
  curl -XPOST http://3.96.147.26:8088/api/reset
  #   token-protected collector: add  -H "X-IQ-Token: <IQ_TOKEN>"
  ```

### Container files
- **`proxy/Dockerfile`** — the **proxy** image: installs `proxy/requirements-proxy.txt`
  (`fastapi`/`uvicorn`/`httpx` + `fastembed`/`numpy`, no Anthropic SDK), copies `core-engine/` +
  `proxy/intercept.py` (sets `PYTHONPATH=/app/core-engine`), runs `uvicorn intercept:app` on :8082.
  Built from the **repo root** context so it can pull in `core-engine/`.
- **`dashboard/Dockerfile`** — the **collector** image: slim, installs only `fastapi`/`uvicorn`/
  `tzdata`, copies `collector.py`, runs on :8088. No Anthropic SDK, no repo code.
- **`compose.yml`** — the `intercept` service (built from the repo root via `proxy/Dockerfile`,
  :8082; honours `COUNT_MODE`/`CONCISE`/`ROUTE_MODELS`/`CACHE_*`, reports to the remote collector via
  `INFERENCEIQ_DASHBOARD`); named volumes persist the embedding model + cache store.

---

## Architecture

### System topology

```
   ┌──────────────────────── CLIENTS ────────────────────────┐
   │                                                          │
   │  Claude Code (hook)      CLI                API client   │
   │  optimize_prompt.py      optimize.py       (curl/SDK)    │
   │        │                    │                   │        │
   │   adds context         prints + reports    ANTHROPIC_BASE_URL
   │   (no proxy)            (no proxy)              │        │
   └────────┼────────────────────┼──────────────────┼────────┘
            │                    │                   ▼
            │                    │      ┌────────────────────────┐
            │                    │      │  intercept.py  :8082    │   ⚠️ needs API key
            │                    │      │  (the ⚡ Auto proxy)     │   (OAuth bypasses it)
            │                    │      │  cache→opt→concise→route│
            │                    │      └───────────┬────────────┘
            │                    │                  │ forward (cache miss)
            │                    │                  ▼
            │                    │        https://api.anthropic.com
            │                    │
            └──────────┬─────────┴──────────────────┐  POST /api/record
                       ▼                             ▼  (host-tagged, privacy-gated)
              ┌──────────────────────────────────────────────┐
              │   dashboard/collector.py   :8088              │   standalone, no repo deps,
              │   /api/record · /api/stats · tabbed UI        │   deployable remotely, multi-host
              └──────────────────────────────────────────────┘
```

Three **reporters** (hook, CLI, proxy) all feed **one collector**. Only the **proxy** sits in the
request path; the hook and CLI act beside it. The collector imports nothing from the repo, so it
runs anywhere and aggregates many machines (each event carries a `host`).

### Proxy request pipeline (`POST /v1/messages`)

The heart of the system. Steps run **in order**; the **safety gate** decides eligibility up front,
and agentic traffic (tools / `tool_result`) skips the lossy stages entirely.

```
request body
   │
   ├─▶ optimize last user turn ............... strip filler from the newest prose (cache-safe)
   │
   ├─▶ ┌ eligible? = no `tools` AND last turn isn't a `tool_result`  ┐
   │   │  SEMANTIC CACHE LOOKUP (exact hash → vector cosine)         │
   │   │     └ HIT → synthesize JSON / replay text SSE → RETURN ─────┼──▶ (no upstream call)
   │   └────────────────────────────────────────────────────────────┘
   │
   ├─▶ add CONCISE directive ................. brevity nudge on the last user turn (opt-in)
   │
   ├─▶ model routing ......................... intent→Haiku/Sonnet/Opus; agentic never routed
   │
   ├─▶ forward to api.anthropic.com .......... streaming (raw SSE re-emit) or non-streaming
   │
   └─▶ on response:
         • report real output tokens + model used  → dashboard
         • STORE in cache  ⟵ only if pure text (no `tool_use`, stop_reason end_turn) and eligible
```

All **other endpoints** (`/v1/messages/count_tokens`, `/v1/models`, …) are proxied **verbatim**
through a catch-all passthrough — no mutation.

### Safety gates (encoded in code, see Design principles)

```
   tools present?  ──yes──▶  cache BYPASS · routing BYPASS · forward unchanged (only filler+CONCISE)
   tool_result turn? ─yes─▶  same — never serve or store synthesized output
   response has tool_use? ─yes─▶ NEVER store in cache
   different system prompt? ───▶ different cache namespace (hash(system)) — never cross-serve
   embed model not loaded? ────▶ cache = clean miss (plain pass-through)
```

### Data & reporting flow

```
 optimize.report()  (CLI/hook, stdlib urllib)  ─┐
 intercept _post_record() (proxy, async httpx) ─┼─▶  POST /api/record  ─▶  collector TALLY
 demo.sh   (heartbeat → Demo indicator)        ─┘     {kind, source, host, ...}      │
   kinds: opt · rec · out · route · cache · cachestat · mode                          ▼
   privacy: counts + host only by default (IQ_REPORT_TEXT=1 to include prompt text)   /api/stats
                                                                                       │
                                              tabbed UI polls (default 5s, Settings) ◀─┘
   Tabs: Overview · Models & Routing · Activity   (+ ROI view and Settings beside them)
```

### Deployment units

| Unit | Image / runtime | Contains | Needs |
|---|---|---|---|
| **Proxy** | `proxy/Dockerfile` (:8082) | `proxy/intercept` + `core-engine/{optimize,router,semcache}` + fastembed/numpy | API key (for real traffic); volumes for model + cache store |
| **Dashboard** | `dashboard/Dockerfile` (:8088) | `collector` only (fastapi/uvicorn/tzdata) | nothing — deploy anywhere, collect from many hosts |
| **Hook** | pure-stdlib script | `optimize_prompt.py` → imports `optimize` | any `python3`; no key, works on OAuth |
| **CLI** | local venv | `optimize.py` | key only for exact counts |

> Which features need the proxy (and therefore an **API key**, since Pro/Max OAuth bypasses it) is
> documented in **[roadmap.md](roadmap.md)**.

---

## How to run

Four ways — pick what you need. **A** is the quickest; **C** is for a central/remote dashboard;
**D** is the only one that works inside a Claude Code session.

> All paths below assume you're in the repo:
> ```bash
> cd /Users/svuillaume/caching_project/inferenceiq
> ```

### A) Full stack in Docker — dashboard + proxy *(recommended start)*

**Plain English:** spins up the dashboard and the auto-optimizing proxy together; you watch
savings in your browser while a terminal `claude` runs through the proxy.

```bash
docker compose up -d --build     # builds + starts: dashboard on :8088, proxy on :8082
docker compose ps                # confirm BOTH 'dashboard' and 'intercept' show "running"
```
Then:
- **Open the dashboard:** http://3.96.147.26:8088
- **Send traffic through the proxy:** `./iq`  *(equivalent to `ANTHROPIC_BASE_URL=http://localhost:8082 claude`)*
- **See the numbers move without real usage:** `./demo.sh`  *(replays sample prompts)*
- **Turn off reply-trimming:** `CONCISE=0 docker compose up -d` *(it's ON by default)*
- **Stop everything:** `docker compose down`

> ⚠️ **OAuth (Pro/Max) caveat.** If your `claude` is signed in with a Claude Pro/Max
> subscription, it ignores `ANTHROPIC_BASE_URL`, so its traffic never reaches the proxy — the
> proxy only sees **API-key** clients. On a subscription, use the hook (**D**) instead.

### B) CLI only — no Docker

**Plain English:** shorten a single prompt from the terminal. Free and offline for `optimize`
(a key only buys *exact* token counts instead of estimates).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only

./core-engine/optimize.py "Hey could you please just clean this up?"             # mechanical, free, offline
```
*(If a dashboard from **A** or **C** is running, these auto-report to it — no extra flags.)*

### C) Dashboard only — standalone, local or on a remote box

**Plain English:** run just the monitor. It depends on nothing else (no API key, no repo code), so
you can host it on one machine — e.g. an AWS box — and have many machines report into it.

**Easiest — the installer script.** Copy just the `dashboard/` folder to the host and run
[`dashboard/install.sh`](dashboard/install.sh). It uses Docker if present, else falls back to a
Python venv + uvicorn, then health-checks the result:

```bash
cd dashboard
./install.sh                                   # local/dev — port 8088, no auth
IQ_TOKEN=$(openssl rand -hex 24) ./install.sh  # public box — require a token on writes (recommended)
#   knobs:  PORT=9000  IQ_TZ=America/Toronto  ./install.sh   ·   ./install.sh --no-docker
```

**No Docker?** Use [`dashboard/run.sh`](dashboard/run.sh) — it makes a local venv (once), installs
the deps, and serves in the foreground (Linux & macOS):

```bash
cd dashboard
./run.sh                                       # http://3.96.147.26:8088 (Ctrl-C to stop)
IQ_TOKEN=$(openssl rand -hex 24) ./run.sh      # with write auth
nohup env IQ_TOKEN=secret ./run.sh > dashboard.log 2>&1 &   # background
```

**Fully manual**, if you'd rather not use either script:

```bash
cd dashboard
docker build -t iq-dashboard . && docker run -d -p 8088:8088 -e IQ_TOKEN=secret --restart unless-stopped iq-dashboard
#   or no Docker:  pip install -r requirements.txt && IQ_TOKEN=secret uvicorn collector:app --host 0.0.0.0 --port 8088
```

**Then point each reporter at that host** (see *Point the hook at a remote dashboard* above for the
slash-command / CLI / config-file forms):

```bash
export INFERENCEIQ_DASHBOARD=http://<collector-host>:8088
export IQ_TOKEN=secret            # only if you started the collector with IQ_TOKEN — must match
#   set INFERENCEIQ_DASHBOARD=off to stop reporting entirely
```

Every report is tagged with the sender's `host` (and `user`), so the dashboard's **By machine**
panel breaks savings down per box.

> **Exposing it publicly (AWS):** set `IQ_TOKEN` (gates the write endpoints), put **HTTPS** in front
> (ALB / CloudFront / Caddy → the collector's port — it speaks plain HTTP), open only 443 in the
> security group, and keep `IQ_REPORT_TEXT=0`. Note the store is **in-memory** (resets on restart).

### D) Claude Code hook — automatic, inside a session

**Plain English:** while you use Claude Code, each prompt is auto-shortened and the reply is
nudged shorter, with no proxy and no confirmation. This is the path that works on a Pro/Max
subscription.

Already enabled in this repo via `.claude/settings.json`. To enable it **globally**, add to
`~/.claude/settings.json`:
```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "/Users/svuillaume/caching_project/inferenceiq/.claude/hooks/optimize_prompt.py",
        "timeout": 15 } ] }
    ]
  }
}
```
Takes effect on the **next** Claude Code session (you may be asked to approve the new hook). The
hook is pure-stdlib, so it runs under any `python3`.

### E) Claude Code plugin — one-command install via `/plugin` *(recommended for the hook)*

**Plain English:** instead of hand-editing `settings.json` (option D), install the hook as a
Claude Code **plugin**. This repo *is* the plugin (manifest at `.claude-plugin/`), and it also
serves as its own one-plugin marketplace — so a teammate installs it in two slash-commands, no
file paths to wire up.

**Prerequisites**
- Claude Code with plugin support, and `python3` on `PATH` (the hook is pure-stdlib).
- The plugin files must be present on the GitHub repo (`.claude-plugin/`, `hooks/hooks.json`,
  `optimize.py`, `.claude/hooks/optimize_prompt.py`) — see *Publishing* below.

**1 — Add the marketplace** (one-time, points at this repo):
```
/plugin marketplace add svuillaume/InferenceIQ
```

**2 — Install the plugin:**
```
/plugin install inferenceiq@inferenceiq
```
*(`inferenceiq@inferenceiq` = `plugin-name@marketplace-name`; both are named `inferenceiq`.)*

CLI equivalents, if you prefer the shell:
```bash
claude plugin marketplace add svuillaume/InferenceIQ
claude plugin install inferenceiq@inferenceiq
```

**3 — Verify:** open a new prompt and confirm Claude received the injected directive — run
`/plugin` and check **inferenceiq** is enabled, or just watch replies get terser. (The plugin's
`hooks/hooks.json` invokes `.claude/hooks/optimize_prompt.py` with
`OPTIMIZER_DIR="${CLAUDE_PLUGIN_ROOT}"`, so `optimize.py` resolves from inside the installed
plugin — no machine-specific paths.)

**4 — Configure (optional):** the same env vars as the manual hook apply — `CONCISE_NOTE`
(override the brevity directive) and `INFERENCEIQ_DASHBOARD` (`off` to stop reporting).

> **Publishing.** `/plugin marketplace add` clones the GitHub repo, so the plugin files must be
> committed and pushed. Right now only `README.md` is on `origin/main` — push at least
> `.claude-plugin/`, `hooks/hooks.json`, `optimize.py`, and `.claude/hooks/optimize_prompt.py`
> (a `.gitignore` for `.env`/`.venv/`/`__pycache__/` is recommended first).

**TL;DR:** run **A**, open http://3.96.147.26:8088, then `./demo.sh`. For just the hook,
use **E**: `/plugin marketplace add svuillaume/InferenceIQ` → `/plugin install inferenceiq@inferenceiq`.

### Point the hook at a remote dashboard (e.g. `foo.com`)

By default the hook reports to `http://3.96.147.26:8088`. To send metrics to a central collector
running elsewhere — say `https://foo.com:8088` (a box you stood up with
[`dashboard/install.sh`](#c-dashboard-only--standalone-local-or-on-a-remote-box)) — point the
hook at it in **any one** of these ways (they resolve in this order: env var → config file →
default):

**1 — Slash command** (after installing the plugin, **E**) — the simplest for plugin users:
```
/inferenceiq:dashboard https://foo.com:8088
/inferenceiq:dashboard https://foo.com:8088 <token>     # if the collector requires IQ_TOKEN
```

**2 — CLI** (writes the same config file from the shell):
```bash
./core-engine/optimize.py --set-dashboard https://foo.com:8088          # add --set-token <secret> if protected
./core-engine/optimize.py --show-config                                  # check what's set
```

**3 — Config file** — both of the above just write `~/.inferenceiq.json` (override the path with
`$IQ_CONFIG`):
```json
{ "dashboard": "https://foo.com:8088", "token": "optional-shared-secret" }
```

**4 — Environment variable** (wins over the file; good for the proxy/compose or CI):
```bash
export INFERENCEIQ_DASHBOARD=https://foo.com:8088
export IQ_TOKEN=optional-shared-secret                       # only if the collector sets IQ_TOKEN
```

The hook then POSTs each run to **`https://foo.com:8088/api/record`** (host-tagged, counts-only by
default). It reads the config **per prompt**, so changes take effect on your next message — no
restart. Set `INFERENCEIQ_DASHBOARD=off` to stop reporting. If the collector was started with
`IQ_TOKEN`, every reporter must send the **same** token (option 1/2's token arg, or `IQ_TOKEN`).

> **Verify it's landing:** `curl https://foo.com:8088/api/stats` should return JSON, and after a
> prompt the **By machine** panel should show your host. The endpoint is `/api/record` (POST) —
> nothing else.

### Point the *proxy* at the same dashboard

The hook/CLI use `~/.inferenceiq.json` (above). The **proxy** is configured separately, by an env
var on the `intercept` service — by default compose points it at the **local** dashboard
(`http://dashboard:8088`). So out of the box: hook → wherever your config says, proxy → local. To
send the proxy's **output-token** savings to the same remote host, set its target and restart:

```bash
INFERENCEIQ_DASHBOARD=http://foo.com:8088 IQ_TOKEN=secret docker compose up -d intercept
#   (or edit the INFERENCEIQ_DASHBOARD line in compose.yml)
```

Now both the hook (input savings) and the proxy (real output savings) land on one dashboard.

### Driving Claude through the proxy — `./iq`

`./iq` brings up the stack and launches Claude Code with `ANTHROPIC_BASE_URL` pointed at the proxy
(scoped to that command — it changes no global config). This is the **only** path that measures the
big lever (shorter replies, from each response's real token usage):

```bash
./iq             # interactive Claude Code, optimized through the proxy
./iq -p "..."    # one-shot
```

### Do I need an API key?

The proxy forwards to `api.anthropic.com`, so it (and exact token counts) needs an
**API-key** login. **A Pro/Max subscription (OAuth) ignores `ANTHROPIC_BASE_URL`** — its traffic
never reaches the proxy. So on a subscription, or with no key at all, use the **hook** (it's
pure-stdlib, needs no key, runs on OAuth):

| Surface | Needs an API key? | What it measures |
|---|---|---|
| **Hook / plugin** (`UserPromptSubmit`) | **No** — works on OAuth/subscription | input trim per prompt + injects the brevity directive (output shrinks but is *not counted* here) |
| **CLI `optimize.py`** | No (key only for *exact* counts; else estimates) | input trim |
| **Proxy `intercept.py` / `./iq`** | **Yes** (and OAuth bypasses it) | real output-token savings, routing, cache |

**Bottom line without a key:** the **plugin/hook is your path** — it reports input savings to your
dashboard and really does shorten replies (you just can't measure the output tokens until you run
through the proxy with an API key).

---

## Configuration

| Where | Variable | Effect |
|---|---|---|
| CLI | `ANTHROPIC_API_KEY` | enables exact token counts |
| hook | `CONCISE_NOTE` | override the injected brevity directive |
| hook | `OPTIMIZER_DIR` | where `optimize.py` lives (default: this repo, auto-detected) |
| proxy | `OPTIMIZE_ENABLED` | `0` to make the proxy a pure passthrough |
| proxy | `ROUTE_MODELS` | intent-based model routing: `on` (**default** — override the model) · `advise` (report the pick, don't change the request) · `off`. **Agentic requests — tools present or a `tool_result` turn — are never routed**, so Claude Code is unaffected |
| proxy | `CACHE_ENABLED` | semantic response cache: `1` (**default**) · `0` to disable. Only ever serves/stores **non-agentic, pure-text** traffic |
| proxy | `CACHE_INDEX` | vector backend: `numpy` (default, brute force) · `hnsw` · `faiss` (falls back to numpy if the lib isn't installed) |
| proxy | `CACHE_HIT` / `CACHE_DEDUP` | cosine thresholds: serve a hit at ≥ `CACHE_HIT` (0.92); merge near-duplicates at ≥ `CACHE_DEDUP` (0.97) |
| proxy | `CACHE_MAX_MB` | store budget in MB (default `50`); hybrid LRU+frequency eviction keeps it under |
| proxy | `CACHE_PER_MODEL` | `1` namespaces the cache per requested model; `0` (default) shares across models |
| proxy | `CACHE_PERSIST_PATH` | file to persist the store across restarts (empty = in-memory only) |
| proxy | `ANTHROPIC_UPSTREAM` | upstream base URL (default `https://api.anthropic.com`; override for tests/self-host) |
| all reporters | `IQ_REPORT_TEXT` | `0` (default) reports **counts + host only**; `1` also sends prompt text (before/after) to the dashboard. Keep `0` for a remote/shared collector |
| dashboard | `IQ_TZ` / `TZ` | pin the feed timezone (e.g. `America/Toronto`); empty = auto-detect from the host's public IP (non-blocking, background) |
| proxy | `COUNT_MODE` | dashboard savings counter: `estimate` (instant, chars/4) · `exact` (background `count_tokens`, no added latency, uses the caller's key) |
| proxy | `CONCISE` | `1` (compose default) appends a brevity nudge to the last user turn → shorter replies; `0` to disable |
| proxy | `CONCISE_NOTE` | override the brevity directive text |
| proxy | `DASHBOARD_PUBLIC_URL` | where `/dashboard` redirects a browser (default `http://3.96.147.26:8088`) |
| all reporters | `INFERENCEIQ_DASHBOARD` | where to report runs (default `http://3.96.147.26:8088`; `http://dashboard:8088` in compose; a remote URL for central collection; `off` disables) |
| dashboard + reporters | `IQ_TOKEN` | shared secret for the **write** endpoints (`/api/record`, `/api/reset`, `/api/tz`). Empty (default) = open. Set it on the collector **and** on every reporter (same value) before exposing the dashboard publicly. Reads (`/api/stats`, `/`) stay open |
| hook / CLI (plugin installs) | `~/.inferenceiq.json` (or `$IQ_CONFIG`) | JSON config read by `optimize.report()` when env vars aren't available (e.g. a `/plugin` hook): `{"dashboard": "https://dash.yourco.com", "token": "…"}`. Env vars win over the file |
| optimizer rules | top of `optimize.py` | edit the `RULES` list to tune mechanical behavior |

> **Central collector on a public cloud (e.g. AWS).** The dashboard is standalone and host-tags
> every event, so one instance can aggregate many machines. Before exposing it: (1) set `IQ_TOKEN`
> on the collector and on each reporter; (2) terminate **HTTPS** in front (ALB / CloudFront /
> nginx) — the collector speaks plain HTTP; (3) keep `IQ_REPORT_TEXT=0` so only counts + host
> leave each machine; (4) note the store is **in-memory** (resets on restart, single process) — add
> persistence/a single instance for durability. For `/plugin` installs that can't set env vars,
> ship a `~/.inferenceiq.json` with the `dashboard` URL and `token`.

---

## Design principles & safety

1. **Never break the agent loop.** No synthesized responses, no stripped tool calls, no dropped
   messages.
2. **Protect the prompt cache.** The proxy only rewrites the *last user turn*; the cached prefix
   (system, tools, history, tool results) is byte-identical, so the ~90% cache discount survives.
3. **Meaning first.** Mechanical rules are conservative and guarded; the LLM rewriter is told never
   to invent content or over-compress into cryptic "SMS-style" text; the reply-trimming nudge cuts
   *padding* (preamble, filler, repetition), never *substance*. And **savings are measured, not
   assumed** — reply savings come from the **real** output-token count of each response.
4. **Transparency.** Every surface shows what changed and why (fired rules, diff, plain-English
   techniques/tips); the dashboard shows it all per source and per machine.
5. **Direct to Anthropic.** Token counting and the rewrite always hit `api.anthropic.com` directly
   — never routed through any local proxy, even if `ANTHROPIC_BASE_URL` is set.

---

## Benefits, limits, and trade-offs

| | Mechanical (`optimize`) |
|---|---|
| Cost | free / offline |
| Saves tokens on the input | yes (filler only) |
| Deterministic | yes |
| Needs a key | no (estimates) |

**Input vs output — where the money is.** Shortening *your* prompt is a small win (your new prose
is a tiny slice of the request, and the cached prefix dominates Claude Code's input cost, already
~90% discounted). Shortening the *reply* is the big win: output tokens are far more expensive per
token and usually larger than your prompt, so a 40–60% shorter answer moves the bill much more than
any input trim. That's why the **`CONCISE`** lever (proxy) matters most — and why it's measured.

| Lever | Surface | Typical effect | Risk |
|---|---|---|---|
| Shorten the prompt | all | small (filler only) | none — meaning-preserving |
| **Shorten the reply** | proxy `CONCISE=1` | **large** (cheaper output tokens) | behavioral, measured on the dashboard |
| **Route to a cheaper model** | proxy `ROUTE_MODELS=on` | **large** (same tokens, lower price/token) | never routes agentic traffic; priced from real tokens on the dashboard |

The advisory token tips (RAG, chunking, summarization, tool-use) tell you *when* a strategy would
help; building it for real needs your actual data/app.

---

## File map

```
core-engine/                           the shared core (importable modules; CLI lives here)
  optimize.py                      mechanical core + CLI; est(); host-tagged, privacy-gated report()
  router.py                        deterministic intent → model routing (Haiku/Sonnet/Opus), no API call
  semcache.py                      3-layer semantic cache (exact + fastembed vector + LLM fallback); non-agentic only
proxy/                             the in-path proxy surface
  intercept.py                     ⚡ Auto proxy (:8082): cache + optimize + CONCISE + routing; imports ../core-engine; /dashboard → :8088
  Dockerfile                       proxy image (copies core-engine/ + proxy/intercept.py; PYTHONPATH=/app/core-engine)
  requirements-proxy.txt           proxy image deps only: fastapi · uvicorn · httpx (no anthropic)
dashboard/                         the standalone monitor surface
  collector.py                     monitor (:8088): per-host, models-used, routing, modern UI
  Dockerfile                       slim collector image (fastapi/uvicorn + tzdata)
  requirements.txt                 fastapi · uvicorn · tzdata
.claude/hooks/optimize_prompt.py   UserPromptSubmit hook (single auto mode; injects context; never blocks)
compose.yml                        intercept service (build: proxy/Dockerfile, context = repo root); reports to remote collector
requirements.txt                   full local/CLI set: fastapi · uvicorn · httpx · anthropic
iq                                 launcher: compose up + claude via the proxy
demo.sh                            drives sample prompts through the proxy to populate the dashboard
```

### Intent-based model routing (Haiku / Sonnet / Opus)

`router.py` maps each request to the smallest capable model with fast, **deterministic** keyword
+ length heuristics — no extra API call on the hot path:

- **Haiku** — simple/repetitive: classify, summarize, translate, define, look up.
- **Sonnet** — the default workhorse: coding, analysis, writing, general tasks (also the fallback
  when intent is unclear — it never silently downgrades real work to Haiku).
- **Opus** — complex reasoning: debugging, architecture, refactors, deep multi-step work.

Routing is **on by default** (`ROUTE_MODELS=on` — the proxy overrides the request's model). Use
`ROUTE_MODELS=advise` to only show the pick on the dashboard without changing the request, or
`off` to disable. **Safety:** any request carrying tools or a `tool_result` is left on its
requested model, so Claude Code's agent loop is never re-routed — routing mainly affects plain
single-turn API clients.

### Semantic cache (3-layer)

`semcache.py` adds a response cache in front of the model — **exact** (hash lookup, instant) →
**semantic** (local `fastembed` ONNX embeddings + cosine search) → **LLM fallback** (call the model,
store the answer). On a hit the proxy returns the stored answer (synthesizing a valid response, or a
text SSE for streaming requests) — **no API call**. Best-practice features: prompt normalization,
fp16-quantized embeddings, gzip-compressed responses, ≥0.97 dedup-on-store, hybrid LRU+frequency
eviction to a `CACHE_MAX_MB` (50MB ≈ 10–20k Q&A pairs), and pluggable `numpy`/`hnswlib`/`faiss`
indexes. On by default; tune with the `CACHE_*` env above.

> **Safety (this is why the original `proxy.py` was deleted).** The cache is **only ever consulted
> or populated for non-agentic, text-only traffic** — any request carrying `tools` or a
> `tool_result` turn bypasses it entirely, and only pure-text replies (no `tool_use`) are stored. So
> Claude Code's tool loop is never served a synthesized answer. It's namespaced by system-prompt
> hash (never serve across a different system instruction), and fail-open (if the embed model can't
> load, every lookup is a clean miss). The 50MB budget is the **store**; the embedding model is a
> separate ~90MB on-disk cost (ONNX, no PyTorch). The dashboard shows hit-rate, calls avoided,
> exact/semantic split, and store size. The dashboard shows both **routing
decisions** and a **Models used** breakdown (replies + output tokens per model).

> Run `docker compose ps` to see both services; the dashboard is the front door at
> **http://3.96.147.26:8088**.
# InferenceIQ
