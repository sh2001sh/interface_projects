# 10 rule_evaluate

主接口：`POST /api/knowledge/rule_evaluate`

## 上下游

- 上游：`07_protocol_generate_rules`
- 下游：无

## 启动

```bash
python app.py
```

默认端口：`6110`

## 说明

- 本项目是规则级评估接口的自包含版本，直接评估接口 7 输出的 `conversion_rules`。
- 评估输入只依赖源协议 XML 目录、目标协议 XML 目录和规则 JSON，不依赖接口 8 生成出的工程。
- 评估过程包含粗召回、精排序、结构校验和综合评分。
- `field_match_accuracy = 成功转换字段数 / 可转换字段数 * 100`
- `semantic_fidelity` 仅对可转换字段统计，按字段语义分取平均。
- `structure_integrity` 仅对可转换字段统计，按字段结构分取平均；`fallback_zero` 和缺规则字段按不完整处理。
- `final_conversion_rate = 成功转换字段数 / 目标字段总数 * 100`
- 根入口 `POST /api/knowledge/rule_evaluate` 支持同路由 SSE：可传 `stream=true` 或 `X-Stream-Response: true`。
- 可选传 `export_payload=true`，将评估阶段生成的字段文本和真实 embedding 向量落盘为 Milvus 可导入 JSON，并在返回的 `data.export_path` 中给出导出路径。
- 可选传 `export_name` 指定导出集合名。
- `export_payload=true` 时必须启用真实 embedding；若 `use_model_inference=false`，接口会直接报错，不会静默降级成“只导出文本”。
- 导出文件默认落到 `runtime/deliverables/milvus_exports/`，行结构对齐现有 Milvus schema：`id/chunk_id/project_id/dataset_id/semantic_type/content/embedding`。
- `data.summary.embedding_parameter_count` / `reranker_parameter_count`：本次评估使用的 embedding / reranker 模型参数量。
- `data.strategy.embedding_model_meta` / `reranker_model_meta`：对应模型目录、型号、参数量、可读展示值和统计来源。
- 2026-05-23 已用真实 HTTP 请求验证：接口返回 `Qwen3-Embedding-0.6B` 与 `Qwen3-Reranker-0.6B` 的精确参数量，二者均为 `595,776,512`。
