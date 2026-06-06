# InferenceIQ intercept proxy image (the ⚡ Auto surface, :8082).
# Installs the proxy's runtime deps (incl. the local semantic-cache embedder; no anthropic SDK).
# The dashboard collector has its OWN slim image under ./dashboard.
FROM python:3.12-slim

WORKDIR /app

COPY requirements-proxy.txt .
RUN pip install --no-cache-dir -r requirements-proxy.txt

COPY optimize.py intercept.py router.py semcache.py ./

# fastembed downloads its ONNX model on first use; cache it on a mounted volume to avoid
# re-downloading on every restart (see compose.yml).
ENV FASTEMBED_CACHE_PATH=/models

EXPOSE 8082

CMD ["uvicorn", "intercept:app", "--host", "0.0.0.0", "--port", "8082"]
