# UDP接口文档

本文档单独说明当前通过 UDP JSON 方式调用的接口能力。当前仅覆盖：

- 接口 7：`07_protocol_generate_rules`
- 接口 8：`08_code_generation`

## 通用说明

### 传输协议

- 协议：UDP
- 编码：UTF-8
- 数据格式：JSON

### 推荐请求格式

```json
{
  "path": "/api/knowledge/protocol_generate_rules",
  "body": {
    "key": "value"
  }
}
```

字段说明：

- `path`：要调用的 HTTP 路由
- `body`：原 HTTP 接口的 JSON 请求体

### 兼容请求格式

如果走的是服务默认路由，可以直接发送原 HTTP JSON 请求体，不包 `path/body` 外层。

### 统一响应格式

```json
{
  "transport": "udp",
  "path": "/api/code_generation/generate",
  "request_id": null,
  "http_status": 200,
  "body": {
    "code": 200,
    "message": "success",
    "data": {}
  }
}
```

字段说明：

- `transport`：固定为 `udp`
- `path`：本次实际命中的路由
- `request_id`：当前未传时返回 `null`
- `http_status`：对应原 HTTP 路由的状态码
- `body`：原 HTTP JSON 响应体

### 大响应处理

如果响应体过大，不会直接塞进一个 UDP 报文里，而是返回落盘信息：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "transport": "udp",
    "truncated": true,
    "response_file": "/path/to/full_response.json",
    "response_bytes": 245803
  }
}
```

此时完整响应 JSON 需要到 `response_file` 指向的文件中读取。

## 接口 7：协议转换规则生成

### UDP 监听地址

- 地址：`127.0.0.1:7107`
- 默认路由：`/api/knowledge/protocol_generate_rules`

### 支持的 UDP 路由

- `/api/knowledge/protocol_convert`
- `/api/knowledge/protocol_generate_rules`
- `/api/knowledge/protocol_convert_bundle`
- `/api/knowledge/protocol_rule_validate`
- `/api/knowledge/protocol_rule_export`
- `/api/knowledge/protocol_rules/manual_writeback`

### 推荐请求示例

下面示例走 `table_rule` 模式，用真实 CSV 样例做规则抽取：

```json
{
  "path": "/api/knowledge/protocol_generate_rules",
  "body": {
    "input_mode": "table_rule",
    "table_rule_files": [
      "/nfs/615/interface_projects/test/data/interface7_table_rule_samples/k16_to_x05_rules.csv"
    ],
    "source_protocol_type": "K1_6",
    "source_message_code": "K1.6",
    "target_protocol_type": "X0_5",
    "target_message_code": "X0.5"
  }
}
```

### 实测响应示例

2026-06-15 真实 UDP 调用返回 `200`：

```json
{
  "transport": "udp",
  "path": "/api/knowledge/protocol_generate_rules",
  "request_id": null,
  "http_status": 200,
  "body": {
    "code": 200,
    "message": "success",
    "data": {
      "conversion_rules_json": "/nfs/protobrige-system/keyan-storage/transformdata/output/rules/07_protocol_generate_rules_20260615162711018862.json",
      "conversion_rules_yaml": "/nfs/protobrige-system/keyan-storage/transformdata/output/rules/07_protocol_generate_rules_20260615162711018862.yaml",
      "relations": [
        {
          "relation_id": "k16_to_x05_rules",
          "source_protocols": ["K1_6"],
          "target_protocol": "X0_5"
        }
      ],
      "summary": {
        "input_mode": "table_rule",
        "table_file_count": 1,
        "parsed_table_count": 1,
        "table_rule_count": 11
      }
    }
  }
}
```

### 产物说明

本次实测会落盘规则文件：

- JSON：`/nfs/protobrige-system/keyan-storage/transformdata/output/rules/07_protocol_generate_rules_20260615162711018862.json`
- YAML：`/nfs/protobrige-system/keyan-storage/transformdata/output/rules/07_protocol_generate_rules_20260615162711018862.yaml`

## 接口 8：代码工程生成

### UDP 监听地址

- 地址：`127.0.0.1:7108`
- 默认路由：`/api/code_generation/generate`

### 推荐请求示例

下面示例使用已验证过的 K/X 样例规则和端口配置生成工程：

```json
{
  "path": "/api/code_generation/generate",
  "body": {
    "source_protocol_dirs": [
      "/nfs/615/interface_projects/test/data/protocol_family_xk_20260508/k_family"
    ],
    "target_protocol_dir": "/nfs/615/interface_projects/test/data/protocol_family_xk_20260508/x_family",
    "conversion_rules_json": "/nfs/615/interface_projects/test/output/interface7_71_8_retest_20260601_150428/rules_round2/07_protocol_generate_rules_20260601150449346612.json",
    "conversion_matrix_json": null,
    "port_config_json": {
      "messageType": "bundle",
      "messageRuleDetailList": [
        {"messageName": "K1.6", "delayRequirement": 10, "filterConfig": {"crcCheck": {"enabled": false, "bindElement": null}, "aggregation": {"mode": "SINGLE", "count": null, "timeMs": null}, "aggregationType": {"type": "TIME", "bindElement": null}}},
        {"messageName": "K1.7", "delayRequirement": 10, "filterConfig": {"crcCheck": {"enabled": false, "bindElement": null}, "aggregation": {"mode": "SINGLE", "count": null, "timeMs": null}, "aggregationType": {"type": "TIME", "bindElement": null}}},
        {"messageName": "K5.1", "delayRequirement": 10, "filterConfig": {"crcCheck": {"enabled": false, "bindElement": null}, "aggregation": {"mode": "SINGLE", "count": null, "timeMs": null}, "aggregationType": {"type": "TIME", "bindElement": null}}},
        {"messageName": "X0.5", "delayRequirement": 10, "filterConfig": {"crcCheck": {"enabled": false, "bindElement": null}, "aggregation": {"mode": "SINGLE", "count": null, "timeMs": null}, "aggregationType": {"type": "TIME", "bindElement": null}}}
      ],
      "endpoints": [
        {"name": "K1.6", "ip": "127.0.0.1", "port": 4716, "type": "udp", "recv": 1, "feedBackPort": 5716},
        {"name": "K1.7", "ip": "127.0.0.1", "port": 4717, "type": "udp", "recv": 1, "feedBackPort": 5717},
        {"name": "K5.1", "ip": "127.0.0.1", "port": 4751, "type": "udp", "recv": 1, "feedBackPort": 5751},
        {"name": "X0.5", "ip": "127.0.0.1", "port": 5705, "type": "udp", "recv": 0, "feedBackPort": 4705}
      ]
    },
    "output_dir": "/nfs/615/interface_projects/test/output/udp_codegen_success_20260615",
    "project_name": "udp_codegen_success_20260615"
  }
}
```

### 实测响应示例

2026-06-15 真实 UDP 调用返回 `200`，由于响应过大触发落盘：

```json
{
  "transport": "udp",
  "path": "/api/code_generation/generate",
  "request_id": null,
  "http_status": 200,
  "body": {
    "code": 200,
    "message": "success",
    "data": {
      "transport": "udp",
      "truncated": true,
      "response_file": "/nfs/615/interface_projects/08_code_generation/runtime/deliverables/udp_responses/08_code_generation_20260615_162822_188425.json",
      "response_bytes": 245803
    }
  }
}
```

### 完整响应位置

完整 JSON 响应保存在：

- `/nfs/615/interface_projects/08_code_generation/runtime/deliverables/udp_responses/08_code_generation_20260615_162822_188425.json`

其中包含原 HTTP 成功响应中的完整字段，例如：

- `data.manifest`
- `data.output.project_dir`
- `data.summary`
- `data.syntax_validation`

## 备注

- 接口 7 和接口 8 当前都保留原 HTTP 调用方式，UDP 是并行新增入口。
- UDP 网关只负责传输层封装，业务处理仍然复用原 Flask 路由逻辑。
- 如果后续再给其他接口开放 UDP，建议沿用本文件结构继续追加。
