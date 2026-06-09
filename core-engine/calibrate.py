#!/usr/bin/env python3
"""
calibrate — a true SAME-PROMPT brevity gauge.

The dashboard's reply-savings are normally a population average (avg concise reply vs avg
normal reply). This tool removes that guesswork: it sends each gauge prompt to the API
TWICE — once WITHOUT the brevity directive (CONCISE=0) and once WITH it (CONCISE=1) — reads
each reply's REAL `output_tokens` from Anthropic's usage, and reports both as `out` events.
So `normal_avg` vs `concise_avg` becomes a real, same-prompt comparison.

Needs ANTHROPIC_API_KEY. Always hits api.anthropic.com DIRECTLY (never the proxy), so the
two calls differ only by the brevity directive.

  ./core-engine/calibrate.py                  # one pass over the built-in gauge prompts
  ./core-engine/calibrate.py --every 60       # gauge regularly: repeat every 60 minutes
  CALIBRATE_MODEL=claude-sonnet-4-6 ./core-engine/calibrate.py
  INFERENCEIQ_DASHBOARD=http://3.96.147.26:8088 ./core-engine/calibrate.py

Env: CALIBRATE_MODEL (default claude-haiku-4-5) · CALIBRATE_MAX_TOKENS (default 1024) ·
     CONCISE_NOTE (override the directive) · INFERENCEIQ_DASHBOARD / IQ_TOKEN (where to report).
"""
import os, sys, time, random, json as _json, urllib.request as _u

# Reuse the one reporting config (dashboard URL, token, host/user) from the shared core.
from optimize import _dashboard_url, _token, HOST, USER

MODEL = os.getenv("CALIBRATE_MODEL", "claude-haiku-4-5")
MAXTOK = int(os.getenv("CALIBRATE_MAX_TOKENS", "1024"))
# How many prompts to randomly pick PER pass (not the whole set every time).
SAMPLE = int(os.getenv("CALIBRATE_SAMPLE", "2"))
NOTE = os.getenv("CONCISE_NOTE") or (
    "Be brief. Lead with the direct answer in a few short sentences. "
    "Omit preamble, background, caveats, and closing summaries unless explicitly asked.")

# A small, representative gauge set — explanatory questions where brevity has room to act.
GAUGE = [
    "Explain how a hash map works.",
    "What are the tradeoffs between REST and GraphQL?",
    "Summarize the main causes of World War I.",
    "How does TCP differ from UDP?",
    "What is dependency injection and why would you use it?",
    "Describe how HTTPS keeps a request secure.",
]


def _report_out(out_tokens: int, concise: bool, in_tokens: int, model: str):
    """POST one `out` event (real output tokens, tagged concise on/off) to the collector."""
    url = _dashboard_url()
    if not url or url.lower() == "off":
        return
    try:
        body = _json.dumps({
            "kind": "out", "source": "calibrate", "out_tokens": int(out_tokens),
            "concise": bool(concise), "in_tokens": int(in_tokens), "model": model,
            "host": HOST, "user": USER,
        }).encode()
        h = {"content-type": "application/json"}
        tok = _token()
        if tok:
            h["X-IQ-Token"] = tok
        _u.urlopen(_u.Request(f"{url}/api/record", data=body, headers=h, method="POST"),
                   timeout=3).read()
    except Exception:
        pass   # never let reporting break the gauge


def _ask(client, prompt: str, brevity: bool):
    """One API call; returns (output_tokens, input_tokens) from the REAL usage object."""
    content = prompt + ("\n\n" + NOTE if brevity else "")
    r = client.messages.create(model=MODEL, max_tokens=MAXTOK,
                               messages=[{"role": "user", "content": content}])
    return r.usage.output_tokens, r.usage.input_tokens


def run_once() -> int:
    from anthropic import Anthropic
    client = Anthropic(base_url="https://api.anthropic.com")   # pin direct — never the proxy
    picks = random.sample(GAUGE, min(max(1, SAMPLE), len(GAUGE)))   # random subset, not all
    n, saved, off_tot = 0, 0, 0
    for p in picks:
        try:
            off_ot, off_it = _ask(client, p, False)   # CONCISE=0 — the baseline
            _report_out(off_ot, False, off_it, MODEL)
            on_ot, on_it = _ask(client, p, True)      # CONCISE=1 — brevity on
            _report_out(on_ot, True, on_it, MODEL)
            d = max(0, off_ot - on_ot)
            n += 1; saved += d; off_tot += off_ot
            print(f"  {p[:42]:42}  off={off_ot:4}  on={on_ot:4}  saved={d:4}")
        except Exception as e:
            print(f"  ! {p[:42]}: {e}", file=sys.stderr)
    pct = round(saved / off_tot * 100) if off_tot else 0
    print(f"calibrate: {n} random prompt(s) · ~{saved} output tokens saved ({pct}% shorter) "
          f"· model {MODEL} · → {_dashboard_url()}")
    return saved


def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("calibrate: set ANTHROPIC_API_KEY (calls the API directly).", file=sys.stderr)
        sys.exit(1)
    every = 0.0
    if "--every" in sys.argv:
        try:
            every = float(sys.argv[sys.argv.index("--every") + 1])
        except Exception:
            print("calibrate: --every needs a number of MINUTES, e.g. --every 60", file=sys.stderr)
            sys.exit(1)
    while True:
        run_once()
        if every <= 0:
            break
        print(f"calibrate: sleeping {every} min…")
        time.sleep(every * 60)


if __name__ == "__main__":
    main()
