#!/bin/bash
# bootstrap.sh — prepares the bot runtime: Python 3.13 (uv standalone)
# + venv ~/.zcode/hooks/venv + pinned dependencies. Idempotent.
#
# Why uv (ADR-010, Oct 2026): the system Python 3.9 has been EOL since
# 2025; Homebrew-python exists for Homebrew formulas and may break on
# upgrades (pydevtools.com/handbook: "Use uv to manage Python instead");
# uv installs official standalone builds (python-build-standalone) in
# user-space, no sudo, native arm64.
set -euo pipefail
cd "$(dirname "$0")"

VENV="$HOME/.zcode/hooks/venv"
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "▶ installing uv (user-space)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

echo "▶ Python 3.13 (uv standalone)"
uv python install 3.13

if [ ! -x "$VENV/bin/python3" ]; then
  echo "▶ creating $VENV"
  uv venv "$VENV" --python 3.13
fi

echo "▶ dependencies (requirements.txt, pinned)"
uv pip install --python "$VENV" -r requirements.txt

"$VENV/bin/python3" --version
"$VENV/bin/python3" -c "import aiohttp; print('aiohttp', aiohttp.__version__)"
echo "✓ runtime ready. deploy.sh uses this venv automatically."
echo "  (LaunchAgents must point to $VENV/bin/python3 — see docs/runbook.md)"
