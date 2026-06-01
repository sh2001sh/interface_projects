"""Internal Qt test-project builder used by the one-click XML test script."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def load_manifest(generated_dir: Path) -> dict[str, Any]:
    """Load protocol_manifest.json from a generated project."""
    manifest_path = generated_dir / "protocol_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"未找到 manifest: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def protocol_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index manifest protocols by type_name."""
    protocols = manifest.get("protocols") or []
    result: dict[str, dict[str, Any]] = {}
    for protocol in protocols:
        type_name = str(protocol.get("type_name") or "").strip()
        if type_name:
            result[type_name] = protocol
    return result


def default_protocols(
    manifest: dict[str, Any],
    source_override: str | None,
    target_override: str | None,
) -> tuple[str, str]:
    """Resolve source and target protocols from manifest conversions."""
    if source_override and target_override:
        return source_override, target_override
    conversions = manifest.get("conversions") or []
    if not conversions:
        raise ValueError("manifest 中没有 conversions，无法自动推断 source/target")
    first = conversions[0]
    sources = first.get("sources") or []
    if not sources:
        raise ValueError("manifest conversion 中没有 sources")
    source_name = source_override or str(sources[0].get("protocol") or "").strip()
    target_name = target_override or str(first.get("target_protocol") or "").strip()
    if not source_name or not target_name:
        raise ValueError("无法从 manifest 推断 source/target protocol")
    return source_name, target_name


def ensure_protocol(protocols: dict[str, dict[str, Any]], type_name: str) -> dict[str, Any]:
    """Return a protocol entry or raise a clear error."""
    protocol = protocols.get(type_name)
    if protocol is None:
        raise KeyError(f"manifest 中未找到协议: {type_name}")
    return protocol


def numeric_default(field: dict[str, Any]) -> int:
    """Convert manifest default_value to int when possible."""
    raw = field.get("default_value")
    if raw in (None, ""):
        return 0
    try:
        return int(str(raw), 10)
    except ValueError:
        return 0


