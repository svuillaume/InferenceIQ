# Standalone InferenceIQ dashboard collector — slim, no Anthropic SDK, no repo deps.
# Build/run this directory on its own (locally or on a remote box) to collect reports.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector.py .

# Persist cumulative savings on a mounted volume so the totals COMPOUND across container
# restarts/redeploys. Mount a named volume at /data (e.g. `-v iq_data:/data`) and this file
# survives `docker rm` + recreate. Without a volume it still works, but resets on recreate.
ENV IQ_PERSIST_PATH=/data/tally.json
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8088

# Graceful stop (SIGTERM) lets the collector flush a final save before exit — use
# `docker stop` (not `kill -9`) on redeploy. --timeout gives uvicorn time to run shutdown.
STOPSIGNAL SIGTERM
CMD ["uvicorn", "collector:app", "--host", "0.0.0.0", "--port", "8088", "--timeout-graceful-shutdown", "10"]
