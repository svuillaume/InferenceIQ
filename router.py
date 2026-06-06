"""
router — deterministic intent → model routing (Haiku / Sonnet / Opus)

Maps a user's request to the smallest capable Claude model, using fast, offline,
keyword + length heuristics (no API call, no LLM on the hot path). Shared by the proxy
(and usable from the CLI). Tiers:

  Haiku  — simple, repetitive: classification, summaries, translation, extraction, routing.
  Sonnet — balanced default workhorse: coding, analysis, writing, general assistants, RAG.
  Opus   — most capable reasoning: complex debugging, architecture, deep multi-step reasoning.

Bias: when intent is unclear, default to **Sonnet** (the safe middle), never silently to Haiku.
"""

import re

# Exact, current model ids (see env: Opus 4.8 / Sonnet 4.6 / Haiku 4.5).
MODELS = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-8",
}

# Complex-reasoning / architecture / debugging → Opus.
_OPUS = re.compile(r"\b("
    r"debug|root cause|race condition|deadlock|concurren\w+|architect\w*|system design|"
    r"design (a|the|an)|refactor|trade[- ]?offs?|prove|proof|trace through|step by step|"
    r"why (does|is|are|did)|optimi[sz]e|bottleneck|security review|vulnerab\w+|exploit|"
    r"distributed|scalab\w+|algorithm|complexity|reason about|migrat\w+|plan (a|the) "
    r")\b", re.I)

# Coding / build signals → at least Sonnet (the coding workhorse). Checked BEFORE Haiku so a
# short "write a function…" is never downgraded just because it also contains a simple verb.
_CODE = re.compile(r"\b("
    r"write (a |an )?(function|method|class|script|test|endpoint|api|component|module)|"
    r"implement|function|method\b|class\b|endpoint|api\b|unit test|"
    r"compile|stack ?trace|build (a|an|the)|code\b"
    r")\b", re.I)

# Simple / repetitive / mechanical → Haiku. Deliberately narrow (no "parse"/"format" — those
# show up in coding requests too) so we don't downgrade real work.
_HAIKU = re.compile(r"\b("
    r"classif\w+|categor\w+|summar\w+|tl;?dr|translate|"
    r"rename|spell\w*|grammar|fix (the )?typo|bullet points?|list (out|the)|"
    r"what is|who is|define|definition|yes or no|true or false|look ?up"
    r")\b", re.I)


def classify_intent(text: str):
    """Return (tier, reason). Intent keywords win; length only escalates up to Opus."""
    t = (text or "").strip()
    if not t:
        return "sonnet", "empty → default workhorse"
    if _OPUS.search(t):
        return "opus", "complex reasoning / debugging / architecture"
    if _CODE.search(t):
        return "sonnet", "coding / build task (workhorse)"
    if _HAIKU.search(t):
        return "haiku", "simple / repetitive (classify, summarize, translate…)"
    # Length only escalates UP — long prose tends to be complex. We never downgrade to Haiku
    # on length alone (a short "fix the bug" is still a coding task → Sonnet), per the bias.
    if len(t) > 600:
        return "opus", "long / likely complex request"
    return "sonnet", "balanced default workhorse"


def route(text: str):
    """Return (model_id, tier, reason) for the given request text."""
    tier, reason = classify_intent(text)
    return MODELS[tier], tier, reason
