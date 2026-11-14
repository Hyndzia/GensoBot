#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

CONTAINER_NAME="gensobot"
IMAGE_NAME="gensobot"
PORT="7777:7777"

COOKIE_FILE_PATH="${PROJECT_ROOT}/cookies.txt"
ENV_FILE="${PROJECT_ROOT}/.env"
DOCKERFILE="${PROJECT_ROOT}/Dockerfile"
SERVERS_DIR="${PROJECT_ROOT}/servers"

echo "Checking required files..."

if [ ! -f "$DOCKERFILE" ]; then
    echo "Error: Dockerfile not found!"
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "Error: .env file not found!"
    exit 1
fi

if [ ! -f "$COOKIE_FILE_PATH" ]; then
    echo "Warning: cookies.txt not found. Starting without cookies."
fi

mkdir -p "$SERVERS_DIR"

echo "Building Docker image '${IMAGE_NAME}'..."

sudo docker build \
    -f "$DOCKERFILE" \
    -t "$IMAGE_NAME" \
    "$PROJECT_ROOT"

if sudo docker ps -aq -f "name=^${CONTAINER_NAME}$" | grep -q .; then
    echo "Container '${CONTAINER_NAME}' exists. Removing..."
    sudo docker rm -f "$CONTAINER_NAME"
fi

echo "Running container '${CONTAINER_NAME}'..."

DOCKER_ARGS=(
    -d
    --name "$CONTAINER_NAME"
    --restart unless-stopped
    --env-file "$ENV_FILE"
    -p "$PORT"
    -v "$SERVERS_DIR:/app/servers"
)

if [ -f "$COOKIE_FILE_PATH" ]; then
    DOCKER_ARGS+=(
        -v "$COOKIE_FILE_PATH:/app/cookies.txt:ro"
        -e YT_COOKIES_FILE=/app/cookies.txt
    )
fi

sudo docker run "${DOCKER_ARGS[@]}" "$IMAGE_NAME"
