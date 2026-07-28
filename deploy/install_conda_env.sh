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
ACCELERATOR_BACKEND="${ACCELERATOR_BACKEND:-nvidia}"
INSTALL_VLLM_FROM_PYPI="${INSTALL_VLLM_FROM_PYPI:-true}"
INSTALL_PROJECT_REQUIREMENTS="${INSTALL_PROJECT_REQUIREMENTS:-true}"
REQUIREMENTS_FILE="${REQUIREMENTS_FILE:-$ROOT_DIR/requirements-all.txt}"

if [[ ! -x "$CONDA_BIN" ]]; then
  echo "未找到 conda 可执行文件: $CONDA_BIN" >&2
  exit 1
fi

if ! "$CONDA_BIN" env list | awk '{print $1}' | grep -Fxq "$CONDA_ENV_NAME"; then
  "$CONDA_BIN" create -y -n "$CONDA_ENV_NAME" "python=$PYTHON_VERSION"
fi

"$CONDA_BIN" run -n "$CONDA_ENV_NAME" python -m pip install --upgrade pip "setuptools<81" wheel
if [[ "$ACCELERATOR_BACKEND" == "dcu" && "$INSTALL_VLLM_FROM_PYPI" == "true" ]]; then
  echo "海光 DCU 环境禁止自动安装 PyPI 通用 vLLM，请设置 INSTALL_VLLM_FROM_PYPI=false" >&2
  exit 1
fi
if [[ "$INSTALL_VLLM_FROM_PYPI" == "true" ]]; then
  "$CONDA_BIN" run -n "$CONDA_ENV_NAME" pip install vllm
fi
if [[ "$INSTALL_PROJECT_REQUIREMENTS" == "true" ]]; then
  if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
    echo "依赖文件不存在: $REQUIREMENTS_FILE" >&2
    exit 1
  fi
  "$CONDA_BIN" run -n "$CONDA_ENV_NAME" pip install -r "$REQUIREMENTS_FILE"
fi

echo "conda 环境已准备完成: $CONDA_ENV_NAME"
echo "计算卡后端: $ACCELERATOR_BACKEND"
echo "激活命令: \"$CONDA_BIN\" activate \"$CONDA_ENV_NAME\""
