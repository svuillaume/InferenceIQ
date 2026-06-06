#!/usr/bin/env bash
# demo.sh — simulate a team of developers using InferenceIQ, with REALISTIC averages,
# and drive every dashboard panel. No API key and no `claude` needed: it feeds the
# collector the same /api/record events the CLI, hook, and proxy emit in real use.
#
# Realistic figures calibrated to morphllm.com/ai-coding-costs (Apr 2026):
#   baseline ~$360/dev/month · ~70% of agent tokens are waste
#   savings levers: model routing 30-40% · context compaction / concise 50-70%
#   prompt caching ~90% on cached tokens · combined real-world reduction 55-70%
#   token prices (match the dashboard): Sonnet $3/$15 · Haiku $1/$5 · Opus $5/$25 per 1M
# So this demo uses real agent-scale token volumes and a ~60% reply reduction / ~30% cache hit.
#
# Usage:  ./demo.sh                 # 5 developers, 5 minutes, http://localhost:8088
#         DEVS=10 DURATION=120 ./demo.sh
#         DASH=http://host:8088 ./demo.sh
set -euo pipefail

DASH="${DASH:-http://localhost:8088}"
DURATION="${DURATION:-300}"
TICK="${TICK:-1}"
DEVS="${DEVS:-5}"
HIT_RATE="${HIT_RATE:-30}"          # target cache hit rate % (routing+cache, per Morph)

post(){ curl -s -o /dev/null --max-time 2 -XPOST "$DASH/api/record" \
        -H 'content-type: application/json' -d "$1" 2>/dev/null || true; }
curl -s --max-time 3 "$DASH/api/stats" >/dev/null 2>&1 \
  || { echo "✗ dashboard not reachable at $DASH (docker compose up -d)"; exit 1; }

# — a team of DEVS developers (machine names) —
allnames=(alex-mbp priya-mbp sam-linux jordan-mbp wei-wsl noor-mbp diego-linux mia-mbp)
devs=(); for ((i=0;i<DEVS;i++)); do devs+=("${allnames[i % ${#allnames[@]}]}-$((i+1))"); done

# — weighted model mix: ~60% Sonnet, 25% Haiku, 15% Opus —
S=claude-sonnet-4-6; H=claude-haiku-4-5-20251001; O=claude-opus-4-8
modmix=($S $S $S $S $S $S $S $S $S $S $S $S  $H $H $H $H $H  $O $O $O)   # 12/5/3 of 20
rules=('drop please' 'in order to → to' "drop 'just'" 'the majority of → most'
       'drop mid-sentence filler' 'collapse whitespace' "drop 'as you know'")
techs=('Lead with a clear imperative' 'Make scope explicit' 'Cut hedging and pleasantries'
       'Use light XML tags' 'Specify output format')
tips=('RAG (retrieve only relevant snippets)' 'Summarize chat history instead of resending'
      'Cache static or repeated context' 'Cap response length' 'Use tools for math/lookups')
# intent|model — routing follows the same mix
intents=('coding|'$S 'analysis|'$S 'general task|'$S 'simple|'$H 'translate|'$H 'complex reasoning|'$O)

pick(){ eval "local a=(\"\${$1[@]}\")"; echo "${a[RANDOM % ${#a[@]}]}"; }     # bash 3.2-safe
r(){ echo $(( $1 + RANDOM % ($2 - $1 + 1) )); }

