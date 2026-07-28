# 新机器全新环境部署指南

本文用于把 `interface_projects` 部署到一台新的 Linux 服务器。该方案只迁移代码和部署材料，不迁移旧数据集、MySQL 数据、Neo4j 图数据、Milvus 数据、PageIndex、运行日志或历史模型训练产物。

## 1. 部署结果

部署完成后，新机器运行以下服务：

| 服务 | 端口 | 说明 |
|---|---:|---|
| vLLM | 8000 | Qwen3-4B OpenAI-compatible API |
| 接口 01-10 | 6101-6110 | HTTP API，均提供 `GET /health` |
| 接口 07 UDP | 7107 | 规则生成 UDP 网关 |
| 接口 08 UDP | 7108 | 代码生成 UDP 网关 |
| MySQL | 3306 | 新建空数据库 |
| Neo4j HTTP/Bolt | 7474/7687 | 新建空知识图谱 |
| Milvus Lite | 无独立端口 | 新建本地数据库文件 |

## 2. 服务器要求

推荐环境：

- Ubuntu 22.04 或 24.04
- Python 3.10
- Docker Engine 和 Docker Compose v2
- NVIDIA CUDA 或海光 DCU/DTK 等与目标硬件匹配的计算栈
- 至少 16 GB 内存；模型和批处理负载建议 32 GB 以上
- `/srv/interface_projects` 存放代码，`/srv/model_cache` 存放模型

麒麟操作系统和海光 DCU 不能直接执行下面的 Ubuntu/NVIDIA 命令，请改用 [KYLIN_HYGON_DCU_DEPLOYMENT.md](KYLIN_HYGON_DCU_DEPLOYMENT.md)。

先安装基础软件：

```bash
sudo apt-get update
sudo apt-get install -y git curl rsync build-essential python3 python3-venv python3-pip docker.io docker-compose-plugin
sudo systemctl enable --now docker
docker version
docker compose version
nvidia-smi
```

若新机没有 GPU，可以先启动 MySQL、Neo4j 和不依赖模型的接口，但不能完成全链路验收。

## 3. 获取代码

```bash
sudo mkdir -p /srv/interface_projects /srv/model_cache
sudo chown -R "$USER":"$USER" /srv/interface_projects /srv/model_cache
git clone https://github.com/sh2001sh/interface_projects.git /srv/interface_projects
cd /srv/interface_projects
```

仓库不包含以下内容，部署时也不应从旧机复制：

- `data/datasets/`
- `runtime/`、`runtime_data/`、`tmp/`
- `deploy/runtime/`、`test/output/`
- `model_cache/`
- `deploy/env.sh`

## 4. 配置环境变量

从模板创建仅属于新机器的配置文件：

```bash
cd /srv/interface_projects
cp deploy/env.example deploy/env.sh
chmod 600 deploy/env.sh
```

编辑 `deploy/env.sh`，至少替换以下占位值：

```bash
export MYSQL_ROOT_PASSWORD="使用新的强密码"
export MYSQL_PASSWORD="使用新的业务用户强密码"
export NEO4J_PASSWORD="使用新的强密码"
```

如果数据库部署在其他服务器，同时修改：

```bash
export MYSQL_HOST="新MySQL地址"
export MYSQL_PORT="3306"
export PROTOCOL_CONVERSION_NEO4J_URI="bolt://新Neo4j地址:7687"
```

不要把 `deploy/env.sh` 提交到 Git。模板中的 `change_me` 仅用于提醒配置，不可用于正式环境。

## 5. 生成接口配置

配置源文件为：

- `configs/global.yaml`
- `configs/interfaces/*.yaml`

生成十个接口各自的 `config.yaml`：

```bash
source deploy/env.sh
python3 scripts/generate_interface_configs.py
```

环境变量在运行时覆盖 `config.yaml`，因此密码只放在 `deploy/env.sh` 中。

## 6. 安装 Python 环境

### 方案 A：Conda（包含 vLLM，推荐）

先安装 Miniconda/Anaconda，并确认 `/opt/anaconda3/condabin/conda` 存在，然后执行：

```bash
cd /srv/interface_projects
source deploy/env.sh
bash deploy/install_conda_env.sh
```

如果 Conda 安装在其他目录，在 `deploy/env.sh` 中设置 `CONDA_BIN`。

### 方案 B：venv（不负责 GPU/vLLM 兼容性）

```bash
cd /srv/interface_projects
bash deploy/install_all.sh /srv/interface_projects/.venv
source /srv/interface_projects/.venv/bin/activate
```

生产环境需要根据新机 CUDA 和驱动版本单独确认 PyTorch、bitsandbytes 与 vLLM 的兼容版本。

## 7. 创建全新数据库

项目自带的 Compose 文件会创建新的 MySQL 8.4 和 Neo4j 5.26.19 Community 实例：

```bash
cd /srv/interface_projects
source deploy/env.sh
bash deploy/start_infra.sh
docker compose -f deploy/docker-compose.yml ps
```

初始化项目本地兼容表：

```bash
/opt/anaconda3/condabin/conda run -n interface_projects \
  python /srv/interface_projects/deploy/init_mysql_tables.py
```

该脚本创建 `datasets`、`blocks`、`chunks`、`qa_pairs`、`pipeline_payloads` 和 `finetune_jobs` 等项目兼容表，不创建 protobridge 业务系统拥有的表。

### 业务表责任边界

