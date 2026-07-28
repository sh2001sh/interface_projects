# 04 semantic_chunk

主接口：`POST /api/data/semantic_chunk`

## 上下游

- 上游：`03_clean`
- 下游：`05_generate_qa`、`07_protocol_generate_rules`

## 启动

```bash
python app.py
```

默认端口：`6104`

## 说明

- 本项目复制自 `api_06_semantic_chunk`。
- 文档索引主入口已前移到 `02_upload_split`，上传文档时就会创建 PageIndex 索引。
- `POST /api/data/semantic_chunk` 默认不再重复创建文档索引；只有显式传 `config.build_doc_index=true` 才会附带重建。
- `POST /api/data/update_doc_index` 现已改为优先使用 `document_path` / `document_paths` 定位文档并重建索引。
- `POST /api/data/update_doc_index` 面向 RAG 补档场景，只更新 PageIndex，不要求传 `project_id`、`dataset_id`、`source_block_ids`；这些字段仅保留兼容用法。

## 当前接口行为

- `POST /api/data/semantic_chunk` 支持 `blocks_file_path`、直接块内容、以及 `content_id` / `document_id` / `dataset_id` 自动取块。
- 只传 `dataset_id` 时，会先从数据库表 `pipeline_payloads` 读取对应块内容，再自动做语义分块。
- 支持 `return_mode=content|path|both`，默认直接返回 `chunks` 内容。
- PageIndex 默认不在本接口重复创建；仅当 `config.build_doc_index=true` 时才重建，且底层已改为超大文档自动分片索引。
- `POST /api/data/update_doc_index` 默认按 `document_path(s)` 反查已上传块并推断索引归属；若命中多个项目才需要补充 `project_id` 消除歧义。
- `POST /api/data/semantic_chunk` 与 `POST /api/data/update_doc_index` 均支持同路由 SSE：可传 `stream=true` 或 `X-Stream-Response: true`。
