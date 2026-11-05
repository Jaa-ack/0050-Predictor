#!/usr/bin/env bash
set -euo pipefail
quant_repo_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$quant_repo_dir"
export LOKY_MAX_CPU_COUNT="${LOKY_MAX_CPU_COUNT:-1}"
exec .venv/bin/python -m twquant --root "${TWQUANT_ROOT:-artifacts/real}" daily \
  --paper --start "${TWQUANT_HISTORY_START:-2014-01-01}" "$@"
