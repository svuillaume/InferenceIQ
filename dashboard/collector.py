"""
InferenceIQ — dashboard collector (standalone)

A pure monitoring service. It does NOT optimize anything itself and imports nothing
from the rest of the repo — every other surface (CLI optimize/recommend, the Claude Code
hook, the intercept proxy) POSTs its results to `/api/record`, and this page shows the
aggregate. Self-contained → deployable on a remote box, collecting from many machines.

Run:
  uvicorn collector:app --host 0.0.0.0 --port 8088
Point reporters at it:
  INFERENCEIQ_DASHBOARD=http://<this-host>:8088
"""

import os, time, hmac, threading, json as _json, urllib.request
from datetime import datetime
from collections import Counter
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# Shared-token auth for WRITE endpoints (/api/record, /api/reset, /api/tz). Empty = open
# (default, backward-compatible: local dev unaffected). Set IQ_TOKEN on a public/cloud
# collector and configure the same token on every reporter (env IQ_TOKEN or ~/.inferenceiq.json).
# Reads (/api/stats, /) stay open so the browser UI needs no token.
IQ_TOKEN = os.getenv("IQ_TOKEN", "")


def _authed(request: Request) -> bool:
    """True if auth is disabled (no IQ_TOKEN) or the request carries the right token
    (header `X-IQ-Token: <t>` or `Authorization: Bearer <t>`). Constant-time compare."""
    if not IQ_TOKEN:
        return True
    tok = request.headers.get("x-iq-token", "")
    if not tok:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    return hmac.compare_digest(tok, IQ_TOKEN)

try:
    from zoneinfo import ZoneInfo          # stdlib 3.9+
except Exception:                          # pragma: no cover
    ZoneInfo = None

app = FastAPI()


# ── Timezone from the dashboard host's PUBLIC IP (non-blocking) ──────────────────
def _detect_tz():
    for url in ("https://ipapi.co/json/", "http://ip-api.com/json/"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "inferenceiq-collector"})
            with urllib.request.urlopen(req, timeout=3) as r:
                d = _json.loads(r.read().decode("utf-8", "ignore"))
            tz = d.get("timezone")
            ip = d.get("ip") or d.get("query")
            if tz:
                return tz, ip
        except Exception:
            continue
    return None, None


_ENV_TZ = os.getenv("IQ_TZ") or os.getenv("TZ")
_tz = {"name": None, "ip": None, "obj": None}


def _resolve_tz():
    name, ip = (_ENV_TZ, None) if _ENV_TZ else _detect_tz()
    if name and ZoneInfo:
        try:
            _tz["obj"] = ZoneInfo(name); _tz["name"] = name; _tz["ip"] = ip
        except Exception:
            pass


threading.Thread(target=_resolve_tz, daemon=True).start()


def _now() -> str:
    obj = _tz["obj"]
    return datetime.now(obj).strftime("%H:%M:%S") if obj else time.strftime("%H:%M:%S")


# ── ONE in-memory store, fed by every source on every machine ────────────────────
def _fresh_tally():
    """A pristine counter store. Used at startup and by the dashboard's Reset button."""
    return {
        "opt_runs": 0, "rec_runs": 0,
        "tokens_saved": 0,                 # cumulative INPUT tokens saved (all sources)
        "rules": Counter(), "techniques": Counter(), "tips": Counter(),
        "sources": Counter(),
        "routes": Counter(),
        "models": Counter(), "model_tokens": {},
        "cache": {"hits": 0, "exact": 0, "semantic": 0},
        "cache_gauge": {},
        # Model-routing savings, priced from REAL token counts (input+output) against the
        # delta between the originally-requested model and the cheaper model actually served.
        "routing": {"usd": 0.0, "in_tokens": 0, "out_tokens": 0, "replies": 0, "pairs": Counter()},
        "hosts": Counter(), "host_saved": {},
        "recent": [],
        "series": [], "_ls": 0.0,
        "t0": 0.0,                         # epoch of the first event → live run-rate window
        "mode_seen": 0.0,                  # last demo.sh heartbeat → show "Demo" vs "live"
        # Output-side; cache_read/creation/input come from Anthropic's REAL usage object.
        "out": {"concise_tokens": 0, "concise_n": 0, "normal_tokens": 0, "normal_n": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0, "input_tokens": 0},
    }


TALLY = _fresh_tally()

# USD per 1M tokens (input, output). Source: claude-api skill pricing table (cached 2026-05).
PRICING = {
    "claude-opus-4-8":   {"label": "Opus 4.8",   "in": 5.0, "out": 25.0},
    "claude-sonnet-4-6": {"label": "Sonnet 4.6", "in": 3.0, "out": 15.0},
    "claude-haiku-4-5":  {"label": "Haiku 4.5",  "in": 1.0, "out": 5.0},
}


def _short(s: str, n: int = 56) -> str:
    s = (s or "").split(" — ")[0].split(":")[0].strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _modlabel(mid: str) -> str:
    m = (mid or "").lower()
    return "Opus" if "opus" in m else "Sonnet" if "sonnet" in m else "Haiku" if "haiku" in m else (mid or "?")


def _price(mid: str):
    """Resolve any model id (incl. date-suffixed) to its PRICING row by tier, or None."""
    m = (mid or "").lower()
    for key, p in PRICING.items():
        if any(t in m and t in key for t in ("opus", "sonnet", "haiku")):
            return p
    return None


def _push(entry: dict):
    TALLY["recent"].append(entry)
    if len(TALLY["recent"]) > 40:
        TALLY["recent"].pop(0)


