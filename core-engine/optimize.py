#!/usr/bin/env python3
"""
optimize — before/after prompt text compressor

Shortens chatty text so it tokenizes more efficiently WITHOUT changing meaning.
Mechanical only: deterministic regex rules, no API call to transform, no LLM rewrite.
Token counts are exact (Anthropic /v1/messages/count_tokens), hitting api.anthropic.com
directly — it never routes through any local proxy.

Usage:
  ./optimize.py "your text here"
  echo "your text" | ./optimize.py
  ./optimize.py --copy "your text"           # copy optimized text to clipboard (macOS)
  ./optimize.py --batch prompts.txt          # optimize many prompts, report total savings
  ./optimize.py --batch prompts.txt --out optimized.txt   # also write the results
  ./optimize.py --batch -                     # read the batch from stdin
  ANTHROPIC_API_KEY=sk-ant-... ./optimize.py "..."   # for exact token counts

  ./optimize.py --set-dashboard https://host:8088    # point this machine at a collector
  ./optimize.py --set-dashboard https://host --set-token SECRET   # protected collector
  ./optimize.py --show-config                 # print the resolved dashboard/token config
  #   writes ~/.inferenceiq.json (or $IQ_CONFIG); used by the Claude Code plugin install.

Batch file format: prompts separated by a line containing only `---`.
If no `---` separators are present, each non-empty line is treated as one prompt.

Without a key it still optimizes and shows a clearly-labelled (estimated) count.
"""

import os, re, sys, subprocess, difflib, socket, getpass

MODEL = "claude-opus-4-8"
# Force a direct connection — never inherit ANTHROPIC_BASE_URL (which may point at a proxy).
ANTHROPIC_DIRECT = "https://api.anthropic.com"


