#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/env.sh}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

"$ROOT_DIR/deploy/install_conda_env.sh"
"$ROOT_DIR/deploy/start_infra.sh"

"${CONDA_BIN:-/opt/anaconda3/condabin/conda}" run -n "${CONDA_ENV_NAME:-interface_projects}" \
  python "$ROOT_DIR/deploy/download_modelscope_models.py"
"${CONDA_BIN:-/opt/anaconda3/condabin/conda}" run -n "${CONDA_ENV_NAME:-interface_projects}" \
  python "$ROOT_DIR/deploy/init_mysql_tables.py"

"$ROOT_DIR/deploy/start_vllm.sh"

echo "等待 vLLM 就绪"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${VLLM_PORT:-8000}/v1/models" >/dev/null 2>&1; then
    echo "vLLM 已就绪"
    break
  fi
  sleep 5
done

"$ROOT_DIR/deploy/start_all.sh"
"$ROOT_DIR/deploy/check_health.sh"
