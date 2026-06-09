"""
intercept — optimizing proxy for Claude Code

Sits between Claude Code and api.anthropic.com. On each /v1/messages request it
optimizes ONLY the final user turn (the newly typed prose) using the same
mechanical, meaning-preserving ruleset as optimize.py, then forwards everything
else BYTE-FOR-BYTE to Anthropic. Streaming responses pass straight through.

Deliberately safe — it never:
  • touches the cached prefix (system / tools / conversation history / tool_results)
    → your prompt cache stays intact (mutating it would cost far more than it saves)
  • synthesizes or caches responses → never strips tool_use, never fakes a turn
  • drops messages → never orphans a tool_result from its tool_use

Run:
  export ANTHROPIC_BASE_URL=http://localhost:8082    # point Claude Code here
  uvicorn intercept:app --host 0.0.0.0 --port 8082
  # dashboard: http://localhost:8082/dashboard
"""

import os, re, json, time, asyncio, sys
from fastapi import FastAPI, Request
from fastapi.responses import (StreamingResponse, JSONResponse,
                               Response, RedirectResponse)
import httpx

# The engine modules (optimize/router/semcache) live in ../engines. Add it to the path so the
# proxy resolves them whether run from the repo, a container, or anywhere else.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engines"))

from optimize import optimize, est, HOST, USER
from router import route

ANTHROPIC = os.getenv("ANTHROPIC_UPSTREAM", "https://api.anthropic.com")   # override for tests/self-host
ENABLED = os.getenv("OPTIMIZE_ENABLED", "1") != "0"
# Intent-based model routing (Haiku/Sonnet/Opus). on (default, override) | advise (report only) | off.
# SAFE: never routes an agentic request (tools present or a tool_result turn) — the agent
# loop keeps its requested model. Mainly benefits plain single-turn API clients.
ROUTE = os.getenv("ROUTE_MODELS", "on").lower()
# Privacy: only report counts + host by default; opt in to send prompt text with IQ_REPORT_TEXT=1.
REPORT_TEXT = os.getenv("IQ_REPORT_TEXT", "0") == "1"
# Shared token for a token-protected (public/cloud) collector; sent as X-IQ-Token. Empty = none.
IQ_TOKEN = os.getenv("IQ_TOKEN", "")
# Semantic cache (3-layer). On by default; non-agentic, text-only traffic only (see semcache.py).
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "1") != "0"
try:
    from semcache import SemanticCache
    CACHE = SemanticCache() if CACHE_ENABLED else None
except Exception:
    CACHE = None   # numpy/fastembed missing or failed → proxy runs as a plain pass-through
COUNT_MODE = os.getenv("COUNT_MODE", "estimate")   # "estimate" (instant) | "exact" (count_tokens)
# Output-side savings (opt-in, OFF by default): append a brevity nudge to the LAST
# user turn only — cache-safe (never touches system/tools) and skips tool_result turns.
CONCISE = os.getenv("CONCISE", "0") == "1"
# The output lever. Default is a STRONG brevity directive — it reliably cuts replies
# (lead with the answer; drop preamble, background, caveats, summaries). Tune the
# aggressiveness with CONCISE_NOTE; softer wording saves less, harder wording saves
# more but trims more substance.
CONCISE_NOTE = os.getenv("CONCISE_NOTE") or (
    "Be brief. Lead with the direct answer in a few short sentences. "
    "Omit preamble, background, caveats, and closing summaries unless explicitly asked.")
# The ONE dashboard (standalone collector). INFERENCEIQ_DASHBOARD = where this proxy reports
# events (internal, e.g. http://dashboard:8088 in docker, or a remote host's URL).
# DASHBOARD_PUBLIC_URL = where a browser hitting /dashboard is redirected.
DASHBOARD_INTERNAL = os.getenv("INFERENCEIQ_DASHBOARD", "http://3.96.147.26:8088").rstrip("/")
DASHBOARD_PUBLIC = os.getenv("DASHBOARD_PUBLIC_URL", "http://3.96.147.26:8088").rstrip("/")
# Hop-by-hop / body headers we must not forward (httpx recomputes length; body changed).
_SKIP = {"host", "content-length", "accept-encoding", "connection"}