def protocol_fields(protocol: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect flattened scalar fields for a protocol."""
    fields = protocol.get("fields") or []
    result = [field for field in fields if str(field.get("cpp_name") or "").strip()]
    if not result:
        raise ValueError(f"协议 {protocol.get('type_name')} 没有可用 fields")
    return sorted(
        result,
        key=lambda item: (
            int(item.get("bit_offset") or 0),
            str(item.get("cpp_name") or ""),
        ),
    )


def write_json(path: Path, payload: Any) -> None:
    """Write JSON with UTF-8 formatting."""
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_generated_headers(generated_dir: Path, output_dir: Path) -> list[str]:
    """Copy generated headers required by codec.h."""
    copied: list[str] = []
    for header in sorted(generated_dir.glob("*.h")):
        target = output_dir / header.name
        shutil.copy2(header, target)
        copied.append(header.name)
    return copied


def required_sources(generated_dir: Path) -> list[Path]:
    """Return generated source files that the test project must compile."""
    codec_path = generated_dir / "codec.cpp"
    if not codec_path.is_file():
        raise FileNotFoundError(f"未找到 codec.cpp: {codec_path}")
    return [codec_path]


def source_assignment_lines(fields: list[dict[str, Any]], indent: str = "    ") -> str:
    """Render C++ field default initialization lines."""
    lines = [f"{indent}message.{field['cpp_name']} = {numeric_default(field)};" for field in fields]
    return "\n".join(lines)


def source_switch_lines(fields: list[dict[str, Any]], indent: str = "    ") -> str:
    """Render C++ field assignment branches."""
    lines: list[str] = []
    for index, field in enumerate(fields):
        keyword = "if" if index == 0 else "else if"
        cpp_name = field["cpp_name"]
        lines.append(f'{indent}{keyword} (fieldName == QStringLiteral("{cpp_name}")) {{')
        lines.append(f"{indent}    message.{cpp_name} = value;")
        lines.append(f"{indent}    return true;")
        lines.append(f"{indent}}}")
    lines.append(f'{indent}if (error != nullptr) *error = QStringLiteral("unknown field: %1").arg(fieldName);')
    lines.append(f"{indent}return false;")
    return "\n".join(lines)


def target_json_lines(fields: list[dict[str, Any]], indent: str = "    ") -> str:
    """Render C++ JSON serialization lines for the target protocol."""
    lines = [
        f'{indent}obj.insert(QStringLiteral("{field["cpp_name"]}"), static_cast<qint64>(message.{field["cpp_name"]}));'
        for field in fields
    ]
    return "\n".join(lines)


def build_main_cpp(
    source_protocol: dict[str, Any],
    target_protocol: dict[str, Any],
    runtime: dict[str, Any],
) -> str:
    """Build the project-specific main.cpp source."""
    source_type = source_protocol["type_name"]
    target_type = target_protocol["type_name"]
    source_fields = protocol_fields(source_protocol)
    target_fields = protocol_fields(target_protocol)
    recv_ip = runtime.get("recv_ip") or "127.0.0.1"
    recv_port = int(runtime.get("recv_port") or 0)
    send_ip = runtime.get("send_ip") or "127.0.0.1"
    send_port = int(runtime.get("send_port") or 0)
    source_names = ", ".join(field["cpp_name"] for field in source_fields)
    target_names = ", ".join(field["cpp_name"] for field in target_fields)
    return f"""#include <QCoreApplication>
#include <QCommandLineOption>
#include <QCommandLineParser>
#include <QFile>
#include <QHostAddress>
#include <QJsonDocument>
#include <QJsonObject>
#include <QList>
#include <QMap>
#include <QTextStream>
#include <QUdpSocket>

#include "codec.h"

namespace {{

struct CliOptions {{
    QString mode;
    QString sourceIp = QStringLiteral("{recv_ip}");
    quint16 sourcePort = {recv_port};
    QString targetIp = QStringLiteral("{send_ip}");
    quint16 targetPort = {send_port};
    int timeoutMs = 5000;
    QString valuesPath;
    QStringList setPairs;
}};

void applyDefaults({source_type}& message)
{{
{source_assignment_lines(source_fields)}
}}

bool assignSourceField(const QString& fieldName, qlonglong value, {source_type}& message, QString* error)
{{
{source_switch_lines(source_fields)}
}}

bool loadFieldValues(const CliOptions& options, QMap<QString, qlonglong>& values, QString* error)
{{
    if (!options.valuesPath.isEmpty()) {{
        QFile file(options.valuesPath);
        if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {{
            if (error != nullptr) *error = QStringLiteral("cannot open values file: %1").arg(options.valuesPath);
            return false;
        }}
        const QJsonDocument document = QJsonDocument::fromJson(file.readAll());
        if (!document.isObject()) {{
            if (error != nullptr) *error = QStringLiteral("values file must be a JSON object");
            return false;
        }}
        const QJsonObject object = document.object();
        for (auto it = object.begin(); it != object.end(); ++it) {{
            values.insert(it.key(), static_cast<qlonglong>(it.value().toVariant().toLongLong()));
        }}
    }}
    for (const QString& pair : options.setPairs) {{
        const int pos = pair.indexOf('=');
        if (pos <= 0) {{
            if (error != nullptr) *error = QStringLiteral("invalid --set pair: %1").arg(pair);
            return false;
        }}
        const QString fieldName = pair.left(pos).trimmed();
        bool ok = false;
        const qlonglong value = pair.mid(pos + 1).trimmed().toLongLong(&ok);
        if (!ok) {{
            if (error != nullptr) *error = QStringLiteral("invalid integer value in --set: %1").arg(pair);
            return false;
        }}
        values.insert(fieldName, value);
    }}
    return true;
}}

{source_type} buildSourceMessage(const CliOptions& options, QString* error)
{{
    {source_type} message;
    applyDefaults(message);
    QMap<QString, qlonglong> values;
    if (!loadFieldValues(options, values, error)) return message;
    for (auto it = values.begin(); it != values.end(); ++it) {{
        if (!assignSourceField(it.key(), it.value(), message, error)) return message;
    }}
    return message;
}}

QJsonObject dumpTarget(const {target_type}& message)
{{
    QJsonObject obj;
{target_json_lines(target_fields)}
    return obj;
}}

void printJson(const QJsonObject& obj)
{{
    QTextStream(stdout) << QJsonDocument(obj).toJson(QJsonDocument::Indented);
}}

bool sendSourceDatagram(const CliOptions& options, const {source_type}& message, QString* error)
{{
    QByteArray data;
    encodeMsg(data, const_cast<{source_type}&>(message));
    QUdpSocket socket;
    const qint64 written = socket.writeDatagram(data, QHostAddress(options.sourceIp), options.sourcePort);
    if (written != data.size()) {{
        if (error != nullptr) *error = QStringLiteral("writeDatagram failed: wrote %1 of %2 bytes").arg(written).arg(data.size());
        return false;
    }}
    return true;
}}

bool receiveTargetDatagram(const CliOptions& options, QJsonObject& result, QString* error)
{{
    QUdpSocket socket;
    if (!socket.bind(QHostAddress(options.targetIp), options.targetPort, QUdpSocket::ShareAddress | QUdpSocket::ReuseAddressHint)) {{
        if (error != nullptr) *error = QStringLiteral("bind failed on %1:%2").arg(options.targetIp).arg(options.targetPort);
        return false;
    }}
    if (!socket.waitForReadyRead(options.timeoutMs)) {{
        if (error != nullptr) *error = QStringLiteral("timeout waiting datagram on %1:%2").arg(options.targetIp).arg(options.targetPort);
        return false;
    }}
    QByteArray datagram;
    datagram.resize(static_cast<int>(socket.pendingDatagramSize()));
    QHostAddress sender;
    quint16 senderPort = 0;
    socket.readDatagram(datagram.data(), datagram.size(), &sender, &senderPort);
    {target_type} message;
    const QString decodeError = decodeMsg(reinterpret_cast<uchar*>(datagram.data()), datagram.size(), message);
    if (!decodeError.isEmpty()) {{
        if (error != nullptr) *error = QStringLiteral("decodeMsg failed: %1").arg(decodeError);
        return false;
    }}
    result = dumpTarget(message);
    result.insert(QStringLiteral("__sender_ip"), sender.toString());
    result.insert(QStringLiteral("__sender_port"), static_cast<int>(senderPort));
    result.insert(QStringLiteral("__bytes"), QJsonValue(QString::fromLatin1(datagram.toHex())));
    return true;
}}

bool sendAndReceive(const CliOptions& options, const {source_type}& message, QJsonObject& result, QString* error)
{{
    QUdpSocket receiver;
    if (!receiver.bind(QHostAddress(options.targetIp), options.targetPort, QUdpSocket::ShareAddress | QUdpSocket::ReuseAddressHint)) {{
        if (error != nullptr) *error = QStringLiteral("bind failed on %1:%2").arg(options.targetIp).arg(options.targetPort);
        return false;
    }}
    QByteArray data;
    encodeMsg(data, const_cast<{source_type}&>(message));
    QUdpSocket senderSocket;
    const qint64 written = senderSocket.writeDatagram(data, QHostAddress(options.sourceIp), options.sourcePort);
    if (written != data.size()) {{
        if (error != nullptr) *error = QStringLiteral("writeDatagram failed: wrote %1 of %2 bytes").arg(written).arg(data.size());
        return false;
    }}
    if (!receiver.waitForReadyRead(options.timeoutMs)) {{
        if (error != nullptr) *error = QStringLiteral("timeout waiting roundtrip datagram on %1:%2").arg(options.targetIp).arg(options.targetPort);
        return false;
    }}
    QByteArray datagram;
    datagram.resize(static_cast<int>(receiver.pendingDatagramSize()));
    QHostAddress sender;
    quint16 senderPort = 0;
    receiver.readDatagram(datagram.data(), datagram.size(), &sender, &senderPort);
    {target_type} targetMessage;
    const QString decodeError = decodeMsg(reinterpret_cast<uchar*>(datagram.data()), datagram.size(), targetMessage);
    if (!decodeError.isEmpty()) {{
        if (error != nullptr) *error = QStringLiteral("decodeMsg failed: %1").arg(decodeError);
        return false;
    }}
    result = dumpTarget(targetMessage);
    result.insert(QStringLiteral("__sender_ip"), sender.toString());
    result.insert(QStringLiteral("__sender_port"), static_cast<int>(senderPort));
    result.insert(QStringLiteral("__bytes"), QJsonValue(QString::fromLatin1(datagram.toHex())));
    return true;
}}

CliOptions parseCli(QCoreApplication& app)
{{
    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral("Standalone message send/recv test for interface8 generated project"));
    parser.addHelpOption();
    const QCommandLineOption modeOption(QStringList{{QStringLiteral("mode")}}, QStringLiteral("send | recv | roundtrip"), QStringLiteral("mode"), QStringLiteral("roundtrip"));
    const QCommandLineOption sourceIpOption(QStringList{{QStringLiteral("source-ip")}}, QStringLiteral("send destination IP for source message"), QStringLiteral("source-ip"), QStringLiteral("{recv_ip}"));
    const QCommandLineOption sourcePortOption(QStringList{{QStringLiteral("source-port")}}, QStringLiteral("send destination port for source message"), QStringLiteral("source-port"), QString::number({recv_port}));
    const QCommandLineOption targetIpOption(QStringList{{QStringLiteral("target-ip")}}, QStringLiteral("listen IP for target message"), QStringLiteral("target-ip"), QStringLiteral("{send_ip}"));
    const QCommandLineOption targetPortOption(QStringList{{QStringLiteral("target-port")}}, QStringLiteral("listen port for target message"), QStringLiteral("target-port"), QString::number({send_port}));
    const QCommandLineOption timeoutOption(QStringList{{QStringLiteral("timeout-ms")}}, QStringLiteral("timeout in milliseconds"), QStringLiteral("timeout-ms"), QStringLiteral("5000"));
    const QCommandLineOption valuesJsonOption(QStringList{{QStringLiteral("values-json")}}, QStringLiteral("JSON object file for source field values"), QStringLiteral("values-json"));
    const QCommandLineOption setOption(QStringList{{QStringLiteral("set")}}, QStringLiteral("field=value override, repeatable"), QStringLiteral("set"));
    parser.addOption(modeOption);
    parser.addOption(sourceIpOption);
    parser.addOption(sourcePortOption);
    parser.addOption(targetIpOption);
    parser.addOption(targetPortOption);
    parser.addOption(timeoutOption);
    parser.addOption(valuesJsonOption);
    parser.addOption(setOption);
    parser.process(app);

    CliOptions options;
    options.mode = parser.value(QStringLiteral("mode")).trimmed().toLower();
    options.sourceIp = parser.value(QStringLiteral("source-ip")).trimmed();
    options.targetIp = parser.value(QStringLiteral("target-ip")).trimmed();
    options.sourcePort = static_cast<quint16>(parser.value(QStringLiteral("source-port")).toUInt());
    options.targetPort = static_cast<quint16>(parser.value(QStringLiteral("target-port")).toUInt());
    options.timeoutMs = parser.value(QStringLiteral("timeout-ms")).toInt();
    options.valuesPath = parser.value(QStringLiteral("values-json")).trimmed();
    options.setPairs = parser.values(QStringLiteral("set"));
    return options;
}}

}}  // namespace

