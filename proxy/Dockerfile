# InferenceIQ intercept proxy image (the ⚡ Auto surface, :8082).
# Installs the proxy's runtime deps (incl. the local semantic-cache embedder; no anthropic SDK).
# The dashboard collector has its OWN slim image under ./dashboard.
# Build context is the REPO ROOT (see compose.yml) so it can copy ../core-engine.
FROM python:3.12-slim

WORKDIR /app

COPY proxy/requirements-proxy.txt .
RUN pip install --no-cache-dir -r requirements-proxy.txt

# Mirror the repo layout: core-engine/ (optimize, router, semcache) + the proxy itself.
COPY core-engine/ ./core-engine/
COPY proxy/intercept.py ./proxy/intercept.py

# Make the engine modules importable regardless of working directory.
ENV PYTHONPATH=/app/core-engine

# fastembed downloads its ONNX model on first use; cache it on a mounted volume to avoid
# re-downloading on every restart (see compose.yml).
ENV FASTEMBED_CACHE_PATH=/models

EXPOSE 8082

WORKDIR /app/proxy
CMD ["uvicorn", "intercept:app", "--host", "0.0.0.0", "--port", "8082"]
