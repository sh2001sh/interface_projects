#!/usr/bin/env python3
"""Send and receive UDP test messages directly from protocol XML definitions."""

from __future__ import annotations

import argparse
import json
import random
import re
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class XmlField:
    """One flattened scalar field from a protocol XML."""

    label: str
    key: str
    path: str
    bit_length: int
    default: int | None


@dataclass(slots=True)
class XmlProtocol:
    """One protocol parsed from an XML file."""

    name: str
    path: Path
    endian: str
    fields: list[XmlField]

    @property
    def total_bits(self) -> int:
        return sum(field.bit_length for field in self.fields)

    @property
    def byte_length(self) -> int:
        return (self.total_bits + 7) // 8


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description="根据协议 XML 构造 UDP 消息并测试接口 8 运行工程")
    parser.add_argument("--source-xml", required=True, help="源协议 XML 文件夹；也兼容单个 XML 文件")
    parser.add_argument("--target-xml", help="目标协议 XML 文件或文件夹；不传时自动查找 sibling target_protocols")
    parser.add_argument("--output-dir", help="测试输出目录；默认写到当前目录 xml_message_test_output")
    parser.add_argument("--mode", choices=("send", "recv", "roundtrip"), default="send", help="测试模式")
    parser.add_argument("--source-ip", default="127.0.0.1", help="源消息发送目标 IP，默认 127.0.0.1")
    parser.add_argument("--source-port", "--recv-port", type=int, help="接口 8 运行工程的源消息接收端口")
    parser.add_argument("--target-ip", default="127.0.0.1", help="目标消息监听 IP，默认 127.0.0.1")
    parser.add_argument("--target-port", "--send-port", type=int, help="目标消息监听端口")
    parser.add_argument("--value-mode", choices=("random", "default"), default="random", help="字段取值方式")
    parser.add_argument("--seed", type=int, help="随机数种子")
    parser.add_argument("--set", action="append", default=[], dest="set_pairs", help="覆盖字段值，格式 field=value")
    parser.add_argument("--missing-value", type=int, default=0, help="XML 无默认值时的字段值，默认 0")
    parser.add_argument("--timeout-ms", type=int, default=5000, help="接收超时时间，默认 5000")
    return parser.parse_args()


def local_name(tag: str) -> str:
    """Return one XML tag local name without namespace."""

    return str(tag or "").split("}", 1)[-1].split(":", 1)[-1]


def namespace_uri(tag: str) -> str:
    """Return the namespace URI embedded in an ElementTree tag."""

    text = str(tag or "")
    if text.startswith("{") and "}" in text:
        return text[1:].split("}", 1)[0]
    return ""


def section_label(element: ET.Element) -> str:
    """Build a readable path label for one NameSpace section."""

    raw = str(element.attrib.get("name") or "").strip()
    if raw:
        return raw
    uri = namespace_uri(element.tag).rstrip("/")
    if uri:
        return uri.rsplit("/", 1)[-1]
    return local_name(element.tag)


def normalize_key(value: Any) -> str:
    """Normalize field names for matching CLI overrides."""

    return re.sub(r"[\s_\-./:：()（）\[\]【】]+", "", str(value or "").strip().lower())


def type_name_from_path(path: Path) -> str:
    """Build a protocol type name from an XML file stem."""

    parts = re.split(r"[^0-9A-Za-z]+", path.stem)
    tokens = [part for part in parts if part]
    return "_".join(token[:1].upper() + token[1:] for token in tokens) or path.stem


def parse_int(value: Any, fallback: int | None = None) -> int | None:
    """Parse one integer-like XML value."""

    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        return int(text, 0)
    except ValueError:
        return fallback


def bit_length_of(element: ET.Element) -> int | None:
    """Read bit length from text or common attributes."""

    for candidate in ((element.text or "").strip(), element.attrib.get("bitLength"), element.attrib.get("length")):
        parsed = parse_int(candidate)
        if parsed is not None:
            return parsed
    return None


def corr_labels(raw_corr: str | None) -> list[str]:
    """Extract control labels from a corr attribute."""

    labels: list[str] = []
    for chunk in str(raw_corr or "").split(","):
        token = chunk.strip()
        if token:
            labels.append(token.rsplit(".", 1)[-1].strip())
    return labels


