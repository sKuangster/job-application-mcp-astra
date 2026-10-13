#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v docker >/dev/null; then
  echo 'Docker unavailable. Install Docker Desktop and enable this WSL2 distribution under Settings > Resources > WSL Integration.' >&2
  exit 2
fi
export APPLYPILOT_SEARCH_SECRET="${APPLYPILOT_SEARCH_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
if [ "${2:-}" = '--host-ollama' ]; then
  export APPLYPILOT_RESEARCH_OLLAMA_URL='http://host.docker.internal:11434'
fi
compose=(docker compose -f services/research/compose.yaml)
case "${1:-doctor}" in
  install) "${compose[@]}" pull ;;
  start)
    if [ "${2:-}" = '--host-ollama' ]; then
      "${compose[@]}" up -d searxng perplexica
      echo 'Pull qwen2.5:7b and nomic-embed-text in host Ollama; allow Docker to reach port 11434 through your local firewall.'
    else
      "${compose[@]}" up -d
      "${compose[@]}" exec -T ollama ollama pull qwen2.5:7b
      "${compose[@]}" exec -T ollama ollama pull nomic-embed-text
    fi
    echo 'Research UI: http://127.0.0.1:3001. Complete upstream setup once using the local Ollama provider.'
    ;;
  stop) "${compose[@]}" stop ;;
  doctor)
    docker info --format '{{.ServerVersion}}'
    "${compose[@]}" ps
    curl --fail --max-time 10 http://127.0.0.1:3001/api/providers
    curl --fail --max-time 10 http://127.0.0.1:8081/healthz
    ;;
  logs) "${compose[@]}" logs --tail 100 ;;
  *) echo 'Usage: bash scripts/research-stack.sh install|start|stop|doctor|logs [--host-ollama]' >&2; exit 2 ;;
esac
