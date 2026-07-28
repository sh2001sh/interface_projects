# interface_projects 部署说明

## 1. 适用范围

本文档仅针对当前 `interface_projects` 目录的独立部署与迁移，不依赖仓库外部脚本。

部署目标：

- 在另一台 Linux 服务器上独立运行 10 个接口
- 使用 Python 虚拟环境安装依赖
- 手工启动接口进程，或使用本目录内置辅助脚本批量启动
- 提供统一测试脚本和统一测试数据目录

## 2. 目录说明

部署时至少需要保留以下目录和文件：

- `interface_projects/01_validate_protocol_files` 到 `interface_projects/10_rule_evaluate`
- `interface_projects/configs/`
- `interface_projects/scripts/generate_interface_configs.py`
- `interface_projects/requirements-all.txt`
- `interface_projects/deploy/`
- `interface_projects/test/`

说明：

- 各接口运行时读取的是自己目录下的 `config.yaml`
- 这些 `config.yaml` 是由统一母配置和每接口差异配置自动生成的
- 不建议手工直接改各接口目录下的 `config.yaml`

## 3. 端口与接口

| 接口 | 目录 | 默认端口 | 主接口 |
|------|------|----------|--------|
| 1 | `01_validate_protocol_files` | `6101` | `POST /api/data/validate_protocol_files` |
| 2 | `02_upload_split` | `6102` | `POST /api/data/upload_split` |
| 3 | `03_clean` | `6103` | `POST /api/data/clean` |
| 4 | `04_semantic_chunk` | `6104` | `POST /api/data/semantic_chunk` |
| 5 | `05_generate_qa` | `6105` | `POST /api/knowledge/generate_qa` |
| 6 | `06_extract_validate_qa` | `6106` | `POST /api/knowledge/extract_validate_qa` |
| 7 | `07_protocol_generate_rules` | `6107` | `POST /api/knowledge/protocol_generate_rules` |
| 8 | `08_code_generation` | `6108` | `POST /api/code_generation/generate` |
| 9 | `09_finetune_runtime` | `6109` | `POST /api/model/finetune/action` |
| 10 | `10_rule_evaluate` | `6110` | `POST /api/knowledge/rule_evaluate` |

所有接口都暴露 `GET /health`。

索引相关补充：

- `02_upload_split` 在上传拆分完成后，会立即按文档创建 PageIndex 文档索引
- `04_semantic_chunk` 默认不再重复创建文档索引
- 如需手工重建，调用 `POST /api/data/update_doc_index` 时优先传 `document_path` / `document_paths`
- `07_protocol_generate_rules` 使用 PageIndex 时优先传 `index_registry_path`，可传单个 registry 文件或 registry 目录

## 4. 服务器基础环境

建议目标环境：

- Ubuntu 22.04 / 24.04
- Python 3.10 或 3.11
- `git`、`curl`、`build-essential`

推荐先安装系统依赖：

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv python3-pip \
  git curl build-essential pkg-config
```

如果需要编译带 CUDA 的 `torch` / `bitsandbytes` / `vllm`，还要提前准备：

- NVIDIA 驱动
- CUDA 运行时
- 对应版本的 `torch`

## 5. 代码迁移

推荐直接把整个 `interface_projects` 目录拷贝到目标服务器：

```bash
rsync -av /path/to/interface_projects/ user@TARGET:/srv/interface_projects/
```

以下文档默认部署目录为：

```bash
/srv/interface_projects
```

## 6. Python 虚拟环境与依赖安装

进入部署目录：

```bash
cd /srv/interface_projects
```

使用本目录内置脚本创建虚拟环境并安装依赖：

```bash
bash deploy/install_all.sh /srv/interface_projects/.venv
source /srv/interface_projects/.venv/bin/activate
```

如果你不想用脚本，也可以手工执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements-all.txt
```

## 7. 生成接口配置

当前配置管理流程：

- 公共配置：`configs/global.yaml`
- 每接口差异配置：`configs/interfaces/*.yaml`
- 生成目标：各接口目录下的 `config.yaml`

首次部署或修改配置后，先执行：

```bash
cd /srv/interface_projects
python3 scripts/generate_interface_configs.py
```

如果只想重生成部分接口：

```bash
python3 scripts/generate_interface_configs.py --interfaces 07_protocol_generate_rules,10_rule_evaluate
```