int main(int argc, char* argv[])
{{
    QCoreApplication app(argc, argv);
    const CliOptions options = parseCli(app);
    QString error;

    if (options.mode == QStringLiteral("recv")) {{
        QJsonObject result;
        if (!receiveTargetDatagram(options, result, &error)) {{
            QTextStream(stderr) << error << '\\n';
            return 1;
        }}
        printJson(result);
        return 0;
    }}

    const {source_type} sourceMessage = buildSourceMessage(options, &error);
    if (!error.isEmpty()) {{
        QTextStream(stderr) << error << '\\n';
        return 1;
    }}

    if (options.mode == QStringLiteral("send")) {{
        if (!sendSourceDatagram(options, sourceMessage, &error)) {{
            QTextStream(stderr) << error << '\\n';
            return 1;
        }}
        QJsonObject result;
        result.insert(QStringLiteral("status"), QStringLiteral("sent"));
        result.insert(QStringLiteral("source_protocol"), QStringLiteral("{source_type}"));
        result.insert(QStringLiteral("target_protocol"), QStringLiteral("{target_type}"));
        result.insert(QStringLiteral("source_fields"), QStringLiteral("{source_names}"));
        result.insert(QStringLiteral("target_fields"), QStringLiteral("{target_names}"));
        printJson(result);
        return 0;
    }}

    if (options.mode == QStringLiteral("roundtrip")) {{
        QJsonObject result;
        if (!sendAndReceive(options, sourceMessage, result, &error)) {{
            QTextStream(stderr) << error << '\\n';
            return 1;
        }}
        printJson(result);
        return 0;
    }}

    QTextStream(stderr) << "unsupported mode: " << options.mode << '\\n';
    return 1;
}}
"""


def build_pro_file() -> str:
    """Build the qmake project file."""
    return """QT -= gui
