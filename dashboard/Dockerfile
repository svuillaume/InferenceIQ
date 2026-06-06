# Standalone InferenceIQ dashboard collector — slim, no Anthropic SDK, no repo deps.
# Build/run this directory on its own (locally or on a remote box) to collect reports.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector.py .

EXPOSE 8088

CMD ["uvicorn", "collector:app", "--host", "0.0.0.0", "--port", "8088"]