def parse_xml_files(path: Path) -> list[Path]:
    """Return XML files from one file or directory."""

    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(f"XML 路径不存在: {path}")
    files = sorted(item for item in path.iterdir() if item.is_file() and item.suffix.lower() == ".xml")
    if not files:
        raise FileNotFoundError(f"XML 目录中没有 xml 文件: {path}")
    return files


def flatten_children(
    element: ET.Element,
    label_defaults: dict[str, int | None],
    path_parts: tuple[str, ...] = (),
) -> list[XmlField]:
    """Flatten Item, Field and Group XML nodes in protocol order."""

    fields: list[XmlField] = []
    for child in list(element):
        local = local_name(child.tag)
        if local in {"MessCode", "Dimen"}:
            continue
        label = str(child.attrib.get("name") or local).strip()
        if local == "NameSpace":
            fields.extend(flatten_children(child, label_defaults, path_parts + (section_label(child),)))
            continue
        if local in {"Item", "StructMess", "NetCtrl", "SpecType"}:
            bit_length = bit_length_of(child)
            if bit_length is None:
                continue
            default = parse_int(child.attrib.get("defaultValue"), None)
            if default is None:
                default = parse_int(child.attrib.get("default"), None)
            if default is None:
                default = parse_int(child.attrib.get("value"), None)
            label_defaults[label] = default
            field_path = path_parts + (label,)
            fields.append(
                XmlField(
                    label=label,
                    key=unique_key(field_path),
                    path="/".join(field_path),
                    bit_length=bit_length,
                    default=default,
                )
            )
            continue
        if local == "Field":
            fields.extend(flatten_children(child, label_defaults, path_parts + (label,)))
            continue
        if local == "Group":
            labels = corr_labels(child.attrib.get("corr"))
            max_repeat = parse_int(child.attrib.get("max"), None)
            repeat = max_repeat
            if repeat is None:
                repeat = next((label_defaults.get(item) for item in labels if label_defaults.get(item) is not None), None)
            repeat = max(1, int(repeat or 1))
            for index in range(repeat):
                group_label = f"{label}_{index + 1}" if repeat > 1 else label
                fields.extend(flatten_children(child, label_defaults, path_parts + (group_label,)))
            continue
        fields.extend(flatten_children(child, label_defaults, path_parts))
    return fields


def unique_key(path_parts: tuple[str, ...]) -> str:
    """Build a stable ASCII-ish key for one XML field path."""

    parts: list[str] = []
    for part in path_parts:
        normalized = normalize_key(part)
        if normalized:
            parts.append(normalized)
    return "_".join(parts) or "field"


def parse_protocol(path: Path) -> XmlProtocol:
    """Parse one protocol XML definition."""

    root = ET.parse(path).getroot()
    endian = "big"
    for child in list(root):
        if local_name(child.tag) == "Dimen":
            raw = str(child.attrib.get("endian") or "").strip().lower()
            endian = "little" if raw in {"0", "little", "le"} else "big"
            break
    defaults: dict[str, int | None] = {}
    fields = flatten_children(root, defaults)
    if not fields:
        raise ValueError(f"XML 中没有可编码字段: {path}")
    return XmlProtocol(name=type_name_from_path(path), path=path, endian=endian, fields=fields)


def section_category(field: XmlField) -> str | None:
    """Classify a field by protocol word section."""

    head = (field.path.split("/", 1)[0] if field.path else "").lower()
    if head.startswith("continue"):
        return "continue"
    if head.startswith("prolong"):
        return "prolong"
    if head.startswith("origin"):
        return "origin"
    return None


def apply_placeholder_lengths(protocols: list[XmlProtocol]) -> None:
    """Apply template ** placeholder lengths to concrete zero-length fields."""

    placeholders: dict[str, list[int]] = {}
    for protocol in protocols:
        for field in protocol.fields:
            category = section_category(field)
            if field.label == "**" and category and field.bit_length > 0:
                placeholders.setdefault(category, []).append(field.bit_length)
    if not placeholders:
        return

    for protocol in protocols:
        cursors = {key: 0 for key in placeholders}
        for field in protocol.fields:
            category = section_category(field)
            if category is None or field.bit_length != 0 or category not in placeholders:
                continue
            values = placeholders[category]
            index = min(cursors[category], len(values) - 1)
            field.bit_length = values[index]
            cursors[category] += 1


