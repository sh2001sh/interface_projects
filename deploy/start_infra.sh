#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/env.sh}"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.yml"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

mkdir -p "${MYSQL_DATA_DIR:-$ROOT_DIR/runtime_data/mysql}"
mkdir -p "${NEO4J_DATA_DIR:-$ROOT_DIR/runtime_data/neo4j/data}"
mkdir -p "${NEO4J_LOGS_DIR:-$ROOT_DIR/runtime_data/neo4j/logs}"
mkdir -p "$(dirname "${SQLITE_DB_PATH:-$ROOT_DIR/runtime_data/sqlite/protocol_db.sqlite3}")"
mkdir -p "$(dirname "${MILVUS_LITE_URI:-$ROOT_DIR/runtime_data/milvus/milvus_lite.db}")"

docker compose -f "$COMPOSE_FILE" up -d mysql neo4j

for container in interface_projects_mysql interface_projects_neo4j; do
  echo "等待容器就绪: $container"
  ready="false"
  for _ in $(seq 1 60); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)"
    if [[ "$status" == "healthy" || "$status" == "running" ]]; then
      echo "$container 状态: $status"
      ready="true"
      break
    fi
    sleep 3
  done
  if [[ "$ready" != "true" ]]; then
    echo "$container 未能在超时时间内就绪" >&2
    docker logs --tail 80 "$container" >&2 || true
    exit 1
  fi
done

docker compose -f "$COMPOSE_FILE" ps
