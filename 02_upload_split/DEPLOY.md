# 02_upload_split 部署说明

## 服务信息

- 目录：`02_upload_split`
- 启动入口：`python app.py`
- 默认端口：`6102`
- 健康检查：`GET /health`
- 主接口：`POST /api/data/upload_split`

## 依赖

- 必需：Python 虚拟环境、`requirements-all.txt`
- 推荐：MySQL
- 可选：SQLite 回退
- PDF/Office 主解析链路依赖 `docling`。PDF 默认使用 Docling 版面模型，`OCR` 默认关闭。
- 若部署环境无法联网拉取模型，请预先准备 Docling 模型缓存，并设置 `DOCLING_ARTIFACTS_PATH=/path/to/docling-artifacts`。
- 如需显式指定推理设备，可设置 `DOCLING_DEVICE=cpu|cuda`。当前默认值为 `cpu`，用于规避环境中的 CUDA 兼容风险。

## 准备 Docling 本地模型缓存

默认建议使用项目内缓存目录：`02_upload_split/runtime/docling-artifacts`

```bash
cd /srv/interface_projects/02_upload_split
python prepare_docling_artifacts.py --output-dir ./runtime/docling-artifacts
export DOCLING_ARTIFACTS_PATH=/srv/interface_projects/02_upload_split/runtime/docling-artifacts
```

当前已确认：

- PDF 主链路最少需要准备两个仓库缓存：
  - `docling-project--docling-layout-old`
  - `docling-project--docling-models`
- 若当前机器无法访问 `huggingface.co`，`prepare_docling_artifacts.py` 会失败；此时需要将这两个仓库的离线目录拷贝到 `runtime/docling-artifacts/` 下，再设置 `DOCLING_ARTIFACTS_PATH`。

## 启动

```bash
cd /srv/interface_projects
source .venv/bin/activate
source deploy/env.sh
cd 02_upload_split
python app.py
```

## 测试

```bash
curl http://127.0.0.1:6102/health
cd /srv/interface_projects
bash test/run_smoke_tests.sh --suites health --interfaces 02
```

详细公共部署步骤见 [DEPLOYMENT.md](/home/hks/sh/interface_projects/DEPLOYMENT.md)。
