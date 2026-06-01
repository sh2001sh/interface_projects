# 接口7表格模式样例数据

这个目录提供 `input_mode=table_rule` 的测试数据。表格中的“转换关系”列使用自然语言说明转换语义，同时保留明确等式或枚举映射，便于接口 7 直接抽取为 `kg_writeback_payload.rules`。

## 文件

- `interface7_table_rule_samples.xlsx`：Excel 工作簿，包含 `temp_sensor_to_report` 和 `k16_to_x05` 两个 sheet。
- `interface7_table_rule_samples.docx`：Word 文档，包含同样两组表格。
- `temp_sensor_to_report_rules.csv`：接口 8 简单温度样例对应的 CSV 表格。
- `k16_to_x05_rules.csv`：K1.6 到 X0.5 的多字段表格样例。
- `request_table_rule_docx.json`：调用接口 7 读取 Word 样例的请求体。
- `request_table_rule_xlsx.json`：调用接口 7 读取 Excel 样例的请求体。
- `request_temp_sensor_csv.json`：调用接口 7 读取简单温度 CSV 样例的请求体。
- `generate_samples.py`：重新生成上述 Word/Excel/CSV/JSON 文件的脚本。

## 调用示例

接口 7 服务启动后，可以直接提交样例请求体：

```bash
curl -sS -X POST "http://127.0.0.1:6107/api/knowledge/protocol_generate_rules" \
  -H "Content-Type: application/json" \
  --data-binary "@/nfs/615/interface_projects/test/data/interface7_table_rule_samples/request_table_rule_xlsx.json"
```

也可以将 `table_rule_files` 换成任意一个 `.docx`、`.xlsx` 或 `.csv` 文件路径。

## 表头契约

当前接口 7 解析器会识别以下列名或近似别名：

- `目标字段`
- `源字段`
- `转换关系` / `转换公式` / `规则` / `表达式`
- `转换类型`
- `说明`
- `字段含义`
- `源协议` / `源消息`
- `目标协议` / `目标消息`

最小可用表格只需要包含 `目标字段`，以及 `源字段` 或 `转换关系` 中至少一个可抽取来源字段的列。为了测试稳定性，这里的样例同时提供 `源字段` 和 `转换关系`。

## 重新生成

```bash
python3 "/nfs/615/interface_projects/test/data/interface7_table_rule_samples/generate_samples.py"
```
