# 接口数据库字段映射

按当前实现、按 `protobridge_dev` 当前 MySQL schema 整理。

每个接口只保留两张表：

- 接口输入字段对应的数据库列
- 接口输出字段建议保存到的数据库列

## 接口1 `validate_protocol_files`

### 输入字段 -> 数据库列

| 接口输入字段 | 数据库列 |
|---|---|
| 无 | 无 |

### 输出字段 -> 保存数据库列

| 接口输出字段 | 保存数据库列 |
|---|---|
| 无 | 无 |

## 接口2 `upload_split`

### 输入字段 -> 数据库列

| 接口输入字段 | 数据库列 |
|---|---|
| `project_id` | `document_split.project_id` |
| `document_id` | `document_split.document_id`、`document_split_block.document_id` |
| `dataset_id` | 当前 schema 无直接对应列 |
| `filepath` / `file_name` | `document_split.file_name` |

### 输出字段 -> 保存数据库列

| 接口输出字段 | 保存数据库列 |
|---|---|
| `data.project_id` | `document_split.project_id` |
| `data.document_id` | `document_split.document_id`、`document_split_block.document_id` |
| `data.dataset_id` | 当前 schema 无直接对应列 |
| `data.total_blocks` | `document_split.total_blocks` |
| 拆分结果块 `block_id` | `document_split_block.id`（当前实现对外返回的块标识使用主键 `id`；原始重复列 `block_id` 仅保留在兼容元数据中） |
| 拆分结果块 `page_num` | `document_split_block.page_num` |
| 拆分结果块 `type` | `document_split_block.type` |
| 拆分结果块 `content` | `document_split_block.content` |
| 拆分结果块 `protocol_fields` | `document_split_block.protocol_fields` |

## 接口3 `clean`

### 输入字段 -> 数据库列

| 接口输入字段 | 数据库列 |
|---|---|
| `dataset_id` | `dataset.id`；清洗结果回查时对应 `document_clean.dataset_id` |
| `block_ids[]` | `document_split_block.id` |
| `project_id` | 当前实现不直接用它查 MySQL 列 |
| `content_id` | 当前 schema 无 `content_id` 列；在 `protobridge_dev` 下会按值解析到 `document_clean.id` / `document_split.document_id` / `dataset.id` |
| `document_id` | 是 `content_id` 的兼容别名；解析规则同上 |

### 输出字段 -> 保存数据库列

| 接口输出字段 | 保存数据库列 |
|---|---|
| `data.dataset_id` | `document_clean.dataset_id` |
| `data.modified_block_ids` | `document_clean.modified_block_ids` |
| `data.cleaning_rate` | `document_clean.cleaning_rate` |
| `data.total_count` | `document_clean.total_count` |
| `data.modified_count` | `document_clean.modified_count` |
| `data.issues[].block_id` | `document_clean_issue.block_id` |
| `data.issues[].issue_type` | `document_clean_issue.issue_type` |
| `data.issues[].description` | `document_clean_issue.description` |
| `data.issues[].original_content` / `original` | `document_clean_issue.original` |
| `data.issues[].cleaned_content` / `cleaned` | `document_clean_issue.cleaned` |
| `data.issues[].page_num` | `document_clean_issue.page_num` |

## 接口4.1 `semantic_chunk`

### 输入字段 -> 数据库列

| 接口输入字段 | 数据库列 |
|---|---|
| `dataset_id` | `dataset.id`；清洗结果回查时对应 `document_clean.dataset_id` |
| `source_block_ids[]` | `document_split_block.id` |
| `project_id` | `document_split.project_id` |
| `content_id` | 当前 schema 无 `content_id` 列；在 `protobridge_dev` 下会按值解析到 `document_clean.id` / `document_split.document_id` / `dataset.id` |
| `document_id` | 是 `content_id` 的兼容别名；解析规则同上 |

### 输出字段 -> 保存数据库列

| 接口输出字段 | 保存数据库列 |
|---|---|
| `data.chunks[].chunk_id` | `rag_chunk_metadata.chunk_id` |
| `data.chunks[].semantic_type` | `rag_chunk_metadata.semantic_type` |
| `data.chunks[].source_block_ids` | `rag_chunk_metadata.source_block_ids` |
| `data.chunks[].merged_content` / `content_snapshot` | `rag_chunk_metadata.content_snapshot` |
| `data.total_chunks` | 当前 schema 无直接对应列；可由 `rag_chunk_metadata` 按 `task_id` 行数统计 |
| `data.dataset_id` | 当前 schema 无直接对应列 |

