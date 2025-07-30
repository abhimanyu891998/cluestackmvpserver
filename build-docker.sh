#!/bin/bash

# Build Docker image for BDD Market Data Server
set -e

# Configuration
IMAGE_NAME="bdd-market-data-server"
TAG=${1:-"latest"}
FULL_IMAGE_NAME="$IMAGE_NAME:$TAG"

echo "🏗️  Building Docker image: $FULL_IMAGE_NAME"

# Build the Docker image
docker build -t "$FULL_IMAGE_NAME" .

echo "✅ Successfully built Docker image: $FULL_IMAGE_NAME"

# Show image details
echo "📋 Image details:"
docker images | grep "$IMAGE_NAME" | head -1

echo ""
echo "🚀 To run the container:"
echo "   docker run -p 8000:8000 $FULL_IMAGE_NAME"
echo ""
echo "🐳 Or use docker-compose:"
echo "   docker-compose up -d"
echo ""
echo "🔧 To run with custom environment:"
echo "   docker run -p 8000:8000 -e HOST=0.0.0.0 -e PORT=8000 $FULL_IMAGE_NAME"