app = FastAPI()

stats = {"requests": 0, "optimized": 0, "tokens_saved": 0, "concise": 0, "routed": 0,
         "cache_hits": 0, "mode": COUNT_MODE, "log": []}
_BG: set = set()   # hold refs to background exact-count tasks so they aren't GC'd


def _last_user_index(msgs) -> int:
    """Index of the LAST user message in the list, or -1. Shared by the two
    last-turn transforms below so the backward walk lives in one place."""
    if not isinstance(msgs, list):
        return -1
    for i in range(len(msgs) - 1, -1, -1):
        if isinstance(msgs[i], dict) and msgs[i].get("role") == "user":
            return i
    return -1


def _last_user_text(body: dict):
    """(text, is_agentic) for the last user turn. is_agentic=True when it's a tool_result
    turn — used to keep agentic requests on their requested model."""
    msgs = body.get("messages")
    i = _last_user_index(msgs)
    if i < 0:
        return None, False
    c = msgs[i].get("content")
    if isinstance(c, str):
        return c, False
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None, True
        return " ".join(b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text"), False
    return None, False


def optimize_last_user_turn(body: dict):
    """
    Mutate body in place: optimize the text of the LAST user message only, and only
    if it's genuine typed prose (not a tool_result turn). Returns (saved_est, before, after).
    """
    msgs = body.get("messages")
    i = _last_user_index(msgs)
    if i < 0:
        return 0, "", ""
    m = msgs[i]
    c = m.get("content")

    # Plain string user turn
    if isinstance(c, str):
        new, _ = optimize(c)
        if new != c:
            msgs[i] = {**m, "content": new}
            return est(c) - est(new), c, new
        return 0, "", ""

    # Block-list user turn — optimize text blocks, but skip the whole turn if it
    # carries a tool_result (that's an agent-loop turn, not user prose).
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return 0, "", ""
        saved, before, after, changed = 0, "", "", False
        blocks = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                no, _ = optimize(b["text"])
                if no != b["text"]:
                    saved += est(b["text"]) - est(no)
                    before, after, changed = b["text"], no, True
                    b = {**b, "text": no}
            blocks.append(b)
        if changed:
            msgs[i] = {**m, "content": blocks}
            return saved, before, after
        return 0, "", ""

    return 0, "", ""


def add_concise_directive(body: dict) -> bool:
    """
    Output-side savings: append CONCISE_NOTE to the LAST user turn only. Cache-safe
    (never touches system/tools, so the prompt cache is untouched) and skips
    tool_result turns (agent-loop turns, not typed prose). Returns True if applied.
    """
    if not CONCISE:
        return False
    msgs = body.get("messages")
    i = _last_user_index(msgs)
    if i < 0:
        return False
    m = msgs[i]
    c = m.get("content")
    if isinstance(c, str):
        if CONCISE_NOTE in c:
            return False
        msgs[i] = {**m, "content": c.rstrip() + "\n\n" + CONCISE_NOTE}
        return True
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return False
        if any(isinstance(b, dict) and b.get("type") == "text"
               and CONCISE_NOTE in (b.get("text") or "") for b in c):
            return False
        msgs[i] = {**m, "content": list(c) + [{"type": "text", "text": CONCISE_NOTE}]}
        return True
    return False


def fwd_headers(request: Request) -> dict:
    return {k: v for k, v in request.headers.items() if k.lower() not in _SKIP}


def log(saved: int, before: str, after: str):
    stats["log"].append({
        "t": time.strftime("%H:%M:%S"), "saved": saved,
        "before": (before[:80] + "…") if len(before) > 80 else before,
        "after": (after[:80] + "…") if len(after) > 80 else after,
    })
    if len(stats["log"]) > 50:
        stats["log"].pop(0)


def fire(coro):
    t = asyncio.create_task(coro)
    _BG.add(t)
    t.add_done_callback(_BG.discard)


async def _post_record(payload: dict):
    if not DASHBOARD_INTERNAL or DASHBOARD_INTERNAL.lower() == "off":
        return
    payload = {**payload, "host": HOST, "user": USER}   # attribute every event to this machine
    headers = {"X-IQ-Token": IQ_TOKEN} if IQ_TOKEN else None   # for a token-protected collector
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            await c.post(f"{DASHBOARD_INTERNAL}/api/record", json=payload, headers=headers)
    except Exception:
        pass   # dashboard down — proxying must never be affected


async def report_central(saved: int, before: str, after: str):
    """Best-effort: feed this optimized (input) turn into the one dashboard (collector)."""
    if not REPORT_TEXT:
        before = after = ""   # privacy default: counts only, never prompt text off-box
    await _post_record({"kind": "opt", "source": "proxy", "saved": max(0, saved),
                        "before": before, "after": after})


async def report_route(frm: str, to: str, intent: str, applied: bool):
    """Report a routing decision (intent → model) to the dashboard."""
    await _post_record({"kind": "route", "source": "proxy",
                        "from_model": frm, "to_model": to,
                        "intent": intent, "applied": bool(applied)})


async def report_cache(layer: str, similarity: float, model: str):
    """Report a cache HIT (exact/semantic) — one LLM call avoided."""
    await _post_record({"kind": "cache", "source": "proxy", "layer": layer,
                        "similarity": round(float(similarity), 3), "model": model or ""})


async def report_cachestat():
    """Report the cache gauges (entries, size, hit-rate) so the dashboard can show the store."""
    if CACHE is None:
        return
    g = CACHE.gauges()
    await _post_record({"kind": "cachestat", "source": "proxy", **g})


# ── Cache helpers ────────────────────────────────────────────────────────────────
def _system_text(body: dict) -> str:
    """The system prompt as a string (it may be a string or a list of text blocks)."""
    s = body.get("system")
    if isinstance(s, str):
        return s
    if isinstance(s, list):
        return " ".join(b.get("text", "") for b in s if isinstance(b, dict))
    return ""


def _cacheable_response(data: dict) -> bool:
    """True only for a PURE-TEXT reply (no tool_use). Never cache an agentic/tool turn."""
    if data.get("stop_reason") not in ("end_turn", "stop_sequence"):
        return False
    content = data.get("content")
    if not isinstance(content, list) or not content:
        return False
    return all(isinstance(b, dict) and b.get("type") == "text" for b in content)


def _text_of(data: dict) -> str:
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if isinstance(b, dict) and b.get("type") == "text")


