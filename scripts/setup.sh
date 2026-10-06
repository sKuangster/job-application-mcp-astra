#!/usr/bin/env bash
# ApplyPilot setup for macOS/Linux/WSL.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null; then uv venv .venv --python 3.12; else python3 -m venv .venv; fi
fi
if command -v uv >/dev/null; then uv pip install --python .venv/bin/python -e ".[dev]"; else .venv/bin/pip install -e ".[dev]"; fi
if command -v npm >/dev/null; then npm install --no-audit --no-fund; else echo "Node.js not found: JS tests/packaging unavailable"; fi
.venv/bin/applypilot setup
