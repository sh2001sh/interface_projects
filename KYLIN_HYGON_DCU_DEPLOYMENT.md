# 麒麟操作系统 + 海光 DCU 部署指南

本文是 [NEW_MACHINE_DEPLOYMENT.md](NEW_MACHINE_DEPLOYMENT.md) 的海光专用补充。数据库仍使用全新 MySQL、Neo4j 和 Milvus Lite，不迁移旧数据。

## 1. 重要边界

海光 DCU 使用 DTK/HIP 软件栈，不应直接安装 NVIDIA CUDA 包，也不能默认执行 `pip install vllm`。以下组件必须由目标机器供应商或光合开发者社区提供相互匹配的版本：

- 海光 DCU 驱动
- DTK/HIP Runtime
- 海光适配的 PyTorch
- 海光适配的 vLLM 或其他 OpenAI-compatible 推理服务

DTK 官方入口：<https://developer.sourcefind.cn/dtk/>。该页面明确列出麒麟为 DTK 支持的操作系统之一。实际安装包必须按 DCU 型号、麒麟发行版、内核和 DTK 版本选择，不要跨版本混装。

## 2. 收集目标机信息

在安装任何 Python GPU 包前执行并保存结果：

```bash
cat /etc/os-release
uname -a
uname -m
lspci -nn | grep -Ei 'display|vga|dcu|hygon'
command -v hy-smi || true
hy-smi || true
rpm -qa | grep -Ei 'dtk|hygon|dcu|hip|docker|podman' || true
```

至少确认：

- 麒麟具体版本和 Service Pack
- CPU 架构，通常应为 `x86_64`
- DCU 具体型号和数量
- 当前内核版本
- 已安装驱动和 DTK 版本
- 目标机是否允许联网、使用 Docker、访问模型仓库

只有“海光计算卡”这一项不足以确定 PyTorch 和 vLLM 安装包版本。

## 3. 安装基础系统依赖

麒麟服务器版通常使用 RPM 系包管理器。按机器实际可用命令执行其中一组：

```bash
sudo dnf install -y git curl rsync gcc gcc-c++ make python3 python3-pip pciutils
```

或：

```bash
sudo yum install -y git curl rsync gcc gcc-c++ make python3 python3-pip pciutils
```

若目标麒麟版本基于 Debian/Ubuntu，再使用 `apt`。不要只根据“麒麟”名称猜测包管理器。

Docker/Podman 应使用麒麟系统或服务器供应商认证的软件源。项目建议：

- MySQL、Neo4j 使用容器运行。
- vLLM 默认在宿主机 DTK 环境运行，减少 DCU 容器透传差异。
- 若必须用 DCU 容器，使用海光提供的基础镜像和设备映射方式。

## 4. 安装驱动和 DTK

从服务器供应商或 DTK 官方下载区获取与当前 DCU、麒麟版本和内核匹配的安装包。驱动安装完成后按厂商要求重启，再验证：

```bash
hy-smi
```

验收要求：

- 命令退出码为 0。
- 能看到全部 DCU。
- 卡型、显存、驱动版本和健康状态正常。

如果 `hy-smi` 不存在或无法识别卡，停止后续 Python 和 vLLM 安装，先修复驱动/DTK。

## 5. 获取项目并配置海光模式

```bash
git clone --branch deploy/fresh-machine-20260728 \
  https://github.com/sh2001sh/interface_projects.git \
  /srv/interface_projects
cd /srv/interface_projects
cp deploy/env.example deploy/env.sh
chmod 600 deploy/env.sh
```

修改 `deploy/env.sh`：

```bash
export ACCELERATOR_BACKEND="dcu"
export ACCELERATOR_VISIBLE_DEVICES="0"
export INSTALL_VLLM_FROM_PYPI="false"
export INSTALL_PROJECT_REQUIREMENTS="true"
export REQUIREMENTS_FILE="/srv/interface_projects/requirements-dcu.txt"
export DTK_ENV_FILE="/opt/dtk/env.sh"

export MODEL_CACHE_DIR="/srv/model_cache"
export LLM_MODEL_DIR="$MODEL_CACHE_DIR/Qwen/Qwen3-4B"
export EMBED_MODEL_DIR="$MODEL_CACHE_DIR/Qwen/Qwen3-Embedding-0.6B"
export RERANK_MODEL_DIR="$MODEL_CACHE_DIR/Qwen/Qwen3-Reranker-0.6B"

export VLLM_BIN="/opt/anaconda3/envs/interface_projects/bin/vllm"
export VLLM_MAX_MODEL_LEN="8192"
export VLLM_GPU_MEMORY_UTILIZATION="0.85"
```

`DTK_ENV_FILE` 必须改成目标机实际存在的环境脚本；部分 DTK 版本可能使用其他路径。

## 6. 安装厂商 PyTorch 和 vLLM

先创建环境：