建议约定：

- 改公共项：编辑 `configs/global.yaml`
- 改单接口端口等差异项：编辑 `configs/interfaces/<接口名>.yaml`
- 改完后重新执行生成脚本

## 8. 环境变量配置

复制环境变量模板：

```bash
cp deploy/env.example deploy/env.sh
```

根据实际环境修改：

- `MYSQL_*`
- `USE_VLLM`
- `VLLM_URL`
- `MODEL_CACHE_DIR`
- `PROTOCOL_CONVERSION_NEO4J_*`：仅接口 `07_protocol_generate_rules` 的规则生成与人工审核回写使用

使用时执行：

```bash
source deploy/env.sh
```

说明：

- 环境变量是**覆盖项**
- 如果环境变量和各接口 `config.yaml` 同名项同时存在，当前实现会优先使用环境变量
- 如果不需要覆盖，保持 `deploy/env.sh` 最小化即可

## 9. 外部依赖安装与迁移

### 9.1 MySQL

适用接口：

- 1、2、4、5、6、7、9、10 推荐配置

快速安装：

```bash
sudo apt-get install -y mysql-server
sudo systemctl enable --now mysql
```

创建数据库与用户：

```bash
sudo mysql -e "CREATE DATABASE protocol_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
sudo mysql -e "CREATE USER 'protocol_user'@'%' IDENTIFIED BY 'change_me';"
sudo mysql -e "GRANT ALL PRIVILEGES ON protocol_db.* TO 'protocol_user'@'%';"
sudo mysql -e "FLUSH PRIVILEGES;"
```

如果迁移已有数据，源服务器执行：

```bash
mysqldump -u root -p --databases protocol_db > protocol_db.sql
```

目标服务器导入：

```bash
mysql -u root -p < protocol_db.sql
```

如果只做快速部署验证，不想先装 MySQL，可以临时使用 SQLite 回退：

```bash
export MYSQL_USE_SQLITE=true
export MYSQL_AUTO_FALLBACK_SQLITE=true
```

### 9.2 Neo4j

适用接口：

- 7 必需

建议与当前环境保持一致，使用 Neo4j Community `5.26.19`。

安装方式一：拷贝现成安装目录。

安装方式二：在目标服务器解压同版本发行包，例如：

```bash
mkdir -p /opt
tar -xf neo4j-community-5.26.19-unix.tar.gz -C /opt
/opt/neo4j-community-5.26.19/bin/neo4j start
```

修改密码：

```bash
/opt/neo4j-community-5.26.19/bin/cypher-shell -u neo4j -p neo4j
```

如果迁移已有图库数据，源服务器执行：

```bash
/opt/neo4j-community-5.26.19/bin/neo4j-admin database dump neo4j --to-path /tmp/neo4j_dump
```

将 dump 文件拷贝到目标服务器后执行：

```bash
/opt/neo4j-community-5.26.19/bin/neo4j-admin database load neo4j --from-path /tmp/neo4j_dump --overwrite-destination=true
```

对应环境变量示例：

```bash
export PROTOCOL_CONVERSION_NEO4J_ENABLED=true
export PROTOCOL_CONVERSION_NEO4J_URI="bolt://127.0.0.1:7687"
export PROTOCOL_CONVERSION_NEO4J_USERNAME="neo4j"
export PROTOCOL_CONVERSION_NEO4J_PASSWORD="change_me"
export PROTOCOL_CONVERSION_NEO4J_DATABASE="neo4j"
export PROTOCOL_CONVERSION_JSON_FALLBACK=false
```

说明：

- 当前只保留接口 `07` 使用知识图谱。
- 当前不再保留本地 JSON 图谱读回退；Neo4j 未配置或不可达时，接口 `07` 会直接报错。
- `07_protocol_generate_rules/data/protocol_conversion_kb/` 仅保留历史文件，不再作为运行时回退源。

### 9.3 模型与 vLLM

适用接口：

- 4、5、6、7、9、10

推荐方式：

- 大模型通过 vLLM 服务提供
- Embedding / Reranker 模型通过本地目录或缓存目录加载

模型缓存目录建议统一放到：

```bash
/srv/model_cache
```

拷贝源服务器模型缓存：

```bash
rsync -av /path/to/model_cache/ user@TARGET:/srv/model_cache/
```