def record_event(kind, source="cli", saved=0, rules=None, techniques=None, tips=None,
                 before="", after="", out_tokens=0, concise=False, host="", user="",
                 from_model="", to_model="", intent="", applied=False, model="",
                 layer="", similarity=0.0, in_tokens=0, cache_read=0, cache_creation=0,
                 routed_from=""):
    """Single funnel for every counted event. Privacy: prompt text is NEVER stored."""
    if not TALLY["t0"]:
        TALLY["t0"] = time.time()   # start the live run-rate window at the first real event
    host = host or "—"
    TALLY["hosts"][host] += 1
    TALLY["sources"][source] += 1

    if kind == "cache":
        TALLY["cache"]["hits"] += 1
        if layer in ("exact", "semantic"):
            TALLY["cache"][layer] += 1
        _push({"t": _now(), "source": source, "host": host, "kind": "cache",
               "layer": layer, "similarity": round(float(similarity or 0), 3),
               "model": _modlabel(model)})
        return

    if kind == "route":
        TALLY["routes"][f"{intent or '?'} → {_modlabel(to_model)}"] += 1
        _push({"t": _now(), "source": source, "host": host, "kind": "route",
               "intent": intent, "from_model": _modlabel(from_model),
               "to_model": _modlabel(to_model), "applied": bool(applied)})
        return

    if kind == "out":
        b = "concise" if concise else "normal"
        n = max(0, int(out_tokens or 0))
        TALLY["out"][f"{b}_tokens"] += n
        TALLY["out"][f"{b}_n"] += 1
        TALLY["out"]["cache_read_tokens"] += max(0, int(cache_read or 0))
        TALLY["out"]["cache_creation_tokens"] += max(0, int(cache_creation or 0))
        TALLY["out"]["input_tokens"] += max(0, int(in_tokens or 0))
        lbl = _modlabel(model)
        if model:
            TALLY["models"][lbl] += 1
            TALLY["model_tokens"][lbl] = TALLY["model_tokens"].get(lbl, 0) + n
        # Routing saving: this reply was downgraded from `routed_from` to `model`. Price the
        # delta on the REAL input+output token counts (cache_read is part of input_tokens here).
        pf, pt = _price(routed_from), _price(model)
        if routed_from and pf and pt and pf is not pt:
            it = max(0, int(in_tokens or 0))
            rt = TALLY["routing"]
            saved_usd = it * (pf["in"] - pt["in"]) / 1e6 + n * (pf["out"] - pt["out"]) / 1e6
            if saved_usd > 0:
                rt["usd"] += saved_usd
                rt["in_tokens"] += it
                rt["out_tokens"] += n
                rt["replies"] += 1
                rt["pairs"][f"{_modlabel(routed_from)} → {lbl}"] += 1
        _push({"t": _now(), "source": source, "host": host, "kind": "out",
               "out_tokens": n, "concise": bool(concise), "model": lbl if model else ""})
        return

    saved = max(0, int(saved or 0))
    if kind == "opt":
        TALLY["opt_runs"] += 1
    elif kind == "rec":
        TALLY["rec_runs"] += 1
    TALLY["tokens_saved"] += saved
    TALLY["host_saved"][host] = TALLY["host_saved"].get(host, 0) + saved
    for r in (rules or []):
        TALLY["rules"][_short(r)] += 1
    for t in (techniques or []):
        TALLY["techniques"][_short(t)] += 1
    for t in (tips or []):
        TALLY["tips"][_short(t)] += 1
    _push({"t": _now(), "source": source, "host": host, "kind": kind, "saved": saved})


