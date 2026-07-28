#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/env.sh}"
HOST="127.0.0.1"
RUN_REAL_TESTS="false"

usage() {
  cat <<'EOF'
用法:
  ./start_once.sh [--real-tests] [--host 127.0.0.1]

说明:
  一次性启动当前项目所需依赖与服务：
  - conda 环境
  - MySQL / Neo4j
  - ModelScope 模型检查与下载
  - MySQL 初始化
  - vLLM
  - 10 个接口
  - 默认健康检查

可选参数:
  --real-tests   启动完成后，额外执行真实数据全链路测试
  --host <host>  真实测试使用的服务地址，默认 127.0.0.1
  -h, --help     显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --real-tests)
      RUN_REAL_TESTS="true"
      shift
      ;;
    --host)
      HOST="${2:-}"
      if [[ -z "$HOST" ]]; then
        echo "--host 缺少参数" >&2
        exit 1
      fi
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

echo "开始执行一键启动"
"$ROOT_DIR/deploy/bootstrap_all.sh"

if [[ "$RUN_REAL_TESTS" == "true" ]]; then
  echo "开始执行真实数据全链路测试"
  "${CONDA_BIN:-/opt/anaconda3/condabin/conda}" run -n "${CONDA_ENV_NAME:-interface_projects}" \
    python "$ROOT_DIR/test/run_real_pipeline_tests.py" --host "$HOST"
fi

cat <<EOF
一键启动完成

常用地址:
- vLLM: http://127.0.0.1:${VLLM_PORT:-8000}/v1/models
- MySQL: 127.0.0.1:${MYSQL_PORT:-3306}
- Neo4j Browser: http://127.0.0.1:7474

常用命令:
- 停止接口和 vLLM: cd $ROOT_DIR && ./deploy/stop_all.sh
- 停止 MySQL/Neo4j: cd $ROOT_DIR && ./deploy/stop_infra.sh
- 真实链路测试: cd $ROOT_DIR && ${CONDA_BIN:-/opt/anaconda3/condabin/conda} run -n ${CONDA_ENV_NAME:-interface_projects} python ./test/run_real_pipeline_tests.py --host $HOST
EOF
