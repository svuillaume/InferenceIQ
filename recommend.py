"""
recommend — Claude-powered "best-practice" prompt rewriter

Takes a user's prompt and rewrites it to elicit a higher-quality, more accurate
response from Claude *while staying at least as token-efficient*. Applies prompt-
engineering best practices (clear & direct, explicit scope, light structure),
balanced compression (abbreviations/symbols/key:value — never cryptic), and a
per-prompt token-optimization analysis. Explains everything in plain English and
suggests the smallest capable model (routing small→large).

Complements optimize.py (offline mechanical shortening). Uses the official Anthropic
SDK on Claude Opus 4.8, pinned to api.anthropic.com (never a local proxy).

CLI:
  ANTHROPIC_API_KEY=sk-ant-... ./recommend.py "your prompt"
  ANTHROPIC_API_KEY=sk-ant-... ./recommend.py --copy "your prompt"   # copy rewrite to clipboard
"""

import os, sys, json, subprocess

MODEL = "claude-opus-4-8"
ANTHROPIC_DIRECT = "https://api.anthropic.com"

SYSTEM = """You are an expert prompt engineer. Rewrite the user's message into a prompt \
that elicits a higher-quality, more accurate, more intelligent response from Claude while \
using no more tokens than necessary. Return: the rewritten prompt, the techniques applied, \
token-saving tips that fit THIS prompt, and the smallest capable model.

CLARITY & STRUCTURE (apply where they help; never pad):
- Lead with a clear, direct imperative stating the task and the desired outcome.
- Make scope explicit when implied ("every X", not just "X").
- Keep only essential context; cut filler, hedging, and pleasantries.
- Use light XML tags (<task>, <context>, <constraints>, <output_format>) ONLY when the \
prompt mixes several kinds of content; never wrap a one-line request in tags.
- Specify output format/length only when it matters; add a role only if it focuses the answer.

COMPRESSION (only as a clear net win, never at the cost of clarity):
- Well-understood abbreviations (server→srv, database→db, config), key:value lines over \
sentences, keywords over prose, compact symbols (→ = | [ ]), or a small consistent shorthand \
for repetitive structured prompts. Never produce cryptic "SMS-style" text. \
Target: structured + short + READABLE.

HARD RULES:
- Never invent requirements, facts, examples, or constraints the user didn't give. No multishot \
examples unless the user supplied them. Preserve the user's intent exactly.
- The rewrite must be at least as token-efficient as the original unless added structure clearly \
buys a better answer; if you add tokens, justify it in `rationale`.

For `token_tips`, list ONLY the strategies that genuinely apply to THIS prompt (skip the rest): \
RAG / retrieve only relevant snippets · chunk large documents · summarize chat history instead of \
resending it · cache static or repeated context · compress structured data · split complex \
workflows into steps · cap response length · use tools/APIs for math and lookups instead of \
reasoning over raw data · send only the context the task needs.

For `suggested_model`, pick the smallest model that does this well (routing small→large): \
"claude-haiku-4-5" for simple lookups/classification/short edits, "claude-sonnet-4-6" for moderate \
tasks, "claude-opus-4-8" for complex reasoning, agentic, or long-horizon work.

Write `techniques`, `token_tips`, and `rationale` in PLAIN ENGLISH a non-technical reader can \
follow — name the idea, then briefly say what it means and why it helps; define any term \
(e.g. "RAG (pull in only the relevant snippets, not the whole document)")."""

SCHEMA = {
    "type": "object",
    "properties": {
        "rewritten": {"type": "string"},
        "techniques": {"type": "array", "items": {"type": "string"}},
        "token_tips": {"type": "array", "items": {"type": "string"}},
        "suggested_model": {"type": "string",
                            "enum": ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8"]},
        "rationale": {"type": "string"},
    },
    "required": ["rewritten", "techniques", "token_tips", "suggested_model", "rationale"],
    "additionalProperties": False,
}


def recommend(text: str) -> dict:
    """Return {rewritten, techniques[], token_tips[], suggested_model, rationale} — or {error}."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {"error": "ANTHROPIC_API_KEY not set — needed for the best-practice rewrite."}
    try:
        import anthropic
    except ImportError:
        return {"error": "anthropic SDK not installed (pip install anthropic)."}
    try:
        # Pin to the real API — ignore any ANTHROPIC_BASE_URL pointing at a local proxy.
        client = anthropic.Anthropic(base_url=ANTHROPIC_DIRECT)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM,
            messages=[{"role": "user", "content": text}],
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        raw = next(b.text for b in resp.content if b.type == "text")
        return json.loads(raw)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def main(argv):
    copy = "--copy" in argv
    argv = [a for a in argv if a != "--copy"]
    text = " ".join(argv).strip() if argv else sys.stdin.read().strip()
    if not text:
        print('usage: recommend.py "your prompt"  (needs ANTHROPIC_API_KEY)', file=sys.stderr)
        return 2
    r = recommend(text)
    if "error" in r:
        print(f"error: {r['error']}", file=sys.stderr)
        return 1
    delta = 0
    try:
        from optimize import count_tokens
        (ot, nt), exact = count_tokens([text, r["rewritten"]])
        unit = "tokens" if exact else "tokens (est)"
        delta = ot - nt
        sign = f"−{delta}" if delta > 0 else (f"+{-delta}" if delta < 0 else "±0")
        head = f"{ot} → {nt} {unit}  ({sign})"
    except Exception:
        head = ""

    print(f"\n┌ ORIGINAL\n│ {text}")
    print(f"├ RECOMMENDED   {head}\n│ " + r["rewritten"].replace("\n", "\n│ "))
    print("└" + "─" * 60)
    if copy:
        try:
            subprocess.run(["pbcopy"], input=r["rewritten"].encode(), check=True)
            print("\n📋 Rewritten prompt copied to clipboard — paste it into Claude (⌘V).")
        except Exception:
            pass
    if r.get("techniques"):
        print("\ntechniques applied (plain English):")
        for t in r["techniques"]:
            print(f"  • {t}")
    if r.get("token_tips"):
        print("\ntoken-optimization tips for this prompt:")
        for t in r["token_tips"]:
            print(f"  • {t}")
    if r.get("suggested_model"):
        print(f"\nsuggested model (routing small→large): {r['suggested_model']}")
    if r.get("rationale"):
        print(f"\nrationale: {r['rationale']}")

    from optimize import report   # → unified dashboard
    report("rec", delta, techniques=r.get("techniques"), tips=r.get("token_tips"),
           before=text, after=r["rewritten"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
