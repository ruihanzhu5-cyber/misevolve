#!/usr/bin/env bash

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"
TARGETS=("$@")
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=(cc codex openclaw hermes)

build() {
  local name="$1" dockerfile="$2" ctx="$3" tag="$4"
  echo "=== $name -> $tag ==="
  docker build --build-arg "PIP_INDEX_URL=$PIP_INDEX_URL" -f "$dockerfile" -t "$tag" "$ctx"
}

for t in "${TARGETS[@]}"; do
  case "$t" in
    cc)       build cc       docker/claudecode/Dockerfile docker/claudecode misevolve-cc:latest ;;
    codex)    build codex    docker/codex/Dockerfile docker/codex misevolve-codex:latest ;;
    openclaw) build openclaw docker/openclaw/Dockerfile docker/openclaw misevolve-openclaw:latest ;;
    hermes)   build hermes   docker/hermes/Dockerfile docker/hermes misevolve-hermes:latest ;;
    *) echo "unknown harness: $t (want: cc codex openclaw hermes)" >&2; exit 2 ;;
  esac
done

echo
echo "=== built ==="
docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}' | grep '^misevolve-' || true