## 接口4.2 `update_doc_index`

### 输入字段 -> 数据库列

| 接口输入字段 | 数据库列 |
|---|---|
| `document_path` / `document_paths[]` | 先取文件名，再匹配 `document_split.file_name` |
| `project_id` | `document_split.project_id` |
| `source_block_ids[]` | `document_split_block.id` |
| `dataset_id` | 当前实现不直接用它查 MySQL 列 |

### 输出字段 -> 保存数据库列

| 接口输出字段 | 保存数据库列 |
|---|---|
| `data.project_id` | 当前 schema 无直接对应列 |
| `data.dataset_id` | 当前 schema 无直接对应列 |
| `data.doc_set_id` | 当前 schema 无直接对应列 |
| `data.index_ref` | 当前 schema 无直接对应列 |
| `data.storage_path` | 当前 schema 无直接对应列 |

## 接口5 `generate_qa`

### 输入字段 -> 数据库列

| 接口输入字段 | 数据库列 |
|---|---|
| `source_chunk_ids[]` | 优先按 `rag_chunk_metadata.id` 主键读取；非数字值再按 `rag_chunk_metadata.chunk_id` 读取 |
| `source_block_ids[]` | `document_split_block.id` |
| `dataset_id` | `dataset.id`；并通过 `dataset.doc_ids` 关联到 `rag_chunk_task.doc_ids` |

### 输出字段 -> 保存数据库列

| 接口输出字段 | 保存数据库列 |
|---|---|
| `data.dataset_id` | 给接口6继续读取时，保存到 `doc_qa_pairs.task_id` |
| `data.qa_pairs[].qa_id` | `doc_qa_pairs.id` |
| `data.qa_pairs[].question` | `doc_qa_pairs.question` |
| `data.qa_pairs[].answer` | `doc_qa_pairs.answer` |
| `data.selected_chunk_ids` | `doc_qa_pairs.source_chunk_ids` |
| `data.qa_pairs[].conversion_mode` | 当前 schema 在 `doc_qa_pairs` 无直接对应列；如需保留，可另存 `qa_pairs.conversion_mode` |
| `data.qa_pairs[].conversion_formula` | 当前 schema 在 `doc_qa_pairs` 无直接对应列；如需保留，可另存 `qa_pairs.conversion_formula` |
| `data.qa_pairs[].source_field` | 当前 schema 在 `doc_qa_pairs` 无直接对应列；如需保留，可另存 `qa_pairs.source_field` |
| `data.qa_pairs[].source_fields` | 当前 schema 在 `doc_qa_pairs` 无直接对应列；如需保留，可另存 `qa_pairs.source_fields` |
| `data.qa_pairs[].target_field` | 当前 schema 在 `doc_qa_pairs` 无直接对应列；如需保留，可另存 `qa_pairs.target_field` |
| `data.qa_pairs[].target_protocol_type` | 当前 schema 在 `doc_qa_pairs` 无直接对应列；如需保留，可另存 `qa_pairs.target_protocol_type` |
| `data.qa_pairs[].target_message_code` | 当前 schema 在 `doc_qa_pairs` 无直接对应列；如需保留，可另存 `qa_pairs.target_message_code` |

## 接口6 `extract_validate_qa`

### 输入字段 -> 数据库列

| 接口输入字段 | 数据库列 |
|---|---|
| `dataset_id` | `doc_qa_pairs.task_id` |
| `qa_id` | `doc_qa_pairs.id` |
| `protocol_type` | 当前 schema 无直接对应列 |

### 输出字段 -> 保存数据库列

| 接口输出字段 | 保存数据库列 |
|---|---|
| `data.qa_id` | `doc_qa_pairs.id` |
| `data.dataset_id` | `doc_qa_pairs.task_id` |
| `data.validation_result.passed` | `doc_qa_pairs.validation_result_status` |
| `data.extracted_info.field_name` | 如需单独落抽取结果，可存 `extracted_info.field_name` |
| `data.extracted_info.bit_width` | `extracted_info.bit_width` |
| `data.extracted_info.resolution` | `extracted_info.resolution` |
| `data.extracted_info.unit` | `extracted_info.unit` |
| `data.extracted_info.range_min` | `extracted_info.range_min` |
| `data.extracted_info.range_max` | `extracted_info.range_max` |
| `data.validation_result.check_items` / `issues` | 如需单独落校验结果，可存 `extraction_validation_item.check_items` |
| `data.validation_result.passed` | 如需单独落校验结果，可存 `extraction_validation_item.passed` |
| `data.message_code` | 当前 schema 无直接对应列 |
| `data.save_success` | 当前 schema 无直接对应列 |

