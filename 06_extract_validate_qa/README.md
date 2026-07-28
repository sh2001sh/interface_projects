# 06 extract_validate_qa

主接口：`POST /api/knowledge/extract_validate_qa`

## 上下游

- 上游：`05_generate_qa`
- 下游：`07_protocol_generate_rules`、`09_finetune_runtime`

## 启动

```bash
python app.py
```

默认端口：`6106`

## 说明

- 本项目复制自 `api_03_extract_validate` 的代码副本，并在根入口只暴露 QA 抽取校验主接口。
- 当前主接口按 `qa_id` 读取 QA 原文；在 `protobridge_dev` 模式下会用它映射 `doc_qa_pairs.id` 主键，再读取 `question`、`answer` 后执行抽取与校验。
- `dataset_id` 现仅保留旧接口兼容，可传可不传，不再参与查库条件。
- `qa_id` 既支持单值，也支持直接传数组；传数组时会自动按批量模式处理。
- 批量模式下，`items[]` 仍然可批量抽取；每项至少传 `qa_id`。
- 由于原始实现与规则生成链路同仓，项目内同时携带一份代码生成器资产副本，避免依赖外部共享目录。
- 根入口 `POST /api/knowledge/extract_validate_qa` 支持同路由 SSE：可传 `stream=true` 或 `X-Stream-Response: true`。
- 统一接口说明见仓库根目录 `接口文档.md`。
