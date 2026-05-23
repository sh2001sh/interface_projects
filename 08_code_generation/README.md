# 08 code_generation

主接口：`POST /api/code_generation/generate`

## 上下游

- 上游：`07_protocol_generate_rules`
- 下游：无

## 启动

```bash
python app.py
```

默认端口：`6108`

## 说明

- 本项目为独立代码生成项目，直接复用本地 `project_generator` 生成 Qt/C++ 协议转换工程。
- 项目不依赖仓库根目录的 `shared/` 或 `code_generate/`，内部已携带所需副本。
- `port_config_json` 支持 JSON 对象、JSON 字符串和 JSON 文件路径；当前推荐直接传文件路径。
- `port_config_json.endpoints` 已支持显式多端口配置，可为多个原协议配置多个接收端口。
- `port_config_json.messageType` 现在可省略；接口会按转换关系自动推断，joint 多源场景默认补成 `joint_bundle`。
- 根入口 `POST /api/code_generation/generate` 支持同路由 SSE：可传 `stream=true` 或 `X-Stream-Response: true`。
- 生成产物的编译步骤见 `COMPILE_GENERATED_PROJECT.md`。
- 统一接口说明见仓库根目录 `接口文档.md`。
- `data.summary.qt_project_generation_time_ms` / `qt_project_generation_time_display`：本次 Qt 工程生成耗时。统计范围覆盖协议物化、映射构建、工程渲染、manifest 读取和语法校验。
- 2026-05-23 已用真实 HTTP 请求验证：同一条真实调用返回了 `qt_project_generation_time_ms=221.9816`，并成功生成 `protocol_manifest.json`。
