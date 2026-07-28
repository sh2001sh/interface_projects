# 09 finetune_runtime

主接口：`POST /api/model/finetune/action`

## 上下游

- 上游：`05_generate_qa`、`06_extract_validate_qa`
- 下游：无

## 启动

```bash
python app.py
```

默认端口：`6109`

## 说明

- 本项目是模型微调运行时的自包含版本，提供启动、暂停、终止、流式监控和模型下载接口。
- 主路由为 `/api/model/finetune/action`，并兼容旧路径 `/api/finetune/job/*`。
- 训练进度流式监控入口为 `/api/model/finetune/stream?job_id=...`，兼容旧路径 `/api/finetune/job/stream?job_id=...`。
- 规则级评估接口已拆分到独立目录 `10_rule_evaluate`，不再由本项目承载。
- 所有成功动作返回现在统一携带 `data.model_info`，包括：
  - `total_parameter_count` / `total_parameter_count_display`：底模总参数量。
  - `finetune_parameter_count` / `finetune_parameter_count_display`：本次 LoRA 可训练参数量。
  - `finetune_ratio` / `finetune_ratio_percent` / `finetune_ratio_display`：微调参数占总参数比例。
  - `total_parameter_count_source` / `finetune_parameter_count_source`：统计来源。
- 2026-05-23 已用真实 HTTP 请求验证：对本地 `Qwen3-4B` 发起真实 `start` 请求时，接口返回总参数量 `4,022,468,096`；在 `lora_rank=8` 下返回微调参数量 `5,898,240`、微调比例 `0.1466%`。随后已真实调用 `stop` 回收该任务。
