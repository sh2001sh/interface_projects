# codegen_message_project

`create_test_project.py` 用于把接口 8 已生成的协议转换工程，拆成一个独立的 Qt 消息收发测试工程。它不读取原始 XML，而是读取接口 8 的生成目录，因为真实的字段名、协议类型名、位布局、端口和编解码实现都已经落在生成产物里。

## 输入要求

`--generated-dir` 指向接口 8 生成工程目录，至少需要包含：

- `protocol_manifest.json`
- `codec.cpp`
- `codec.h`
- 对应的 `*_def.h`

如果目录里存在 `config.xml`，脚本会一起复制到测试工程中，便于核对端口和转换配置。

## 生成命令

```bash
python3 "/nfs/615/codegen_message_project/create_test_project.py" \
  --generated-dir "/nfs/615/interface_projects/test/output/generated_project" \
  --output-dir "/nfs/615/interface_projects/test/output/generated_project_message_test"
```

当一个生成目录中有多条 conversion，或者默认推断的协议不是本次要测的那一组，可以显式指定：

```bash
python3 "/nfs/615/codegen_message_project/create_test_project.py" \
  --generated-dir "/path/to/generated_project" \
  --output-dir "/path/to/generated_project_message_test" \
  --source-protocol "K1_6" \
  --target-protocol "X0_5"
```

## 输出结构

生成后的 `--output-dir` 中会包含：

- `main.cpp`：独立 sender/receiver/roundtrip CLI。
- `xml_message_test.pro`：qmake 工程文件。
- `example_values.json`：源协议字段默认值模板，可直接作为 `--values-json` 输入。
- `README.md`：面向该生成目录的专用说明，包含协议名、端口和字段列表。
- `protocol_manifest.json`：从接口 8 生成目录复制，用于核对协议和位布局。
- `config.xml`：如果接口 8 生成目录存在则复制。
- `codec.cpp`、`codec.h`、`*_def.h`：编译测试工程所需的编解码依赖。

## 编译

进入生成出来的测试工程目录：

```bash
mkdir -p build
cd build
qmake ../xml_message_test.pro
make -j"$(nproc)"
```

编译产物是 `build/xml_message_test`。

## 运行模式

测试程序支持三种模式：

- `send`：构造源协议二进制消息，并发送到源消息接收端口。
- `recv`：监听目标消息端口，接收一帧目标协议二进制并解码为 JSON。
- `roundtrip`：先发送源协议消息，再等待接口 8 生成主程序转换后的目标协议消息。

先启动接口 8 生成出来的主转换程序，然后运行：

```bash
./xml_message_test --mode send --set temperature=123 --set status=2
./xml_message_test --mode recv
./xml_message_test --mode roundtrip --set temperature=123 --set status=2
```

字段值也可以通过 JSON 文件传入：

```bash
./xml_message_test --mode roundtrip --values-json ../example_values.json
```

`--set field=value` 可以重复出现，并会覆盖 `--values-json` 中同名字段。

## 常用参数

- `--mode`：`send`、`recv` 或 `roundtrip`，默认 `roundtrip`。
- `--source-ip` / `--source-port`：源协议消息发送目的地址，默认从 `protocol_manifest.json` 的 transport 信息推断。
- `--target-ip` / `--target-port`：目标协议消息监听地址，默认从 `protocol_manifest.json` 的 transport 信息推断。
- `--timeout-ms`：接收超时时间，默认 `5000`。
- `--values-json`：源协议字段值 JSON 文件。
- `--set`：单个字段覆盖值，格式为 `字段名=整数值`，可重复传入。

## 验收要点

一次完整测试通常需要确认：

- `create_test_project.py` 成功输出测试工程。
- `qmake` 和 `make` 编译通过。
- 接口 8 生成的主转换程序已经启动并监听源协议端口。
- `roundtrip` 能输出目标协议 JSON，且目标字段值符合接口 7/8 规则预期。

如果 `roundtrip` 超时，先检查接口 8 主程序是否已启动、`protocol_manifest.json` 中的端口是否与运行时一致，以及本机端口是否被其他进程占用。
