# codegen_message_project

本目录只保留一键 XML UDP 测试入口：`run_xml_message_test.py`。

脚本只依赖协议 XML。它按 XML 中的 `Item`、`Field`、`Group` 定义解析字段顺序、位宽、默认值和循环次数，直接构造源协议二进制 UDP 报文；不读取接口 8 生成工程目录，也不需要 `protocol_manifest.json`、`codec.cpp` 或测试工程编译。

## 一键运行

先启动接口 8 生成出来的主转换程序，然后执行：

```bash
python3 "/nfs/615/codegen_message_project/run_xml_message_test.py" \
  --source-xml "/nfs/615/interface_projects/test/data/real_protocol_bundle/source_protocols" \
  --source-port 4620 \
  --target-port 5620 \
  --mode roundtrip
```

`--source-xml` 建议传一个协议相关的全部源 XML 文件夹。联合转换时，脚本会按文件顺序反向发送多个源消息；例如真实 K 样例会先发送 `K1_7`，再发送 `K1_6`，用于触发聚集后的目标转换。

如果目标 XML 和源 XML 同级存在 `target_protocols/` 目录，`roundtrip` 收到目标 UDP 后会自动按目标 XML 解码；否则输出原始 `__bytes`。

## 常用参数

- `--source-xml`：源协议 XML 文件夹；也兼容单个 XML 文件。
- `--target-xml`：目标协议 XML 文件或文件夹；不传时自动查找同级 `target_protocols/`。
- `--source-port`：接口 8 运行工程的源消息接收端口。
- `--target-port`：目标消息监听端口，`roundtrip` 和 `recv` 模式需要。
- `--mode`：`send`、`recv` 或 `roundtrip`，默认 `send`。
- `--seed`：固定随机种子，便于复现实验。
- `--value-mode default`：使用 XML 的默认值；默认 `random` 会按字段位宽随机生成合法值。
- `--set field=value`：覆盖单个字段值，可重复传入；字段名可用 XML 字段名或字段路径。
- `--output-dir`：测试输出目录，默认 `./xml_message_test_output`。
- `--timeout-ms`：接收超时时间，默认 `5000`。

运行后，脚本会写入 `source_xml_values.json`，记录每个源协议实际发送的字段值和十六进制报文。`roundtrip` 成功时会输出目标协议 JSON，并包含 `__bytes`、`__sender_ip`、`__sender_port` 等接收信息。

## K 协议子消息识别

K 父协议中存在 `消息标识`、`消息子标识` 这类路由字段，可用于父协议帧内的子消息判断。但真实样例里的 `k1.6.xml`、`k1.7.xml` 是子协议 XML，子消息本身不一定显式声明完整父协议路由头。

因此，用本工具直接从子协议 XML 构造二进制帧时，接口 8 生成工程会在 joint 共用接收端按唯一帧长度把数据归类到 `K1_6`、`K1_7` 等具体源协议名，避免都落到 `joint_bundle` 后无法聚集转换。
