#!/usr/bin/env bash
set -euo pipefail
quant_repo_dir="$(git rev-parse --show-toplevel)"
cd "$quant_repo_dir"
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -m twquant secrets audit
elif [[ -x .venv/Scripts/python.exe ]]; then
  exec .venv/Scripts/python.exe -m twquant secrets audit
else
  exec python3 -m twquant secrets audit
fi
