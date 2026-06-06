#!/usr/bin/env bash
# install.sh — stand up the InferenceIQ dashboard (collector) on its own.
#
# The dashboard is self-contained: it imports nothing from the rest of the repo, needs no
# API key, and aggregates reports from many machines (each event is host-tagged). Copy just
# this `dashboard/` folder to a box (e.g. AWS) and run this script.
#
# Docker is used if available; otherwise it falls back to a local Python venv + uvicorn.
#
# Usage:
#   ./install.sh                                   # Docker on :8088, no auth (local/dev)
#   IQ_TOKEN=$(openssl rand -hex 24) ./install.sh  # require a token on write endpoints (public!)
#   PORT=9000 IQ_TZ=America/Toronto ./install.sh
#   ./install.sh --no-docker                       # force the Python/uvicorn path
#
# Env:
#   PORT       host port to expose            (default 8088)
#   IQ_TOKEN   shared secret for writes       (default empty = OPEN; SET THIS before exposing publicly)
#   IQ_TZ      pin dashboard timezone         (default empty = auto-detect via public IP)
#   NAME       docker container name          (default iq-dashboard)
#   IMAGE      docker image tag               (default iq-dashboard)
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # the dashboard/ dir (Dockerfile + collector.py)

PORT="${PORT:-8088}"
IQ_TOKEN="${IQ_TOKEN:-}"
IQ_TZ="${IQ_TZ:-}"
NAME="${NAME:-iq-dashboard}"
IMAGE="${IMAGE:-iq-dashboard}"
USE_DOCKER=1
[ "${1:-}" = "--no-docker" ] && USE_DOCKER=0

if [ -z "$IQ_TOKEN" ]; then
  echo "⚠  IQ_TOKEN is empty — the write endpoints (/api/record, /api/reset, /api/tz) will be OPEN."
  echo "   Fine for local/dev. Before exposing publicly, re-run with: IQ_TOKEN=<secret> ./install.sh"
fi

health() {   # poll until /api/stats answers, or give up
  for _ in $(seq 1 20); do
    if curl -fs --max-time 2 "http://localhost:$PORT/api/stats" >/dev/null 2>&1; then
      echo "✓ dashboard up at http://localhost:$PORT  (stats: /api/stats)"
      [ -n "$IQ_TOKEN" ] && echo "  writes require header  X-IQ-Token: <your token>"
      return 0
    fi
    sleep 1
  done
  echo "✗ dashboard did not become healthy on :$PORT — check logs." >&2
  return 1
}

if [ "$USE_DOCKER" = 1 ] && command -v docker >/dev/null 2>&1; then
  echo "▶ Docker build…"
  docker build -t "$IMAGE" .
  echo "▶ (re)starting container '$NAME' on :$PORT…"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --restart unless-stopped \
    -p "$PORT:8088" \
    -e IQ_TOKEN="$IQ_TOKEN" \
    -e IQ_TZ="$IQ_TZ" \
    "$IMAGE" >/dev/null
  health
  echo "  logs:  docker logs -f $NAME    stop:  docker rm -f $NAME"
else
  [ "$USE_DOCKER" = 1 ] && echo "ℹ docker not found — using the Python/uvicorn path."
  echo "▶ Python venv + uvicorn on :$PORT…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
  echo "▶ starting (foreground — Ctrl-C to stop; use a process manager/systemd for production)…"
  exec env IQ_TOKEN="$IQ_TOKEN" IQ_TZ="$IQ_TZ" \
    ./.venv/bin/uvicorn collector:app --host 0.0.0.0 --port "$PORT" --log-level warning
fi
