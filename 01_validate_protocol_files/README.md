# 01 validate_protocol_files

主接口：`POST /api/data/validate_protocol_files`

## 上下游

- 上游：无
- 下游：`02_upload_split`

## 启动

```bash
python app.py
```

默认端口：`6101`

## 说明

- 本项目复制自 `api_01_upload_split`，并在根入口只暴露文件校验接口。
- 项目自带一份 `shared/` 副本，不依赖仓库根目录共享层。
- 下游 `02_upload_split` 在实际上传拆分时会直接按文档创建 PageIndex 文档索引。
- 根入口 `POST /api/data/validate_protocol_files` 支持同路由 SSE：可传 `stream=true` 或 `X-Stream-Response: true`。
- 同时保留异步任务接口：`/status` 查询状态，`/stream` 订阅后台任务进度。
- 当前默认使用快速校验模式：只做文件头校验、少量页/Sheet/段落采样和最小可读性检查，不再默认全文解析。
- 如需兼容旧的全文校验，可显式传 `validation_mode=deep`。
