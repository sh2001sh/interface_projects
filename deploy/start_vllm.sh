#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/env.sh}"
RUNTIME_DIR="$ROOT_DIR/deploy/runtime"
LOG_DIR="$RUNTIME_DIR/logs"
PID_DIR="$RUNTIME_DIR/pids"
LOG_FILE="$LOG_DIR/vllm.log"
PID_FILE="$PID_DIR/vllm.pid"
mkdir -p "$LOG_DIR" "$PID_DIR"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

CONDA_BIN="${CONDA_BIN:-/opt/anaconda3/condabin/conda}"
CONDA_ENV_NAME="${CONDA_ENV_NAME:-interface_projects}"
CONDA_ENV_PREFIX="${CONDA_ENV_PREFIX:-/opt/anaconda3/envs/$CONDA_ENV_NAME}"
VLLM_BIN="${VLLM_BIN:-$CONDA_ENV_PREFIX/bin/vllm}"
LLM_MODEL_DIR="${LLM_MODEL_DIR:-$ROOT_DIR/model_cache/Qwen/Qwen3-4B}"
VLLM_HOST="${VLLM_HOST:-0.0.0.0}"
VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_GPU_DEVICES="${VLLM_GPU_DEVICES:-0}"
VLLM_TENSOR_PARALLEL_SIZE="${VLLM_TENSOR_PARALLEL_SIZE:-1}"
VLLM_MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-8192}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.90}"
ACCELERATOR_BACKEND="${ACCELERATOR_BACKEND:-nvidia}"
ACCELERATOR_VISIBLE_DEVICES="${ACCELERATOR_VISIBLE_DEVICES:-$VLLM_GPU_DEVICES}"
DTK_ENV_FILE="${DTK_ENV_FILE:-}"

if [[ "$ACCELERATOR_BACKEND" == "dcu" ]]; then
  if [[ -n "$DTK_ENV_FILE" && -f "$DTK_ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$DTK_ENV_FILE"
  fi
  if ! command -v hy-smi >/dev/null 2>&1; then
    echo "未找到 hy-smi，请先安装与海光 DCU 匹配的驱动和 DTK" >&2
    exit 1
  fi
fi

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "vLLM 已在运行，PID=$(cat "$PID_FILE")"
  exit 0
fi

if [[ ! -d "$LLM_MODEL_DIR" ]]; then
  echo "未找到大模型目录: $LLM_MODEL_DIR" >&2
  exit 1
fi

if [[ ! -x "$VLLM_BIN" ]]; then
  echo "未找到 vLLM 可执行文件: $VLLM_BIN" >&2
  exit 1
fi

setsid env \
  PATH="$CONDA_ENV_PREFIX/bin:$PATH" \
  PYTHONPATH="${PYTHONPATH:-}" \
  LD_LIBRARY_PATH="${CONDA_ENV_PREFIX}/lib:${LD_LIBRARY_PATH:-}" \
  CUDA_VISIBLE_DEVICES="$ACCELERATOR_VISIBLE_DEVICES" \
  HIP_VISIBLE_DEVICES="$ACCELERATOR_VISIBLE_DEVICES" \
  ROCR_VISIBLE_DEVICES="$ACCELERATOR_VISIBLE_DEVICES" \
  PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}" \
  "$VLLM_BIN" serve "$LLM_MODEL_DIR" \
  --host "$VLLM_HOST" \
  --port "$VLLM_PORT" \
  --served-model-name "Qwen3-4B" \
  --tensor-parallel-size "$VLLM_TENSOR_PARALLEL_SIZE" \
  --max-model-len "$VLLM_MAX_MODEL_LEN" \
  --gpu-memory-utilization "$VLLM_GPU_MEMORY_UTILIZATION" \
  >"$LOG_FILE" 2>&1 < /dev/null &

echo $! >"$PID_FILE"
VLLM_PID="$(cat "$PID_FILE")"
READY_URL="http://127.0.0.1:${VLLM_PORT}/v1/models"

for _ in $(seq 1 180); do
  if curl -fsS "$READY_URL" >/dev/null 2>&1; then
    echo "已启动 vLLM"
    echo "PID: $VLLM_PID"
    echo "日志: $LOG_FILE"
    echo "就绪接口: $READY_URL"
    exit 0
  fi
  if ! kill -0 "$VLLM_PID" 2>/dev/null; then
    echo "vLLM 启动失败，请检查日志: $LOG_FILE" >&2
    exit 1
  fi
  sleep 2
done

echo "vLLM 仍在启动中，请稍后检查: $READY_URL" >&2
echo "PID: $VLLM_PID"
echo "日志: $LOG_FILE"
exit 1
