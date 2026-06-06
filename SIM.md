# SIM.md — InferenceIQ simulation guide

`demo.sh` is a **simulator**: it feeds the dashboard collector the *same* `/api/record` events the
real CLI, hook, and proxy emit — but from N fake developers, with **no API key and no `claude`**.
Use it to populate and demo the dashboard, and to sanity-check the math.

Each named scenario below is just `demo.sh` with different parameters. Per-scenario files live in
[`sims/`](sims/).

## How the simulator works

For each tick (~1s) per developer it POSTs a realistic mix of events to `http://localhost:8088/api/record`:

| Event `kind` | Simulates | Key fields |
|---|---|---|
| `opt` | input filler-strip (hook + CLI) | `saved`, `rules[]`, `host` |
| `rec` | best-practice rewrite (web) | `saved`, `techniques[]`, `tips[]` |
| `out` (normal) | a full reply + **real-usage** fields | `out_tokens`, `in_tokens`, `cache_read`, `cache_creation`, `model` |
| `out` (concise) | the same reply kept short | `out_tokens` (smaller), `concise:true` |
| `route` | intent→model routing decision | `intent`, `to_model`, `applied` |
| `cache` | a semantic-cache hit (no API call) | `layer` (exact/semantic), `similarity`, `model` |
| `cachestat` | the cache store gauge | `entries`, `bytes`, `hit_rate`, `index` |

**Calibration (from morphllm.com/ai-coding-costs):** ~60% reply reduction, ~25–30% cache hit rate,
model mix ≈ 60% Sonnet / 25% Haiku / 15% Opus, ~$300/dev/month baseline, and `out` events carry
realistic `cache_read` (≈85% of a coding prompt is cached) so the dashboard's **real** prompt-cache
savings populate. All clearly labelled as simulated/modeled on the dashboard.

## Parameters (env vars)

| Var | Default | Meaning |
|---|---|---|
| `DEVS` | `5` | number of simulated developers (distinct hosts) |
| `DURATION` | `300` | run length in seconds |
| `TICK` | `1` | seconds between bursts |
| `HIT_RATE` | `30` | target cache hit rate % |
| `DASH` | `http://localhost:8088` | collector URL |

## Run (any scenario)

```bash
# 0. dashboard must be up
docker compose up -d            # or: cd dashboard && uvicorn collector:app --port 8088

# 1. run a scenario (examples)
./demo.sh                       # 5 devs, 5 min
DEVS=15 DURATION=120 ./demo.sh  # 15 devs, 2 min
HIT_RATE=40 ./demo.sh           # heavier cache

# 2. watch it: http://localhost:8088
```

## How it's tested / verified

After (or during) a run, confirm the numbers landed:

```bash
curl -s localhost:8088/api/stats | python3 -c "
import sys,json;d=json.load(sys.stdin);o=d['output']
print('developers   :', len([h for h in d['by_host'] if h['host']!='—']))
print('reply reduce :', o['pct_shorter'], '%')
print('cache hits   :', d['cache'])
print('cache_read   :', o['cache_read_tokens'], 'tokens (real prompt-cache saving source)')
print('model mix    :', [(m['model'],m['replies']) for m in d['by_model']])
print('routes       :', [(r['name'],r['count']) for r in d['top_routes']])
"
```

Expected: `developers` = `DEVS`; `reply reduce` ≈ 60%; cache hits accumulate at ≈`HIT_RATE`%; model
mix trends 60/25/15 over a full run; `cache_read` > 0 (drives the **Prompt-cache saved** KPI).

The **Monthly Cost Savings** tab is *modeled* from the live reply-reduction % and the team-size
selector — it does not depend on `DEVS`. The Overview KPIs (Total saved, Reply reduction,
Prompt-cache saved) are driven by the actual events the simulator posts.

## Scenarios

| File | Scenario | Command |
|---|---|---|
| [sims/01-startup-5.md](sims/01-startup-5.md) | Startup — 5 developers | `DEVS=5 ./demo.sh` |
| [sims/02-growth-15.md](sims/02-growth-15.md) | Growth team — 15 developers | `DEVS=15 ./demo.sh` |
| [sims/03-scaleup-25.md](sims/03-scaleup-25.md) | Scale-up — 25 developers | `DEVS=25 ./demo.sh` |
| [sims/04-cache-heavy.md](sims/04-cache-heavy.md) | Cache-heavy / repetitive workload | `HIT_RATE=45 DEVS=15 ./demo.sh` |

> Want a real (non-simulated) run? Point an **API-key** `claude -p` or curl at the proxy
> (`ANTHROPIC_BASE_URL=http://localhost:8082`) — non-agentic prompts then exercise the real cache,
> routing, and Anthropic `usage` path. See `README.md` → How to run.
