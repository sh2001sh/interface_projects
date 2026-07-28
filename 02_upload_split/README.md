# 02 upload_split

主接口：`POST /api/data/upload_split`

## 上下游

- 上游：`01_validate_protocol_files`
- 下游：`03_clean`

## 启动

```bash
python app.py
```

默认端口：`6102`

## 说明

- 本项目复制自 `api_01_upload_split`，并在根入口只暴露上传拆分接口。
- 项目自带一份 `shared/` 副本，不依赖仓库根目录共享层。
- 上传拆分成功后，会立即按当前上传文档创建对应的 PageIndex 文档索引。

## 当前接口行为

- `POST /api/data/upload_split` 支持 `return_mode=content|path|both`，默认直接返回 `blocks` 内容。
- `POST /api/data/upload_split` 支持同路由 SSE：可传 `stream=true` 或 `X-Stream-Response: true`。
- 响应不再依赖内部 `task_id` 作为同步结果主键；外部可直接拿返回内容自行入库。
- 可选透传 `dataset_id`、`document_id`，接口会原样回传，便于外部系统绑定自己的主键。
- 上传后创建的 PageIndex 现已支持超大文档自动分片建索引。
- 保留后台任务接口：`/status` 查询状态，`/stream` 订阅后台拆分进度。
