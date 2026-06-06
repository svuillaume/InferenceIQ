#!/usr/bin/env bash
# run.sh — run the InferenceIQ dashboard WITHOUT Docker (Linux & macOS).
#
# The collector is self-contained: no API key, no repo code, nothing but FastAPI + uvicorn.
# This script creates a local virtualenv (once), installs the deps, and serves the dashboard
# in the foreground. Ctrl-C to stop. For a long-running server, see the nohup/systemd tips below.
#
# Usage:
#   ./run.sh                                   # http://0.0.0.0:8088, no auth (local/dev)
#   IQ_TOKEN=$(openssl rand -hex 24) ./run.sh  # require a token on write endpoints (public!)
#   PORT=9000 IQ_TZ=America/Toronto ./run.sh
#
# Run in the background instead of the foreground:
#   nohup env IQ_TOKEN=secret ./run.sh > dashboard.log 2>&1 &   # logs to dashboard.log
#
# Env:  PORT (8088) · IQ_TOKEN (empty = OPEN) · IQ_TZ (empty = auto-detect via public IP)
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # the dashboard/ dir (collector.py lives here)

PORT="${PORT:-8088}"
IQ_TOKEN="${IQ_TOKEN:-}"
IQ_TZ="${IQ_TZ:-}"

# Pick a Python 3.
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "✗ python3 not found — install Python 3.9+ first." >&2; exit 1; }

# Create the venv once, then install deps once.
if [ ! -d .venv ]; then
  echo "▶ creating virtualenv (.venv)…"
  if ! "$PY" -m venv .venv 2>/dev/null; then
    echo "✗ couldn't create a venv. On Debian/Ubuntu:  sudo apt install python3-venv" >&2
    exit 1
  fi
  echo "▶ installing fastapi · uvicorn · tzdata…"
  ./.venv/bin/python -m pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

[ -z "$IQ_TOKEN" ] && echo "⚠  IQ_TOKEN empty — write endpoints are OPEN (fine local; set it before exposing publicly)."
echo "▶ dashboard → http://localhost:$PORT   (Ctrl-C to stop)"
exec env IQ_TOKEN="$IQ_TOKEN" IQ_TZ="$IQ_TZ" \
  ./.venv/bin/uvicorn collector:app --host 0.0.0.0 --port "$PORT" --log-level warning