QT += core network

CONFIG += c++17 cmdline
TARGET = xml_message_test
TEMPLATE = app

SOURCES += \
    main.cpp \
    codec.cpp

HEADERS += \
    codec.h
"""


def build_readme(
    source_protocol: dict[str, Any],
    target_protocol: dict[str, Any],
    runtime: dict[str, Any],
    header_names: list[str],
) -> str:
    """Build README content for the generated standalone test project."""
    source_fields = ", ".join(field["cpp_name"] for field in protocol_fields(source_protocol))
    target_fields = ", ".join(field["cpp_name"] for field in protocol_fields(target_protocol))
    recv_ip = runtime.get("recv_ip") or "127.0.0.1"
    recv_port = int(runtime.get("recv_port") or 0)
    send_ip = runtime.get("send_ip") or "127.0.0.1"
    send_port = int(runtime.get("send_port") or 0)
    headers_text = ", ".join(header_names)
    return f"""# xml_message_test

这是一个从接口 8 生成目录自动拆出来的独立测试工程。

## 当前绑定

- source protocol: `{source_protocol["type_name"]}`
- target protocol: `{target_protocol["type_name"]}`
- source send target: `{recv_ip}:{recv_port}`
- target listen port: `{send_ip}:{send_port}`

## 字段

- source fields: `{source_fields}`
- target fields: `{target_fields}`