## 接口7 `protocol_generate_rules`

### 输入字段 -> 数据库列

| 接口输入字段 | 数据库列 |
|---|---|
| 无 | 无 |

### 输出字段 -> 保存数据库列

| 接口输出字段 | 保存数据库列 |
|---|---|
| `data.conversion_rules_json` | `protocol_transform_result.conversion_rules_json` |
| `data.target_protocol.protocol_type` | `protocol_transform_rule.target_protocol_type` |
| `data.relations[].rules[].source_fields` | `protocol_transform_rule.source_fields` |
| `data.relations[].rules[].target_field` | `protocol_transform_rule.target_field` |
| `data.relations[].rules[].conversion_mode` | `protocol_transform_rule.conversion_mode` |
| `data.relations[].rules[].formula` | `protocol_transform_rule.formula` |
| `data.relations[].relation_id` | 当前 schema 无直接对应列 |
| `data.relations[].scores.field_match_accuracy` | 当前 schema 无直接对应列 |
| `data.relations[].scores.semantic_fidelity` | 当前 schema 无直接对应列 |
| `data.relations[].scores.structure_integrity` | 当前 schema 无直接对应列 |
| `data.relations[].scores.overall_correctness_score` | 当前 schema 无直接对应列 |

## 接口8 `code_generation`

### 输入字段 -> 数据库列

| 接口输入字段 | 数据库列 |
|---|---|
| 无 | 无 |

### 输出字段 -> 保存数据库列

| 接口输出字段 | 保存数据库列 |
|---|---|
| `data.project_name` | 当前 schema 无直接对应列 |
| `data.output.project_dir` | 当前 schema 无直接对应列 |
| `data.output.files[]` | 当前 schema 无直接对应列 |
| `data.output.conversion_units[]` | 当前 schema 无直接对应列 |
| `data.manifest` | 当前 schema 无直接对应列 |
| `data.syntax_validation` | 当前 schema 无直接对应列 |

## 接口9 `finetune_runtime`

### 输入字段 -> 数据库列

| 接口输入字段 | 数据库列 |
|---|---|
| `job_id` | `finetune_jobs.job_id` |
| `config.base_model_path` | `finetune_jobs.base_model` |
| `config.train_file_path` | 当前 schema 无直接对应列；当前实现保存在 `finetune_jobs.config` JSON |
| `config.preference_file_path` | 当前 schema 无直接对应列；当前实现保存在 `finetune_jobs.config` JSON |
| `config.parameters` | `finetune_jobs.config` |

### 输出字段 -> 保存数据库列

| 接口输出字段 | 保存数据库列 |
|---|---|
| `data.job_id` | `finetune_jobs.job_id` |
| `data.status` | `finetune_jobs.status` |
| `data.progress` | `finetune_jobs.progress` |
| `data.output_dir` | `finetune_jobs.model_path` |
| `data.last_checkpoint` | `finetune_jobs.last_checkpoint` |
| `data.summary.best_model_path` | `finetune_jobs.model_path` |
| `data.summary.best_metric` | `finetune_jobs.metrics` |
| `data.monitor_url` | 当前 schema 无直接对应列 |

## 接口10 `rule_evaluate`

### 输入字段 -> 数据库列

| 接口输入字段 | 数据库列 |
|---|---|
| 无 | 无 |

### 输出字段 -> 保存数据库列

| 接口输出字段 | 保存数据库列 |
|---|---|
| `data.trace_id` | `protocol_transform_task.trace_id` |
| `data.summary` | `protocol_transform_task.rule_evaluate_response` |
| `data.field_results` | `protocol_transform_task.rule_evaluate_response` |
| `data.strategy` | `protocol_transform_task.rule_evaluate_response` |
| `data.scores.field_match_accuracy` | 当前 schema 无直接对应列 |
| `data.scores.semantic_fidelity` | 当前 schema 无直接对应列 |
| `data.scores.structure_integrity` | 当前 schema 无直接对应列 |
| `data.scores.dimension_consistency_accuracy` | 当前 schema 无直接对应列 |
| `data.scores.field_coverage_rate` | 当前 schema 无直接对应列 |
| `data.scores.final_conversion_rate` | 当前 schema 无直接对应列 |