def load_protocols(path: Path) -> list[XmlProtocol]:
    """Load XML protocols and apply same-folder template placeholders."""

    protocols = [parse_protocol(item) for item in parse_xml_files(path)]
    apply_placeholder_lengths(protocols)
    return protocols


def protocol_values(
    protocol: XmlProtocol,
    args: argparse.Namespace,
    rng: random.Random,
) -> dict[str, int]:
    """Build field values for one protocol."""

    values: dict[str, int] = {}
    field_lookup: dict[str, XmlField] = {}
    for field in protocol.fields:
        for candidate in {field.key, field.label, field.path, field.path.split("/")[-1]}:
            field_lookup[normalize_key(candidate)] = field
        if args.value_mode == "default":
            value = args.missing_value if field.default is None else field.default
        else:
            value = rng.randint(0, (1 << min(field.bit_length, 30)) - 1)
        values[field.key] = value

    for pair in args.set_pairs:
        if "=" not in pair:
            raise ValueError(f"--set 格式错误，应为 field=value: {pair}")
        raw_name, raw_value = pair.split("=", 1)
        field = field_lookup.get(normalize_key(raw_name))
        if field is None:
            raise KeyError(f"XML 中没有字段: {raw_name}")
        parsed = parse_int(raw_value)
        if parsed is None:
            raise ValueError(f"--set 值不是整数: {pair}")
        values[field.key] = parsed
    return values


def append_bits(bits: list[int], value: int, bit_length: int, endian: str) -> None:
    """Append one integer value as bits."""

    if value < 0 or value >= (1 << bit_length):
        raise ValueError(f"字段值 {value} 超出 {bit_length} bit 可表示范围")
    if endian == "little":
        bits.extend((value >> index) & 1 for index in range(bit_length))
    else:
        bits.extend((value >> index) & 1 for index in range(bit_length - 1, -1, -1))