def _synth_json(text: str, model: str) -> dict:
    return {"id": "msg_cache_" + str(int(time.time() * 1000)), "type": "message",
            "role": "assistant", "model": model or "cache",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": est(text)}}


def _synth_sse(text: str, model: str):
    """Replay a cached answer as a valid Anthropic text SSE stream (no tool_use ever)."""
    mid = "msg_cache_" + str(int(time.time() * 1000))
    def ev(t, d):
        return f"event: {t}\ndata: {json.dumps(d)}\n\n".encode()
    yield ev("message_start", {"type": "message_start", "message": {
        "id": mid, "type": "message", "role": "assistant", "model": model or "cache",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0}}})
    yield ev("content_block_start", {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}})
    yield ev("content_block_delta", {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": text}})
    yield ev("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield ev("message_delta", {"type": "message_delta",
             "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": est(text)}})
    yield ev("message_stop", {"type": "message_stop"})


async def report_output(out_tokens: int, concise: bool, model: str = "",
                        in_tokens: int = 0, cache_read: int = 0, cache_creation: int = 0,
                        routed_from: str = ""):
    """Report Anthropic's REAL usage for this reply: output tokens (reply-length savings),
    the model used, and the prompt-cache fields (cache_read = served at ~0.1×, the real
    cache saving; cache_creation = written at ~1.25×; input_tokens = uncached full-price).
    `routed_from` carries the originally-requested model when routing downgraded this reply,
    so the dashboard can price the routing saving against the real token counts."""
    await _post_record({"kind": "out", "source": "proxy",
                        "out_tokens": max(0, int(out_tokens or 0)),
                        "concise": bool(concise), "model": model or "",
                        "in_tokens": max(0, int(in_tokens or 0)),
                        "cache_read": max(0, int(cache_read or 0)),
                        "cache_creation": max(0, int(cache_creation or 0)),
                        "routed_from": routed_from or ""})


