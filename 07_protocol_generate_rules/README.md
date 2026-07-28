# 07 protocol_generate_rules

主接口：

- `POST /api/knowledge/protocol_generate_rules`
- `POST /api/knowledge/protocol_rules/manual_writeback`

## 上下游

- 上游：`04_semantic_chunk`、`06_extract_validate_qa`
- 下游：`08_code_generation`、`10_rule_evaluate`

## 启动

```bash
python app.py
```

默认端口：`6107`

UDP 入口：`7107`

## 接口 7 返回重点

- `conversion_rules_json`：已落盘规则文件路径。
- `conversion_rules_yaml`：与 JSON 同步导出的 YAML 规则文件路径。
- `relations`：按转换关系分组后的最终规则输出；每个关系下直接携带本关系全部 `rules`。
- `relations[].scores`：当前子消息转换关系的评分，包含 `field_match_accuracy`、`semantic_fidelity`、`structure_integrity`、`overall_correctness_score`。
- `relations[].rules[]`：精简规则项，仅保留 `source_fields`、`source_vars`、`target_field`、`target_var`、`formula`。
- `kg_writeback_payload.rules`：人工审核通过后可写回知识图谱的候选规则。
- `summary.model_info`：当前接口 7 主生成模型的目录、型号、总参数量、可读展示值和统计来源。
- `summary.semantic_match_avg_query_time_ms`：知识图谱节点/源字段语义匹配阶段的平均单条数据元素查询耗时，单位毫秒，不包含后续 LLM 生成耗时。
- `summary.semantic_match_time_target_met`：是否满足“平均单条数据元素语义匹配查询耗时不超过 20ms”。
- `summary.rule_generation_avg_time_ms`：知识图谱规则边检索与规则组装阶段的平均单条目标数据元素生成耗时，单位毫秒，不包含 LLM 生成耗时。
- `summary.rule_generation_time_target_met`：是否满足“平均单条数据元素转换规则生成耗时不超过 50ms”。
- `summary.knowledge_graph_avg_rule_time_ms`：兼容旧字段，当前等同于 `summary.rule_generation_avg_time_ms`。
- `summary.knowledge_graph_rule_time_target_met`：兼容旧字段，当前等同于 `summary.rule_generation_time_target_met`。
- `input_mode=table_rule` + `table_rule_files`：新增表格规则抽取模式，支持直接读取 `docx/xlsx/xls/csv` 中的字段转换表并返回可写回知识图谱的规则。

其中 `relations[].relation_id` 的格式通常为：

- `J1.0_K1.1_to_X2.3`

## 说明

- 当前链路为 LLM-first：知识图谱命中可直接复用；其余字段由候选召回 + 文档证据 + LLM 最终生成。
- 当前知识图谱只由接口 `07` 使用；接口 `08` 代码生成和接口 `10` 评估当前都不依赖知识图谱。
- 使用 PageIndex 时，只需要传 `index_registry_path`；可以是单个 `registry.json`，也可以是包含多个 registry 的目录，服务端会自动筛选相关索引。
- 候选字段只作为召回和排序线索，不直接作为最终规则输出。
- `protocol_rules/manual_writeback` 用于前端人工审核后，将确认通过的规则直接写回知识图谱；服务端会统一写为 `approved/manual_review`。
- 当前不再保留本地 JSON 图谱读回退；Neo4j 不可用时，接口会直接返回错误。
- 根入口 `POST /api/knowledge/protocol_generate_rules` 支持同路由 SSE：可传 `stream=true` 或 `X-Stream-Response: true`。
- 当前同时支持 UDP JSON 请求。UDP 默认路由为 `/api/knowledge/protocol_generate_rules`，也可显式指定以下 `path`：`/api/knowledge/protocol_convert`、`/api/knowledge/protocol_generate_rules`、`/api/knowledge/protocol_convert_bundle`、`/api/knowledge/protocol_rule_validate`、`/api/knowledge/protocol_rule_export`、`/api/knowledge/protocol_rules/manual_writeback`。
- UDP 报文体必须是 UTF-8 JSON。推荐格式为 `{"path":"/api/knowledge/protocol_generate_rules","body":{...HTTP 原 JSON 请求体...}}`；若省略 `path`，则按默认路由处理。
- UDP 响应统一为 `{"transport":"udp","path":"...","http_status":200,"body":{...原 HTTP JSON 响应体...}}`。若响应过大，服务端会返回 `body.data.response_file`，完整 JSON 会落盘到 `runtime/deliverables/udp_responses/`。
- 统一接口说明见仓库根目录 `接口文档.md`。
- 2026-05-23 已用真实 HTTP 请求验证：接口会同时落盘 JSON/YAML 规则文件；优化后在 `force_regenerate=true` 的真实链路下返回了 `knowledge_graph_field_count=8`、`knowledge_graph_avg_rule_time_ms=1.0912`、`knowledge_graph_rule_time_target_met=true`，以及 `Qwen3-4B` 的精确参数量 `4,022,468,096`。