## 生成时复制的头文件

`{headers_text}`

## 编译

```bash
mkdir -p build
cd build
qmake ../xml_message_test.pro
make -j"$(nproc)"
```

## 运行

先启动接口 8 生成的转换程序，再在本目录执行：

```bash
./xml_message_test --mode send --set temperature=123 --set status=2
./xml_message_test --mode recv
./xml_message_test --mode roundtrip --set temperature=123 --set status=2
```

也可以通过 JSON 文件批量传值：

```bash
./xml_message_test --mode roundtrip --values-json example_values.json
```

`roundtrip` 会先向 `{recv_ip}:{recv_port}` 发送源协议二进制，再监听 `{send_ip}:{send_port}` 接收并解码目标协议消息。
"""


def build_project(
    generated_dir: Path,
    output_dir: Path,
    source_name: str | None,
    target_name: str | None,
) -> None:
    """Create a standalone test project from a generated interface8 project."""
    manifest = load_manifest(generated_dir)
    protocols = protocol_map(manifest)
    source_type, target_type = default_protocols(manifest, source_name, target_name)
    source_protocol = ensure_protocol(protocols, source_type)
    target_protocol = ensure_protocol(protocols, target_type)
    runtime = (manifest.get("runtime") or {}).get("transport") or {}

    output_dir.mkdir(parents=True, exist_ok=True)
    copied_headers = copy_generated_headers(generated_dir, output_dir)
    shutil.copy2(generated_dir / "protocol_manifest.json", output_dir / "protocol_manifest.json")
    if (generated_dir / "config.xml").is_file():
        shutil.copy2(generated_dir / "config.xml", output_dir / "config.xml")
    for source_path in required_sources(generated_dir):
        shutil.copy2(source_path, output_dir / source_path.name)

    (output_dir / "main.cpp").write_text(
        build_main_cpp(source_protocol, target_protocol, runtime),
        encoding="utf-8",
    )
    (output_dir / "xml_message_test.pro").write_text(build_pro_file(), encoding="utf-8")
    (output_dir / "README.md").write_text(
        build_readme(source_protocol, target_protocol, runtime, copied_headers),
        encoding="utf-8",
    )
    example_values = {
        field["cpp_name"]: numeric_default(field)
        for field in protocol_fields(source_protocol)
    }
    write_json(output_dir / "example_values.json", example_values)