async def exact_savings(before: str, after: str, api_key: str, version: str, model: str):
    """Background: exact tokens saved via count_tokens (uses the caller's key). Est fallback."""
    h = {"x-api-key": api_key, "anthropic-version": version, "content-type": "application/json"}
    def payload(t):
        return {"model": model, "messages": [{"role": "user", "content": t or " "}]}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            rb = await c.post(f"{ANTHROPIC}/v1/messages/count_tokens", headers=h, json=payload(before))
            ra = await c.post(f"{ANTHROPIC}/v1/messages/count_tokens", headers=h, json=payload(after))
            rb.raise_for_status(); ra.raise_for_status()
            stats["tokens_saved"] += max(0, rb.json()["input_tokens"] - ra.json()["input_tokens"])
    except Exception:
        stats["tokens_saved"] += max(0, est(before) - est(after))   # fall back to estimate


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    stats["requests"] += 1

    saved, before, after = (0, "", "")
    if ENABLED:
        saved, before, after = optimize_last_user_turn(body)
        if after and after != before:
            stats["optimized"] += 1
            log(saved, before, after)
            fire(report_central(saved, before, after))   # → unified dashboard
            key = request.headers.get("x-api-key")
            if COUNT_MODE == "exact" and key:
                fire(exact_savings(before, after, key,
                                   request.headers.get("anthropic-version", "2023-06-01"),
                                   body.get("model", "claude-opus-4-8")))
            else:
                stats["tokens_saved"] += max(0, saved)   # instant estimate

    # ── Semantic cache lookup (before concise/routing so the key is clean user prose).
    # SAFE: tools present or a tool_result turn always bypasses the cache entirely.
    cache_ctx = None
    if CACHE is not None and not body.get("tools"):
        ctext, agentic = _last_user_text(body)
        if ctext and not agentic:
            sys_text = _system_text(body)
            model_id = body.get("model", "")
            hit = CACHE.lookup(ctext, sys_text, model_id)
            if hit:
                text, layer, sim = hit
                stats["cache_hits"] += 1
                fire(report_cache(layer, sim, model_id))
                fire(report_cachestat())
                if body.get("stream"):
                    return StreamingResponse(_synth_sse(text, model_id),
                                             media_type="text/event-stream")
                return JSONResponse(_synth_json(text, model_id))
            cache_ctx = (ctext, sys_text, model_id)   # remember for store-on-miss

    concise = add_concise_directive(body)   # output-side nudge (opt-in); cache-safe
    if concise:
        stats["concise"] += 1

    # Intent-based model routing (opt-in). Skip any agentic request (tools present or a
    # tool_result turn) so Claude Code's tool loop always keeps its requested model.
    routed_from = ""   # original model, set only when routing actually downgrades this reply
    if ROUTE in ("advise", "on"):
        text, agentic = _last_user_text(body)
        if text and not agentic and not body.get("tools"):
            requested = body.get("model", "")
            model, tier, reason = route(text)
            if model != requested:
                if ROUTE == "on":
                    body["model"] = model
                    routed_from = requested   # reply is served by the cheaper model → price the delta
                stats["routed"] += 1
                intent = reason.split(" /")[0].split(" (")[0].strip()   # short human label
                fire(report_route(requested, model, intent, ROUTE == "on"))

    headers = fwd_headers(request)

    if body.get("stream"):
        async def gen():
            last, acc, tool_seen, pending = 0, [], False, ""
            in_tok, cache_read, cache_creation = 0, 0, 0
            async with httpx.AsyncClient(timeout=600) as client:
                async with client.stream("POST", f"{ANTHROPIC}/v1/messages",
                                         headers=headers, json=body) as r:
                    # aiter_bytes() decompresses; aiter_raw() would forward gzip bytes
                    # without a content-encoding header and corrupt the SSE stream.
                    async for chunk in r.aiter_bytes():
                        yield chunk
                        # Parse SSE lines to (a) scrape the real output-token count, (b) accumulate
                        # text for the cache, and (c) detect tool_use so we NEVER cache a tool turn.
                        pending += chunk.decode("utf-8", "ignore")
                        lines = pending.split("\n")
                        pending = lines.pop()   # keep the last (possibly partial) line
                        for line in lines:
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            try:
                                d = json.loads(line[5:].strip())
                            except Exception:
                                continue
                            t = d.get("type")
                            if t == "content_block_start" and \
                               d.get("content_block", {}).get("type") == "tool_use":
                                tool_seen = True
                            elif t == "content_block_delta" and \
                                 d.get("delta", {}).get("type") == "text_delta":
                                acc.append(d["delta"].get("text", ""))
                            elif t == "message_start":
                                u = (d.get("message", {}).get("usage") or {})
                                in_tok = int(u.get("input_tokens", 0) or 0)
                                cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
                                cache_creation = int(u.get("cache_creation_input_tokens", 0) or 0)
                                if "output_tokens" in u:
                                    last = int(u["output_tokens"])
                            elif t == "message_delta":
                                u = d.get("usage") or {}
                                if "output_tokens" in u:
                                    last = int(u["output_tokens"])
            if last:
                fire(report_output(last, concise, body.get("model", ""),
                                   in_tok, cache_read, cache_creation, routed_from))
            # Store on miss — only a pure-text reply (no tool_use) for a non-agentic request.
            if cache_ctx and not tool_seen and acc:
                CACHE.store(cache_ctx[0], cache_ctx[1], cache_ctx[2], "".join(acc))
                fire(report_cachestat())
        return StreamingResponse(gen(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.post(f"{ANTHROPIC}/v1/messages", headers=headers, json=body)
    try:
        data = r.json()
        u = data.get("usage") or {}
        out = u.get("output_tokens", 0)
        if out:
            fire(report_output(out, concise, data.get("model") or body.get("model", ""),
                               u.get("input_tokens", 0), u.get("cache_read_input_tokens", 0),
                               u.get("cache_creation_input_tokens", 0), routed_from))
        if cache_ctx and _cacheable_response(data):   # store only pure-text replies
            CACHE.store(cache_ctx[0], cache_ctx[1], cache_ctx[2], _text_of(data))
            fire(report_cachestat())
        return JSONResponse(data, status_code=r.status_code)
    except Exception:
        return Response(r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type"))


@app.get("/stats")
async def get_stats():
    return JSONResponse(stats)


@app.get("/dashboard")
@app.get("/")
async def dashboard():
    # One dashboard for everything — send viewers to the unified view on :8088.
    return RedirectResponse(DASHBOARD_PUBLIC, status_code=307)


# Catch-all passthrough MUST be defined last, or it shadows the routes above.
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def passthrough(request: Request, path: str):
    """Everything else (count_tokens, models, …) forwarded verbatim — no mutation."""
    body = await request.body()
    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.request(request.method, f"{ANTHROPIC}/{path}",
                                 headers=fwd_headers(request), content=body)
    return Response(r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type"))
