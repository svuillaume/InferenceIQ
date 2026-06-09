#!/bin/bash

set -e

APP_NAME="iq-dashboard"
IMAGE_NAME="iq-dashboard"
PORT=8088
VOLUME="iq_data"

echo "🔄 Pulling latest code..."
git pull

echo "🏗️ Building Docker image..."
docker build -t $IMAGE_NAME ./dashboard

echo "🛑 Stopping container (if running)..."
docker stop $APP_NAME 2>/dev/null || true
docker rm $APP_NAME 2>/dev/null || true

echo "🚀 Starting new container..."
docker run -d \
  --name $APP_NAME \
  -p $PORT:$PORT \
  -v $VOLUME:/data \
  --restart unless-stopped \
  $IMAGE_NAME

echo "✅ Deployment complete!"
