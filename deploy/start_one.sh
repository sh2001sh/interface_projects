#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/deploy/runtime"
LOG_DIR="$RUNTIME_DIR/logs"
PID_DIR="$RUNTIME_DIR/pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

if [[ -f "$ROOT_DIR/deploy/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/deploy/env.sh"
fi

if [[ -n "${PYTHON_BIN:-}" ]]; then
  RESOLVED_PYTHON_BIN="$PYTHON_BIN"
elif [[ -x "/opt/anaconda3/envs/interface_projects/bin/python" ]]; then
  RESOLVED_PYTHON_BIN="/opt/anaconda3/envs/interface_projects/bin/python"
else
  RESOLVED_PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
fi

if [[ ! -x "$RESOLVED_PYTHON_BIN" ]]; then
  echo "未找到可执行 Python: $RESOLVED_PYTHON_BIN" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "用法: $0 <接口目录名>" >&2
  exit 1
fi

PROJECT_NAME="$1"
PROJECT_DIR="$ROOT_DIR/$PROJECT_NAME"
APP_FILE="$PROJECT_DIR/app.py"
PID_FILE="$PID_DIR/$PROJECT_NAME.pid"
LOG_FILE="$LOG_DIR/$PROJECT_NAME.log"

if [[ ! -f "$APP_FILE" ]]; then
  echo "接口目录不存在或缺少 app.py: $PROJECT_DIR" >&2
  exit 1
fi

LAUNCH_MODE="inline-runner"
case "$PROJECT_NAME" in
  07_protocol_generate_rules|08_code_generation)
    LAUNCH_MODE="app-main"
    ;;
esac

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "$PROJECT_NAME 已在运行，PID=$(cat "$PID_FILE")"
  exit 0
fi

(
  cd "$PROJECT_DIR"
  CONDA_ENV_PREFIX="$(cd "$(dirname "$RESOLVED_PYTHON_BIN")/.." && pwd)"
  if [[ "$LAUNCH_MODE" == "app-main" ]]; then
    setsid env \
      PATH="$CONDA_ENV_PREFIX/bin:$PATH" \
      PYTHONPATH="${PYTHONPATH:-}" \
      LD_LIBRARY_PATH="${CONDA_ENV_PREFIX}/lib:${LD_LIBRARY_PATH:-}" \
      PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}" \
      "$RESOLVED_PYTHON_BIN" "$APP_FILE" \
      >"$LOG_FILE" 2>&1 < /dev/null &
  else
    setsid env \
      PATH="$CONDA_ENV_PREFIX/bin:$PATH" \
      PYTHONPATH="${PYTHONPATH:-}" \
      LD_LIBRARY_PATH="${CONDA_ENV_PREFIX}/lib:${LD_LIBRARY_PATH:-}" \
      PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}" \
      "$RESOLVED_PYTHON_BIN" -c 'from app import app; from runtime_config import get_service_runner_config; runner = get_service_runner_config(); app.run(host=runner["host"], port=runner["port"], debug=False, threaded=runner["threaded"], use_reloader=False)' \
      >"$LOG_FILE" 2>&1 < /dev/null &
  fi
  echo $! >"$PID_FILE"
)

echo "已启动 $PROJECT_NAME"
echo "PID: $(cat "$PID_FILE")"
echo "日志: $LOG_FILE"