启动 vLLM 示例：

```bash
pip install vllm
vllm serve /srv/model_cache/Qwen3-4B --host 0.0.0.0 --port 8000
```

环境变量：

```bash
export MODEL_CACHE_DIR="/srv/model_cache"
export USE_VLLM=true
export VLLM_URL="http://127.0.0.1:8000"
export LLM_MODEL_NAME="/srv/model_cache/Qwen3-4B"
export EMBED_MODEL_DIR="/srv/model_cache/Qwen/Qwen3-Embedding-0___6B"
export EMBED_MODEL_PATH="/srv/model_cache/Qwen/Qwen3-Embedding-0___6B"
export RERANK_MODEL_DIR="/srv/model_cache/Qwen3-Reranker-0___6B"
```

如果不想通过环境变量覆盖，也可以直接把这些路径写入：

- `configs/global.yaml`

然后重新执行：

```bash
python3 scripts/generate_interface_configs.py
```

### 9.4 Milvus / milvus-lite

适用接口：

- 4 必需
- 10 若开启向量检索评估则建议配置

快速方式建议直接使用 `milvus-lite`，它已经包含在 `requirements-all.txt`。

如果需要独立 Milvus 服务，再按目标服务器实际方案部署。

## 10. 启动方式

### 10.1 单接口启动

```bash
cd /srv/interface_projects
source .venv/bin/activate
source deploy/env.sh
bash deploy/start_one.sh 07_protocol_generate_rules
```

### 10.2 全部接口启动

```bash
cd /srv/interface_projects
source .venv/bin/activate
source deploy/env.sh
bash deploy/start_all.sh
```

日志与 PID 文件目录：

- `deploy/runtime/logs/`
- `deploy/runtime/pids/`

停止全部接口：

```bash
bash deploy/stop_all.sh
```

## 11. 按目录手工启动

如果不用辅助脚本，也可以逐个目录启动：

```bash
cd /srv/interface_projects/07_protocol_generate_rules
source ../.venv/bin/activate
source ../deploy/env.sh
python app.py
```

如果你改过 `configs/global.yaml` 或 `configs/interfaces/*.yaml`，启动前先重新生成：

```bash
cd /srv/interface_projects
python3 scripts/generate_interface_configs.py
```

## 12. 统一测试

测试脚本与测试数据统一放在：

- `test/run_smoke_tests.py`
- `test/run_smoke_tests.sh`
- `test/data/`

### 12.1 快速健康检查

```bash
cd /srv/interface_projects
source .venv/bin/activate
source deploy/env.sh
bash test/run_smoke_tests.sh --suites health
```

### 12.2 健康检查 + 合同检查

```bash
bash test/run_smoke_tests.sh --suites health,contract
```

### 12.3 增加接口 8 功能验证

```bash
bash test/run_smoke_tests.sh --suites health,contract,codegen --interfaces 08,10
```

### 12.4 增加接口 10 规则评估验证

接口 10 可以在无模型模式下运行回退评估：

```bash
bash test/run_smoke_tests.sh --suites health,rule-eval --interfaces 10
```

测试结果会写到：

- `test/output/smoke_report.json`

## 13. 接口依赖总览

| 接口 | 必要依赖 | 建议依赖 |
|------|----------|----------|
| 1 | Python、文档解析库 | MySQL |
| 2 | Python、文档解析库 | MySQL |
| 3 | Python | 无 |
| 4 | Python、Embedding、Milvus-lite | MySQL、vLLM |
| 5 | Python、LLM | MySQL |
| 6 | Python、LLM | MySQL |
| 7 | Python、LLM | Neo4j、MySQL |
| 8 | Python | 无 |
| 9 | Python、微调依赖 | GPU、模型权重 |
| 10 | Python | Embedding、Reranker |

## 14. 建议部署顺序

1. 迁移 `interface_projects`
2. 创建虚拟环境并安装 `requirements-all.txt`
3. 修改 `configs/global.yaml` 与 `configs/interfaces/*.yaml`
4. 执行 `python3 scripts/generate_interface_configs.py`
5. 配置 `deploy/env.sh`（仅写需要覆盖的项）
6. 安装 MySQL / Neo4j / 模型服务
7. 启动 8、10，先跑最小功能验证
8. 启动其余接口，再跑全量健康检查