接口 03、04、05、06 会读取以下 protobridge 业务表中的新数据：

- `dataset`
- `document_split`
- `document_split_block`
- `document_clean`、`document_clean_issue`
- `rag_chunk_task`、`rag_chunk_metadata`
- `doc_qa_pairs`

这些接口保持输出型行为，不负责把结果自动写回业务表。新环境的业务系统必须创建兼容表结构，并负责写入本次部署产生的新数据。没有这些表和写入流程时，健康检查可以通过，但按 `dataset_id` 执行的业务调用会查不到数据。

### Neo4j 空库

Neo4j 不导入旧图数据。接口 07 在空库中不会自动获得历史规则，应按以下流程建立新规则：

1. 使用新协议数据调用接口 07 生成候选规则。
2. 人工审核候选规则。
3. 调用接口 7.1 `POST /api/knowledge/protocol_rules/manual_writeback` 写入 Neo4j。
4. 再次调用接口 07 验证已审核规则能够被读取。

接口 07 不使用本地 JSON 图谱作为运行时回退；Neo4j 不可用时该接口会明确报错。

### Milvus Lite 空库

`MILVUS_LITE_URI` 默认指向：

```text
/srv/interface_projects/runtime_data/milvus/milvus_lite.db
```

首次运行会创建新的空文件，不需要执行数据导入。

## 8. 下载模型

项目使用以下模型：

- `Qwen/Qwen3-4B`
- `Qwen/Qwen3-Embedding-0.6B`
- `Qwen/Qwen3-Reranker-0.6B`

在新机联网下载：

```bash
cd /srv/interface_projects
source deploy/env.sh
/opt/anaconda3/condabin/conda run -n interface_projects \
  python deploy/download_modelscope_models.py
```

确认目录：

```bash
test -d /srv/model_cache/Qwen/Qwen3-4B
test -d /srv/model_cache/Qwen/Qwen3-Embedding-0.6B
test -d /srv/model_cache/Qwen/Qwen3-Reranker-0.6B
```

如果新机不能联网，只复制上述三个模型目录；不要复制旧数据集或运行目录。

## 9. 启动服务

先启动并验证 vLLM：

```bash
cd /srv/interface_projects
source deploy/env.sh
bash deploy/start_vllm.sh
curl -fsS http://127.0.0.1:8000/v1/models
```

再启动十个接口：

```bash
bash deploy/start_all.sh
bash deploy/check_health.sh
```

PID 和日志写入：

- `deploy/runtime/pids/`
- `deploy/runtime/logs/`

停止服务：

```bash
bash deploy/stop_all.sh
bash deploy/stop_vllm.sh
bash deploy/stop_infra.sh
```

## 10. 防火墙和访问控制

只对可信调用方开放接口端口。MySQL、Neo4j Bolt 和 vLLM 默认不应暴露到公网。

```bash
sudo ufw allow from 可信网段 to any port 6101:6110 proto tcp
sudo ufw allow from 可信网段 to any port 7107:7108 proto udp
```

如果数据库与应用在同一台机器，不要对外开放 `3306`、`7474`、`7687` 和 `8000`。

## 11. 部署验收

### 基础健康检查

```bash
cd /srv/interface_projects
source deploy/env.sh
bash deploy/check_health.sh
```

### 合同检查

```bash
bash test/run_smoke_tests.sh --suites health,contract
```

### 全新数据业务验收

按顺序使用一份新协议文档完成：

1. 接口 01 校验文件。
2. 接口 02 上传和拆分，并由业务系统写入新业务库。
3. 接口 03 清洗。
4. 接口 04 语义分块并创建新的 Milvus/PageIndex 数据。
5. 接口 05 生成 QA。
6. 接口 06 抽取和校验 QA。
7. 接口 07 生成规则，人工审核后通过接口 7.1 写入新 Neo4j。
8. 接口 08 生成代码并编译目标工程。
9. 接口 10 评估新规则。

验收时记录每一步的 HTTP 状态码、响应体、`dataset_id`、块数量、QA 数量、规则写入数量和输出路径。

## 12. 常见故障

### 服务健康但业务数据为空

检查新业务库是否已创建 protobridge 表，并确认外部业务系统已经写入新数据。项目兼容表不能替代 protobridge 业务表。

### 接口 07 报 Neo4j 不可用

```bash
docker logs --tail 100 interface_projects_neo4j
docker exec interface_projects_neo4j cypher-shell \
  -u neo4j -p "$NEO4J_PASSWORD" "RETURN 1;"
```

### vLLM 启动失败

```bash
nvidia-smi
tail -n 200 deploy/runtime/logs/vllm.log
```

显存不足时降低 `VLLM_MAX_MODEL_LEN` 或 `VLLM_GPU_MEMORY_UTILIZATION`，但修改后必须重新执行模型和接口验收。

### 配置仍指向旧机器

```bash
rg -n "192\\.168\\.|/nfs/615|/home/hks|password123" \
  configs deploy/env.example 01_validate_protocol_files/config.yaml \
  02_upload_split/config.yaml 03_clean/config.yaml 04_semantic_chunk/config.yaml \
  05_generate_qa/config.yaml 06_extract_validate_qa/config.yaml \
  07_protocol_generate_rules/config.yaml 08_code_generation/config.yaml \
  09_finetune_runtime/config.yaml 10_rule_evaluate/config.yaml
```

该检查应没有输出。`deploy/env.sh` 中的新机真实地址和密码除外。
