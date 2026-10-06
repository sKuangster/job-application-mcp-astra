#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/ruff check src tests/unit tests/integration
.venv/bin/mypy
.venv/bin/pytest -q "$@"
if command -v node >/dev/null; then
  node --test tests/js/
  node --test tests/js/ui/
  node_modules/.bin/web-ext lint --source-dir firefox --ignore-files icons/make_icons.py
fi
