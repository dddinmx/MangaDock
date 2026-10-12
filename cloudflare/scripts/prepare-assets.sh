#!/usr/bin/env bash
# Copy the existing Flask static/ tree into public/static so Workers Static
# Assets serves it at the same /static/... URLs the templates already use.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf public && mkdir -p public
mkdir -p public/static && cp -R ../static/. public/static/ && rm -rf public/static/comic public/static/cover
