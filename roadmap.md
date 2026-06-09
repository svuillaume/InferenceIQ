# InferenceIQ — Roadmap

Token-optimization techniques, their status, expected savings, and — critically — **which ones
require the proxy** (and therefore an **Anthropic API key**).

## ⚠️ Proxy vs API key (read first)

The proxy (`intercept.py`, :8082) only sees traffic when the client respects `ANTHROPIC_BASE_URL`.
**A Claude Pro/Max subscription logs in via OAuth and ignores `ANTHROPIC_BASE_URL`** — so its traffic
never reaches the proxy. Routing through the proxy therefore requires **API-key auth**
(`ANTHROPIC_API_KEY`), not a subscription login.

| Surface | Needs proxy? | Needs API key? | Works on Pro/Max OAuth? |
|---|---|---|---|
| **Claude Code hook** (input advice + CONCISE output) | ❌ | ❌ | ✅ yes — the only in-session lever on a subscription |
| **CLI** (`optimize.py`) | ❌ | only for exact counts | ✅ yes |
| **Proxy** (on-the-wire rewrite, cache, routing, `max_tokens`) | ✅ | ✅ **yes** | ❌ no (OAuth bypasses the proxy) |

> **Rule of thumb:** anything that changes bytes *on the wire* (cache hits, model override,
> input rewrite, `max_tokens` cap, on-wire CONCISE) **requires the proxy → requires an API key**.
> Advice-only features (hook context, CLI suggestions) work under any login.

---

## Status legend
✅ Applied · ⚠️ Planned (safe, proxy) · 📋 Advisory only · 🧩 Needs a separate tool-wrapper · ❌ Unsafe in a transparent proxy

## 1. Already applied
| Technique | Surface | Needs proxy/key | Notes |
|---|---|---|---|
| ✅ Output verbosity control (CONCISE) | hook + proxy | hook: no · on-wire: **proxy/key** | Brevity directive; the big lever (output ≈5× input) |
| ✅ Prompt-cache preservation (~90% on cached) | proxy | **proxy/key** | Invariant: never mutate system/tools/history, so Anthropic's cache discount survives |
| ✅ Semantic response cache (exact→vector→LLM) | proxy | **proxy/key** | `semcache.py`, non-agentic only; 50MB store |
| ✅ Intent model routing (Haiku/Sonnet/Opus) | proxy | **proxy/key** | `router.py`; ~30–40% savings (Morph); agentic traffic never routed |
| ✅ Mechanical input trim (filler) | all | hook: advisory · on-wire: **proxy/key** | Small (filler only) |

## 2. Planned — safe to add (proxy + advisory)
| Technique | Expected | Needs proxy/key | Plan |
|---|---|---|---|
| ⚠️ `max_tokens` cap | caps runaway output | **proxy/key** | Opt-in `MAX_TOKENS_CAP`, **non-agentic only** (capping agentic requests could truncate tool turns) |
| ⚠️ Stronger "agent" CONCISE preset | 60–80% output | hook (no key) + proxy | `CONCISE_NOTE` = "data only · PASS/FAIL not full logs · JSON errors only" |
| 📋 Expand advisory tips | — | none (advice) | Add to the hook: AST summaries, search-before-reading, command distillation, byte-range reads, prompt-cache prefix ordering |

## 3. Needs a separate tool-wrapper (NOT the proxy)
These act on `tool_result` (file contents, command output) or the `system` prompt. A transparent
proxy **cannot** touch those without breaking Claude Code's agent loop / prompt cache (the reason
the original `proxy.py` was deleted). They belong in a **tool wrapper / agent harness** the client
calls *before* content enters the context — a future opt-in component, no API key of its own.
| Technique | Expected | Why not the proxy |
|---|---|---|
| 🧩 AST file summaries | 75–95% on file reads | Rewrites file content (arrives as `tool_result`) |
| 🧩 Command-output distillation | ~99% on tool output | Rewrites tool output (`tool_result`) |
| 🧩 Search-before-reading | 40–60% | A `system`-prompt instruction (cached prefix is off-limits) |

## 4. Advisory only (need your data/app)
| Technique | Expected | Notes |
|---|---|---|
| 📋 RAG / retrieve relevant chunks | 60–80% | Advisory; building it needs your corpus |
| 📋 Chunking / byte-range reads | varies | Advisory |
| 📋 Prompt-cache prefix ordering | discounted | Put static context first, dynamic query last |

---

## Sequencing
1. **Now (no key):** keep using the **hook** — input advice + CONCISE output, works on Pro/Max.
2. **Next (safe proxy, needs API key):** `max_tokens` cap + stronger CONCISE preset + expand advisory tips.
3. **Later (separate component):** the AST/distill **tool-wrapper** — the biggest agent-token savings,
   but architecturally separate from the proxy.

> Directional savings %s come from morphllm.com/ai-coding-costs; where InferenceIQ measures real
> numbers it uses **Anthropic's own usage data** (see below), not estimates.

---

## References (source: Anthropic)

We don't re-document third-party technique write-ups here — the authoritative source for the
levers InferenceIQ actually uses or measures is Anthropic's own docs:

- **Token counting** (exact, model-specific): `POST /v1/messages/count_tokens` —
  platform.claude.com/docs/en/build-with-claude/token-counting
- **Prompt caching** (~90% on cached tokens; verify via `usage.cache_read_input_tokens`):
  platform.claude.com/docs/en/build-with-claude/prompt-caching
- **`usage` object** (real billed `input_tokens` / `output_tokens` / `cache_read_input_tokens` /
  `cache_creation_input_tokens`) — returned on every `/v1/messages` response.
- **Context editing & compaction** (server-side context reduction):
  platform.claude.com/docs/en/build-with-claude/context-editing · …/compaction
- **Effort & `max_tokens`** (output control): …/effort · the Messages API `max_tokens` parameter
- **Tool search** (load only relevant tool schemas):
  platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool

The agent-side techniques not covered above (AST summaries, command distillation, search-before-
reading) are external/tool-wrapper concerns — kept as one-line pointers in §3, not re-documented.
