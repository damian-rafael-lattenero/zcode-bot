#!/bin/bash
# bootstrap.sh — prepara el runtime del bot: Python 3.13 (uv standalone)
# + venv ~/.zcode/hooks/venv + dependencias pinneadas. Idempotente.
#
# Por qué uv (ADR-010, oct 2026): Python 3.9 del sistema está EOL desde
# 2025; Homebrew-python existe para fórmulas de Homebrew y puede romper
# en upgrades (pydevtools.com/handbook: "Use uv to manage Python instead");
# uv instala builds standalone oficiales (python-build-standalone) en
# user-space, sin sudo, arm64 nativo.
set -euo pipefail
cd "$(dirname "$0")"

VENV="$HOME/.zcode/hooks/venv"
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "▶ instalando uv (user-space)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

echo "▶ Python 3.13 (uv standalone)"
uv python install 3.13

if [ ! -x "$VENV/bin/python3" ]; then
  echo "▶ creando $VENV"
  uv venv "$VENV" --python 3.13
fi

echo "▶ dependencias (requirements.txt, pinneadas)"
uv pip install --python "$VENV" -r requirements.txt

"$VENV/bin/python3" --version
"$VENV/bin/python3" -c "import aiohttp; print('aiohttp', aiohttp.__version__)"
echo "✓ runtime listo. deploy.sh usa este venv automáticamente."
echo "  (Los LaunchAgents deben apuntar a $VENV/bin/python3 — ver docs/runbook.md)"
