# 03 clean

主接口：`POST /api/data/clean`

## 上下游

- 上游：`02_upload_split`
- 下游：`04_semantic_chunk`

## 启动

```bash
python app.py
```

默认端口：`6103`

## 说明

- 本项目复制自 `api_02_clean`。
- 项目自带一份 `shared/` 副本，不依赖仓库根目录共享层。

## 当前接口行为

- `POST /api/data/clean` 支持 `blocks_file_path`、直接 `blocks` / `blocks_content`、以及 `content_id` / `dataset_id` 取数。
- 当使用 `content_id` / `dataset_id` 时，服务会从数据库表 `pipeline_payloads` 读取块内容。
- 支持 `return_mode=content|path|both`，默认直接返回 `cleaned_blocks` 内容。
- 支持同路由 SSE：可传 `stream=true` 或 `X-Stream-Response: true`。
