#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/env.sh}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

CONDA_BIN="${CONDA_BIN:-/opt/anaconda3/condabin/conda}"
CONDA_ENV_NAME="${1:-${CONDA_ENV_NAME:-interface_projects}}"
PYTHON_VERSION="${PYTHON_VERSION:-3.10}"

if [[ ! -x "$CONDA_BIN" ]]; then
  echo "未找到 conda 可执行文件: $CONDA_BIN" >&2
  exit 1
fi

if ! "$CONDA_BIN" env list | awk '{print $1}' | grep -Fxq "$CONDA_ENV_NAME"; then
  "$CONDA_BIN" create -y -n "$CONDA_ENV_NAME" "python=$PYTHON_VERSION"
fi

"$CONDA_BIN" run -n "$CONDA_ENV_NAME" python -m pip install --upgrade pip "setuptools<81" wheel
"$CONDA_BIN" run -n "$CONDA_ENV_NAME" pip install vllm
"$CONDA_BIN" run -n "$CONDA_ENV_NAME" pip install -r "$ROOT_DIR/requirements-all.txt"

echo "conda 环境已准备完成: $CONDA_ENV_NAME"
echo "激活命令: source /opt/anaconda3/bin/activate \"$CONDA_ENV_NAME\""
