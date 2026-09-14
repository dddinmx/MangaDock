#!/usr/bin/env bash
# Build and push one Docker Hub manifest that supports AMD64 and ARM64.
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-dddinmx/mangadock}"
BUILDER_NAME="${BUILDER_NAME:-mangadock-multiarch}"
VERSION_TAG="${1:-latest}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI is required." >&2
  exit 1
fi

if [[ ! -f Dockerfile || ! -f comic.json ]]; then
  echo "Run this script from the dockerhub-src directory." >&2
  exit 1
fi

python3 - <<'PY'
import json
from pathlib import Path

for name in ("comic.json", "data/comic.json"):
    path = Path(name)
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"{name} is not a valid JSON file: {error}")
    if content != {}:
        raise SystemExit(f"Refusing to publish: {name} must be an empty JSON object.")
print("Release content check passed: comic mapping templates are empty.")
PY

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is unavailable. Start Docker or grant this user access to it." >&2
  exit 1
fi

if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
  docker buildx create --name "$BUILDER_NAME" --driver docker-container --use
else
  docker buildx use "$BUILDER_NAME"
fi
docker buildx inspect --bootstrap

TAGS=(--tag "$IMAGE_NAME:$VERSION_TAG")
if [[ "$VERSION_TAG" != "latest" ]]; then
  TAGS+=(--tag "$IMAGE_NAME:latest")
fi

echo "Publishing ${IMAGE_NAME} (${VERSION_TAG}) for linux/amd64 and linux/arm64..."
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --pull \
  --push \
  "${TAGS[@]}" \
  .

echo "Published: https://hub.docker.com/r/${IMAGE_NAME}/tags"
echo "Verify: docker buildx imagetools inspect ${IMAGE_NAME}:${VERSION_TAG}"