def est(t: str) -> int:
    """Rough token estimate (chars/4). Shared by the proxy and the hook for instant,
    offline numbers; the exact path is count_tokens() below."""
    return max(0, len(t or "") // 4)


# Machine identity attached to every dashboard report, so one central (possibly remote)
# collector can break savings down per host. Best-effort, stdlib-only, never fatal.
try:
    # IQ_HOST wins so a containerized proxy can report the real machine name (not the
    # container id) and share one host tag with the host-run hook/CLI.
    HOST = os.getenv("IQ_HOST") or socket.gethostname()
except Exception:
    HOST = "unknown"
try:
    USER = getpass.getuser()
except Exception:
    USER = "unknown"

# Privacy: by default we send only COUNTS + host to the dashboard — never your prompt text.
# Opt in with IQ_REPORT_TEXT=1 to also send before/after text (useful for a local debug view).
REPORT_TEXT = os.getenv("IQ_REPORT_TEXT", "0") == "1"


# ── Rules ─────────────────────────────────────────────────────────────────────
# Each rule is (compiled pattern, replacement, label). Order matters: verbose→concise
# phrase swaps run first, then pure-filler removals, then guarded hedge words, then
# whitespace/cleanup. Every rule is meaning-preserving by design; tune freely.

def _w(p):  # whole-phrase, case-insensitive, word-bounded
    return re.compile(r"\b" + p + r"\b", re.I)

RULES = [
    # 1. Verbose phrase → concise equivalent (same meaning)
    (_w(r"due to the fact that"),         "because", "due to the fact that → because"),
    (_w(r"in spite of the fact that"),    "although", "in spite of the fact that → although"),
    (_w(r"despite the fact that"),        "although", "despite the fact that → although"),
    (_w(r"in the event that"),            "if", "in the event that → if"),
    (_w(r"in order to"),                  "to", "in order to → to"),
    (_w(r"for the purpose of"),           "to", "for the purpose of → to"),
    (_w(r"at this point in time"),        "now", "at this point in time → now"),
    (_w(r"at the present time"),          "now", "at the present time → now"),
    (_w(r"a large number of"),            "many", "a large number of → many"),
    (_w(r"the majority of"),              "most", "the majority of → most"),
    (_w(r"a majority of"),                "most", "a majority of → most"),
    (_w(r"a small number of"),            "a few", "a small number of → a few"),
    (_w(r"in the near future"),           "soon", "in the near future → soon"),
    (_w(r"on a regular basis"),           "regularly", "on a regular basis → regularly"),
    (_w(r"with regard to"),               "about", "with regard to → about"),
    (_w(r"with respect to"),              "about", "with respect to → about"),
    (_w(r"in regard to"),                 "about", "in regard to → about"),

    # 2. Pure filler — carries no information, safe to drop
    (_w(r"I was wondering if you could"), "could you", "'I was wondering if you could' → 'could you'"),
    (_w(r"I would appreciate it if you could"), "could you", "'I would appreciate it if you could' → 'could you'"),
    (_w(r"I would like you to"),          "", "drop 'I would like you to'"),
    (_w(r"I'd like you to"),              "", "drop 'I'd like you to'"),
    (_w(r"if you don't mind"),            "", "drop 'if you don't mind'"),
    (_w(r"if you would"),                 "", "drop 'if you would'"),
    (_w(r"as you( may)? know"),           "", "drop 'as you know'"),
    (_w(r"it goes without saying( that)?"), "", "drop 'it goes without saying'"),
    (_w(r"needless to say"),              "", "drop 'needless to say'"),
    (_w(r"for what it's worth"),          "", "drop 'for what it's worth'"),
    (_w(r"please"),                       "", "drop 'please'"),
    (_w(r"kindly"),                       "", "drop 'kindly'"),

    # 2b. Politeness / gratitude — the most common no-information forms. Phrase-specific
    # variants first so the generic 'thanks'/'thank you' don't shadow them.
    (_w(r"thank you so much"),            "", "drop 'thank you so much'"),
    (_w(r"thanks a lot"),                 "", "drop 'thanks a lot'"),
    (_w(r"thanks a million"),             "", "drop 'thanks a million'"),
    (_w(r"thanks in advance"),            "", "drop 'thanks in advance'"),
    (_w(r"thank you in advance"),         "", "drop 'thank you in advance'"),
    (_w(r"many thanks"),                  "", "drop 'many thanks'"),
    (_w(r"thank you very much"),          "", "drop 'thank you very much'"),
    (_w(r"thank you"),                    "", "drop 'thank you'"),
    # 'thanks' but NOT 'thanks to X' (which means 'because of') — guard it.
    (re.compile(r"\bthanks\b(?!\s+to\b)", re.I), "", "drop 'thanks'"),
    (_w(r"thanx"),                        "", "drop 'thanx'"),
    (_w(r"tyvm"),                         "", "drop 'tyvm'"),
    (_w(r"thx"),                          "", "drop 'thx'"),
    (_w(r"ty"),                           "", "drop 'ty'"),
    (_w(r"much appreciated"),             "", "drop 'much appreciated'"),
    (_w(r"I (really )?appreciate it"),    "", "drop 'I appreciate it'"),
    (_w(r"appreciate it"),                "", "drop 'appreciate it'"),
    (_w(r"no worries"),                   "", "drop 'no worries'"),
    (_w(r"no problem"),                   "", "drop 'no problem'"),
    (_w(r"sorry to bother you"),          "", "drop 'sorry to bother you'"),
    (_w(r"sorry to bother"),              "", "drop 'sorry to bother'"),
    (_w(r"sorry for bothering you"),      "", "drop 'sorry for bothering you'"),

    # 3. Hedge fillers — guarded so they only fire where they're genuinely filler
    (re.compile(r"^(basically|actually|honestly|essentially)[,\s]+", re.I), "",
        "drop sentence-initial hedge (basically/actually/…)"),
    (re.compile(r"(?<=[.!?]\s)(basically|actually|honestly|essentially)[,\s]+", re.I), "",
        "drop hedge after sentence break"),
    # Slightly more aggressive: mid-sentence basically/actually. Almost always filler,
    # but can carry meaning ("the function basically works" = roughly). Drop this rule
    # if you want to be maximally conservative.
    (re.compile(r"\s+(basically|actually)\s+", re.I), " ",
        "drop mid-sentence filler 'basically/actually'"),
    (re.compile(r"\b(could|can|would)\s+you\s+just\b", re.I), r"\1 you",
        "'could you just' → 'could you'"),
    (re.compile(r"\bI\s+just\s+want(ed)?\s+you\s+to\b", re.I), "",
        "drop 'I just want you to'"),
]

# Cleanup applied after the rules above (fix artifacts left by removals)
CLEANUP = [
    (re.compile(r"([.!?])\s+([.!?])"), r"\1"),   # two spaced terminal marks (removal artifact) → first
    (re.compile(r"\s+([,.;:!?])"), r"\1"),       # space before punctuation
    (re.compile(r"([,;:])\1+"), r"\1"),          # doubled punctuation from removals
    (re.compile(r"[ \t]{2,}"), " "),             # collapse runs of spaces/tabs
    (re.compile(r" *\n *"), "\n"),               # trim spaces around newlines
    (re.compile(r"\n{3,}"), "\n\n"),             # cap blank-line runs at one
]


# ── Exclusion: never touch CLI / shell / commands / code / scripts ────────────
_SHELL_VERBS = ("git", "npm", "npx", "yarn", "pnpm", "docker", "docker-compose", "kubectl",
    "helm", "curl", "wget", "pip", "pip3", "python", "python3", "node", "deno", "bun", "ssh",
    "scp", "rsync", "cat", "ls", "grep", "egrep", "awk", "sed", "echo", "export", "source",
    "chmod", "chown", "mkdir", "rm", "mv", "cp", "cd", "touch", "tar", "unzip", "brew", "apt",
    "apt-get", "yum", "dnf", "make", "cargo", "go", "rustc", "javac", "mvn", "gradle",
    "terraform", "ansible", "aws", "gcloud", "az", "systemctl", "service", "ps", "kill",
    "lsof", "netstat", "ping", "dig", "jq", "sqlite3", "psql", "mysql", "redis-cli",
    "uvicorn", "pytest", "bash", "sh", "zsh", "sudo")
# a command word ANYWHERE, followed (closely) by a flag / path / pipe / redirect / code ext
_CMD_LINE = re.compile(
    r'\b(?:sudo\s+)?(?:' + "|".join(_SHELL_VERBS) + r')\b'
    r'[^\n]{0,40}?'
    r'(?:\s-{1,2}[A-Za-z]|\s/[\w.]|\s\|\s|>{1,2}|&&|\|\||\$\(|`'
    r'|\.(?:sh|py|js|ts|go|rb|rs|java|cpp|json|ya?ml|toml|sql|env|conf|cfg|ini)\b)', re.I)

def is_code_or_command(text: str) -> bool:
    """True if the text looks like a shell command, CLI invocation, or code/script."""
    t = text.strip()
    if not t:
        return False
    if "```" in t:                                   # fenced code block
        return True
    for line in t.splitlines():
        if line.strip().startswith(("#!", "$ ", "./", "sudo ")):
            return True
    if _CMD_LINE.search(t):                           # shell command with flag/path/pipe
        return True
    if re.search(r'(?m)(^|\s)(&&|\|\||2>&1|>>)(\s|$)', t):   # shell operators
        return True
    if re.search(r'\b(def|class|function|const|let|var|import|return)\b[^.\n]*[;{(=]', t):
        return True                                   # code keyword + code punctuation
    return False


def optimize(text: str):
    """Return (optimized_text, [labels of rules that fired])."""
    if is_code_or_command(text):     # never modify CLI / shell / commands / code / scripts
        return text, []
    out = text
    fired = []
    for pat, repl, label in RULES:
        new = pat.sub(repl, out)
        if new != out:
            fired.append(label)
            out = new
    for pat, repl in CLEANUP:
        out = pat.sub(repl, out)
    out = out.strip()
    # Drop a dangling leading comma/colon left by removing a sentence-initial filler
    # (e.g. "As you know, the build…" → ", the build…" → "the build…").
    out = re.sub(r"^[\s,;:]+", "", out)
    # Re-capitalize sentence starts that a removal or swap may have lowercased
    # (start of text, after . ! ?, and after a line break).
    out = re.sub(r"(^|[.!?]\s+|\n\s*)([a-z])",
                 lambda m: m.group(1) + m.group(2).upper(), out)
    return out, fired


# ── Token counting ────────────────────────────────────────────────────────────
def count_tokens(texts):
    """
    Return (counts: list[int], exact: bool) for pure text content.
    Exact path subtracts the empty-message baseline so structural overhead doesn't
    inflate the numbers. Falls back to a labelled estimate with no API key / offline.
    """
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        try:
            import httpx
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
            def raw(t):
                r = httpx.post(f"{ANTHROPIC_DIRECT}/v1/messages/count_tokens",
                               headers=headers, timeout=20,
                               json={"model": MODEL,
                                     "messages": [{"role": "user", "content": t or " "}]})
                r.raise_for_status()
                return r.json()["input_tokens"]
            base = raw("")
            return [max(0, raw(t) - base) for t in texts], True
        except Exception as e:
            print(f"  (exact count unavailable: {e} — showing estimate)\n", file=sys.stderr)
    # Offline heuristic — clearly NOT the real tokenizer, just a rough gauge.
    return [max(1, round(len(t) / 4)) for t in texts], False


# ── Unified dashboard reporting ─────────────────────────────────────────────────
def _config() -> dict:
    """Optional JSON config for plugin / GUI installs that can't easily set env vars.
    Path: $IQ_CONFIG, else ~/.inferenceiq.json. Keys: "dashboard", "token". Fail-open."""
    try:
        import json as _json
        path = os.getenv("IQ_CONFIG") or os.path.join(os.path.expanduser("~"), ".inferenceiq.json")
        with open(path, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _dashboard_url() -> str:
    """Where to report: env var wins, then ~/.inferenceiq.json, then localhost. So a
    `/plugin`-installed hook can target a remote/AWS collector via the config file."""
    return (os.getenv("INFERENCEIQ_DASHBOARD") or _config().get("dashboard")
            or "http://3.96.147.26:8088").rstrip("/")


def _token() -> str:
    """Shared token for a protected (public/cloud) collector. env IQ_TOKEN, then config."""
    return os.getenv("IQ_TOKEN") or _config().get("token") or ""


def _config_path() -> str:
    return os.getenv("IQ_CONFIG") or os.path.join(os.path.expanduser("~"), ".inferenceiq.json")


def set_config(dashboard=None, token=None):
    """Write/merge ~/.inferenceiq.json so a `/plugin` install can target a remote/AWS
    collector without env vars. Keys match what report() reads: `dashboard`, `token`."""
    import json as _json
    cfg = _config()
    if dashboard is not None:
        cfg["dashboard"] = dashboard.rstrip("/")
    if token is not None:
        cfg["token"] = token
    path = _config_path()
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(cfg, f, indent=2)
        f.write("\n")
    return path, cfg


def report(kind, saved, source="cli", rules=None, techniques=None, tips=None,
           before="", after=""):
    """
    Best-effort: tell the one dashboard (collector on :8088, or a remote/AWS host) about
    this run so CLI, web, and proxy activity all land in a SINGLE view. Fire-and-forget —
    silent and fast if the dashboard isn't running. Disable with INFERENCEIQ_DASHBOARD=off.
    """
    url = _dashboard_url()
    if not url or url.lower() == "off":
        return
    # stdlib only — the CLI runs under whatever python the user has (often no httpx),
    # so reporting must never depend on a third-party package or it'll silently no-op.
    try:
        import json as _json, urllib.request as _u
        if not REPORT_TEXT:
            before = after = ""   # privacy default: counts only, never prompt text off-box
        body = _json.dumps({
            "kind": kind, "source": source, "saved": max(0, int(saved or 0)),
            "rules": rules or [], "techniques": techniques or [], "tips": tips or [],
            "before": before, "after": after, "host": HOST, "user": USER,
        }).encode()
        headers = {"content-type": "application/json"}
        tok = _token()
        if tok:
            headers["X-IQ-Token"] = tok   # required by a token-protected collector
        req = _u.Request(f"{url}/api/record", data=body, headers=headers, method="POST")
        _u.urlopen(req, timeout=1.5).read()
    except Exception:
        pass   # dashboard down / offline — the CLI must never fail because of it


# ── Rendering ─────────────────────────────────────────────────────────────────
def _c(s, code):
    return s if os.getenv("NO_COLOR") or not sys.stdout.isatty() else f"\033[{code}m{s}\033[0m"

def word_diff(a: str, b: str) -> str:
    """Inline diff with explicit [-removed-] / {+added+} markers (also colored on a tty)."""
    aw, bw = a.split(), b.split()
    sm = difflib.SequenceMatcher(a=aw, b=bw)
    parts = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            parts.append(" ".join(aw[i1:i2]))
        else:
            if i2 > i1:
                parts.append(_c(f"[-{' '.join(aw[i1:i2])}-]", "9;31"))   # removed
            if j2 > j1:
                parts.append(_c(f"{{+{' '.join(bw[j1:j2])}+}}", "32"))   # added
    return " ".join(p for p in parts if p)


def split_prompts(raw: str):
    """Records separated by a line of exactly `---`; else one prompt per non-empty line."""
    if re.search(r"(?m)^\s*---\s*$", raw):
        return [c.strip() for c in re.split(r"(?m)^\s*---\s*$", raw) if c.strip()]
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def run_batch(path: str, out_path: str | None):
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    prompts = split_prompts(raw)
    if not prompts:
        print("no prompts found", file=sys.stderr)
        return 2
    results = [optimize(p) for p in prompts]
    opts = [r[0] for r in results]
    all_fired = [rule for _, fired in results for rule in fired]
    counts, exact = count_tokens(prompts + opts)   # one baseline call, shared
    n = len(prompts)
    orig, new = counts[:n], counts[n:]
    label = "tok" if exact else "tok~"
    print()
    for i, (o, ot, nt) in enumerate(zip(opts, orig, new), 1):
        s, pc = ot - nt, (round((ot - nt) / ot * 100) if ot else 0)
        preview = o.replace("\n", " ")
        preview = preview[:47] + "…" if len(preview) > 48 else preview
        tag = _c(f"−{s}, {pc}%" if s else "no change", "32" if s else "33")
        print(f"{i:>3}  {ot:>4} → {nt:<4} {label}  ({tag})  {preview}")
    to, tn = sum(orig), sum(new)
    ts, tp = to - tn, (round((to - tn) / to * 100) if to else 0)
    print(_c("─" * 64, "2"))
    unit = "tokens" if exact else "tokens (estimated)"
    print(_c(f"{n} prompts · {to} → {tn} {unit} · saved {ts} ({tp}%)", "1"))
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n---\n".join(opts) + "\n")
        print(_c(f"✓ optimized prompts written to {out_path}", "32"))

    report("opt", ts, rules=all_fired)   # → unified dashboard (aggregate for the batch)
    return 0


def main(argv):
    copy = False
    batch_path = out_path = None
    set_dash = set_tok = None
    show_cfg = False
    rest = []
    it = iter(argv)
    for a in it:
        if a == "--copy":
            copy = True
        elif a == "--batch":
            batch_path = next(it, None)
        elif a == "--out":
            out_path = next(it, None)
        elif a == "--set-dashboard":          # point this machine at a (remote/AWS) collector
            set_dash = next(it, None)
        elif a == "--set-token":              # token for a protected collector
            set_tok = next(it, None)
        elif a == "--show-config":
            show_cfg = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            rest.append(a)

    # Config management: write/show ~/.inferenceiq.json (used by `/plugin` installs).
    if show_cfg:
        import json as _json
        print(f"config: {_config_path()}")
        print(_json.dumps(_config(), indent=2))
        return 0
    if set_dash is not None or set_tok is not None:
        path, cfg = set_config(set_dash, set_tok)
        print(f"✓ wrote {path}")
        print(f"  dashboard: {cfg.get('dashboard', '(unset → http://3.96.147.26:8088)')}")
        print(f"  token:     {'set' if cfg.get('token') else '(none)'}")
        return 0

    if batch_path is not None:
        return run_batch(batch_path, out_path)

    text = " ".join(rest).strip() if rest else sys.stdin.read().strip()
    if not text:
        print("usage: optimize.py [--copy] \"text\"  |  --batch FILE [--out FILE]",
              file=sys.stderr)
        return 2

    opt, fired = optimize(text)
    counts, exact = count_tokens([text, opt])
    orig_tok, opt_tok = counts
    saved = orig_tok - opt_tok
    pct = round(saved / orig_tok * 100) if orig_tok else 0
    label = "tokens" if exact else "tokens (estimated)"

    bar = "─" * 60
    print()
    print(_c(f"┌ ORIGINAL   {orig_tok} {label}", "1"))
    print("│ " + text.replace("\n", "\n│ "))
    print(_c(f"├ OPTIMIZED  {opt_tok} {label}   " +
             _c(f"(−{saved}, {pct}% smaller)" if saved else "(no change)",
                "32" if saved else "33"), "1"))
    print("│ " + opt.replace("\n", "\n│ "))
    print(_c("└" + bar, "2"))

    if fired:
        print(_c("\nrules applied:", "1"))
        for f in fired:
            print(f"  • {f}")
    else:
        print(_c("\nno rules fired — text is already tight.", "33"))

    if saved and "\n" not in text:
        print(_c("\ndiff:", "1"))
        print("  " + word_diff(text, opt))

    if copy and opt:
        try:
            subprocess.run(["pbcopy"], input=opt.encode(), check=True)
            print(_c("\n✓ optimized text copied to clipboard", "32"))
        except Exception as e:
            print(f"\n(could not copy: {e})", file=sys.stderr)

    report("opt", saved, rules=fired, before=text, after=opt)   # → unified dashboard
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
