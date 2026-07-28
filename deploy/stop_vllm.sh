#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/deploy/runtime/pids/vllm.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "未找到 vLLM PID 文件"
  exit 0
fi

pid="$(cat "$PID_FILE")"
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  echo "已停止 vLLM (PID=$pid)"
fi
rm -f "$PID_FILE"
