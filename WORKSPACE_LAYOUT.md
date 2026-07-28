# interface_projects 工作区结构

本目录是从原 `/nfs/615/interface_projects` 备份后整理出的正式运行工作区，原目录已保留为 `/nfs/615/interface_projects_original_20260521_1535`。

## 目录分层

- `01_validate_protocol_files/` 到 `10_rule_evaluate/`：10 个接口的源码、局部配置和最小运行入口。
- `configs/`：统一配置源。`configs/global.yaml` 保存公共配置，`configs/interfaces/*.yaml` 保存各接口端口和差异配置。
- `scripts/generate_interface_configs.py`：根据统一配置重新生成各接口的 `config.yaml`。
- `deploy/`：新工作区启动、健康检查和运行时 PID/log 目录。
- `test/`：回归测试入口和必要测试数据，历史输出不随源码复制。
- `runtime/`、`tmp/`、`deploy/runtime/`、`test/output/`：新工作区本地运行产物目录。

## 外部运行时资源

以下目录是软链接，用于复用旧工作区的大文件资源，避免再次把模型和运行时数据复制进源码层：

- `model_cache -> /nfs/615/interface_projects_original_20260521_1535/model_cache`
- `models -> /nfs/615/interface_projects_original_20260521_1535/models`
- `runtime_data -> /nfs/615/interface_projects_original_20260521_1535/runtime_data`
- `09_finetune_runtime/models -> /nfs/615/interface_projects_original_20260521_1535/09_finetune_runtime/models`

## 端口

新工作区接口已替换到正式端口 `6101-6110`。旧工作区源码不再承载这些端口，原版备份保留在 `/nfs/615/backups/interface_projects_20260521_142530/source_snapshot`。

如需重新生成接口配置：

```bash
/opt/anaconda3/envs/interface_projects/bin/python /nfs/615/interface_projects/scripts/generate_interface_configs.py
```

如需运行 smoke test：

```bash
/opt/anaconda3/envs/interface_projects/bin/python /nfs/615/interface_projects/test/run_smoke_tests.py --host 127.0.0.1 --suites health,contract,codegen,rule-eval
```
