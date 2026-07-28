#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/env.sh}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

CONDA_BIN="${CONDA_BIN:-/opt/anaconda3/condabin/conda}"
CONDA_ENV_NAME="${CONDA_ENV_NAME:-interface_projects}"

"$CONDA_BIN" run -n "$CONDA_ENV_NAME" python "$ROOT_DIR/test/run_smoke_tests.py" --host 127.0.0.1 --suites health,contract,rule-eval
