#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v node >/dev/null && [ -x "$HOME/.local/node/bin/node" ]; then
  export PATH="$HOME/.local/node/bin:$PATH"
fi
.venv/bin/ruff check src tests/unit tests/integration
.venv/bin/mypy
.venv/bin/pytest -q tests/unit tests/integration tests/test_db.py tests/test_state.py "$@"
if command -v node >/dev/null; then
  node --test tests/js/
  node --test tests/js/ui/
  node --test tests/browser/dom.test.cjs tests/browser/ownership.test.cjs
  node_modules/.bin/web-ext lint --source-dir firefox --ignore-files icons/make_icons.py
fi