@app.post("/api/record")
async def api_record(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        b = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if b.get("kind") == "mode":
        # Heartbeat from demo.sh — the dashboard shows "Demo" while these keep arriving,
        # and reverts to "live" once real Claude Code traffic (no heartbeat) takes over.
        TALLY["mode_seen"] = time.time()
        return JSONResponse({"ok": True})
    if b.get("kind") == "cachestat":
        TALLY["cache_gauge"] = {k: b.get(k) for k in (
            "entries", "bytes", "max_bytes", "hit_rate", "exact_hits",
            "semantic_hits", "misses", "evictions", "ready", "index", "model")}
        TALLY["sources"][b.get("source", "proxy")] += 1
        return JSONResponse({"ok": True})
    record_event(
        kind=b.get("kind", "opt"), source=b.get("source", "cli"), saved=b.get("saved", 0),
        rules=b.get("rules"), techniques=b.get("techniques"), tips=b.get("tips"),
        before=b.get("before", ""), after=b.get("after", ""),
        out_tokens=b.get("out_tokens", 0), concise=b.get("concise", False),
        host=b.get("host", ""), user=b.get("user", ""),
        from_model=b.get("from_model", ""), to_model=b.get("to_model", ""),
        intent=b.get("intent", ""), applied=b.get("applied", False), model=b.get("model", ""),
        layer=b.get("layer", ""), similarity=b.get("similarity", 0.0),
        in_tokens=b.get("in_tokens", 0), cache_read=b.get("cache_read", 0),
        cache_creation=b.get("cache_creation", 0), routed_from=b.get("routed_from", ""),
    )
    return JSONResponse({"ok": True})


def _output_summary():
    o = TALLY["out"]
    cn, nn = o["concise_n"], o["normal_n"]
    c_avg = round(o["concise_tokens"] / cn) if cn else 0
    n_avg = round(o["normal_tokens"] / nn) if nn else 0
    pct = round((n_avg - c_avg) / n_avg * 100) if (c_avg and n_avg) else 0
    out_saved = max(0, (n_avg - c_avg)) * cn if (c_avg and n_avg) else 0
    return {"concise_avg": c_avg, "normal_avg": n_avg, "concise_n": cn, "normal_n": nn,
            "pct_shorter": pct, "out_tokens_saved": out_saved,
            "cache_read_tokens": o["cache_read_tokens"],          # real, from Anthropic usage
            "cache_creation_tokens": o["cache_creation_tokens"],
            "input_tokens": o["input_tokens"]}


def _by_model():
    rows = [{"model": m, "replies": n, "out_tokens": TALLY["model_tokens"].get(m, 0)}
            for m, n in TALLY["models"].items()]
    rows.sort(key=lambda r: -r["out_tokens"])
    return rows


def _by_host():
    rows = [{"host": h, "events": n, "saved": TALLY["host_saved"].get(h, 0)}
            for h, n in TALLY["hosts"].items()]
    rows.sort(key=lambda r: (-r["saved"], -r["events"]))
    return rows[:12]


@app.get("/api/stats")
async def api_stats():
    def top(c: Counter, n=6):
        return [{"name": k, "count": v} for k, v in c.most_common(n)]
    o = _output_summary()
    total_saved = TALLY["tokens_saved"] + o["out_tokens_saved"]
    now = time.time()
    if now - TALLY["_ls"] >= 3:
        TALLY["_ls"] = now
        TALLY["series"].append({"t": _now(), "saved": int(total_saved)})
        if len(TALLY["series"]) > 200:
            TALLY["series"].pop(0)
    return JSONResponse({
        "opt_runs": TALLY["opt_runs"], "rec_runs": TALLY["rec_runs"],
        "tokens_saved": TALLY["tokens_saved"],
        "top_rules": top(TALLY["rules"]), "top_techniques": top(TALLY["techniques"]),
        "top_tips": top(TALLY["tips"]), "sources": dict(TALLY["sources"]),
        "top_routes": top(TALLY["routes"]), "by_model": _by_model(),
        "cache": dict(TALLY["cache"]), "cache_gauge": TALLY["cache_gauge"],
        "routing": {"usd": round(TALLY["routing"]["usd"], 4),
                    "in_tokens": TALLY["routing"]["in_tokens"],
                    "out_tokens": TALLY["routing"]["out_tokens"],
                    "replies": TALLY["routing"]["replies"],
                    "pairs": [{"name": k, "count": v}
                              for k, v in TALLY["routing"]["pairs"].most_common(6)]},
        "by_host": _by_host(), "recent": list(reversed(TALLY["recent"]))[:15],
        "output": o, "series": TALLY["series"], "pricing": PRICING,
        "elapsed_seconds": round(time.time() - TALLY["t0"]) if TALLY["t0"] else 0,
        "mode": "demo" if (time.time() - TALLY["mode_seen"]) < 12 else "live",
        "tz": _tz["name"] or "", "public_ip": _tz["ip"] or "",
    })


@app.post("/api/tz")
async def set_tz(request: Request):
    """Set the feed timezone explicitly (skips the public-IP lookup). 'auto' re-detects."""
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        name = ((await request.json()).get("tz") or "").strip()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    if not name:
        return JSONResponse({"ok": False})
    if name.lower() == "auto":
        _tz.update({"obj": None, "name": None, "ip": None})
        threading.Thread(target=_resolve_tz, daemon=True).start()
        return JSONResponse({"ok": True, "tz": "auto"})
    if ZoneInfo:
        try:
            _tz.update({"obj": ZoneInfo(name), "name": name, "ip": None})
            return JSONResponse({"ok": True, "tz": name})
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid timezone"})
    return JSONResponse({"ok": False, "error": "zoneinfo unavailable"})


@app.post("/api/reset")
async def api_reset(request: Request):
    """Zero every counter (in-memory, all sources/machines). Timezone detection is untouched."""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global TALLY
    TALLY = _fresh_tally()
    return JSONResponse({"ok": True})


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>InferenceIQ · savings</title>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{--bg:#0a0e14;--bg2:#0e131b;--card:#141a23;--card2:#181f2a;--line:#222b38;--line2:#2c3848;
    --fg:#e8edf4;--muted:#8a97a8;--dim:#5b6675;--accent:#6ea8fe;--green:#46d39a;--amber:#f0b84e;
    --violet:#a98bfa;--r:14px}
  body{font-family:'Inter',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
    background:radial-gradient(1100px 560px at 82% -12%,#16202e 0%,var(--bg) 55%);color:var(--fg);
    min-height:100vh;padding:26px clamp(16px,4vw,44px);-webkit-font-smoothing:antialiased}
  header{display:flex;align-items:center;gap:13px;flex-wrap:wrap}
  .logo{font-size:1.4rem;font-weight:800;letter-spacing:-.02em;
    background:linear-gradient(90deg,var(--accent),var(--violet));-webkit-background-clip:text;
    background-clip:text;color:transparent}
  .pill{font-size:.7rem;color:var(--muted);background:#ffffff09;border:1px solid var(--line);
    border-radius:999px;padding:3px 11px;font-weight:500}
  .pill.on{color:var(--accent);border-color:var(--accent);background:#6ea8fe18}
  .live{display:inline-flex;align-items:center;gap:6px;font-size:.7rem;color:var(--green)}
  .dot{width:7px;height:7px;background:var(--green);border-radius:50%;animation:pulse 2.2s infinite}
  .live.demo{color:var(--amber)}.live.demo .dot{background:var(--amber);animation:none}
  @keyframes pulse{0%{box-shadow:0 0 0 0 #46d39a66}70%{box-shadow:0 0 0 7px #46d39a00}100%{box-shadow:0 0 0 0 #46d39a00}}
  .sub{color:var(--muted);font-size:.8rem;margin:6px 0 22px}
  .green{color:var(--green)}.amber{color:var(--amber)}.accent{color:var(--accent)}.violet{color:var(--violet)}.c{color:var(--dim)}

  /* hero KPIs */
  .hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(186px,1fr));gap:14px;margin-bottom:22px}
  .kpi{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);
    border-radius:var(--r);padding:18px;position:relative;overflow:hidden}
  .kpi::after{content:"";position:absolute;inset:0 0 auto 0;height:2px;
    background:linear-gradient(90deg,transparent,var(--accent),transparent);opacity:.45}
  .kpi .l{font-size:.66rem;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin-bottom:9px}
  .kpi .v{font-size:1.95rem;font-weight:800;line-height:1;letter-spacing:-.02em}
  .kpi .s{font-size:.71rem;color:var(--dim);margin-top:7px}

  nav{display:flex;gap:4px;margin:0 0 18px;border-bottom:1px solid var(--line);flex-wrap:wrap}
  .tab{background:none;border:none;color:var(--muted);font:inherit;font-size:.84rem;font-weight:600;
    padding:9px 16px;cursor:pointer;border-bottom:2px solid transparent;border-radius:8px 8px 0 0}
  .tab:hover{color:var(--fg);background:#ffffff06}
  .tab.on{color:var(--accent);border-bottom-color:var(--accent)}
  .page{display:none}.page.on{display:block;animation:fade .2s ease}
  @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:16px;margin-bottom:16px}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px}
  .panel h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
    margin-bottom:14px;display:flex;justify-content:space-between;align-items:center;gap:8px}
  .panel h2 .hint{font-weight:400;text-transform:none;letter-spacing:0;color:var(--dim);font-size:.72rem}
  .full{grid-column:1/-1}
  select{background:var(--bg2);color:var(--fg);border:1px solid var(--line2);border-radius:7px;
    padding:3px 8px;font:inherit;font-size:.74rem}

  .two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .three{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
  .mini{background:var(--card2);border:1px solid var(--line);border-radius:11px;padding:14px}
  .mini .t{font-size:.72rem;color:var(--muted);margin-bottom:8px}
  .mini .b{font-size:1.4rem;font-weight:700}
  .mini .x{font-size:.68rem;color:var(--dim);margin-top:4px}

  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{text-align:left;color:var(--muted);font-weight:500;font-size:.7rem;text-transform:uppercase;
    letter-spacing:.05em;padding:0 8px 8px;border-bottom:1px solid var(--line)}
  td{padding:8px;border-bottom:1px solid var(--bg2)}tr:last-child td{border-bottom:none}
  .mono{font-family:ui-monospace,'SF Mono',Menlo,monospace}
  .barcell{width:48%}
  .bar{height:7px;border-radius:999px;background:var(--line);overflow:hidden}
  .bar>i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--accent),var(--violet))}
  .chip{display:inline-block;font-size:.72rem;background:#ffffff0d;border:1px solid var(--line);
    border-radius:999px;padding:2px 10px;margin:0 6px 6px 0}.chip b{color:var(--accent)}
  .list{list-style:none;font-size:.8rem}
  .list li{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px solid var(--bg2)}
  .list li:last-child{border-bottom:none}
  .feed div{padding:9px 2px;border-bottom:1px solid var(--bg2);font-size:.8rem;display:flex;gap:10px;align-items:baseline}
  .feed div:last-child{border-bottom:none}
  .feed .ts{color:var(--dim);font-size:.72rem;font-variant-numeric:tabular-nums}
  .feed .src{font-weight:700;font-size:.72rem;text-transform:uppercase;letter-spacing:.04em}
  .feed .hp{color:var(--muted);font-size:.7rem;background:#ffffff0a;border:1px solid var(--line);border-radius:6px;padding:0 6px}
  .empty{color:var(--dim);font-size:.8rem;padding:10px 0}

  /* light theme (applied via html[data-theme=light]) */
  html[data-theme=light]{--bg:#f3f6fb;--bg2:#e9eef6;--card:#ffffff;--card2:#f6f8fc;
    --line:#dde4ee;--line2:#cbd5e4;--fg:#101722;--muted:#5d6b7e;--dim:#90a0b3}
  html[data-theme=light] body{background:radial-gradient(1100px 560px at 82% -12%,#e6eefb 0%,var(--bg) 55%)}
  html[data-theme=light] .kpi,html[data-theme=light] .mini{box-shadow:0 1px 3px #0b1a3310}

  /* settings popover */
  .settings{position:absolute;right:clamp(16px,4vw,44px);margin-top:6px;z-index:20;
    background:var(--card);border:1px solid var(--line2);border-radius:12px;padding:14px 16px;
    box-shadow:0 14px 40px #0008;min-width:250px}
  .settings .srow{display:flex;justify-content:space-between;align-items:center;gap:14px;margin:9px 0}
  .settings label{font-size:.78rem;color:var(--muted)}
  .settings select{min-width:130px}
  .resetbtn{background:#e5484d18;color:#f0686d;border:1px solid #e5484d55;border-radius:7px;
    padding:4px 12px;font:inherit;font-size:.76rem;font-weight:600;cursor:pointer}
  .resetbtn:hover{background:#e5484d2e;border-color:#e5484d}
  .resetbtn.done{background:#46d39a22;color:var(--green);border-color:#46d39a66}
  .settings .shint{font-size:.68rem;color:var(--dim);margin-top:8px;line-height:1.5}

  /* help tooltips */
  .help{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;
    border:1px solid var(--line2);border-radius:50%;font-size:.62rem;color:var(--muted);
    margin-left:6px;cursor:help;font-weight:600;flex:none;vertical-align:middle}
  .help:hover{color:var(--accent);border-color:var(--accent)}
  #tip{position:fixed;z-index:50;max-width:300px;background:var(--card);border:1px solid var(--line2);
    border-radius:10px;padding:11px 13px;font-size:.76rem;line-height:1.55;color:var(--fg);
    box-shadow:0 14px 40px #0009;pointer-events:none;display:none}
  #tip b{color:var(--accent)}#tip .f{color:var(--muted);font-family:ui-monospace,Menlo,monospace;font-size:.72rem}
</style></head><body>

<header>
  <span class="logo">⚡ InferenceIQ</span>
  <span class="pill" id="tzpill" style="display:none"></span>
  <span class="live" id="livepill"><span class="dot"></span> <span id="modetxt">live</span></span>
  <button id="roibtn" class="pill" style="cursor:pointer;margin-left:auto">📊 ROI</button>
  <button id="gear" class="pill" style="cursor:pointer">⚙ Settings</button>
</header>

<div id="settings" class="settings" hidden>
  <div class="srow"><label>Refresh</label>
    <select id="set-refresh"><option value="5">5 sec</option><option value="10">10 sec</option>
      <option value="15">15 sec</option><option value="30">30 sec</option><option value="60">60 sec</option></select></div>
  <div class="srow"><label>Theme</label>
    <select id="set-theme"><option value="system">System</option><option value="dark">Dark</option><option value="light">Light</option></select></div>
  <div class="srow"><label>Timezone</label><select id="set-tz"></select></div>
  <div class="shint">Setting a timezone skips the public-IP lookup. Applies to new events.</div>
  <div class="srow" style="border-top:1px solid var(--line);padding-top:11px;margin-top:11px">
    <label>Counters</label><button id="set-reset" class="resetbtn">↺ Reset all</button></div>
  <div class="shint">Zeroes every counter, chart, and feed across all sources &amp; machines. Cannot be undone.</div>
</div>

<div id="tip"></div>

<div class="hero" id="hero"></div>

<nav>
  <button class="tab on" data-t="overview">Overview</button>
  <button class="tab" data-t="models">Models &amp; Routing</button>
  <button class="tab" data-t="activity">Activity</button>
</nav>

<section class="page on" data-p="overview">
  <div class="panel full" style="margin-bottom:16px">
    <h2>Savings accumulating <span class="hint">cumulative tokens saved over time</span>
      <span class="help" data-help="<b>Savings accumulating</b> — watch total tokens saved climb.<br><span class='f'>cumulative (input tokens saved + estimated output tokens saved), sampled ~3s</span>">i</span></h2>
    <div id="chart"><div class="empty">Collecting data… the line builds as savings accumulate.</div></div>
  </div>
  <div class="panel full">
    <h2>Where the savings come from <span class="hint">priced as <select id="model"></select></span>
      <span class="help" data-help="<b>Where the savings come from</b> — the three money levers, priced by the selected model. Hover each card for its formula.">i</span></h2>
    <div class="three" id="levers"></div>
  </div>
</section>

<section class="page" data-p="models">
  <div class="grid">
    <div class="panel"><h2>Models used <span class="hint">real replies served</span>
      <span class="help" data-help="<b>Models used</b> — actual model per reply (from the response). Replies + output tokens served by each model.">i</span></h2>
      <div id="models_t"><div class="empty">No replies recorded yet.</div></div></div>
    <div class="panel"><h2>Model routing <span class="hint">intent → model</span>
      <span class="help" data-help="<b>Model routing</b> — intent→model decisions (Haiku/Sonnet/Opus), deterministic. Agentic (tool) requests are never routed.">i</span></h2>
      <div id="routes"><div class="empty">No routing yet.</div></div></div>
  </div>
</section>

<section class="page" data-p="team">
  <div class="panel full" id="roi-live" style="margin-bottom:18px;padding:24px 20px"></div>
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:22px 0 14px">
    <span style="font-size:.8rem;color:var(--muted)">Or model a hypothetical team of</span>
    <select id="roi-size"><option value="5">5 developers</option><option value="15" selected>15 developers</option><option value="25">25 developers</option><option value="50">50 developers</option><option value="100">100 developers</option></select>
    <span style="font-size:.74rem;color:var(--dim)">coding 6 hrs/day · ~22 days/mo · ~$300/dev/mo baseline (morphllm.com)</span>
  </div>
  <div class="panel full" id="roi-head" style="margin-bottom:16px;text-align:center;padding:26px 18px"></div>
  <div class="hero" id="roi-kpis" style="margin-bottom:18px"></div>
  <div class="grid">
    <div class="panel full"><h2>Scenario comparison <span class="hint">money saved per month &amp; per year</span></h2>
      <div id="roi-table"></div></div>
  </div>
  <div class="grid">
    <div class="panel"><h2>Where the savings come from <span class="hint" id="roi-lvl">at 15 devs</span></h2><div id="roi-levers"></div></div>
    <div class="panel"><h2>Savings over time <span class="hint" id="roi-projlvl">recurring · tokens &amp; $</span></h2><div id="roi-proj"></div></div>
  </div>
  <div class="panel full"><h2>Model cost comparison <span class="hint">5 devs Claude Code · Opus 4.8 vs Sonnet 4.6 vs Haiku 4.5 · baseline → with InferenceIQ</span>
      <span class="help" data-help="<b>Model cost comparison</b> — monthly Claude Code spend per team size on each model tier, baseline (strike-through) → with InferenceIQ. Same per-dev token volume (~45M in / 2.5M out per dev/mo); only the model price changes. Choosing a cheaper tier AND running InferenceIQ compound.">i</span></h2>
    <div id="roi-models"></div></div>
</section>

<section class="page" data-p="activity">
  <div class="grid">
    <div class="panel"><h2>By machine <span class="help" data-help="<b>By machine</b> — per-host events and input tokens saved, from host-tagged reports (one row per developer/box).">i</span></h2><div id="hosts"><div class="empty">No machines yet.</div></div></div>
    <div class="panel"><h2>By source <span class="help" data-help="<b>By source</b> — where events came from: web · CLI · proxy · hook.">i</span></h2><div id="sources"><div class="empty">Nothing yet.</div></div></div>
  </div>
  <div class="grid">
    <div class="panel"><h2>Top mechanical rules <span class="help" data-help="<b>Top rules</b> — most-fired meaning-preserving filler-strip rules (e.g. drop 'please', 'in order to'→'to').">i</span></h2><ul class="list" id="rules"><li class="empty">Nothing yet.</li></ul></div>
    <div class="panel"><h2>Top best-practice tips <span class="help" data-help="<b>Top tips</b> — best-practice token tips recommended per prompt (RAG, caching, scoping…).">i</span></h2><ul class="list" id="tips"><li class="empty">Nothing yet.</li></ul></div>
  </div>
  <div class="panel full"><h2>Live activity <span class="hint">all sources &amp; machines</span>
      <span class="help" data-help="<b>Live activity</b> — every event across all sources & machines, newest first.">i</span></h2>
    <div class="feed" id="feed"><div class="empty">Waiting for reports…</div></div></div>
</section>

<script>
const $=id=>document.getElementById(id);
const set=(id,h)=>{const e=$(id);if(e)e.innerHTML=h};
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const usd=v=>'$'+(Math.abs(v)>=1?v.toFixed(2):v.toFixed(4));
const k=n=>n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':(''+n);
const SRC={web:['web','var(--accent)'],cli:['CLI','var(--green)'],proxy:['proxy','var(--violet)'],hook:['hook','var(--amber)']};
// POST helper for the admin write endpoints (reset / tz). Sends the stored IQ_TOKEN if any;
// on 401 (token-protected cloud collector) prompts once, stores it, and retries.
async function iqPost(url,opts){
  opts=opts||{};const h={...(opts.headers||{})};
  let t=localStorage.getItem('iq_token')||''; if(t)h['X-IQ-Token']=t;
  let r=await fetch(url,{...opts,method:'POST',headers:h});
  if(r.status===401){
    t=prompt('This dashboard is token-protected. Enter the admin token (IQ_TOKEN):','');
    if(t){localStorage.setItem('iq_token',t);h['X-IQ-Token']=t;
      r=await fetch(url,{...opts,method:'POST',headers:h});}
  }
  return r;
}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{
  $('roibtn').classList.remove('on');
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('.page').forEach(p=>p.classList.toggle('on',p.dataset.p===b.dataset.t));
});
$('roibtn').onclick=()=>{   // ROI lives beside Settings, not in the tab bar
  $('roibtn').classList.add('on');
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.page').forEach(p=>p.classList.toggle('on',p.dataset.p==='team'));
};

// ── help tooltips: objective + how each stat is calculated ──
const HELP={
  total:'<b>Total saved</b> — money saved across every lever, priced by the selected model.<br><span class="f">input$ + output$ + cache$ = inTok×in + outTok×out + cacheRead×in×0.9</span>',
  toks:'<b>Tokens saved</b> — measured tokens trimmed = input filler removed + output tokens cut by concise mode.<br><span class="f">tokens_saved + out_tokens_saved</span> · cached-read tokens (billed @0.1×) are shown separately in the sub-line.',
  perprompt:'<b>Avg saved / prompt</b> — mean saving across every prompt handled.<br><span class="f">total $ saved ÷ prompts</span> · the hook measures the INPUT trim per prompt; the OUTPUT saving (the big lever) is only counted when traffic runs through the proxy.',
  perdev:'<b>Saved / developer</b> — average saving per reporting machine.<br><span class="f">total $ ÷ active machines</span>',
  calls:'<b>LLM calls avoided</b> — requests served from the semantic cache with NO API call.<br><span class="f">count of exact + semantic cache hits</span>',
  reply:'<b>Reply reduction</b> — how much shorter concise replies are (the big lever; output ≈5× input).<br><span class="f">(normal_avg − concise_avg) ÷ normal_avg</span> · from real output_tokens',
  cachek:'<b>Prompt-cache saved</b> — REAL money saved by Anthropic prompt caching (cached reads cost ~0.1×).<br><span class="f">cache_read_input_tokens × in_price × 0.9</span> · from the usage object',
  lvIn:'<b>Shorter prompts</b> — input tokens trimmed (filler + rewrite).<br><span class="f">input tokens saved × input price</span>',
  lvOut:'<b>Shorter replies</b> — output tokens saved by concise mode (5× price).<br><span class="f">(normal_avg−concise_avg)×concise_replies × output price</span>',
  lvRoute:'<b>Model routing</b> — saving from serving a reply on a cheaper model than requested, on REAL token counts.<br><span class="f">Σ inTok×(from.in−to.in) + outTok×(from.out−to.out)</span> · only when ROUTE_MODELS=on',
  lvCache:'<b>Prompt cache</b> — real saving from Anthropic usage.<br><span class="f">cache_read tokens × input price × 0.9</span>',
  chart:'<b>Savings accumulating</b> — see total tokens saved climb over time.<br><span class="f">cumulative (input tokens saved + estimated output tokens saved), sampled ~3s</span>',
  roiLive:'<b>Projected monthly savings</b> — the REAL $ saved so far, extrapolated to 30 days from the live run-rate. No team model — uses actual measured usage and the number of machines currently reporting.<br><span class="f">total $ saved × (30d ÷ elapsed observed); needs ~2 min of data</span>',
  roiMo:'<b>Saved / month</b> — modeled recurring saving for the chosen team.<br><span class="f">baseline × reduction; baseline = $2.27/dev/h × 6h × 22d × devs (~$300/dev/mo, morphllm.com)</span>',
  roiYr:'<b>Saved / year</b><br><span class="f">saved/month × 12</span>',
  roi3:'<b>Saved over 3 years</b><br><span class="f">saved/month × 36</span>',
  roiDev:'<b>Saved / developer / month</b><br><span class="f">saved/month ÷ developers</span>',
  roiRed:'<b>Cost reduction</b> — combined % off baseline.<br><span class="f">concise + cache + routing + prompt-caching ≈ 60% (morphllm.com)</span>',
  models:'<b>Models used</b> — actual model per reply, from the response usage (replies + output tokens).',
  routes:'<b>Model routing</b> — intent→model decisions (Haiku/Sonnet/Opus). Agentic requests are never routed.',
  hosts:'<b>By machine</b> — per-host events and input tokens saved, from host-tagged reports.',
  sources:'<b>By source</b> — where events came from: web · CLI · proxy · hook.',
  rules:'<b>Top rules</b> — most-fired mechanical filler-strip rules.',
  tips:'<b>Top tips</b> — best-practice token tips recommended for prompts.',
  feed:'<b>Live activity</b> — every event across all sources & machines, newest first.',
};
const H=k=>`<span class="help" data-help="${(HELP[k]||'').replace(/"/g,'&quot;')}">i</span>`;
const TIP=$('tip');
document.addEventListener('mouseover',e=>{const t=e.target.closest('[data-help]');if(!t)return;
  TIP.innerHTML=t.getAttribute('data-help');TIP.style.display='block';
  const r=t.getBoundingClientRect();let x=r.left,y=r.bottom+8;
  if(x+300>innerWidth)x=innerWidth-310;if(x<8)x=8;TIP.style.left=x+'px';TIP.style.top=y+'px';});
document.addEventListener('mouseout',e=>{if(e.target.closest('[data-help]'))TIP.style.display='none';});

function chart(series){
  if(!series||series.length<2) return '<div class="empty">Collecting data… the line builds as savings accumulate.</div>';
  const W=1000,H=190,P=10,n=series.length,v=series.map(p=>p.saved),mx=Math.max(...v,1),mn=Math.min(...v,0);
  const X=i=>P+i*(W-2*P)/(n-1),Y=z=>H-P-(z-mn)/((mx-mn)||1)*(H-2*P);
  const pts=series.map((p,i)=>X(i).toFixed(1)+','+Y(p.saved).toFixed(1)).join(' ');
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:200px;display:block">
    <defs><linearGradient id="ga" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0" stop-color="#46d39a" stop-opacity=".35"/><stop offset="1" stop-color="#46d39a" stop-opacity="0"/></linearGradient></defs>
    <polygon points="${P},${H-P} ${pts} ${W-P},${H-P}" fill="url(#ga)"/>
    <polyline points="${pts}" fill="none" stroke="#46d39a" stroke-width="2.5" vector-effect="non-scaling-stroke"/></svg>
    <div style="display:flex;justify-content:space-between;font-size:.74rem;color:var(--muted);margin-top:8px">
      <span>${series[0].t}</span><span>cumulative tokens saved → <b class="green">${v[n-1].toLocaleString()}</b></span><span>${series[n-1].t}</span></div>`;
}

// Team ROI model — N devs, 6h/day, ~22 days/mo, ~$300/dev/mo baseline (morphllm.com/ai-coding-costs).
// Per-dev monthly token volume (input-heavy, prompt-cached). Tuned so Opus 4.8 ≈ the $300/dev baseline,
// which lets us also price the SAME usage on cheaper tiers for the model-cost comparison.
const IN_DEV=45e6, OUT_DEV=2.5e6;
function teamCalc(n,o){
  const base=2.27*6*22*n;                                   // monthly baseline spend
  const concise=(o&&o.pct_shorter)?Math.max(.18,Math.min(.35,o.pct_shorter/100*.45)):.25;
  const lv=[['💬 Concise replies',concise],['⚡ Semantic cache',.15],['🔀 Model routing',.12],['📦 Prompt caching',.08]];
  const red=lv.reduce((s,l)=>s+l[1],0);
  const tokMo=(IN_DEV+OUT_DEV)*n;                           // baseline tokens / mo
  return {n,base,red,saved:base*red,opt:base*(1-red),lv,tokMo,tokSaved:tokMo*red};
}
function renderROI(o){
  const sz=+($('roi-size').value||15), c=teamCalc(sz,o);
  $('roi-lvl').textContent='at '+sz+' devs';
  // Headline — make the SAVED MONEY unmistakable
  set('roi-head',`<div style="font-size:.74rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)">Estimated money saved · ${sz} developers</div>
    <div class="green" style="font-size:3.2rem;font-weight:800;letter-spacing:-.03em;line-height:1.05;margin:6px 0 2px">${usd(c.saved)} <span style="font-size:1.3rem;color:var(--muted);font-weight:600">/ month</span></div>
    <div style="font-size:.95rem;color:var(--fg)">= <b class="green">${usd(c.saved*12)}</b> saved per year · spend drops <b>${usd(c.base)} → ${usd(c.opt)}</b> /mo (<b>${Math.round(c.red*100)}% less</b>)</div>`);
  // KPIs — lead with monthly money saved
  const kpi=(v,l,s,cl)=>`<div class="kpi"><div class="l">${l}</div><div class="v ${cl||''}">${v}</div><div class="s">${s||''}</div></div>`;
  set('roi-kpis',
    kpi(usd(c.saved),'Saved / month'+H('roiMo'),`recurring · ${sz} developers`,'green')+
    kpi(usd(c.saved*12),'Saved / year'+H('roiYr'),`${Math.round(c.red*100)}% off ${usd(c.base*12)}/yr`,'green')+
    kpi(usd(c.saved*36),'Saved over 3 years'+H('roi3'),'same trajectory','green')+
    kpi(usd(c.saved/sz),'Saved / dev / month'+H('roiDev'),'per developer','violet')+
    kpi(Math.round(c.red*100)+'%','Cost reduction'+H('roiRed'),`${usd(c.base)} → ${usd(c.opt)} /mo`,'accent'));
  // scenario comparison table — Saved/mo is the hero column
  const sizes=[5,15,25,50];
  set('roi-table',`<table><thead><tr><th>Team</th><th>Spend now / mo</th><th>With InferenceIQ / mo</th><th>💰 Saved / mo</th><th>Saved / yr</th><th>Less</th></tr></thead><tbody>`+
    sizes.map(n=>{const t=teamCalc(n,o);const hl=n===sz?' style="background:#6ea8fe14"':'';
      return `<tr${hl}><td><b>${n} devs</b></td><td class="c">${usd(t.base)}</td><td>${usd(t.opt)}</td>
        <td class="green"><b style="font-size:.95rem">${usd(t.saved)}</b></td><td class="green">${usd(t.saved*12)}</td><td class="c">${Math.round(t.red*100)}%</td></tr>`}).join('')+`</tbody></table>`);
  // lever breakdown at selected size
  set('roi-levers',c.lv.map(l=>`<div style="display:flex;justify-content:space-between;font-size:.78rem;margin:9px 0 4px"><span>${l[0]}</span><span class="c">${usd(c.base*l[1])}/mo · ${Math.round(l[1]*100)}%</span></div><div class="bar"><i style="width:${Math.round(l[1]/c.red*100)}%"></i></div>`).join(''));
  // savings over time — 1mo / 6mo / 1yr / 2yr / 3yr, cumulative $ AND tokens saved
  $('roi-projlvl').textContent='cumulative at '+sz+' devs';
  const horizons=[['1 month',1],['6 months',6],['1 year',12],['2 years',24],['3 years',36]];
  set('roi-proj',`<table><thead><tr><th>Horizon</th><th>💰 $ saved</th><th>Tokens saved</th></tr></thead><tbody>`+
    horizons.map(([lbl,m])=>`<tr><td><b>${lbl}</b></td><td class="green"><b>${usd(c.saved*m)}</b></td><td class="c">${k(c.tokSaved*m)}</td></tr>`).join('')+`</tbody></table>`);

  // model cost comparison — same per-dev usage priced on each tier, baseline → with InferenceIQ
  const tiers=[['claude-opus-4-8','Opus 4.8'],['claude-sonnet-4-6','Sonnet 4.6'],['claude-haiku-4-5','Haiku 4.5']];
  const teams=[5,10,15,20];
  set('roi-models',`<table><thead><tr><th>Team</th>`+tiers.map(t=>`<th>${t[1]} / mo</th>`).join('')+`</tr></thead><tbody>`+
    teams.map(n=>{const rc=teamCalc(n,o);const hl=n===sz?' style="background:#6ea8fe14"':'';
      return `<tr${hl}><td><b>${n} devs</b></td>`+tiers.map(([id,lbl])=>{
        const pr=(LASTP&&LASTP[id])||{in:5,out:25};
        const cost=(IN_DEV*pr.in+OUT_DEV*pr.out)/1e6*n, opt=cost*(1-rc.red);
        return `<td><span class="c" style="text-decoration:line-through">${usd(cost)}</span> <b class="green">${usd(opt)}</b><div class="x">save ${usd(cost-opt)}/mo · ${usd((cost-opt)*12)}/yr</div></td>`;
      }).join('')+`</tr>`;}).join('')+`</tbody></table>`);
}

let LASTO={}, LASTP={};
async function tick(){
  let d;try{d=await(await fetch('/api/stats')).json()}catch{return}
  const o=d.output||{}, cache=d.cache||{}, cg=d.cache_gauge||{};
  LASTO=o;
  const runs=(d.opt_runs||0)+(d.rec_runs||0);

  // tz pill
  if(d.tz){const p=$('tzpill');p.style.display='';p.textContent='🕑 '+d.tz;}

  // live vs demo indicator: "Demo" while demo.sh heartbeats arrive, else "live" (real Claude Code)
  const demo=d.mode==='demo';
  $('livepill').classList.toggle('demo',demo);
  $('modetxt').textContent=demo?'Demo':'live';

  // pricing selector
  const P=d.pricing||{}, sel=$('model'); LASTP=P;
  if(sel&&!sel.dataset.init&&Object.keys(P).length){
    sel.innerHTML=Object.entries(P).map(([id,p])=>`<option value="${id}">${p.label} — $${p.in}/$${p.out} per 1M</option>`).join('');
    sel.dataset.init='1';sel.onchange=tick;
  }
  const pid=(sel&&sel.value)||Object.keys(P)[0]||'';const price=P[pid]||{in:5,out:25};

  // dollars
  const inUSD=(d.tokens_saved||0)/1e6*price.in;
  const outUSD=(o.out_tokens_saved||0)/1e6*price.out;
  const cacheUSD=(o.cache_read_tokens||0)/1e6*price.in*0.9;   // cache read ~0.1x → save 0.9x (REAL)
  const routing=d.routing||{}, routeUSD=routing.usd||0;       // server-priced cross-model delta (REAL)
  const totUSD=inUSD+outUSD+cacheUSD+routeUSD;
  const machines=(d.by_host||[]).filter(h=>h.host&&h.host!=='—').length;

  // HERO KPIs (business-first)
  const kpi=(v,l,s,c)=>`<div class="kpi"><div class="l">${l}</div><div class="v ${c||''}">${v}</div><div class="s">${s||''}</div></div>`;
  const perPromptUSD=totUSD/Math.max(1,runs);
  const trimTok=(d.tokens_saved||0)+(o.out_tokens_saved||0);   // measured tokens trimmed (in+out)
  const perPromptTok=trimTok/Math.max(1,runs);
  set('hero',
    kpi(usd(totUSD),'Total saved'+H('total'),`across ${runs.toLocaleString()} prompts · ${machines||0} devs`,totUSD>0?'green':'')+
    kpi(k(trimTok),'Tokens saved'+H('toks'),`${k(d.tokens_saved||0)} in + ${k(o.out_tokens_saved||0)} out · +${k(o.cache_read_tokens||0)} cached`,trimTok>0?'green':'')+
    kpi(runs?usd(perPromptUSD):'—','Avg saved / prompt'+H('perprompt'),runs?`~${k(Math.round(perPromptTok))} tokens · ${runs.toLocaleString()} prompts`:'no prompts yet',perPromptUSD>0?'green':'')+
    kpi(usd(totUSD/Math.max(1,machines)),'Saved / developer'+H('perdev'),`${machines||0} machines reporting`,totUSD>0?'green':'')+
    kpi((cache.hits||0).toLocaleString(),'LLM calls avoided'+H('calls'),`semantic cache · ${cg.hit_rate!=null?cg.hit_rate+'% hit':'—'}`,cache.hits>0?'accent':'')+
    kpi(o.concise_n?(o.pct_shorter||0)+'%':'—','Reply reduction'+H('reply'),'shorter answers (the big lever)',o.pct_shorter>0?'green':'amber')+
    kpi(usd(cacheUSD),'Prompt-cache saved'+H('cachek'),'real, from Anthropic usage',cacheUSD>0?'violet':'')
  );

  // OVERVIEW: chart + where savings come from (3 levers incl. real cache)
  set('chart',chart(d.series||[]));
  const lever=(icon,t,v,x,c)=>`<div class="mini"><div class="t">${icon} ${t}</div><div class="b ${c||''}">${v}</div><div class="x">${x}</div></div>`;
  set('levers',
    lever('✍','Shorter prompts'+H('lvIn'),usd(inUSD),`${k(d.tokens_saved||0)} input tokens · small lever`,inUSD>0?'green':'')+
    lever('💬','Shorter replies'+H('lvOut'),usd(outUSD),`${k(o.out_tokens_saved||0)} output tokens · 5× price`,outUSD>0?'green':'')+
    lever('🔀','Model routing'+H('lvRoute'),usd(routeUSD),`${routing.replies||0} replies downgraded · real`,routeUSD>0?'green':'')+
    lever('📦','Prompt cache'+H('lvCache'),usd(cacheUSD),`${k(o.cache_read_tokens||0)} cached reads @0.1× · real`,cacheUSD>0?'green':'')
  );

  // MODELS
  const models=d.by_model||[],mxM=Math.max(1,...models.map(x=>x.out_tokens));
  set('models_t',!models.length?'<div class="empty">No replies recorded yet.</div>'
    :`<table><thead><tr><th>Model</th><th>Replies</th><th class="barcell">Output tokens</th></tr></thead><tbody>`+
     models.map(x=>`<tr><td><b>${esc(x.model)}</b></td><td class="c">${x.replies}</td>
       <td class="barcell"><div style="display:flex;align-items:center;gap:9px"><div class="bar" style="flex:1"><i style="width:${Math.round(x.out_tokens/mxM*100)}%"></i></div><span class="c" style="min-width:54px;text-align:right">${k(x.out_tokens)}</span></div></td></tr>`).join('')+`</tbody></table>`);
  const routes=d.top_routes||[];
  const routeHdr=routeUSD>0
    ?`<div class="mini" style="margin-bottom:12px"><div class="t">🔀 Saved by routing <span class="c">· ${routing.replies||0} replies on a cheaper model · real token counts</span></div><div class="b green">${usd(routeUSD)}</div><div class="x">${k(routing.in_tokens||0)} in · ${k(routing.out_tokens||0)} out re-priced at the cheaper tier</div></div>`
    :'';
  set('routes',routeHdr+(!routes.length?'<div class="empty">No routing yet — set <code>ROUTE_MODELS=advise|on</code>.</div>'
    :routes.map(r=>`<span class="chip"><b class="accent">${esc(r.name)}</b> &nbsp;${r.count}</span>`).join('')));

  // LIVE PROJECTION — extrapolate the REAL measured savings to a month (no team model)
  const el=d.elapsed_seconds||0, MONTH=2592000;
  const fmtDur=s=>s<90?Math.round(s)+'s':s<5400?Math.round(s/60)+'m':s<172800?(s/3600).toFixed(1)+'h':(s/86400).toFixed(1)+'d';
  const liveHead=`<div style="font-size:.74rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)">Projected monthly savings · measured from live usage${H('roiLive')}</div>`;
  let liveBody;
  if(el>=120&&totUSD>0){
    const mo=totUSD*MONTH/el, perDev=mo/Math.max(1,machines), perDay=totUSD*86400/el;
    const toksMo=((d.tokens_saved||0)+(o.out_tokens_saved||0))*MONTH/el;
    liveBody=`<div class="green" style="font-size:3.2rem;font-weight:800;letter-spacing:-.03em;line-height:1.05;margin:6px 0 2px">${usd(mo)} <span style="font-size:1.3rem;color:var(--muted);font-weight:600">/ month</span></div>
      <div style="font-size:.95rem;color:var(--fg)">extrapolated from <b class="green">${usd(totUSD)}</b> saved over <b>${fmtDur(el)}</b> of live data across <b>${machines||0}</b> developer${machines===1?'':'s'} · run-rate <b>${usd(perDay)}</b>/day</div>
      <div class="three" style="margin-top:16px">
        <div class="mini"><div class="t">Per developer / mo</div><div class="b violet">${usd(perDev)}</div><div class="x">${machines||0} reporting now</div></div>
        <div class="mini"><div class="t">Tokens saved / mo</div><div class="b green">${k(toksMo)}</div><div class="x">input + output, projected</div></div>
        <div class="mini"><div class="t">Annualized</div><div class="b green">${usd(mo*12)}</div><div class="x">at the current run-rate</div></div>
      </div>`;
  }else{
    liveBody=`<div class="empty" style="font-size:.9rem;margin-top:8px">Collecting live data… need ~2 min of events to project a stable run-rate${el?` (have ${fmtDur(el)})`:''}. Meanwhile, model a hypothetical team below.</div>`;
  }
  set('roi-live',liveHead+liveBody);

  // TEAM ROI
  const rs=$('roi-size'); if(rs&&!rs.dataset.init){rs.dataset.init='1';rs.onchange=()=>renderROI(LASTO);}
  renderROI(o);

  // ACTIVITY
  const hosts=d.by_host||[],mxH=Math.max(1,...hosts.map(h=>h.saved));
  set('hosts',!hosts.length?'<div class="empty">No machines reporting yet.</div>'
    :`<table><thead><tr><th>Machine</th><th>Events</th><th class="barcell">Tokens saved</th></tr></thead><tbody>`+
     hosts.map(h=>`<tr><td class="mono">${esc(h.host)}</td><td class="c">${h.events}</td>
       <td class="barcell"><div style="display:flex;align-items:center;gap:9px"><div class="bar" style="flex:1"><i style="width:${Math.round(h.saved/mxH*100)}%"></i></div><span class="c" style="min-width:50px;text-align:right">${k(h.saved)}</span></div></td></tr>`).join('')+`</tbody></table>`);
  const src=Object.entries(d.sources||{}).sort((a,b)=>b[1]-a[1]);
  set('sources',!src.length?'<div class="empty">Nothing yet.</div>'
    :src.map(([s,n])=>{const m=SRC[s]||[s,'var(--muted)'];return `<span class="chip" style="border-color:${m[1]}55"><b style="color:${m[1]}">${m[0]}</b> &nbsp;${n}</span>`}).join(''));
  const li=a=>!a||!a.length?'<li class="empty">Nothing yet.</li>':a.map(x=>`<li><span>${esc(x.name)}</span><span class="c">${x.count}</span></li>`).join('');
  set('rules',li(d.top_rules));
  set('tips',li((d.top_tips&&d.top_tips.length)?d.top_tips:d.top_techniques));
  const feed=d.recent||[];
  set('feed',!feed.length?'<div class="empty">Waiting for reports…</div>'
    :feed.map(e=>{const m=SRC[e.source]||[e.source,'var(--muted)'];let w;
      if(e.kind==='cache')w=`<span class="green">cache hit</span> ${e.layer==='semantic'?`(semantic ${e.similarity})`:'(exact)'} · saved a call`;
      else if(e.kind==='route')w=`routed <b>${esc(e.intent||'?')}</b> → <b class="accent">${esc(e.to_model)}</b>`;
      else if(e.kind==='out')w=`reply${e.concise?' <span class="green">kept short</span>':''} · ${e.out_tokens} tok${e.model?` · <b>${esc(e.model)}</b>`:''}`;
      else w=(e.saved>0?`<span class="green">saved ${e.saved} tokens</span>`:'no change');
      const h=e.host&&e.host!=='—'?`<span class="hp mono">${esc(e.host)}</span>`:'';
      return `<div><span class="ts">${e.t}</span><span class="src" style="color:${m[1]}">${m[0]}</span>${h}<span>${w}</span></div>`}).join(''));
}

// ── settings (persisted in localStorage) ──
const TZS=['auto','UTC','America/Toronto','America/New_York','America/Chicago','America/Los_Angeles',
  'Europe/London','Europe/Paris','Europe/Berlin','Asia/Dubai','Asia/Kolkata','Asia/Singapore','Asia/Tokyo','Australia/Sydney'];
const LS=(k,d)=>localStorage.getItem('iq_'+k)||d;
let REF=+LS('refresh','5'), TIMER=null;
const startTimer=()=>{if(TIMER)clearInterval(TIMER);TIMER=setInterval(tick,REF*1000)};
const applyTheme=t=>{const sys=matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';
  document.documentElement.dataset.theme=(t==='system'?sys:t)};
function initSettings(){
  const r=$('set-refresh'),th=$('set-theme'),tz=$('set-tz');
  tz.innerHTML=TZS.map(z=>`<option value="${z}">${z==='auto'?'Auto (detect via IP)':z}</option>`).join('');
  r.value=LS('refresh','5');th.value=LS('theme','system');tz.value=LS('tz','auto');
  applyTheme(th.value);
  $('gear').onclick=()=>{$('settings').hidden=!$('settings').hidden};
  r.onchange=()=>{REF=+r.value;localStorage.setItem('iq_refresh',r.value);startTimer()};
  th.onchange=()=>{localStorage.setItem('iq_theme',th.value);applyTheme(th.value)};
  tz.onchange=async()=>{localStorage.setItem('iq_tz',tz.value);
    try{await iqPost('/api/tz',{headers:{'content-type':'application/json'},body:JSON.stringify({tz:tz.value})})}catch{}};
  $('set-reset').onclick=async(e)=>{const b=e.target;
    if(!confirm('Reset ALL counters across every source and machine? This cannot be undone.'))return;
    let ok=false;try{ok=(await iqPost('/api/reset')).ok}catch{}
    b.textContent=ok?'✓ Reset':'✗ auth';b.classList.toggle('done',ok);tick();
    setTimeout(()=>{b.textContent='↺ Reset all';b.classList.remove('done')},2000)};
  matchMedia('(prefers-color-scheme: light)').addEventListener('change',()=>{if(th.value==='system')applyTheme('system')});
  if(tz.value&&tz.value!=='auto')tz.onchange();   // re-apply saved tz so the server skips the IP check
}
initSettings();
tick();startTimer();
</script></body></html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8088, log_level="warning")