```bash
export ACCELERATOR_BACKEND=dcu
export INSTALL_VLLM_FROM_PYPI=false
export INSTALL_PROJECT_REQUIREMENTS=false
export REQUIREMENTS_FILE=/srv/interface_projects/requirements-dcu.txt
bash deploy/install_conda_env.sh
```

然后在同一个 `interface_projects` Conda 环境中，按照海光提供的安装说明安装：

1. 与当前 DTK 匹配的 PyTorch wheel。
2. 与该 PyTorch/DTK 匹配的 vLLM wheel、源码构建产物或推理镜像。
3. 海光版本明确要求的 Transformers 等依赖版本。

厂商 PyTorch 和 vLLM 安装完成后，再安装项目依赖：

```bash
export INSTALL_PROJECT_REQUIREMENTS=true
bash deploy/install_conda_env.sh
```

这个顺序可以避免 `sentence-transformers` 等上层包在环境为空时自动拉取通用 PyTorch。

不要再执行以下命令：

```text
pip install torch
pip install vllm
pip install bitsandbytes
```

这些 PyPI 通用包可能覆盖厂商适配版本。`requirements-dcu.txt` 已故意排除 `torch`、`vllm` 和 `bitsandbytes`。

如果厂商 vLLM 暂不支持当前 Qwen3-4B，可以把模型服务部署在另一台兼容机器上，然后设置：

```bash
export USE_VLLM=true
export VLLM_URL="http://模型服务器地址:8000"
```

接口层只依赖 OpenAI-compatible HTTP 服务，不要求模型服务必须与接口运行在同一台机器。

## 7. 验证 DCU Python 环境

```bash
cd /srv/interface_projects
source deploy/env.sh
bash deploy/check_hygon_dcu.sh
```

检查脚本必须同时证明：

- `hy-smi` 正常。
- 厂商 PyTorch 可以导入。
- `torch.cuda.is_available()` 为 `True`。
- 至少识别到一张计算卡。
- 能输出卡名称。

海光适配的 PyTorch 通常保持 `torch.cuda` API 兼容层，因此这里检查 `torch.cuda` 并不表示使用 NVIDIA CUDA。

## 8. 启动模型服务

```bash
source deploy/env.sh
bash deploy/start_vllm.sh
curl -fsS http://127.0.0.1:8000/v1/models
```

`start_vllm.sh` 在 DCU 模式会：

- 检查 `hy-smi`。
- 按需加载 `DTK_ENV_FILE`。
- 同时设置 `HIP_VISIBLE_DEVICES`、`ROCR_VISIBLE_DEVICES` 和兼容层使用的 `CUDA_VISIBLE_DEVICES`。
- 使用 `VLLM_BIN` 指定的厂商 vLLM。

首次启动后检查日志：

```bash
tail -n 200 deploy/runtime/logs/vllm.log
```

必须实际发送一次推理请求，不能只以进程存在作为验收结果。

## 9. 数据库和接口部署

MySQL、Neo4j、Milvus Lite 的全新环境流程不依赖 CUDA/DCU，继续执行通用文档的以下章节：

- 创建 `deploy/env.sh` 中的新密码。
- `bash deploy/start_infra.sh`。
- `python deploy/init_mysql_tables.py`。
- 下载或离线复制三个 Qwen 模型目录。
- `bash deploy/start_all.sh`。
- `bash deploy/check_health.sh`。

业务库责任边界不变：protobridge 业务表和新数据由外部业务系统创建、写入，03/04/05 不自动写回业务库。

## 10. 海光环境验收

按顺序执行：

```bash
hy-smi
bash deploy/check_hygon_dcu.sh
curl -fsS http://127.0.0.1:8000/v1/models
bash deploy/check_health.sh
bash test/run_smoke_tests.sh --suites health,contract
```

再使用一份新协议文档完成接口 01 至 10 的业务链路。记录：

- 麒麟版本、内核、DCU 型号、驱动和 DTK 版本。
- PyTorch 和 vLLM 的厂商包版本。
- 模型请求响应和首 token/总耗时。
- 十个接口健康状态。
- 新数据集、QA、规则和生成代码的实际结果。

## 11. 常见错误

### 安装脚本提示禁止 PyPI vLLM

说明已设置 `ACCELERATOR_BACKEND=dcu`，但仍保留 `INSTALL_VLLM_FROM_PYPI=true`。改为 `false`，再安装厂商 vLLM。

### `hy-smi` 正常但 PyTorch 看不到卡

通常是 PyTorch wheel、DTK 或 Python 版本不匹配。核对厂商兼容矩阵，不要用通用 PyTorch 覆盖安装。

### vLLM 导入时报 HIP/算子错误

核对厂商 vLLM 与 DTK、PyTorch、Triton/算子包版本。该错误不能通过改项目业务代码解决。

### `bitsandbytes` 安装或加载失败

DCU 部署默认不安装它。若微调流程强依赖量化，必须使用海光支持的等价实现或厂商明确支持的版本，并单独验证接口 09。