echo "▶ Simulating $DEVS developers on $DASH for ${DURATION}s — watch the numbers climb."
start=$SECONDS; n=0; chits=0; calls=0
while (( SECONDS - start < DURATION )); do
  elapsed=$(( SECONDS - start ))
  dev=$(pick devs)

  # heartbeat: flips the dashboard header to "Demo" (reverts to "live" for real Claude Code)
  post '{"kind":"mode","source":"demo","mode":"demo"}'

  # input optimization (trimming verbose prose on large coding prompts) — cli + hook
  post "{\"kind\":\"opt\",\"source\":\"hook\",\"host\":\"$dev\",\"saved\":$(r 12 90),\"rules\":[\"$(pick rules)\"]}"
  (( RANDOM % 2 == 0 )) && post "{\"kind\":\"opt\",\"source\":\"cli\",\"host\":\"$(pick devs)\",\"saved\":$(r 20 140),\"rules\":[\"$(pick rules)\"]}"
  (( RANDOM % 4 == 0 )) && post "{\"kind\":\"rec\",\"source\":\"web\",\"host\":\"$dev\",\"saved\":$(r 40 260),\"techniques\":[\"$(pick techs)\"],\"tips\":[\"$(pick tips)\"]}"

  # a reply happens — cache hit (~HIT_RATE%) or a real model call
  if (( RANDOM % 100 < HIT_RATE )); then
    chits=$((chits+1))
    if (( RANDOM % 4 == 0 )); then
      post "{\"kind\":\"cache\",\"source\":\"proxy\",\"host\":\"$dev\",\"layer\":\"exact\",\"similarity\":1.0,\"model\":\"$(pick modmix)\"}"
    else
      post "{\"kind\":\"cache\",\"source\":\"proxy\",\"host\":\"$dev\",\"layer\":\"semantic\",\"similarity\":0.9$(r 2 8),\"model\":\"$(pick modmix)\"}"
    fi
  else
    calls=$((calls+1)); m=$(pick modmix); rf=""
    # half the calls were routed DOWN from the default (Opus) to a cheaper tier. Decide first so
    # the reply is served by — and priced against — the routed model (real proxy threads this too).
    if (( RANDOM % 2 == 0 )); then
      IFS='|' read -r intent to <<< "$(pick intents)"
      post "{\"kind\":\"route\",\"source\":\"proxy\",\"host\":\"$dev\",\"from_model\":\"$O\",\"to_model\":\"$to\",\"intent\":\"$intent\",\"applied\":true}"
      [ "$to" != "$O" ] && { rf="$O"; m="$to"; }   # served by the cheaper model; price the delta
    fi
    # both a normal and a concise reply so reply-reduction (~60%, per Morph compaction) computes.
    # in_tokens + cache_read mimic Anthropic's real usage: most of a coding prompt is cached (~85%).
    # routed_from on the primary (full-usage) reply drives the routing-savings panel.
    post "{\"kind\":\"out\",\"source\":\"proxy\",\"host\":\"$dev\",\"model\":\"$m\",\"out_tokens\":$(r 900 1900),\"concise\":false,\"in_tokens\":$(r 900 2600),\"cache_read\":$(r 7000 24000),\"cache_creation\":$(r 0 1500),\"routed_from\":\"$rf\"}"
    post "{\"kind\":\"out\",\"source\":\"proxy\",\"host\":\"$dev\",\"model\":\"$m\",\"out_tokens\":$(r 360 760),\"concise\":true}"
  fi

  # cache store gauge — steady realistic hit rate, store fills gradually
  total=$(( chits + calls )); hr=$(( total>0 ? chits*100/total : HIT_RATE ))
  entries=$(( 200 + total * 6 )); bytes=$(( entries * 2600 )); (( bytes>52428800 )) && bytes=52428800
  post "{\"kind\":\"cachestat\",\"source\":\"proxy\",\"entries\":$entries,\"bytes\":$bytes,\"max_bytes\":52428800,\"hit_rate\":$hr,\"exact_hits\":$((chits/4)),\"semantic_hits\":$((chits*3/4)),\"misses\":$calls,\"evictions\":$(( total/200 )),\"ready\":true,\"index\":\"numpy\",\"model\":\"BAAI/bge-small-en-v1.5\"}"

  n=$((n+1))
  printf '\r  %3ds/%ss · %d devs · %d calls · %d cached (%d%% hit) ' "$elapsed" "$DURATION" "$DEVS" "$calls" "$chits" "$hr"
  sleep "$TICK"
done
echo; echo "✓ Done — $DEVS devs, $n bursts, ${chits} cache hits / $((chits+calls)) replies. See $DASH"
