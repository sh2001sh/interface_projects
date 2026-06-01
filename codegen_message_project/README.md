# codegen_message_project

本目录只保留一键 XML 消息测试入口：`run_xml_message_test.py`。

脚本输入源协议 XML 文件夹和接口 8 生成工程的端口信息，自动完成以下步骤：

- 匹配接口 8 生成目录中的 `protocol_manifest.json`
- 按源 XML 字段构造合法测试值，默认随机生成
- 自动生成并编译临时 Qt 测试工程
- 通过 UDP 向接口 8 生成工程发送源协议消息
- `roundtrip` 模式下监听目标端口并输出解码后的目标协议 JSON

## 一键运行

先启动接口 8 生成出来的主转换程序，然后执行：

```bash
python3 "/nfs/615/codegen_message_project/run_xml_message_test.py" \
  --source-xml "/nfs/615/interface_projects/test/data/real_protocol_bundle/source_protocols" \
  --source-port 4620 \
  --target-port 5620 \
  --mode roundtrip
```

`--source-xml` 建议传一个协议相关的全部源 XML 文件夹。联合转换时，脚本会按字段名把目录内 XML 匹配到 manifest 里的源协议，并按联合转换依赖顺序发送多个源消息。

如果自动扫描到多个接口 8 生成目录，显式传 `--generated-dir`：

```bash
python3 "/nfs/615/codegen_message_project/run_xml_message_test.py" \
  --generated-dir "/nfs/615/interface_projects/test/output/interface7_71_8_clean_20260521/generated" \
  --source-xml "/nfs/615/interface_projects/test/data/real_protocol_bundle/source_protocols" \
  --source-port 4620 \
  --target-port 5620 \
  --mode roundtrip
```

## 常用参数

- `--source-xml`：源协议 XML 文件夹；兼容单个 XML 文件，但正式联调建议传文件夹。
- `--generated-dir`：接口 8 生成工程目录；不传时按 `--source-xml` 和 `--source-port` 自动扫描。
- `--source-port`：接口 8 生成工程的源消息接收端口。
- `--target-port`：目标消息监听端口，`roundtrip` 和 `recv` 模式需要。
- `--mode`：`send`、`recv` 或 `roundtrip`，默认 `send`。
- `--seed`：固定随机种子，便于复现实验。
- `--value-mode default`：使用 XML 的默认值；默认 `random` 会按字段位宽随机生成合法值。
- `--set field=value`：覆盖单个字段值，可重复传入；通常不需要传。
- `--timeout-ms`：接收超时时间，默认 `5000`。

运行后，脚本会在测试工程目录写入 `source_xml_values.json`，记录本次实际使用的字段值。`roundtrip` 成功时会输出目标协议 JSON，并包含 `__bytes`、`__sender_ip`、`__sender_port` 等接收信息。

## K 协议子消息识别

K 父协议中存在 `消息标识`、`消息子标识` 这类路由字段，可用于父协议帧内的子消息判断。但真实样例里的 `k1.6.xml`、`k1.7.xml` 是子协议 XML，子消息本身不一定显式声明完整父协议路由头。

因此，用本工具直接从子协议 XML 构造二进制帧时，接口 8 生成工程会在 joint 共用接收端按唯一帧长度把数据归类到 `K1_6`、`K1_7` 等具体源协议名，避免都落到 `joint_bundle` 后无法聚集转换。
