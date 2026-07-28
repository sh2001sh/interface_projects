# 接口独立项目总览

本目录按 `接口文档.docx` 与当前实现口径，将现有接口整理为独立项目副本。

## 设计原则

- 每个目录对应一个主接口项目
- 每个项目保留自己的代码副本和 `shared/`，不依赖仓库根目录共享层
- 每个项目运行时只读取自己目录下的 `config.yaml`
- 重复配置统一收敛到母配置，再自动生成各接口本地 `config.yaml`
- 原仓库 `api_*` 与 `shared/` 保留，作为研发主基座
- 本目录用于独立交付、独立启动和按上下游关系阅读

## 上下游关系

1. `01_validate_protocol_files`
2. `02_upload_split`
3. `03_clean`
4. `04_semantic_chunk`
5. `05_generate_qa`
6. `06_extract_validate_qa`
7. `07_protocol_generate_rules`
8. `08_code_generation`
9. `09_finetune_runtime`
10. `10_rule_evaluate`

主流程：

```text
validate_protocol_files
  -> upload_split
  -> clean
  -> semantic_chunk
  -> generate_qa
  -> extract_validate_qa
  -> protocol_generate_rules
  -> code_generation

protocol_generate_rules
  -> rule_evaluation
```

索引链路：

- `02_upload_split` 在文档上传拆分完成后，立即按文档创建 PageIndex 文档索引
- `04_semantic_chunk` 默认不再重复创建文档索引，只负责语义分块
- 如需重建索引，调用 `POST /api/data/update_doc_index` 时优先传 `document_path` / `document_paths`
- `07_protocol_generate_rules` 使用 PageIndex 时优先传 `index_registry_path`，可传单个 registry 文件或 registry 目录

## 说明

- `09_finetune_runtime` 为模型微调运行时项目，主接口为 `POST /api/model/finetune/action`。
- `06_extract_validate_qa` 当前主接口按 `qa_id` 读取 QA 原文；`dataset_id` 仅保留兼容，不再参与查库条件，且 `qa_id` 支持数组直传批量处理。
- `10_rule_evaluate` 为独立规则级评估项目，主接口为 `POST /api/knowledge/rule_evaluate`。
- `06_extract_validate_qa`、`07_protocol_generate_rules` 由于源实现同属 `api_03_extract_validate`，各自携带一份完整代码副本，但项目根入口只暴露对应主接口。

## 配置管理

当前配置流程已经改成：

1. 统一母配置
   `configs/global.yaml`

2. 每接口差异配置
   `configs/interfaces/*.yaml`

3. 生成各接口本地运行配置
   `01_validate_protocol_files/config.yaml`
   ...
   `10_rule_evaluate/config.yaml`

生成命令：

```bash
cd /srv/interface_projects
python3 scripts/generate_interface_configs.py
```

约束：

- 不要直接编辑各接口目录下的 `config.yaml`
- 需要改公共项时，改 `configs/global.yaml`
- 需要改单接口端口等差异项时，改 `configs/interfaces/<接口名>.yaml`
- 改完后重新执行生成脚本

这样可以同时满足：

- 重复项只维护一份
- 单接口 Docker 部署时仍然只依赖自己的本地 `config.yaml`

## 部署与测试

- 新机器全新环境部署：[NEW_MACHINE_DEPLOYMENT.md](NEW_MACHINE_DEPLOYMENT.md)
- 总部署文档：[DEPLOYMENT.md](DEPLOYMENT.md)
- 统一接口文档：[接口文档.md](接口文档.md)
- 统一测试目录：[test](test)
- 配置源目录：[configs](configs)
- 配置生成脚本目录：[scripts](scripts)
- 部署辅助脚本目录：[deploy](deploy)

每个接口目录下均已补充独立的 `DEPLOY.md`。