def bits_to_bytes(bits: list[int]) -> bytes:
    """Pack bits into bytes using network bit order inside each byte."""

    output = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            output[index // 8] |= 1 << (7 - (index % 8))
    return bytes(output)


def encode_protocol(protocol: XmlProtocol, values: dict[str, int]) -> bytes:
    """Encode one protocol message from XML field values."""

    bits: list[int] = []
    for field in protocol.fields:
        append_bits(bits, int(values[field.key]), field.bit_length, protocol.endian)
    return bits_to_bytes(bits)


def read_bits(data: bytes, bit_offset: int, bit_length: int, endian: str) -> int:
    """Read one integer from packed bytes."""

    value = 0
    if endian == "little":
        for index in range(bit_length):
            absolute = bit_offset + index
            bit = (data[absolute // 8] >> (7 - (absolute % 8))) & 1
            value |= bit << index
        return value
    for index in range(bit_length):
        absolute = bit_offset + index
        bit = (data[absolute // 8] >> (7 - (absolute % 8))) & 1
        value = (value << 1) | bit
    return value


def decode_protocol(protocol: XmlProtocol, data: bytes) -> dict[str, int]:
    """Decode one message into XML field values."""

    if len(data) < protocol.byte_length:
        raise ValueError(f"数据长度 {len(data)} 小于协议 {protocol.name} 需要的 {protocol.byte_length}")
    result: dict[str, int] = {}
    offset = 0
    for field in protocol.fields:
        result[field.key] = read_bits(data, offset, field.bit_length, protocol.endian)
        offset += field.bit_length
    return result


def write_json(path: Path, payload: Any) -> None:
    """Write formatted UTF-8 JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send_datagram(ip: str, port: int, payload: bytes) -> None:
    """Send one UDP datagram."""

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sent = sock.sendto(payload, (ip, port))
    if sent != len(payload):
        raise OSError(f"UDP 发送不完整: {sent}/{len(payload)} bytes")


def receive_datagram(ip: str, port: int, timeout_ms: int) -> tuple[bytes, tuple[str, int]]:
    """Receive one UDP datagram."""

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((ip, port))
        sock.settimeout(timeout_ms / 1000)
        return sock.recvfrom(65535)


def auto_target_xml(source_path: Path) -> Path | None:
    """Find a sibling target_protocols directory when available."""

    base = source_path if source_path.is_dir() else source_path.parent
    candidates = [base.parent / "target_protocols", base / "target_protocols"]
    return next((path for path in candidates if path.exists()), None)


def load_target_protocols(args: argparse.Namespace, source_path: Path) -> list[XmlProtocol]:
    """Load target XML protocols for optional received-message decoding."""

    target_path = Path(args.target_xml).resolve() if args.target_xml else auto_target_xml(source_path)
    if target_path is None:
        return []
    return load_protocols(target_path)


def decode_received(data: bytes, targets: list[XmlProtocol]) -> dict[str, Any]:
    """Decode received bytes using a target XML with matching byte length."""

    result: dict[str, Any] = {"__bytes": data.hex(), "__byte_length": len(data)}
    matches = [protocol for protocol in targets if protocol.byte_length == len(data)]
    if len(matches) != 1:
        if targets:
            result["__decode_note"] = "没有唯一匹配目标 XML，保留原始字节"
        return result
    protocol = matches[0]
    result["__protocol"] = protocol.name
    result["fields"] = decode_protocol(protocol, data)
    return result


def source_protocols(source_path: Path) -> list[XmlProtocol]:
    """Load source protocols in send order."""

    protocols = load_protocols(source_path)
    return list(reversed(protocols)) if source_path.is_dir() else protocols


def build_source_messages(
    protocols: list[XmlProtocol],
    args: argparse.Namespace,
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Build source UDP payloads and persist values."""

    rng = random.Random(args.seed)
    messages: list[dict[str, Any]] = []
    all_values: dict[str, Any] = {}
    for protocol in protocols:
        values = protocol_values(protocol, args, rng)
        payload = encode_protocol(protocol, values)
        all_values[protocol.name] = {"xml": str(protocol.path), "values": values, "bytes": payload.hex()}
        messages.append({"protocol": protocol, "values": values, "payload": payload})
    write_json(output_dir / "source_xml_values.json", all_values)
    return messages


def require_port(value: int | None, name: str) -> int:
    """Require a UDP port for the selected mode."""

    if value is None:
        raise ValueError(f"缺少 {name}")
    return int(value)


def main() -> int:
    """Run the XML-driven UDP test."""

    args = parse_args()
    source_path = Path(args.source_xml).resolve()
    output_dir = Path(args.output_dir or "xml_message_test_output").resolve()
    sources = source_protocols(source_path)
    targets = load_target_protocols(args, source_path)
    messages = build_source_messages(sources, args, output_dir)

    if args.mode == "recv":
        data, sender = receive_datagram(args.target_ip, require_port(args.target_port, "--target-port"), args.timeout_ms)
        result = decode_received(data, targets)
        result["__sender_ip"] = sender[0]
        result["__sender_port"] = sender[1]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    source_port = require_port(args.source_port, "--source-port")
    sent: list[dict[str, Any]] = []
    receiver: socket.socket | None = None
    if args.mode == "roundtrip":
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        receiver.bind((args.target_ip, require_port(args.target_port, "--target-port")))
        receiver.settimeout(args.timeout_ms / 1000)

    try:
        for message in messages:
            protocol: XmlProtocol = message["protocol"]
            payload: bytes = message["payload"]
            send_datagram(args.source_ip, source_port, payload)
            sent.append({"protocol": protocol.name, "xml": str(protocol.path), "byte_length": len(payload)})
            time.sleep(0.05)

        if args.mode == "send":
            print(json.dumps({"status": "sent", "sent": sent, "values": str(output_dir / "source_xml_values.json")}, ensure_ascii=False, indent=2))
            return 0

        assert receiver is not None
        data, sender = receiver.recvfrom(65535)
        result = decode_received(data, targets)
        result["status"] = "roundtrip"
        result["sent"] = sent
        result["__sender_ip"] = sender[0]
        result["__sender_port"] = sender[1]
        result["values"] = str(output_dir / "source_xml_values.json")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        if receiver is not None:
            receiver.close()


if __name__ == "__main__":
    raise SystemExit(main())
