"""Text template helpers for generated Qt/C++ projects."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from project_generator.models import (
    MessageRuleDetailSpec,
    BranchNode,
    ChoreographySpec,
    ConversionSpec,
    EndpointSpec,
    GroupNode,
    ProtocolVerifySpec,
    ProtocolNode,
    ProtocolSpec,
    ScalarNode,
    TransportSpec,
)
from project_generator.utils import normalize_token, to_snake_name


_NUMERIC_LITERAL_RE = re.compile(r"^[+-]?(?:0|[1-9][0-9]*|0[xX][0-9A-Fa-f]+)$")
_GROUP_BOOL_REPLACEMENTS = (
    (re.compile(r"\band\b", re.IGNORECASE), "&&"),
    (re.compile(r"\bor\b", re.IGNORECASE), "||"),
)
_GROUP_ASSIGN_RE = re.compile(r"(?<![!<>=])=(?!=)")
_OPTIONAL_CONTROL_CPP_RE = re.compile(r"(?:^|_)(?:fpi|gpi)\d+$", re.IGNORECASE)


def _msvc_utf8_preamble() -> str:
    """Renders one MSVC-friendly UTF-8 preamble."""

    return '#ifdef _MSC_VER\n#pragma execution_character_set("utf-8")\n#endif\n'


def _cpp_field_name(path_parts: tuple[str, ...]) -> str:
    """Builds one flattened C++ field name."""

    tokens = [normalize_token(part) for part in path_parts if part]
    return "_".join(token for token in tokens if token) or "field"


def _indent(level: int, lines: list[str]) -> list[str]:
    """Applies indentation to non-empty lines."""

    prefix = "    " * level
    return [f"{prefix}{line}" if line else "" for line in lines]


def _quoted(text: str) -> str:
    """Renders one QStringLiteral value."""

    escaped: list[str] = []
    for char in text:
        codepoint = ord(char)
        if char == "\\":
            escaped.append("\\\\")
        elif char == '"':
            escaped.append('\\"')
        elif char == "\n":
            escaped.append("\\n")
        elif char == "\r":
            escaped.append("\\r")
        elif char == "\t":
            escaped.append("\\t")
        elif 32 <= codepoint <= 126:
            escaped.append(char)
        elif codepoint <= 0xFFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(f"\\U{codepoint:08x}")
    return f'QStringLiteral("{"".join(escaped)}")'


def _mapping_target_var_name(target_type: str) -> str:
    """Builds one readable target variable name for generated mapping/runtime code."""

    return f"{to_snake_name(target_type)}Target"


def _xml_attr(text: str | None) -> str:
    """Escapes one XML attribute value."""

    raw = str(text or "")
    return (
        raw.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _field_spec_for_path(protocol: ProtocolSpec, path_parts: tuple[str, ...]):
    """Finds one flattened field spec by XML path."""

    for field in protocol.fields:
        if field.path_parts == path_parts:
            return field
    return None


def _field_spec_for_cpp_name(protocol: ProtocolSpec, cpp_name: str):
    """Finds one flattened field spec by bound C++ name."""

    for field in protocol.fields:
        if field.cpp_name == cpp_name:
            return field
    return None


def _resolve_scalar_binding(
    protocol: ProtocolSpec,
    node: ScalarNode,
    path_parts: tuple[str, ...],
):
    """Resolves one scalar node to its bound C++ field name and manifest entry."""

    field = _field_spec_for_path(protocol, path_parts + (node.label,))
    if field is not None:
        return field.cpp_name, field
    field_name = node.cpp_name
    field = _field_spec_for_cpp_name(protocol, field_name)
    return field_name, field


def _typed_default_literal(field) -> str:
    """Builds one typed default literal for a generated field."""

    default_value = field.default_value
    if default_value is None or not str(default_value).strip():
        if field.cpp_type == "float":
            return "0.0f"
        if field.cpp_type == "double":
            return "0.0"
        if field.cpp_type == "char":
            return "static_cast<char>(0)"
        return "0"
    raw = str(default_value).strip()
    if field.cpp_type == "char":
        return f"static_cast<char>({raw})"
    if field.cpp_type in {"float", "double"}:
        try:
            decimal_value = Decimal(raw)
            rendered = format(decimal_value, "f").rstrip("0").rstrip(".")
            if not rendered:
                rendered = "0"
        except InvalidOperation:
            rendered = raw
        if field.cpp_type == "float" and "." not in rendered and "e" not in rendered.lower():
            rendered = f"{rendered}.0"
        if field.cpp_type == "double" and "." not in rendered and "e" not in rendered.lower():
            rendered = f"{rendered}.0"
        return f"{rendered}f" if field.cpp_type == "float" else rendered
    return raw


def _decode_value_expr(field, endian_func: str, bit_length: int) -> str:
    """Builds one typed decode expression for a scalar field."""

    read_expr = f"{endian_func}(raw, bitOffset, {bit_length})"
    if field.cpp_type == "float" and bit_length == 32:
        return f"bitsToFloat(static_cast<quint32>({read_expr}))"
    if field.cpp_type == "double" and bit_length == 64:
        return f"bitsToDouble(static_cast<quint64>({read_expr}))"
    if field.cpp_type == "char":
        return f"static_cast<char>({read_expr})"
    return f"static_cast<{field.cpp_type}>({read_expr})"


def _encode_value_expr(field_name: str, field, bit_length: int | None) -> str:
    """Builds one typed encode expression for a scalar field."""

    value_expr = f"value.{field_name}"
    if field.cpp_type == "float":
        return f"static_cast<quint64>(floatToBits({value_expr}))"
    if field.cpp_type == "double":
        return f"doubleToBits({value_expr})"
    if field.cpp_type == "char":
        if bit_length is None:
            return f"static_cast<quint64>(static_cast<unsigned char>({value_expr}))"
        return f"normalizeUnsignedBits(static_cast<qint64>(static_cast<unsigned char>({value_expr})), {bit_length})"
    if bit_length is None:
        return f"static_cast<quint64>({value_expr})"
    return f"normalizeUnsignedBits(static_cast<qint64>({value_expr}), {bit_length})"


def _collect_group_labels(nodes: list[ProtocolNode]) -> set[str]:
    """Collects all scalar labels under one group."""

    labels: set[str] = set()
    for node in nodes:
        if isinstance(node, ScalarNode):
            labels.add(node.label)
            continue
        labels.update(_collect_group_labels(node.children))
    return labels


def _node_contains_any_label(node: ProtocolNode, labels: set[str]) -> bool:
    """Checks whether one node subtree references any target label."""

    if isinstance(node, ScalarNode):
        return node.label in labels
    return any(_node_contains_any_label(child, labels) for child in node.children)


def _group_condition_anchor_index(nodes: list[ProtocolNode], labels: set[str]) -> int | None:
    """Finds the last child index required to evaluate one group condition."""

    if not labels:
        return None
    last_index: int | None = None
    for index, child in enumerate(nodes):
        if _node_contains_any_label(child, labels):
            last_index = index
    return last_index


def _group_condition_expr(
    node: GroupNode,
    protocol: ProtocolSpec,
    iteration_path: tuple[str, ...],
) -> tuple[str | None, set[str]]:
    """Builds one iteration-scoped condition expression for a conditional group."""

    raw_condition = (node.condition or "").strip()
    if not raw_condition:
        return None, set()

    expression = raw_condition
    referenced_labels: set[str] = set()
    candidate_labels = sorted(_collect_group_labels(node.children), key=len, reverse=True)
    for label in candidate_labels:
        field = _field_spec_for_path(protocol, iteration_path + (label,))
        if field is None or label not in expression:
            continue
        expression = expression.replace(label, f"value.{field.cpp_name}")
        referenced_labels.add(label)

    for pattern, replacement in _GROUP_BOOL_REPLACEMENTS:
        expression = pattern.sub(replacement, expression)
    expression = _GROUP_ASSIGN_RE.sub("==", expression)
    return expression, referenced_labels


def render_main_cpp() -> str:
    """Renders the shared main.cpp file."""

    return _msvc_utf8_preamble() + """#include <QCoreApplication>
#include <QDebug>
#include <QDomDocument>
#include <QFile>
#include <memory>
#include "messageconvert.h"

int readMessageXML(
    QString path,
    QVector<std::shared_ptr<messageConvert::NetInfo>>& netlist,
    QVector<std::shared_ptr<messageConvert::MessageRuleInfo>>& messageRuleList)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        qDebug() << "Cannot open file for reading:" << qPrintable(file.errorString());
        return 1;
    }
    QDomDocument doc;
    if (!doc.setContent(&file)) {
        qDebug() << "Failed to load document";
        file.close();
        return 2;
    }
    file.close();
    QDomElement root = doc.documentElement();
    QDomNodeList childNodes = root.childNodes();
    for (int index = 0; index < childNodes.count(); ++index) {
        QDomNode node = childNodes.at(index);
        if (!node.isElement()) continue;
        QDomElement element = node.toElement();
        if (element.tagName() == "Item") {
            auto ip = element.attributes().namedItem("ip");
            auto port = element.attributes().namedItem("port");
            auto type = element.attributes().namedItem("type");
            auto recv = element.attributes().namedItem("recv");
            auto name = element.attributes().namedItem("name");
            auto feedBackPort = element.attributes().namedItem("feedBackPort");
            std::shared_ptr<messageConvert::NetInfo> net(new messageConvert::NetInfo);
            net->ip = ip.nodeValue();
            net->name = name.nodeValue();
            net->port = port.nodeValue().toInt();
            net->feedBackPort = feedBackPort.nodeValue().toInt();
            net->bRecvTag = recv.nodeValue().toInt();
            if (type.nodeValue().toUpper() == "TCP") net->netType = messageConvert::emTCP;
            else if (type.nodeValue().toUpper() == "DDS") net->netType = messageConvert::emDDS;
            else net->netType = messageConvert::emUDP;
            netlist.push_back(net);
            continue;
        }
        if (element.tagName() == "Transport") {
            QDomNodeList messageRuleNodes = element.childNodes();
            for (int ruleIndex = 0; ruleIndex < messageRuleNodes.count(); ++ruleIndex) {
                QDomNode ruleNode = messageRuleNodes.at(ruleIndex);
                if (!ruleNode.isElement()) continue;
                QDomElement ruleElement = ruleNode.toElement();
                if (ruleElement.tagName() != "MessageRule") continue;
                std::shared_ptr<messageConvert::MessageRuleInfo> rule(new messageConvert::MessageRuleInfo);
                rule->messageName = ruleElement.attribute("messageName");
                rule->delayRequirement = ruleElement.attribute("delayRequirement").toInt();
                QDomNodeList filterNodes = ruleElement.childNodes();
                for (int filterIndex = 0; filterIndex < filterNodes.count(); ++filterIndex) {
                    QDomNode filterNode = filterNodes.at(filterIndex);
                    if (!filterNode.isElement()) continue;
                    QDomElement filterElement = filterNode.toElement();
                    if (filterElement.tagName() == "CrcCheck") {
                        rule->crcCheck.enabled = filterElement.attribute("enabled").toInt() != 0;
                        rule->crcCheck.bindElement = filterElement.attribute("bindElement");
                    } else if (filterElement.tagName() == "Aggregation") {
                        rule->aggregation.mode = filterElement.attribute("mode");
                        rule->aggregation.count = filterElement.attribute("count").isEmpty() ? -1 : filterElement.attribute("count").toInt();
                        rule->aggregation.timeMs = filterElement.attribute("timeMs").isEmpty() ? -1 : filterElement.attribute("timeMs").toInt();
                        rule->aggregation.compareOperator = filterElement.attribute("operator").trimmed().toUpper();
                        rule->aggregation.compareValue = filterElement.attribute("value").trimmed();
                    } else if (filterElement.tagName() == "AggregationType") {
                        rule->aggregationType.type = filterElement.attribute("type");
                        rule->aggregationType.bindElement = filterElement.attribute("bindElement");
                    }
                }
                messageRuleList.push_back(rule);
            }
        }
    }
    return 0;
}

int main(int argc, char* argv[])
{
    QCoreApplication application(argc, argv);
    QVector<std::shared_ptr<messageConvert::NetInfo>> netlist;
    QVector<std::shared_ptr<messageConvert::MessageRuleInfo>> messageRuleList;
    const QString configPath = QCoreApplication::applicationDirPath() + "/config.xml";
    readMessageXML(configPath, netlist, messageRuleList);
    messageConvert converter;
    converter.start(netlist, messageRuleList);
    return application.exec();
}
"""


def render_config_xml(endpoints: list[EndpointSpec], transport: TransportSpec | None = None) -> str:
    """Renders config.xml."""

    if not endpoints:
        endpoints = [
            EndpointSpec(
                ip="127.0.0.1",
                port=3333,
                net_type="udp",
                recv=True,
                feedback_port=3333,
                name="INPUT",
            ),
            EndpointSpec(
                ip="127.0.0.1",
                port=3336,
                net_type="udp",
                recv=False,
                feedback_port=3333,
                name="OUTPUT",
            ),
        ]
    items = []
    for endpoint in endpoints:
        items.append(
            "    "
            f'<Item ip="{endpoint.ip}" port="{endpoint.port}" type="{endpoint.net_type}" '
            f'recv="{1 if endpoint.recv else 0}" feedBackPort="{endpoint.feedback_port}" '
            f'name="{endpoint.name}" />'
        )
    if transport is not None:
        items.append(
            "    "
            f'<Transport messageType="{_xml_attr(transport.message_type)}" '
            f'recvIp="{_xml_attr(transport.recv_ip)}" recvPort="{transport.recv_port}" '
            f'sendIp="{_xml_attr(transport.send_ip)}" sendPort="{transport.send_port}">'
        )
        for rule in transport.message_rules:
            items.append(
                "        "
                f'<MessageRule messageName="{_xml_attr(rule.message_name)}" '
                f'delayRequirement="{rule.delay_requirement}">'
            )
            items.append(
                "            "
                f'<CrcCheck enabled="{1 if rule.crc_check.enabled else 0}" '
                f'bindElement="{_xml_attr(rule.crc_check.bind_element)}" />'
            )
            items.append(
                "            "
                f'<Aggregation mode="{_xml_attr(rule.aggregation.mode)}" '
                f'count="{"" if rule.aggregation.count is None else rule.aggregation.count}" '
                f'timeMs="{"" if rule.aggregation.time_ms is None else rule.aggregation.time_ms}" '
                f'operator="{_xml_attr(rule.aggregation.operator)}" '
                f'value="{_xml_attr(rule.aggregation.value)}" />'
            )
            items.append(
                "            "
                f'<AggregationType type="{_xml_attr(rule.aggregation_type.type)}" '
                f'bindElement="{_xml_attr(rule.aggregation_type.bind_element)}" />'
            )
            items.append("        </MessageRule>")
        items.append("    </Transport>")
    return "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<NameSpace>\n" + "\n".join(items) + "\n</NameSpace>\n"


def render_protocol_header(protocol: ProtocolSpec) -> str:
    """Renders one protocol definition header."""

    guard = f"{protocol.type_name.upper()}_DEF_H"
    field_lines = []
    seen_fields: set[str] = set()
    for field in protocol.fields:
        if field.cpp_name in seen_fields:
            continue
        seen_fields.add(field.cpp_name)
        field_lines.append(f"    {field.cpp_type} {field.cpp_name} = {_typed_default_literal(field)};")
    return f"""{_msvc_utf8_preamble()}
#ifndef {guard}
#define {guard}

class {protocol.type_name} {{
public:
{chr(10).join(field_lines)}
}};

#endif
"""


def mapping_file_base(conversion: ConversionSpec) -> str:
    """Returns the mapping file base name for one conversion."""

    source_part = "_".join(to_snake_name(source.protocol) for source in conversion.sources)
    target_part = to_snake_name(conversion.target_protocol)
    return f"{source_part}_to_{target_part}"


def render_codec_header(protocols: list[ProtocolSpec], mapping_headers: list[str]) -> str:
    """Renders codec.h."""

    protocol_headers = [f'{protocol.file_stem}_def.h' for protocol in protocols]
    includes = [f'#include "{header}"' for header in [*protocol_headers, *mapping_headers]]
    declarations: list[str] = []
    for protocol in protocols:
        declarations.extend(
            [
                f"QString decodeMsg(uchar* pData, int len, {protocol.type_name}& value);",
                f"void encodeMsg(QByteArray& data, {protocol.type_name}& value);",
                f"int checkObjMaps(QString strVerify, QByteArray& data, {protocol.type_name}& value);",
                "",
            ]
        )
    return (
        f"{_msvc_utf8_preamble()}\n#ifndef CODEC_H\n#define CODEC_H\n\n"
        + "\n".join(includes)
        + "\n#include <QtGlobal>\n#include <QByteArray>\n#include <QString>\n\n"
        + "bool applyRuntimeCrc(const QString& protocolName, const QString& bindElement, QByteArray& data);\n"
        + "bool validateRuntimeCrc(const QString& protocolName, const QString& bindElement, const QByteArray& data);\n\n"
        + "QString extractRuntimeFieldValue(const QString& protocolName, const QString& bindElement, const QByteArray& data);\n\n"
        + "\n".join(declarations).rstrip()
        + "\n\n#endif\n"
    )


def _control_expr(control_fields: tuple[str, ...], values: str | None, protocol: ProtocolSpec) -> str:
    """Builds a branch-control expression."""

    if not control_fields or not values:
        return "true"
    targets = [part.strip() for part in values.split(",")]
    checks: list[str] = []
    for index, field_label in enumerate(control_fields):
        cpp_name = protocol.label_to_cpp.get(field_label)
        if cpp_name is None:
            continue
        expected = targets[min(index, len(targets) - 1)]
        checks.append(f"value.{cpp_name} == {expected}")
    return " && ".join(checks) or "true"


def _group_repeat_expr(node: GroupNode, protocol: ProtocolSpec, var_name: str) -> list[str]:
    """Builds repeat-count lines for one group."""

    control_cpp = protocol.label_to_cpp.get(node.control_fields[0], "") if node.control_fields else ""
    limit = node.repeat_count
    if control_cpp:
        return [
            f"int {var_name} = static_cast<int>(value.{control_cpp});",
            f"if ({var_name} < 0) {var_name} = 0;",
            f"if ({var_name} > {limit}) {var_name} = {limit};",
        ]
    return [f"const int {var_name} = {limit};"]


def _render_decode_nodes(
    nodes: list[ProtocolNode],
    protocol: ProtocolSpec,
    path_parts: tuple[str, ...],
    level: int,
    endian_func: str,
    loop_index: int = 0,
) -> tuple[list[str], int]:
    """Renders decode statements for one node list."""

    lines: list[str] = []
    current_loop_index = loop_index
    for node in nodes:
        if isinstance(node, ScalarNode):
            field_name, field = _resolve_scalar_binding(protocol, node, path_parts)
            if node.bit_length is None:
                lines.extend(_indent(level, [f"Q_UNUSED(value.{field_name});"]))
                continue
            if field is None:
                lines.extend(_indent(level, [f"Q_UNUSED(value.{field_name});"]))
                continue
            if node.bit_length == 0:
                if node.source_field:
                    source_field = _resolve_protocol_field_name(protocol, node.source_field)
                    lines.extend(
                        _indent(
                            level,
                            [
                                f"value.{field_name} = static_cast<decltype(value.{field_name})>(value.{source_field});",
                            ],
                        )
                    )
                else:
                    lines.extend(_indent(level, [f"Q_UNUSED(value.{field_name});"]))
                continue
            if node.bit_length > 64:
                lines.extend(
                    _indent(
                        level,
                        [
                            f"if (bitOffset + {node.bit_length} > len * 8) return;",
                            f"value.{field_name} = static_cast<decltype(value.{field_name})>(0);",
                            f"bitOffset += {node.bit_length};",
                        ],
                    )
                )
                continue
            lines.extend(
                _indent(
                    level,
                    [
                        f"if (bitOffset + {node.bit_length} > len * 8) return;",
                        f"value.{field_name} = {_decode_value_expr(field, endian_func, node.bit_length)};",
                    ],
                )
            )
            continue
        if isinstance(node, BranchNode):
            condition = _control_expr(node.control_fields, node.value, protocol)
            nested_lines, current_loop_index = _render_decode_nodes(
                node.children,
                protocol,
                path_parts + (node.label,),
                level + 1,
                endian_func,
                current_loop_index,
            )
            lines.extend(_indent(level, [f"if ({condition}) {{"]))
            lines.extend(nested_lines)
            lines.extend(_indent(level, ["}"]))
            continue

        repeat_var = f"repeatCount_{current_loop_index}"
        current_loop_index += 1
        lines.extend(_indent(level, _group_repeat_expr(node, protocol, repeat_var)))
        if node.condition:
            continue_var = f"continueGroup_{current_loop_index - 1}"
            lines.extend(_indent(level, [f"bool {continue_var} = true;"]))
        for index in range(node.repeat_count):
            iteration_label = f"{node.label}_{index + 1}" if node.repeat_count > 1 else node.label
            iteration_path = path_parts + (iteration_label,)
            guard = f"{repeat_var} > {index}"
            if node.condition:
                guard = f"{guard} && {continue_var}"
            lines.extend(_indent(level, [f"if ({guard}) {{"]))
            condition_expr, referenced_labels = _group_condition_expr(node, protocol, iteration_path)
            anchor_index = _group_condition_anchor_index(node.children, referenced_labels)
            if node.condition and condition_expr and anchor_index is not None:
                prefix_children = node.children[: anchor_index + 1]
                suffix_children = node.children[anchor_index + 1 :]
                prefix_lines, current_loop_index = _render_decode_nodes(
                    prefix_children,
                    protocol,
                    iteration_path,
                    level + 1,
                    endian_func,
                    current_loop_index,
                )
                lines.extend(prefix_lines)
                lines.extend(_indent(level + 1, [f"if ({condition_expr}) {{"]))
                suffix_lines, current_loop_index = _render_decode_nodes(
                    suffix_children,
                    protocol,
                    iteration_path,
                    level + 2,
                    endian_func,
                    current_loop_index,
                )
                lines.extend(suffix_lines)
                lines.extend(_indent(level + 1, ["} else {", f"    {continue_var} = false;", "}"]))
            else:
                nested_lines, current_loop_index = _render_decode_nodes(
                    node.children,
                    protocol,
                    iteration_path,
                    level + 1,
                    endian_func,
                    current_loop_index,
                )
                lines.extend(nested_lines)
            lines.extend(_indent(level, ["}"]))
    return lines, current_loop_index


def _render_encode_nodes(
    nodes: list[ProtocolNode],
    protocol: ProtocolSpec,
    path_parts: tuple[str, ...],
    level: int,
    endian_func: str,
    loop_index: int = 0,
) -> tuple[list[str], int]:
    """Renders encode statements for one node list."""

    lines: list[str] = []
    current_loop_index = loop_index
    for node in nodes:
        if isinstance(node, ScalarNode):
            field_name, field = _resolve_scalar_binding(protocol, node, path_parts)
            if node.bit_length is None:
                continue
            if field is None:
                continue
            if node.source_field:
                source_field = _resolve_protocol_field_name(protocol, node.source_field)
                lines.extend(
                    _indent(
                        level,
                        [
                            f"value.{field_name} = static_cast<decltype(value.{field_name})>(value.{source_field});",
                        ],
                    )
                )
            if node.bit_length == 0:
                continue
            if node.bit_length > 64:
                lines.extend(_indent(level, [f"appendZeroBits(data, bitOffset, {node.bit_length});"]))
                continue
            lines.extend(
                _indent(
                    level,
                    [f"{endian_func}(data, {_encode_value_expr(field_name, field, node.bit_length)}, bitOffset, {node.bit_length});"],
                )
            )
            continue
        if isinstance(node, BranchNode):
            condition = _control_expr(node.control_fields, node.value, protocol)
            nested_lines, current_loop_index = _render_encode_nodes(
                node.children,
                protocol,
                path_parts + (node.label,),
                level + 1,
                endian_func,
                current_loop_index,
            )
            lines.extend(_indent(level, [f"if ({condition}) {{"]))
            lines.extend(nested_lines)
            lines.extend(_indent(level, ["}"]))
            continue

        repeat_var = f"repeatCount_{current_loop_index}"
        current_loop_index += 1
        lines.extend(_indent(level, _group_repeat_expr(node, protocol, repeat_var)))
        if node.condition:
            continue_var = f"continueGroup_{current_loop_index - 1}"
            lines.extend(_indent(level, [f"bool {continue_var} = true;"]))
        for index in range(node.repeat_count):
            iteration_label = f"{node.label}_{index + 1}" if node.repeat_count > 1 else node.label
            iteration_path = path_parts + (iteration_label,)
            guard = f"{repeat_var} > {index}"
            if node.condition:
                guard = f"{guard} && {continue_var}"
            lines.extend(_indent(level, [f"if ({guard}) {{"]))
            condition_expr, referenced_labels = _group_condition_expr(node, protocol, iteration_path)
            anchor_index = _group_condition_anchor_index(node.children, referenced_labels)
            if node.condition and condition_expr and anchor_index is not None:
                prefix_children = node.children[: anchor_index + 1]
                suffix_children = node.children[anchor_index + 1 :]
                prefix_lines, current_loop_index = _render_encode_nodes(
                    prefix_children,
                    protocol,
                    iteration_path,
                    level + 1,
                    endian_func,
                    current_loop_index,
                )
                lines.extend(prefix_lines)
                lines.extend(_indent(level + 1, [f"if ({condition_expr}) {{"]))
                suffix_lines, current_loop_index = _render_encode_nodes(
                    suffix_children,
                    protocol,
                    iteration_path,
                    level + 2,
                    endian_func,
                    current_loop_index,
                )
                lines.extend(suffix_lines)
                lines.extend(_indent(level + 1, ["} else {", f"    {continue_var} = false;", "}"]))
            else:
                nested_lines, current_loop_index = _render_encode_nodes(
                    node.children,
                    protocol,
                    iteration_path,
                    level + 1,
                    endian_func,
                    current_loop_index,
                )
                lines.extend(nested_lines)
            lines.extend(_indent(level, ["}"]))
    return lines, current_loop_index


def _section_func_suffix(section_name: str) -> str:
    """Builds one function suffix from a section name."""

    token = normalize_token(section_name)
    parts = [part for part in token.split("_") if part]
    return "".join(part[:1].upper() + part[1:] for part in parts) or "Origin"


def _protocol_helper_name(protocol: ProtocolSpec, stem: str, label: str | None = None) -> str:
    """Builds one protocol-scoped helper function name."""

    suffix = _section_func_suffix(label or protocol.type_name)
    return f"{stem}{protocol.type_name}{suffix}"


def _member_condition(protocol: ProtocolSpec, member, value_name: str) -> str:
    """Builds one sequence member condition expression."""

    if not member.control_fields:
        return "true"
    targets = [part.strip() for part in (member.value or "").split(",") if part.strip()]
    checks: list[str] = []
    for index, field_label in enumerate(member.control_fields):
        cpp_name = protocol.label_to_cpp.get(field_label)
        if cpp_name is None:
            continue
        expected = targets[min(index, len(targets) - 1)] if targets else "0"
        checks.append(f"{value_name}.{cpp_name} == {expected}")
    return " && ".join(checks) or "true"


def _resolve_protocol_field_name(protocol: ProtocolSpec, field_name: str) -> str:
    """Resolves one protocol field name for verify-state generation."""

    valid_fields = {field.cpp_name for field in protocol.fields}
    if field_name in valid_fields:
        return field_name
    if field_name.startswith("namespace_"):
        candidate = field_name[len("namespace_") :]
        if candidate in valid_fields:
            return candidate
    normalized = normalize_token(field_name)
    if normalized in valid_fields:
        return normalized
    return field_name


def _render_sequence_helpers(protocol: ProtocolSpec, include_verify: bool = True) -> str:
    """Renders sequence helper functions."""

    sequences = protocol.sequences or []
    if not sequences:
        lines = [
            f"static QString check{protocol.type_name}SeqNum(const QString& seqNum)",
            "{",
            '    return seqNum.isEmpty() ? QStringLiteral("Seq_1") : seqNum;',
            "}",
        ]
        if include_verify:
            lines.extend(
                [
                    "",
                    f"static QString Verify{protocol.type_name}Seq({protocol.type_name}& value, const QString& seq)",
                    "{",
                    "    Q_UNUSED(value);",
                    '    return seq.isEmpty() ? QStringLiteral("Seq_1") : seq;',
                    "}",
                ]
            )
        return "\n".join(lines) + "\n"

    lines: list[str] = []
    for sequence in sequences:
        condition = " && ".join(_member_condition(protocol, member, "value") for member in sequence.members) or "true"
        lines.append(f"static bool match{protocol.type_name}_{sequence.name}({protocol.type_name}& value)")
        lines.append("{")
        lines.append(f"    return {condition};")
        lines.append("}")
        lines.append("")
    lines.append(f"static QString check{protocol.type_name}SeqNum(const QString& seqNum)")
    lines.append("{")
    for sequence in sequences:
        lines.append(f"    if (seqNum == {_quoted(sequence.name)}) return {_quoted(sequence.name)};")
    lines.append("    return QString();")
    lines.append("}")
    if include_verify:
        lines.append("")
        lines.append(f"static QString Verify{protocol.type_name}Seq({protocol.type_name}& value, const QString& seq)")
        lines.append("{")
        for sequence in sequences:
            lines.append(f"    if (seq == {_quoted(sequence.name)} && match{protocol.type_name}_{sequence.name}(value)) return {_quoted(sequence.name)};")
        lines.append("    return QString();")
        lines.append("}")
    return "\n".join(lines) + "\n"


def _render_verify_state_machine(
    protocol: ProtocolSpec,
    verify_spec: ProtocolVerifySpec,
    write_seq_func_names: dict[str, str],
) -> str:
    """Renders verify and response-state helpers for one protocol."""

    lines: list[str] = []
    for constraint in verify_spec.constraints:
        lines.extend(
            [
                f"static bool checkConstraint_{constraint.name}({protocol.type_name}& value)",
                "{",
                f"    return {constraint.check or 'true'};",
                "}",
                "",
                f"static bool setConstraint_{constraint.name}({protocol.type_name}& value)",
                "{",
            ]
        )
        if constraint.assignments:
            for assignment in constraint.assignments:
                field_name = _resolve_protocol_field_name(protocol, assignment.field)
                lines.append(
                    f"    value.{field_name} = static_cast<decltype(value.{field_name})>({assignment.value});"
                )
            lines.append("    return true;")
        else:
            lines.extend(["    Q_UNUSED(value);", "    return true;"])
        lines.extend(["}", ""])

    for rule in verify_spec.verify_rules:
        condition = f"seq == {_quoted(rule.when_seq)}"
        if rule.constraint:
            condition = f"{condition} && checkConstraint_{rule.constraint}(value)"
        lines.extend(
            [
                f"static QString checkVerify_{rule.name}({protocol.type_name}& value, const QString& seq)",
                "{",
                f"    return ({condition}) ? {_quoted(rule.name)} : QString();",
                "}",
                "",
            ]
        )

    for index, action in enumerate(verify_spec.response_actions, start=1):
        lines.extend(
            [
                f"static bool applyResponse_{index}({protocol.type_name}& value, QByteArray& data)",
                "{",
            ]
        )
        if action.set_constraint:
            lines.append(f"    setConstraint_{action.set_constraint}(value);")
        if action.encode_seq:
            write_func_name = write_seq_func_names.get(action.encode_seq)
            if write_func_name is None:
                write_func_name = f"write{protocol.type_name}{_section_func_suffix(action.encode_seq)}"
            lines.append(f"    {write_func_name}(value, data);")
        else:
            lines.append("    data.clear();")
        lines.extend(["    return true;", "}", ""])

    lines.extend(
        [
            f"static QString Verify{protocol.type_name}Seq({protocol.type_name}& value, const QString& seq)",
            "{",
        ]
    )
    for rule in verify_spec.verify_rules:
        lines.extend(
            [
                f"    QString verify_{rule.name} = checkVerify_{rule.name}(value, seq);",
                f"    if (verify_{rule.name}.isEmpty() == false) return verify_{rule.name};",
            ]
        )
    if verify_spec.default_verify is not None:
        lines.append(f"    return {_quoted(verify_spec.default_verify)};")
    else:
        lines.append("    return QString();")
    lines.extend(["}", "", f"int checkObjMaps(QString strVerify, QByteArray& data, {protocol.type_name}& value)", "{"])
    for index, action in enumerate(verify_spec.response_actions, start=1):
        lines.extend(
            [
                f"    if (strVerify == {_quoted(action.on_verify)}) {{",
                f"        applyResponse_{index}(value, data);",
                f"        return {action.return_code};",
                "    }",
            ]
        )
    lines.append("    data.clear();")
    lines.append(f"    return {verify_spec.default_return_code};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _crc_bind_candidates(field) -> list[str]:
    """Builds runtime bind-element candidate names for one field."""

    candidates: list[str] = []
    for candidate in (
        str(field.label or "").strip(),
        str(field.path_parts[-1] if field.path_parts else "").strip(),
        str(field.cpp_name or "").strip(),
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _render_crc_protocol_dispatch(protocol: ProtocolSpec, mode: str) -> str:
    """Renders one protocol-level CRC dispatch helper."""

    helper_name = f"{mode}RuntimeCrc{protocol.type_name}"
    action_name = "validateCrcField" if mode == "validate" else "applyCrcField"
    little_flag = "true" if protocol.endian == "little" else "false"
    lines = [
        f"static bool {helper_name}(const QString& bindElement, {'const QByteArray& data' if mode == 'validate' else 'QByteArray& data'})",
        "{",
    ]
    for field in protocol.fields:
        if field.bit_length is None or field.bit_length <= 0:
            continue
        comparisons = " || ".join(f"bindElement == {_quoted(candidate)}" for candidate in _crc_bind_candidates(field))
        if not comparisons:
            continue
        lines.extend(
            [
                f"    if ({comparisons}) {{",
                f"        return {action_name}(data, {field.bit_offset}, {field.bit_length}, {little_flag});",
                "    }",
            ]
        )
    lines.extend(["    return false;", "}"])
    return "\n".join(lines)


def _render_crc_dispatchers(protocols: list[ProtocolSpec]) -> str:
    """Renders protocol-level CRC dispatcher functions."""

    helper_blocks: list[str] = []
    validate_dispatch: list[str] = [
        "bool validateRuntimeCrc(const QString& protocolName, const QString& bindElement, const QByteArray& data)",
        "{",
    ]
    apply_dispatch: list[str] = [
        "bool applyRuntimeCrc(const QString& protocolName, const QString& bindElement, QByteArray& data)",
        "{",
    ]
    for protocol in protocols:
        helper_blocks.append(_render_crc_protocol_dispatch(protocol, "validate"))
        helper_blocks.append(_render_crc_protocol_dispatch(protocol, "apply"))
        validate_dispatch.extend(
            [
                f"    if (protocolName == {_quoted(protocol.type_name)}) return validateRuntimeCrc{protocol.type_name}(bindElement, data);",
            ]
        )
        apply_dispatch.extend(
            [
                f"    if (protocolName == {_quoted(protocol.type_name)}) return applyRuntimeCrc{protocol.type_name}(bindElement, data);",
            ]
        )
    validate_dispatch.extend(["    return false;", "}"])
    apply_dispatch.extend(["    return false;", "}"])
    return "\n\n".join([*helper_blocks, "\n".join(validate_dispatch), "\n".join(apply_dispatch)])


def _render_runtime_value_protocol_dispatch(protocol: ProtocolSpec) -> str:
    """Renders one protocol-level bind-element value extractor."""

    helper_name = f"extractRuntimeFieldValue{protocol.type_name}"
    little_flag = "true" if protocol.endian == "little" else "false"
    lines = [
        f"static QString {helper_name}(const QString& bindElement, const QByteArray& data)",
        "{",
    ]
    for field in protocol.fields:
        if field.bit_length is None or field.bit_length <= 0 or field.bit_length > 64:
            continue
        comparisons = " || ".join(f"bindElement == {_quoted(candidate)}" for candidate in _crc_bind_candidates(field))
        if not comparisons:
            continue
        lines.extend(
            [
                f"    if ({comparisons}) {{",
                f"        return QString::number(static_cast<qulonglong>(readFieldBits(data, {field.bit_offset}, {field.bit_length}, {little_flag})));",
                "    }",
            ]
        )
    lines.extend(["    return QString();", "}"])
    return "\n".join(lines)


def _render_runtime_value_dispatchers(protocols: list[ProtocolSpec]) -> str:
    """Renders protocol-level runtime bind-element value dispatchers."""

    helper_blocks: list[str] = []
    dispatch_lines: list[str] = [
        "QString extractRuntimeFieldValue(const QString& protocolName, const QString& bindElement, const QByteArray& data)",
        "{",
    ]
    for protocol in protocols:
        helper_blocks.append(_render_runtime_value_protocol_dispatch(protocol))
        dispatch_lines.append(
            f"    if (protocolName == {_quoted(protocol.type_name)}) return extractRuntimeFieldValue{protocol.type_name}(bindElement, data);"
        )
    dispatch_lines.extend(["    return QString();", "}"])
    return "\n\n".join([*helper_blocks, "\n".join(dispatch_lines)])


def _render_runtime_crc_validate_lines(
    protocol: ProtocolSpec,
    data_var: str,
    transport: TransportSpec | None,
    parent_aliases: list[str] | None = None,
) -> list[str]:
    """Renders receive-side CRC validation lines."""

    lines: list[str] = []
    for rule in _match_message_rules(transport, protocol, parent_aliases=parent_aliases):
        if not rule.crc_check.enabled or not rule.crc_check.bind_element:
            continue
        lines.append(
            f"if (!validateRuntimeCrc({_quoted(protocol.type_name)}, {_quoted(rule.crc_check.bind_element)}, {data_var})) continue;"
        )
    return lines


def _render_runtime_crc_apply_lines(
    protocol: ProtocolSpec,
    data_var: str,
    transport: TransportSpec | None,
    parent_aliases: list[str] | None = None,
) -> list[str]:
    """Renders send-side CRC writeback lines."""

    lines: list[str] = []
    for rule in _match_message_rules(transport, protocol, parent_aliases=parent_aliases):
        if not rule.crc_check.enabled or not rule.crc_check.bind_element:
            continue
        lines.append(
            f"applyRuntimeCrc({_quoted(protocol.type_name)}, {_quoted(rule.crc_check.bind_element)}, {data_var});"
        )
    return lines


def _render_codec_impl(protocol: ProtocolSpec, verify_spec: ProtocolVerifySpec | None = None) -> str:
    """Renders codec.cpp functions for one protocol."""

    read_func = "readBitsLE" if protocol.endian == "little" else "readBits"
    append_func = "appendBitsLE" if protocol.endian == "little" else "appendBits"
    section_helpers: list[str] = []
    decode_calls: list[str] = []
    encode_section_calls: list[str] = []
    for section in protocol.sections:
        suffix = _section_func_suffix(section.name)
        read_section_name = f"read{protocol.type_name}{suffix}"
        write_section_name = f"write{protocol.type_name}{suffix}"
        decode_lines, _ = _render_decode_nodes(section.nodes, protocol, (), 1, read_func)
        encode_lines, _ = _render_encode_nodes(section.nodes, protocol, (), 1, append_func)
        if not decode_lines:
            decode_lines = _indent(1, ["Q_UNUSED(value);", "Q_UNUSED(raw);", "Q_UNUSED(len);", "Q_UNUSED(bitOffset);"])
        if not encode_lines:
            encode_lines = _indent(1, ["Q_UNUSED(value);", "Q_UNUSED(data);"])
        section_helpers.append(
            "\n".join(
                [
                    f"static void {read_section_name}({protocol.type_name}& value, const QByteArray& raw, int len, int& bitOffset)",
                    "{",
                    *decode_lines,
                    "}",
                    "",
                    f"static void {write_section_name}({protocol.type_name}& value, QByteArray& data, int& bitOffset)",
                    "{",
                    *encode_lines,
                    "}",
                ]
            )
        )
        decode_calls.append(f"    {read_section_name}(value, raw, len, bitOffset);")
        encode_section_calls.append(f"    {write_section_name}(value, data, bitOffset);")

    sequences = protocol.sequences or []
    default_seq_name = sequences[0].name if sequences else "Seq_1"
    write_seq_names = [sequence.name for sequence in sequences] or [default_seq_name]
    write_seq_func_names = {
        seq_name: f"write{protocol.type_name}{_section_func_suffix(seq_name)}"
        for seq_name in write_seq_names
    }
    default_write_seq_func = write_seq_func_names[default_seq_name]
    check_encode_name = f"checkEncodeSeqNumber{protocol.type_name}"
    verify_field_name = f"VerifyField{protocol.type_name}"
    update_field_name = f"updateFieldValue{protocol.type_name}"
    update_group_name = f"updateGroupFlag{protocol.type_name}"
    branch_control_rules = _collect_branch_control_rules(protocol)

    seq_choose_lines = [
        f"    if (match{protocol.type_name}_{sequence.name}(value)) return {_quoted(sequence.name)};"
        for sequence in sequences
    ]
    encode_default_return_line = f"    return {_quoted(default_seq_name)};"

    decode_match_lines = [
        f"    if (match{protocol.type_name}_{sequence.name}(value)) return Verify{protocol.type_name}Seq(value, {_quoted(sequence.name)});"
        for sequence in sequences
    ]
    if not decode_match_lines:
        decode_match_lines.append(f"    return Verify{protocol.type_name}Seq(value, {_quoted(default_seq_name)});")
    else:
        decode_match_lines.append(f"    return Verify{protocol.type_name}Seq(value, {_quoted(default_seq_name)});")

    write_seq_blocks: list[str] = []
    for seq_name in write_seq_names:
        write_seq_blocks.append(
            "\n".join(
                [
                    f"static void {write_seq_func_names[seq_name]}({protocol.type_name}& value, QByteArray& data)",
                    "{",
                    "    data.clear();",
                    "    int bitOffset = 0;",
                    *encode_section_calls,
                    "}",
                ]
            )
        )

    encode_dispatch_lines = [
        f"    if (seq == {_quoted(seq_name)}) {{ {write_seq_func_names[seq_name]}(value, data); return; }}"
        for seq_name in write_seq_names
    ]
    if not encode_dispatch_lines:
        encode_dispatch_lines = [f"    {default_write_seq_func}(value, data);", "    return;"]

    check_obj_dispatch_lines = [
        f"    if (seq == {_quoted(seq_name)}) {{ {write_seq_func_names[seq_name]}(value, data); return 0; }}"
        for seq_name in write_seq_names
    ]

    write_seq_forward_decl_text = "\n".join(
        f"static void {write_seq_func_names[seq_name]}({protocol.type_name}& value, QByteArray& data);"
        for seq_name in write_seq_names
    )
    sequence_helpers_text = _render_sequence_helpers(protocol, include_verify=verify_spec is None)
    verify_state_text = (
        _render_verify_state_machine(protocol, verify_spec, write_seq_func_names)
        if verify_spec is not None
        else ""
    )
    section_helpers_text = "\n\n".join(section_helpers)
    write_seq_text = "\n\n".join(write_seq_blocks)
    decode_calls_text = "\n".join(decode_calls)
    seq_choose_text = "\n".join(seq_choose_lines)
    decode_match_text = "\n".join(decode_match_lines)
    encode_dispatch_text = "\n".join(encode_dispatch_lines)
    check_obj_dispatch_text = "\n".join(check_obj_dispatch_lines)
    update_group_text = "\n".join(
        [f"    value.{control_cpp} = ({expr}) ? 1 : 0;" for control_cpp, expr in branch_control_rules]
        or ["    Q_UNUSED(value);"]
    )

    generic_check_obj_text = f"""int checkObjMaps(QString strVerify, QByteArray& data, {protocol.type_name}& value)
{{
    const QString seq = check{protocol.type_name}SeqNum(strVerify);
    if (seq.isEmpty()) {{
        data.clear();
        return -1;
    }}
    if (Verify{protocol.type_name}Seq(value, seq).isEmpty()) {{
        data.clear();
        return -1;
    }}
{check_obj_dispatch_text}
    data.clear();
    return -1;
}}"""

    return f"""{sequence_helpers_text}
{write_seq_forward_decl_text}
{verify_state_text}
{section_helpers_text}

{write_seq_text}

{generic_check_obj_text if verify_spec is None else ""}

QString decodeMsg(uchar* pData, int len, {protocol.type_name}& value)
{{
    QByteArray raw(reinterpret_cast<const char*>(pData), len);
    int bitOffset = 0;
{decode_calls_text}
{decode_match_text}
}}

static QString {check_encode_name}({protocol.type_name}& value)
{{
{seq_choose_text}
{encode_default_return_line}
}}

static void {verify_field_name}({protocol.type_name}& value)
{{
    Q_UNUSED(value);
}}

static void {update_field_name}({protocol.type_name}& value)
{{
    Q_UNUSED(value);
}}

static void {update_group_name}({protocol.type_name}& value)
{{
{update_group_text}
}}

void encodeMsg(QByteArray& data, {protocol.type_name}& value)
{{
    const QString seq = {check_encode_name}(value);
    {verify_field_name}(value);
    {update_field_name}(value);
    {update_group_name}(value);
{encode_dispatch_text}
    data.clear();
    {default_write_seq_func}(value, data);
}}
"""


def render_codec_cpp(
    protocols: list[ProtocolSpec],
    protocol_verifies: dict[str, ProtocolVerifySpec] | None = None,
) -> str:
    """Renders codec.cpp."""

    blocks = [
        _msvc_utf8_preamble().rstrip(),
        '#include "codec.h"',
        "#include <cstring>",
        "#include <QStringList>",
        "#include <QtGlobal>",
        "",
        "namespace {",
        "quint64 readBits(const QByteArray& data, int& bitOffset, int bitLength)",
        "{",
        "    quint64 value = 0;",
        "    for (int index = 0; index < bitLength; ++index) {",
        "        const int absoluteBit = bitOffset + index;",
        "        const int byteIndex = absoluteBit / 8;",
        "        const int bitIndex = 7 - (absoluteBit % 8);",
        "        if (byteIndex >= data.size()) return value;",
        "        const quint8 byteValue = static_cast<quint8>(data.at(byteIndex));",
        "        value = (value << 1) | ((byteValue >> bitIndex) & 0x01);",
        "    }",
        "    bitOffset += bitLength;",
        "    return value;",
        "}",
        "",
        "quint64 readBitsLE(const QByteArray& data, int& bitOffset, int bitLength)",
        "{",
        "    quint64 value = 0;",
        "    int remaining = bitLength;",
        "    while (remaining > 0) {",
        "        const int byteIndex = bitOffset / 8;",
        "        const int usedBits = bitOffset % 8;",
        "        if (byteIndex >= data.size()) return value;",
        "        const int chunkBits = qMin(remaining, 8 - usedBits);",
        "        const int firstBitIndex = 8 - usedBits - chunkBits;",
        "        quint64 chunkValue = 0;",
        "        for (int index = 0; index < chunkBits; ++index) {",
        "            const int bitIndex = firstBitIndex + index;",
        "            const quint8 byteValue = static_cast<quint8>(data.at(byteIndex));",
        "            chunkValue = (chunkValue << 1) | ((byteValue >> (7 - bitIndex)) & 0x01);",
        "        }",
        "        value = (value << chunkBits) | chunkValue;",
        "        bitOffset += chunkBits;",
        "        remaining -= chunkBits;",
        "    }",
        "    return value;",
        "}",
        "",
        "void appendBits(QByteArray& data, quint64 value, int& bitOffset, int bitLength)",
        "{",
        "    const int startBit = bitOffset;",
        "    const int totalBits = bitOffset + bitLength;",
        "    const int requiredBytes = (totalBits + 7) / 8;",
        "    if (data.size() < requiredBytes) data.append(QByteArray(requiredBytes - data.size(), '\\0'));",
        "    for (int index = 0; index < bitLength; ++index) {",
        "        const int absoluteBit = startBit + index;",
        "        const int byteIndex = absoluteBit / 8;",
        "        const int bitIndex = 7 - (absoluteBit % 8);",
        "        const quint64 bitValue = (value >> (bitLength - index - 1)) & 0x01ULL;",
        "        char byteValue = data[byteIndex];",
        "        if (bitValue != 0) byteValue = static_cast<char>(byteValue | (1 << bitIndex));",
        "        else byteValue = static_cast<char>(byteValue & ~(1 << bitIndex));",
        "        data[byteIndex] = byteValue;",
        "    }",
        "    bitOffset += bitLength;",
        "}",
        "",
        "void appendBitsLE(QByteArray& data, quint64 value, int& bitOffset, int bitLength)",
        "{",
        "    int remaining = bitLength;",
        "    while (remaining > 0) {",
        "        const int byteIndex = bitOffset / 8;",
        "        const int usedBits = bitOffset % 8;",
        "        const int chunkBits = qMin(remaining, 8 - usedBits);",
        "        const int totalBits = bitOffset + chunkBits;",
        "        const int requiredBytes = (totalBits + 7) / 8;",
        "        if (data.size() < requiredBytes) data.append(QByteArray(requiredBytes - data.size(), '\\0'));",
        "        const int firstBitIndex = 8 - usedBits - chunkBits;",
        "        const quint64 chunkValue = (value >> (remaining - chunkBits)) & ((chunkBits == 64) ? ~0ULL : ((1ULL << chunkBits) - 1));",
        "        for (int index = 0; index < chunkBits; ++index) {",
        "            const quint64 bitValue = (chunkValue >> (chunkBits - index - 1)) & 0x01ULL;",
        "            char byteValue = data[byteIndex];",
        "            const int bitIndex = firstBitIndex + index;",
        "            if (bitValue != 0) byteValue = static_cast<char>(byteValue | (1 << (7 - bitIndex)));",
        "            else byteValue = static_cast<char>(byteValue & ~(1 << (7 - bitIndex)));",
        "            data[byteIndex] = byteValue;",
        "        }",
        "        bitOffset += chunkBits;",
        "        remaining -= chunkBits;",
        "    }",
        "}",
        "",
        "void appendZeroBits(QByteArray& data, int& bitOffset, int bitLength)",
        "{",
        "    int remaining = bitLength;",
        "    while (remaining > 0) {",
        "        const int chunkBits = qMin(remaining, 64);",
        "        appendBits(data, 0, bitOffset, chunkBits);",
        "        remaining -= chunkBits;",
        "    }",
        "}",
        "",
        "float bitsToFloat(quint32 bits)",
        "{",
        "    float value = 0.0f;",
        "    std::memcpy(&value, &bits, sizeof(value));",
        "    return value;",
        "}",
        "",
        "double bitsToDouble(quint64 bits)",
        "{",
        "    double value = 0.0;",
        "    std::memcpy(&value, &bits, sizeof(value));",
        "    return value;",
        "}",
        "",
        "quint32 floatToBits(float value)",
        "{",
        "    quint32 bits = 0;",
        "    std::memcpy(&bits, &value, sizeof(bits));",
        "    return bits;",
        "}",
        "",
        "quint64 doubleToBits(double value)",
        "{",
        "    quint64 bits = 0;",
        "    std::memcpy(&bits, &value, sizeof(bits));",
        "    return bits;",
        "}",
        "",
        "quint16 computeGeneratedCrc16(const QByteArray& raw)",
        "{",
        "    quint16 crc = 0xFFFF;",
        "    for (unsigned char byte : raw) {",
        "        crc ^= static_cast<quint16>(byte);",
        "        for (int index = 0; index < 8; ++index) {",
        "            if (crc & 0x0001) crc = static_cast<quint16>((crc >> 1) ^ 0xA001);",
        "            else crc = static_cast<quint16>(crc >> 1);",
        "        }",
        "    }",
        "    return crc;",
        "}",
        "",
        "quint64 crcBitMask(int bitLength)",
        "{",
        "    if (bitLength >= 64) return ~0ULL;",
        "    return (1ULL << bitLength) - 1ULL;",
        "}",
        "",
        "quint64 normalizeUnsignedBits(qint64 value, int bitLength)",
        "{",
        "    if (bitLength <= 0) return 0ULL;",
        "    if (value <= 0) return 0ULL;",
        "    if (bitLength >= 63) return static_cast<quint64>(value);",
        "    const quint64 maxValue = (1ULL << bitLength) - 1ULL;",
        "    const quint64 rawValue = static_cast<quint64>(value);",
        "    return rawValue > maxValue ? maxValue : rawValue;",
        "}",
        "",
        "quint64 readFieldBits(const QByteArray& data, int bitOffset, int bitLength, bool littleEndian)",
        "{",
        "    int runtimeOffset = bitOffset;",
        "    return littleEndian ? readBitsLE(data, runtimeOffset, bitLength) : readBits(data, runtimeOffset, bitLength);",
        "}",
        "",
        "void writeFieldBits(QByteArray& data, quint64 value, int bitOffset, int bitLength, bool littleEndian)",
        "{",
        "    int runtimeOffset = bitOffset;",
        "    if (littleEndian) appendBitsLE(data, value, runtimeOffset, bitLength);",
        "    else appendBits(data, value, runtimeOffset, bitLength);",
        "}",
        "",
        "bool validateCrcField(const QByteArray& data, int bitOffset, int bitLength, bool littleEndian)",
        "{",
        "    const quint64 actual = readFieldBits(data, bitOffset, bitLength, littleEndian);",
        "    QByteArray payload = data;",
        "    writeFieldBits(payload, 0, bitOffset, bitLength, littleEndian);",
        "    const quint64 expected = static_cast<quint64>(computeGeneratedCrc16(payload)) & crcBitMask(bitLength);",
        "    return actual == expected;",
        "}",
        "",
        "bool applyCrcField(QByteArray& data, int bitOffset, int bitLength, bool littleEndian)",
        "{",
        "    QByteArray payload = data;",
        "    writeFieldBits(payload, 0, bitOffset, bitLength, littleEndian);",
        "    const quint64 crc = static_cast<quint64>(computeGeneratedCrc16(payload)) & crcBitMask(bitLength);",
        "    writeFieldBits(data, crc, bitOffset, bitLength, littleEndian);",
        "    return true;",
        "}",
        "}  // namespace",
        "",
    ]
    verify_lookup = protocol_verifies or {}
    blocks.append(_render_crc_dispatchers(protocols))
    blocks.append("")
    blocks.append(_render_runtime_value_dispatchers(protocols))
    blocks.append("")
    blocks.extend(_render_codec_impl(protocol, verify_lookup.get(protocol.type_name)) for protocol in protocols)
    return "\n".join(blocks).rstrip() + "\n"


def render_mapping_header(file_guard: str, function_signature: str, includes: list[str]) -> str:
    """Renders one mapping header."""

    include_lines = "\n".join(f'#include "{header}"' for header in includes)
    return f"""{_msvc_utf8_preamble()}
#ifndef {file_guard}
#define {file_guard}

{include_lines}

{function_signature};

#endif
"""


def render_mapping_cpp(
    header_name: str,
    function_signature: str,
    target_protocol: str,
    target_var_name: str,
    body: str,
) -> str:
    """Renders one mapping source file."""

    return f"""{_msvc_utf8_preamble()}
#include "{header_name}"

{function_signature}
{{
    {target_protocol} {target_var_name};
{body}    return {target_var_name};
}}
"""


def render_choreography_header() -> str:
    """Renders the choreography header."""

    return _msvc_utf8_preamble() + """#ifndef TO_CODE_CHOREOGRAPHY_H
#define TO_CODE_CHOREOGRAPHY_H

#include <QMap>
#include <QObject>
#include <QString>
#include <QVector>
#include <QtGlobal>

class code_test {
public:
    static qulonglong getDstMsg_41(QString name);
    static qulonglong getSrcTime_41(QString s1, QString s2);
    QMap<QString, uint> getAllSrcTime_41();
    QMap<QString, uint> getAllDstTime_41();
    int getStatus_41(QString s1);
};

#endif
"""


def render_choreography_cpp(spec: ChoreographySpec) -> str:
    """Renders the choreography implementation."""

    dest_proto_list = ",".join(_quoted(target.protocol) for target in spec.targets)
    template_list = ",".join(_quoted(target.template_name) for target in spec.targets)
    status_list = ",".join("true" if target.initial_status == "cache" else "false" for target in spec.targets)
    src_list = ",".join(_quoted(source.protocol) for source in spec.sources)
    receive_windows = ",".join(str(target.receive_window_ms) for target in spec.targets)
    matrix_rows = spec.joint_groups[0].matrix.values if spec.joint_groups else []
    matrix_cpp_rows = []
    for row in matrix_rows:
        rendered = ",".join("-1" if value is None else str(value) for value in row)
        matrix_cpp_rows.append("{" + rendered + "}")
    matrix_cpp = ",".join(matrix_cpp_rows) if matrix_cpp_rows else "{0}"
    return f"""{_msvc_utf8_preamble()}
#include "to_code_Choreography.h"

QVector<QString> destProtoList_41 = {{{dest_proto_list}}};
QVector<QString> templateList_41 = {{{template_list}}};
QVector<bool> statusList41 = {{{status_list}}};
QVector<QString> src_list_41 = {{{src_list}}};
QVector<qulonglong> src_receive_time_list_41 = {{{receive_windows}}};
QVector<QVector<int>> target_send_martix_41 = {{{matrix_cpp}}};

qulonglong code_test::getDstMsg_41(QString name)
{{
    int pos = -1;
    for (int index = 0; index < destProtoList_41.size(); ++index) {{
        if (destProtoList_41[index] == name) {{
            pos = index;
            break;
        }}
    }}
    if (pos == -1) return pos;
    return src_receive_time_list_41[pos];
}}

qulonglong code_test::getSrcTime_41(QString s1, QString s2)
{{
    int left = -1;
    int right = -1;
    for (int index = 0; index < src_list_41.size(); ++index) {{
        if (src_list_41[index] == s1) left = index;
        if (src_list_41[index] == s2) right = index;
    }}
    if (left == -1 || right == -1) return -1;
    return target_send_martix_41[left][right];
}}

QMap<QString, uint> code_test::getAllSrcTime_41()
{{
    QMap<QString, uint> result;
    for (int index = 0; index < templateList_41.size(); ++index) result[templateList_41[index]] = src_receive_time_list_41[index];
    return result;
}}

QMap<QString, uint> code_test::getAllDstTime_41()
{{
    QMap<QString, uint> result;
    for (int left = 0; left < src_list_41.size(); ++left) {{
        for (int right = 0; right < src_list_41.size(); ++right) result[src_list_41[left] + ":" + src_list_41[right]] = target_send_martix_41[left][right];
    }}
    return result;
}}

int code_test::getStatus_41(QString s1)
{{
    int pos = -1;
    for (int index = 0; index < destProtoList_41.size(); ++index) {{
        if (destProtoList_41[index] == s1) pos = index;
    }}
    if (pos == -1) return -1;
    return statusList41[pos];
}}
"""


def _render_qmake_block(items: list[str]) -> str:
    """Renders one qmake item block."""

    lines = []
    last_index = len(items) - 1
    for index, item in enumerate(items):
        suffix = " \\" if index != last_index else ""
        lines.append(f"\t{item}{suffix}")
    return "\n".join(lines)


def render_pro_file(project_name: str, headers: list[str], sources: list[str], joint: bool) -> str:
    """Renders peach.pro."""

    all_sources = ["main.cpp", "messageconvert.cpp", *sources, "codec.cpp"]
    if joint:
        all_sources.append("to_code_Choreography.cpp")
    all_headers = ["messageconvert.h", *headers, "codec.h"]
    if joint:
        all_headers.append("to_code_Choreography.h")
    return f"""QT = core xml network concurrent

CONFIG += c++17 cmdline
TARGET = {project_name}
SOURCES += \\
{_render_qmake_block(all_sources)}

HEADERS += \\
{_render_qmake_block(all_headers)}
"""


def render_messageconvert_header(process_methods: list[str], joint: bool) -> str:
    """Renders messageconvert.h."""

    extra_slot = "    void onCheckDataTimer();\n" if joint else ""
    extra_member = "    QTimer checkDataTimer;\n" if joint else ""
    extra_check = "    void checkData(QString name, int time);\n" if joint else ""
    state_decl = "QStringList state = {};"
    process_decls = "\n".join(f"    void {method}();" for method in process_methods)
    return f"""{_msvc_utf8_preamble()}
#ifndef MESSAGECONVERT_H
#define MESSAGECONVERT_H

#include <QObject>
#include <QHostAddress>
#include <QMap>
#include <QMutex>
#include <QStringList>
#include <QTimer>
#include <QUdpSocket>
#include <QVector>
#include <memory>

class messageConvert : public QObject
{{
    Q_OBJECT
public:
    explicit messageConvert(QObject* parent = nullptr);
    enum NetType {{ emTCP, emUDP, emDDS }};
    class NetInfo {{ public: QString name; QString ip; int port = 0; quint16 feedBackPort = 0; int netType = emUDP; bool bRecvTag = true; }};
    class CrcCheckInfo {{ public: bool enabled = false; QString bindElement; }};
    class AggregationInfo {{ public: QString mode = QStringLiteral("SINGLE"); int count = -1; int timeMs = -1; QString compareOperator; QString compareValue; }};
    class AggregationTypeInfo {{ public: QString type = QStringLiteral("TIME"); QString bindElement; }};
    class MessageRuleInfo {{
    public:
        QString messageName;
        int delayRequirement = 0;
        CrcCheckInfo crcCheck;
        AggregationInfo aggregation;
        AggregationTypeInfo aggregationType;
    }};
    class msgDataInfo {{ public: QByteArray data; QVector<qulonglong> time; QString name; QString protocolName; QString cacheName; QString ip; quint16 port = 0; {state_decl} int num = 0; int cacheNum = 0; bool cacheOnly = false; bool jointControlled = false; }};

signals:
    void showMessage(QString msg);

public slots:
    void readPendingDatagrams(QString name, QHostAddress ip, quint16 port, QByteArray data);
{extra_slot}private:
    int _maxThread = 5;
    int _threadExit = 0;
    std::shared_ptr<QUdpSocket> udpSend;
    QVector<std::shared_ptr<NetInfo>> udpSendList;
    QVector<std::shared_ptr<QUdpSocket>> udpRecvList;
    QVector<std::shared_ptr<MessageRuleInfo>> messageRuleList;
    QMap<QString, QString> crcValueMap;
    QVector<std::shared_ptr<msgDataInfo>> dataInfo;
    QMutex dataMutex;
{extra_member}    void pushData(std::shared_ptr<msgDataInfo> data);
    void getData(QString protocolName, QString name, QString consumerKey, int time, int num, QByteArray& data, QString& ip, int& port, int& outTime);
    void getDataBatch(QString protocolName, QString name, QString consumerKey, int time, int num, int maxBatchSize, QVector<QByteArray>& batchData, QVector<QString>& batchIps, QVector<int>& batchPorts, QVector<int>& batchTimes);
{extra_check}    void msgConvertThread();
    void onSendMessage(QByteArray msg);
    void onSendMessage(const QString& protocolName, const QString& targetName, QByteArray msg);
    QString computeCrc16Hex(const QString& raw) const;
    void cacheCrcValue(const QString& messageName, const QString& bindElement, const QString& rawValue);
    void cacheGeneratedTarget(const QString& targetName, int num, const QByteArray& data);
    QString normalizeRuntimeMessageName(const QString& value) const;
    QString resolveCanonicalRuntimeMessageName(const QString& protocolName, const QString& messageName) const;
    QString resolveInboundProtocolName(const QString& messageName, const QByteArray& data) const;
    std::shared_ptr<MessageRuleInfo> findMessageRule(const QString& protocolName, const QString& messageName) const;
    bool isLoopAggregationSource(const QString& protocolName, const QString& messageName) const;
    QString resolveAggregationBindValue(const QString& protocolName, const QString& bindElement, const QByteArray& data) const;
    bool compareRuleConditionValue(const QString& operatorName, const QString& actualValue, const QString& expectedValue) const;
    bool shouldApplyRuleCondition(const QString& protocolName, const QString& messageName, const QByteArray& data) const;
    QVector<int> collectReadyBatchIndexes(const QVector<std::shared_ptr<msgDataInfo>>& queue, const QString& protocolName, const QString& messageName, int time, int num, bool forceTimeWindow) const;
    QVector<int> normalizeAggregatedBatchIndexes(const QVector<std::shared_ptr<msgDataInfo>>& queue, const QVector<int>& batchIndexes, const QString& protocolName, const QString& messageName) const;
    void removeQueueIndexes(QVector<std::shared_ptr<msgDataInfo>>& queue, const QVector<int>& indexes);
    void trimQueue(QVector<std::shared_ptr<msgDataInfo>>& queue, const QString& messageName, int maxEntries);
    void routeGeneratedTarget(const QString& protocolName, const QString& targetName, const QString& cacheName, int cacheNum, bool jointControlled, bool cacheOnly, const QByteArray& data);
{process_decls}

public:
    int start(QVector<std::shared_ptr<NetInfo>> netlist, QVector<std::shared_ptr<MessageRuleInfo>> ruleList, int maxThread = 5);
    int stop();
}};

#endif
"""


def _fetch_runtime_source(conversion: ConversionSpec, alias: str):
    """Returns runtime metadata for one alias."""

    for item in conversion.runtime.sources:
        if item.alias == alias:
            return item
    return None


def _resolve_route_target_protocol(
    protocol_lookup: dict[str, ProtocolSpec],
    route_target: str,
) -> ProtocolSpec | None:
    """Resolves one root-route target protocol against loaded XML specs."""

    target_key = normalize_token(route_target)
    for protocol in protocol_lookup.values():
        if target_key in {
            normalize_token(protocol.type_name),
            normalize_token(protocol.file_stem),
        }:
            return protocol
    return None


def _build_route_alias_maps(
    protocol_lookup: dict[str, ProtocolSpec],
    used_protocol_names: set[str],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Builds parent-child alias maps for runtime route compatibility."""

    used_keys = {normalize_token(name) for name in used_protocol_names}
    parent_to_children: dict[str, list[str]] = {}
    child_to_parents: dict[str, list[str]] = {}
    for parent_protocol in protocol_lookup.values():
        seen_children: set[str] = set()
        for route in parent_protocol.routes:
            child_protocol = _resolve_route_target_protocol(protocol_lookup, route.target_protocol)
            if child_protocol is None:
                continue
            if normalize_token(child_protocol.type_name) not in used_keys:
                continue
            if child_protocol.type_name in seen_children:
                continue
            seen_children.add(child_protocol.type_name)
            parent_to_children.setdefault(parent_protocol.type_name, []).append(child_protocol.type_name)
            child_to_parents.setdefault(child_protocol.type_name, []).append(parent_protocol.type_name)
    return parent_to_children, child_to_parents


def _render_route_runtime_helpers(
    protocol_lookup: dict[str, ProtocolSpec],
    used_protocol_names: set[str],
    inbound_protocol_names: set[str] | None = None,
) -> str:
    """Renders runtime helpers that consume parent XML route metadata."""

    parent_to_children, child_to_parents = _build_route_alias_maps(protocol_lookup, used_protocol_names)
    inbound_names = inbound_protocol_names or used_protocol_names

    canonical_lines = [
        "QString messageConvert::resolveCanonicalRuntimeMessageName(const QString& protocolName, const QString& messageName) const",
        "{",
        "    const QString normalizedProtocol = normalizeRuntimeMessageName(protocolName);",
        "    const QString normalizedMessage = normalizeRuntimeMessageName(messageName);",
        "    if (normalizedMessage.isEmpty()) return protocolName;",
        "    if (normalizedProtocol == normalizedMessage) return protocolName;",
    ]
    for child_name, parent_names in child_to_parents.items():
        canonical_lines.append(
            f"    if (normalizedProtocol == normalizeRuntimeMessageName({_quoted(child_name)})) {{"
        )
        for parent_name in parent_names:
            canonical_lines.append(
                f"        if (normalizedMessage == normalizeRuntimeMessageName({_quoted(parent_name)})) return {_quoted(child_name)};"
            )
        canonical_lines.append("    }")
    for parent_name, child_names in parent_to_children.items():
        if len(child_names) != 1:
            continue
        canonical_lines.append(
            f"    if (normalizedProtocol == normalizeRuntimeMessageName({_quoted(parent_name)}) && "
            f"normalizedMessage == normalizeRuntimeMessageName({_quoted(parent_name)})) return {_quoted(child_names[0])};"
        )
    canonical_lines.extend(["    return messageName;", "}"])

    inbound_lines = [
        "QString messageConvert::resolveInboundProtocolName(const QString& messageName, const QByteArray& data) const",
        "{",
        "    const QString normalizedMessage = normalizeRuntimeMessageName(messageName);",
    ]
    for parent_name, child_names in parent_to_children.items():
        parent_protocol = protocol_lookup[parent_name]
        inbound_lines.append(
            f"    if (normalizedMessage == normalizeRuntimeMessageName({_quoted(parent_name)})) {{"
        )
        for route in parent_protocol.routes:
            child_protocol = _resolve_route_target_protocol(protocol_lookup, route.target_protocol)
            if child_protocol is None or child_protocol.type_name not in child_names:
                continue
            expected_values = [part.strip() for part in str(route.value or "").split(",")]
            comparisons: list[str] = []
            if route.control_fields and len(route.control_fields) == len(expected_values):
                for control_field, expected_value in zip(route.control_fields, expected_values):
                    if not control_field or not expected_value:
                        comparisons = []
                        break
                    comparisons.append(
                        f'extractRuntimeFieldValue({_quoted(parent_protocol.type_name)}, {_quoted(control_field)}, data) == {_quoted(expected_value)}'
                    )
            if comparisons:
                inbound_lines.append(f"        if ({' && '.join(comparisons)}) return {_quoted(child_protocol.type_name)};")
        if len(child_names) == 1:
            inbound_lines.append(f"        return {_quoted(child_names[0])};")
        inbound_lines.append("    }")
    direct_frame_sizes: dict[int, list[str]] = {}
    for protocol_name in sorted(inbound_names):
        protocol = protocol_lookup.get(protocol_name)
        if protocol is None or protocol.total_bits <= 0:
            continue
        direct_frame_sizes.setdefault((protocol.total_bits + 7) // 8, []).append(protocol.type_name)
    for byte_size, protocol_names in sorted(direct_frame_sizes.items()):
        if len(protocol_names) != 1:
            continue
        inbound_lines.append(f"    if (data.size() == {byte_size}) return {_quoted(protocol_names[0])};")
    inbound_lines.extend(
        [
            "    return resolveCanonicalRuntimeMessageName(messageName, messageName);",
            "}",
        ]
    )
    return "\n\n".join(["\n".join(canonical_lines), "\n".join(inbound_lines)])


def _match_message_rules(
    transport: TransportSpec | None,
    protocol: ProtocolSpec,
    parent_aliases: list[str] | None = None,
) -> list[MessageRuleDetailSpec]:
    """Returns runtime message rules applicable to one protocol."""

    if transport is None:
        return []
    protocol_keys = {
        normalize_token(protocol.type_name),
        normalize_token(protocol.file_stem),
    }
    for alias in parent_aliases or []:
        protocol_keys.add(normalize_token(alias))
    return [
        rule
        for rule in transport.message_rules
        if normalize_token(rule.message_name) in protocol_keys
    ]


def _resolve_bind_field(protocol: ProtocolSpec, bind_element: str | None) -> str | None:
    """Resolves one bind-element display name to a flattened C++ field name."""

    candidate = str(bind_element or "").strip()
    if not candidate:
        return None
    candidate_key = normalize_token(candidate)
    mapped = protocol.label_to_cpp.get(candidate)
    if mapped:
        return mapped
    for field in protocol.fields:
        keys = {
            normalize_token(field.cpp_name),
            normalize_token(field.label),
            normalize_token(field.path_parts[-1] if field.path_parts else field.cpp_name),
        }
        if candidate_key in keys:
            return field.cpp_name
    return None


def _render_crc_capture_lines(
    protocol: ProtocolSpec,
    value_var: str,
    transport: TransportSpec | None,
    parent_aliases: list[str] | None = None,
) -> list[str]:
    """Renders CRC calculation lines for one decoded/encoded protocol object."""

    lines: list[str] = []
    for rule in _match_message_rules(transport, protocol, parent_aliases=parent_aliases):
        if not rule.crc_check.enabled:
            continue
        field_name = _resolve_bind_field(protocol, rule.crc_check.bind_element)
        if not field_name:
            continue
        lines.append(
            f'cacheCrcValue({_quoted(rule.message_name)}, {_quoted(rule.crc_check.bind_element or "")}, '
            f'QString::number(static_cast<qlonglong>({value_var}.{field_name})));'
        )
    return lines


def _method_name(conversion: ConversionSpec) -> str:
    """Returns one generated process method name."""

    if conversion.runtime.process_method:
        return conversion.runtime.process_method
    if len(conversion.sources) == 1:
        return f"{conversion.sources[0].protocol}_to_{conversion.target_protocol}dataPro"
    source_part = "_".join(source.protocol for source in conversion.sources)
    return f"{source_part}_to_{conversion.target_protocol}dataPro"


def _group_iteration_label(node: GroupNode, index: int) -> str:
    """Returns one iteration label for a repeated XML group."""

    return f"{node.label}_{index + 1}" if node.repeat_count > 1 else node.label


def _collect_scalar_cpp_fields(
    protocol: ProtocolSpec,
    nodes: list[ProtocolNode],
    path_parts: tuple[str, ...],
) -> list[str]:
    """Collects flattened scalar field names under one XML subtree."""

    fields: list[str] = []
    for node in nodes:
        if isinstance(node, ScalarNode):
            field_name, field = _resolve_scalar_binding(protocol, node, path_parts)
            resolved_name = field.cpp_name if field is not None else field_name
            if resolved_name and resolved_name not in fields:
                fields.append(resolved_name)
            continue
        if isinstance(node, BranchNode):
            nested = _collect_scalar_cpp_fields(protocol, node.children, path_parts + (node.label,))
            for field_name in nested:
                if field_name not in fields:
                    fields.append(field_name)
            continue
        for index in range(node.repeat_count):
            nested = _collect_scalar_cpp_fields(
                protocol,
                node.children,
                path_parts + (_group_iteration_label(node, index),),
            )
            for field_name in nested:
                if field_name not in fields:
                    fields.append(field_name)
    return fields


def _collect_branch_control_rules(protocol: ProtocolSpec) -> list[tuple[str, str]]:
    """Collects optional-branch control updates for one protocol."""

    rules: dict[str, str] = {}

    def collect_descendant_control_fields(nodes: list[ProtocolNode]) -> set[str]:
        names: set[str] = set()
        for item in nodes:
            if isinstance(item, ScalarNode):
                continue
            if item.control_fields:
                control_cpp = protocol.label_to_cpp.get(item.control_fields[0], "")
                if control_cpp:
                    names.add(control_cpp)
            names.update(collect_descendant_control_fields(item.children))
        return names

    def walk(nodes: list[ProtocolNode], path_parts: tuple[str, ...] = ()) -> None:
        for node in nodes:
            if isinstance(node, ScalarNode):
                continue
            if isinstance(node, BranchNode):
                branch_path = path_parts + (node.label,)
                walk(node.children, branch_path)
                control_cpp = protocol.label_to_cpp.get(node.control_fields[0], "") if node.control_fields else ""
                if not control_cpp:
                    continue
                descendant_controls = collect_descendant_control_fields(node.children)
                checks: list[str] = []
                for field_name in _collect_scalar_cpp_fields(protocol, node.children, branch_path):
                    if (
                        field_name == control_cpp
                        or field_name in descendant_controls
                        or _OPTIONAL_CONTROL_CPP_RE.search(field_name)
                    ):
                        continue
                    field = _field_spec_for_cpp_name(protocol, field_name)
                    if field is None:
                        continue
                    checks.append(f"value.{field_name} != {_typed_default_literal(field)}")
                if not checks:
                    continue
                expr = " || ".join(checks)
                if control_cpp in rules:
                    rules[control_cpp] = f"({rules[control_cpp]}) || ({expr})"
                else:
                    rules[control_cpp] = expr
                continue
            walk(node.children, path_parts + (node.label,))

    walk(protocol.nodes)
    return [(control_cpp, expr) for control_cpp, expr in rules.items()]


def _collect_top_repeated_group_families(protocol: ProtocolSpec) -> list[dict[str, object]]:
    """Collects top-level repeated-group field families for one protocol."""

    families: list[dict[str, object]] = []

    def walk(
        nodes: list[ProtocolNode],
        path_parts: tuple[str, ...] = (),
        repeated_ancestor: bool = False,
    ) -> None:
        for node in nodes:
            if isinstance(node, ScalarNode):
                continue
            if isinstance(node, BranchNode):
                walk(node.children, path_parts + (node.label,), repeated_ancestor)
                continue
            if node.repeat_count > 1 and not repeated_ancestor:
                iterations: list[list[str]] = []
                for index in range(node.repeat_count):
                    iteration_fields = _collect_scalar_cpp_fields(
                        protocol,
                        node.children,
                        path_parts + (_group_iteration_label(node, index),),
                    )
                    iterations.append(iteration_fields)
                control_cpp = protocol.label_to_cpp.get(node.control_fields[0], "") if node.control_fields else ""
                families.append(
                    {
                        "name": "_".join((*path_parts, node.label)) or node.label,
                        "repeat_count": node.repeat_count,
                        "control_cpp": control_cpp,
                        "iterations": iterations,
                    }
                )
                continue
            walk(node.children, path_parts + (node.label,), repeated_ancestor or node.repeat_count > 1)

    walk(protocol.nodes)
    return families


def _uses_batch_loop_fill(conversion: ConversionSpec, protocol_lookup: dict[str, ProtocolSpec]) -> bool:
    """Checks whether one conversion should aggregate fixed messages into a target loop."""

    if len(conversion.sources) != 1:
        return False
    source_protocol = protocol_lookup[conversion.sources[0].protocol]
    target_protocol = protocol_lookup[conversion.target_protocol]
    return (
        not _collect_top_repeated_group_families(source_protocol)
        and bool(_collect_top_repeated_group_families(target_protocol))
    )


def _uses_source_loop_split(conversion: ConversionSpec, protocol_lookup: dict[str, ProtocolSpec]) -> bool:
    """Checks whether one conversion should split one looped source into fixed targets."""

    if len(conversion.sources) != 1:
        return False
    source_protocol = protocol_lookup[conversion.sources[0].protocol]
    target_protocol = protocol_lookup[conversion.target_protocol]
    return (
        bool(_collect_top_repeated_group_families(source_protocol))
        and not _collect_top_repeated_group_families(target_protocol)
    )


def _batch_helper_name(conversion: ConversionSpec) -> str:
    """Returns one generated batch-conversion helper name."""

    return f"convert_{to_snake_name(conversion.name)}_batch"


def _split_helper_name(conversion: ConversionSpec) -> str:
    """Returns one generated split-conversion helper name."""

    return f"convert_{to_snake_name(conversion.name)}_split"


def _render_repeated_group_helpers(protocol: ProtocolSpec) -> str:
    """Renders protocol-scoped helpers for repeated group normalization/copying."""

    families = _collect_top_repeated_group_families(protocol)
    if not families:
        return ""

    protocol_type = protocol.type_name
    suffix = normalize_token(protocol_type)
    clear_name = f"clearRepeatedGroups{suffix}"
    count_name = f"repeatedGroupCount{suffix}"
    capacity_name = f"repeatedGroupCapacity{suffix}"
    load_name = f"loadRepeatedGroupIteration{suffix}"
    copy_name = f"copyCanonicalRepeatedGroupToIndex{suffix}"
    set_count_name = f"setRepeatedGroupCount{suffix}"

    repeated_fields: list[str] = []
    control_fields: list[tuple[str, int]] = []
    multi_family_slots = len(families) > 1
    capacity_limit = len(families) if multi_family_slots else int(families[0]["repeat_count"])
    count_lines = ["static int " + count_name + f"(const {protocol_type}& value)", "{", "    int count = 0;"]

    for family in families:
        repeat_count = int(family["repeat_count"])
        control_cpp = str(family["control_cpp"] or "")
        for iteration_fields in family["iterations"]:
            for field_name in iteration_fields:
                if field_name not in repeated_fields:
                    repeated_fields.append(field_name)
        if control_cpp:
            if (control_cpp, repeat_count) not in control_fields:
                control_fields.append((control_cpp, repeat_count))
            if multi_family_slots:
                count_lines.append(f"    if (static_cast<int>(value.{control_cpp}) > 0) count += 1;")
            else:
                count_lines.append(
                    f"    count = std::max(count, std::min(std::max(0, static_cast<int>(value.{control_cpp})), {repeat_count}));"
                )
        else:
            if multi_family_slots:
                count_lines.append("    count += 1;")
            else:
                count_lines.append(f"    count = std::max(count, {repeat_count});")
    count_lines.extend(["    return count;", "}"])

    clear_lines = ["static void " + clear_name + f"({protocol_type}& value)", "{"]
    for field_name in repeated_fields:
        field = _field_spec_for_cpp_name(protocol, field_name)
        if field is None:
            continue
        clear_lines.append(f"    value.{field_name} = {_typed_default_literal(field)};")
    for control_cpp, _repeat_count in control_fields:
        clear_lines.append(f"    value.{control_cpp} = 0;")
    clear_lines.append("}")

    load_slots: list[dict[str, object]] = []
    copy_slots: list[dict[str, object]] = []
    if multi_family_slots:
        for family in families:
            control_cpp = str(family["control_cpp"] or "")
            first_iteration = family["iterations"][0]
            load_slots.append(
                {
                    "source_fields": first_iteration,
                    "target_fields": first_iteration,
                    "control_cpp": control_cpp,
                }
            )
            copy_slots.append(
                {
                    "source_fields": first_iteration,
                    "target_fields": first_iteration,
                    "control_cpp": control_cpp,
                }
            )
    else:
        family = families[0]
        control_cpp = str(family["control_cpp"] or "")
        iterations = family["iterations"]
        for iteration_fields in iterations:
            load_slots.append(
                {
                    "source_fields": iteration_fields,
                    "target_fields": iterations[0],
                    "control_cpp": control_cpp,
                }
            )
        for iteration_fields in iterations:
            copy_slots.append(
                {
                    "source_fields": iterations[0],
                    "target_fields": iteration_fields,
                    "control_cpp": control_cpp,
                }
            )

    load_lines = [
        "static void " + load_name + f"(const {protocol_type}& source, int index, {protocol_type}& value)",
        "{",
        f"    {clear_name}(value);",
        "    switch (index) {",
    ]
    for case_index, slot in enumerate(load_slots):
        case_lines = [f"    case {case_index}:"]
        has_copy = False
        source_fields = list(slot["source_fields"])
        target_fields = list(slot["target_fields"])
        for target_field, source_field in zip(target_fields, source_fields):
            case_lines.append(f"        value.{target_field} = source.{source_field};")
            has_copy = True
        control_cpp = str(slot["control_cpp"] or "")
        if control_cpp:
            case_lines.append(f"        value.{control_cpp} = 1;")
        case_lines.append("        break;")
        if has_copy:
            load_lines.extend(case_lines)
    load_lines.extend(["    default:", "        break;", "    }", "}"])

    copy_lines = [
        "static void " + copy_name + f"(const {protocol_type}& source, int index, {protocol_type}& target)",
        "{",
        "    switch (index) {",
    ]
    for case_index, slot in enumerate(copy_slots):
        case_lines = [f"    case {case_index}:"]
        has_copy = False
        source_fields = list(slot["source_fields"])
        target_fields = list(slot["target_fields"])
        for target_field, source_field in zip(target_fields, source_fields):
            case_lines.append(f"        target.{target_field} = source.{source_field};")
            has_copy = True
        control_cpp = str(slot["control_cpp"] or "")
        if control_cpp:
            case_lines.append(f"        target.{control_cpp} = 1;")
        case_lines.append("        break;")
        if has_copy:
            copy_lines.extend(case_lines)
    copy_lines.extend(["    default:", "        break;", "    }", "}"])

    set_count_lines = [
        "static void " + set_count_name + f"(int count, {protocol_type}& value)",
        "{",
        "    if (count < 0) count = 0;",
    ]
    if multi_family_slots:
        for index, (control_cpp, _repeat_count) in enumerate(control_fields):
            set_count_lines.append(f"    value.{control_cpp} = (count > {index}) ? 1 : 0;")
    else:
        for control_cpp, repeat_count in control_fields:
            set_count_lines.append(f"    value.{control_cpp} = std::min(count, {repeat_count});")
    set_count_lines.append("}")

    capacity_lines = [
        "static int " + capacity_name + "()",
        "{",
        f"    return {capacity_limit};",
        "}",
    ]

    return "\n".join(
        [
            "\n".join(count_lines),
            "",
            "\n".join(capacity_lines),
            "",
            "\n".join(clear_lines),
            "",
            "\n".join(load_lines),
            "",
            "\n".join(copy_lines),
            "",
            "\n".join(set_count_lines),
        ]
    )


def _render_batch_conversion_helper(
    conversion: ConversionSpec,
    protocol_lookup: dict[str, ProtocolSpec],
) -> str:
    """Renders one fixed-to-loop batch helper for a conversion."""

    if not _uses_batch_loop_fill(conversion, protocol_lookup):
        return ""
    source_protocol = protocol_lookup[conversion.sources[0].protocol]
    target_protocol = protocol_lookup[conversion.target_protocol]
    target_type = target_protocol.type_name
    source_type = source_protocol.type_name
    target_suffix = normalize_token(target_type)
    helper_name = _batch_helper_name(conversion)
    base_convert = f"convert_{to_snake_name(conversion.name)}"
    return "\n".join(
        [
            f"static {target_type} {helper_name}(const QVector<{source_type}>& batch)",
            "{",
            f"    {target_type} result = {{0}};",
            "    if (batch.isEmpty()) return result;",
            "    const int capacity = repeatedGroupCapacity" + target_suffix + "();",
            "    const int usedCount = std::min(static_cast<int>(batch.size()), capacity);",
            f"    result = {base_convert}(batch.first());",
            f"    clearRepeatedGroups{target_suffix}(result);",
            "    for (int index = 0; index < usedCount; ++index) {",
            f"        {target_type} itemTarget = {base_convert}(batch[index]);",
            f"        copyCanonicalRepeatedGroupToIndex{target_suffix}(itemTarget, index, result);",
            "    }",
            f"    setRepeatedGroupCount{target_suffix}(usedCount, result);",
            "    return result;",
            "}",
        ]
    )


def _render_split_conversion_helper(
    conversion: ConversionSpec,
    protocol_lookup: dict[str, ProtocolSpec],
) -> str:
    """Renders one loop-to-fixed split helper for a conversion."""

    if not _uses_source_loop_split(conversion, protocol_lookup):
        return ""
    source_protocol = protocol_lookup[conversion.sources[0].protocol]
    target_protocol = protocol_lookup[conversion.target_protocol]
    source_type = source_protocol.type_name
    target_type = target_protocol.type_name
    source_suffix = normalize_token(source_type)
    helper_name = _split_helper_name(conversion)
    base_convert = f"convert_{to_snake_name(conversion.name)}"
    return "\n".join(
        [
            f"static QVector<{target_type}> {helper_name}(const {source_type}& source)",
            "{",
            f"    QVector<{target_type}> outputs;",
            f"    const int itemCount = repeatedGroupCount{source_suffix}(source);",
            "    for (int index = 0; index < itemCount; ++index) {",
            f"        {source_type} item = source;",
            f"        loadRepeatedGroupIteration{source_suffix}(source, index, item);",
            f"        outputs.append({base_convert}(item));",
            "    }",
            "    return outputs;",
            "}",
        ]
    )


def _render_process_function(
    conversion: ConversionSpec,
    protocol_lookup: dict[str, ProtocolSpec],
    source_cache_keys: dict[str, str],
    source_protocol_names: dict[str, str],
    target_protocol_names: dict[str, str],
    joint: bool,
    transport: TransportSpec | None,
    child_to_parents: dict[str, list[str]] | None = None,
) -> str:
    """Renders one conversion process method."""

    method_name = _method_name(conversion)
    batch_loop_fill = _uses_batch_loop_fill(conversion, protocol_lookup)
    source_loop_split = _uses_source_loop_split(conversion, protocol_lookup)
    lines = [f"void messageConvert::{method_name}()", "{"]
    if joint:
        lines.extend(_indent(1, ["QStringList msgNameList;", "QVector<int> msgTimeList;"]))
    for source in conversion.sources:
        runtime_source = _fetch_runtime_source(conversion, source.alias)
        protocol = protocol_lookup[source.protocol]
        parent_aliases = (child_to_parents or {}).get(protocol.type_name, [])
        message_name = (
            runtime_source.message_name
            if runtime_source and runtime_source.message_name
            else source_cache_keys.get(source.protocol, source.protocol)
        )
        display_name = (
            runtime_source.display_name
            if runtime_source and runtime_source.display_name
            else source_protocol_names.get(source.protocol, source.protocol)
        )
        fetches = runtime_source.fetches if runtime_source else []
        counts = ", ".join(str(item.count) for item in fetches)
        cycles = ", ".join(str(item.cycle_ms) for item in fetches)
        count_size = len(fetches)
        base = source.alias
        if batch_loop_fill:
            target_protocol = protocol_lookup[conversion.target_protocol]
            target_suffix = normalize_token(target_protocol.type_name)
            lines.extend(
                _indent(
                    1,
                    [
                        f"QVector<QByteArray> {base}BatchData;",
                        f"QVector<QString> {base}BatchIps;",
                        f"QVector<int> {base}BatchPorts;",
                        f"QVector<int> {base}BatchTimes;",
                        f"QVector<{protocol.type_name}> {base}Batch;",
                        f"QString {base}Ip;",
                        f"int {base}Port = 0;",
                        f"int {base}Time = 0;",
                        f"int count_{base}[{count_size}] = {{ {counts} }};",
                        f"int cycle_{base}[{count_size}] = {{ {cycles} }};",
                        f"int num_{base} = {count_size};",
                        f"while (num_{base}-- > 0) {{",
                        f"    getDataBatch({_quoted(protocol.type_name)}, {_quoted(message_name)}, {_quoted(method_name)}, cycle_{base}[num_{base}], count_{base}[num_{base}], repeatedGroupCapacity{target_suffix}(), {base}BatchData, {base}BatchIps, {base}BatchPorts, {base}BatchTimes);",
                        f"    if ({base}BatchData.isEmpty() == false) {{",
                        f"        for (int batchIndex = 0; batchIndex < {base}BatchData.size(); ++batchIndex) {{",
                        f"            QByteArray itemData = {base}BatchData[batchIndex];",
                        f"            {protocol.type_name} item = {{0}};",
                        *_render_runtime_crc_validate_lines(protocol, "itemData", transport, parent_aliases=parent_aliases),
                        f"            QString ret = decodeMsg((uchar*)itemData.data(), itemData.size(), item);",
                        "            if (ret.isEmpty() == false) {",
                        f"                if (!shouldApplyRuleCondition({_quoted(protocol.type_name)}, {_quoted(message_name)}, itemData)) continue;",
                        "                QByteArray sdata;",
                        "                int iret = checkObjMaps(ret, sdata, item);",
                        "                if (iret == 0) {",
                        f"                    {base}Batch.append(item);",
                        "                }",
                    ],
                )
            )
            if conversion.runtime.response_enabled:
                lines.extend(
                    _indent(
                        4,
                        [
                            f"if (iret != -1 && batchIndex < {base}BatchPorts.size() && {base}BatchPorts[batchIndex] > 0) {{",
                            "    QUdpSocket soc;",
                            f"    qint64 written = soc.writeDatagram(sdata, QHostAddress({base}BatchIps[batchIndex]), {base}BatchPorts[batchIndex]);",
                            f"    logUdpSendResult(QStringLiteral(\"[UDP FEEDBACK]\"), QStringLiteral(\"{protocol.type_name}\"), QStringLiteral(\"{message_name}\"), QString(), {base}BatchIps[batchIndex], {base}BatchPorts[batchIndex], sdata.size(), written, soc.errorString());",
                            "}",
                        ],
                    )
                )
            lines.extend(
                _indent(
                    4,
                    [
                        "            }",
                        "        }",
                        f"        if ({base}Batch.isEmpty() == false) {{",
                        f"            {base}Ip = {base}BatchIps.isEmpty() ? QString() : {base}BatchIps.first();",
                        f"            {base}Port = {base}BatchPorts.isEmpty() ? 0 : {base}BatchPorts.first();",
                        f"            {base}Time = {base}BatchTimes.isEmpty() ? 0 : {base}BatchTimes.first();",
                        "            break;",
                        "        }",
                        "    }",
                        "}",
                        f"if ({base}Batch.isEmpty()) return;",
                    ],
                )
            )
        else:
            lines.extend(
                _indent(
                    1,
                    [
                        f"QByteArray {base}Data;",
                        f"{protocol.type_name} {base} = {{0}};",
                        f"int {base}Flag = 0;",
                        f"QString {base}Ip;",
                        f"int {base}Port = 0;",
                        f"int {base}Time = 0;",
                        f"int count_{base}[{count_size}] = {{ {counts} }};",
                        f"int cycle_{base}[{count_size}] = {{ {cycles} }};",
                        f"int num_{base} = {count_size};",
                        f"while (num_{base}-- > 0) {{",
                        f"    getData({_quoted(protocol.type_name)}, {_quoted(message_name)}, {_quoted(method_name)}, cycle_{base}[num_{base}], count_{base}[num_{base}], {base}Data, {base}Ip, {base}Port, {base}Time);",
                        f"    if ({base}Data.isEmpty() == false) {{",
                        *_render_runtime_crc_validate_lines(protocol, f"{base}Data", transport, parent_aliases=parent_aliases),
                        f"        QString ret = decodeMsg((uchar*){base}Data.data(), {base}Data.size(), {base});",
                        "        if (ret.isEmpty() == false) {",
                        f"            if (!shouldApplyRuleCondition({_quoted(protocol.type_name)}, {_quoted(message_name)}, {base}Data)) break;",
                        "            QByteArray sdata;",
                        f"            int iret = checkObjMaps(ret, sdata, {base});",
                        f"            if (iret == 0) {base}Flag = 1;",
                    ],
                )
            )
            if conversion.runtime.response_enabled:
                lines.extend(
                    _indent(
                        3,
                        [
                            f"if (iret != -1 && {base}Port > 0) {{",
                            "    QUdpSocket soc;",
                            f"    qint64 written = soc.writeDatagram(sdata, QHostAddress({base}Ip), {base}Port);",
                            f"    logUdpSendResult(QStringLiteral(\"[UDP FEEDBACK]\"), QStringLiteral(\"{protocol.type_name}\"), QStringLiteral(\"{message_name}\"), QString(), {base}Ip, {base}Port, sdata.size(), written, soc.errorString());",
                            "}",
                        ],
                    )
                )
            lines.extend(
                _indent(
                    3,
                    [
                        "}",
                        "break;",
                        "    }",
                        "}",
                        f"if (1 != {base}Flag) return;",
                    ],
                )
            )
        if joint:
            lines.extend(_indent(1, [f"msgNameList.append({_quoted(display_name)});", f"msgTimeList.append({base}Time);"]))
    if joint:
        lines.extend(
            _indent(
                1,
                [
                    "if (msgNameList.size() >= 2) {",
                    "    int state = 0;",
                    "    for (int i = 0; i < msgNameList.size() - 1; ++i) {",
                    "        for (int j = i + 1; j < msgNameList.size(); ++j) {",
                    "            int s = code_test::getSrcTime_41(msgNameList[i], msgNameList[j]);",
                    "            if (-1 == s || (s + (msgTimeList[i] - msgTimeList[j])) > 0) state += 1;",
                    "        }",
                    "    }",
                    "    if (msgNameList.size() != state + 1) return;",
                    "}",
                ],
            )
        )
    args = ", ".join(source.alias for source in conversion.sources)
    target_protocol = protocol_lookup[conversion.target_protocol]
    target_parent_aliases = (child_to_parents or {}).get(target_protocol.type_name, [])
    target_var_name = _mapping_target_var_name(target_protocol.type_name)
    target_name = target_protocol_names.get(conversion.target_protocol, conversion.target_protocol)
    cache_name = conversion.runtime.cache_name or target_protocol.type_name
    send_mode = conversion.runtime.send_mode or "direct"
    if batch_loop_fill:
        lines.extend(
            _indent(
                1,
                [
                    f"{target_protocol.type_name} {target_var_name} = {_batch_helper_name(conversion)}({conversion.sources[0].alias}Batch);",
                    "QByteArray sendData;",
                    f"encodeMsg(sendData, {target_var_name});",
                    *_render_runtime_crc_apply_lines(target_protocol, "sendData", transport, parent_aliases=target_parent_aliases),
                    f"routeGeneratedTarget({_quoted(target_protocol.type_name)}, {_quoted(target_name)}, {_quoted(cache_name)}, {conversion.runtime.cache_num}, {'true' if joint else 'false'}, {'true' if send_mode == 'cache' else 'false'}, sendData);",
                ],
            )
        )
    elif source_loop_split:
        lines.extend(
            _indent(
                1,
                [
                    f"QVector<{target_protocol.type_name}> generatedTargets = {_split_helper_name(conversion)}({args});",
                    "if (generatedTargets.isEmpty()) return;",
                    "for (int targetIndex = 0; targetIndex < generatedTargets.size(); ++targetIndex) {",
                    "    QByteArray sendData;",
                    "    auto generatedTarget = generatedTargets[targetIndex];",
                    "    encodeMsg(sendData, generatedTarget);",
                    *_indent(1, _render_runtime_crc_apply_lines(target_protocol, "sendData", transport, parent_aliases=target_parent_aliases)),
                    f"    routeGeneratedTarget({_quoted(target_protocol.type_name)}, {_quoted(target_name)}, {_quoted(cache_name)}, {conversion.runtime.cache_num}, {'true' if joint else 'false'}, {'true' if send_mode == 'cache' else 'false'}, sendData);",
                    "}",
                ],
            )
        )
    else:
        lines.extend(
            _indent(
                1,
                [
                    f"{target_protocol.type_name} {target_var_name} = convert_{to_snake_name(conversion.name)}({args});",
                    "QByteArray sendData;",
                    f"encodeMsg(sendData, {target_var_name});",
                    *_render_runtime_crc_apply_lines(target_protocol, "sendData", transport, parent_aliases=target_parent_aliases),
                    f"routeGeneratedTarget({_quoted(target_protocol.type_name)}, {_quoted(target_name)}, {_quoted(cache_name)}, {conversion.runtime.cache_num}, {'true' if joint else 'false'}, {'true' if send_mode == 'cache' else 'false'}, sendData);",
                ],
            )
        )
    lines.append("}")
    return "\n".join(lines)


def render_messageconvert_cpp(
    conversions: list[ConversionSpec],
    protocol_lookup: dict[str, ProtocolSpec],
    source_cache_keys: dict[str, str],
    source_protocol_names: dict[str, str],
    target_protocol_names: dict[str, str],
    joint: bool,
    loop_sleep_ms: int,
    check_data_interval_ms: int,
    transport: TransportSpec | None,
) -> str:
    """Renders messageconvert.cpp."""

    used_protocol_names: set[str] = set()
    inbound_protocol_names: set[str] = set()
    repeated_helper_protocols: dict[str, ProtocolSpec] = {}
    for conversion in conversions:
        for source in conversion.sources:
            protocol = protocol_lookup[source.protocol]
            used_protocol_names.add(protocol.type_name)
            inbound_protocol_names.add(protocol.type_name)
            if _collect_top_repeated_group_families(protocol):
                repeated_helper_protocols[protocol.type_name] = protocol
        target_protocol = protocol_lookup[conversion.target_protocol]
        used_protocol_names.add(target_protocol.type_name)
        if _collect_top_repeated_group_families(target_protocol):
            repeated_helper_protocols[target_protocol.type_name] = target_protocol
    _parent_to_children, child_to_parents = _build_route_alias_maps(protocol_lookup, used_protocol_names)
    route_runtime_helpers = _render_route_runtime_helpers(
        protocol_lookup,
        used_protocol_names,
        inbound_protocol_names=inbound_protocol_names,
    )
    repeated_helper_blocks = [
        _render_repeated_group_helpers(protocol)
        for protocol in repeated_helper_protocols.values()
    ]
    conversion_helper_blocks = [
        block
        for conversion in conversions
        for block in (
            _render_batch_conversion_helper(conversion, protocol_lookup),
            _render_split_conversion_helper(conversion, protocol_lookup),
        )
        if block
    ]
    helper_block = "\n\n".join(
        block for block in [*repeated_helper_blocks, *conversion_helper_blocks] if block
    )
    process_methods = [_method_name(conversion) for conversion in conversions]
    process_blocks = [
        _render_process_function(
            conversion,
            protocol_lookup,
            source_cache_keys,
            source_protocol_names,
            target_protocol_names,
            joint,
            transport,
            child_to_parents=child_to_parents,
        )
        for conversion in conversions
    ]
    timer_block = ""
    if joint:
        checks = [
            f"    checkData({_quoted(name)}, static_cast<int>(code_test::getDstMsg_41({_quoted(target_protocol_names[name])})));"
            for name in target_protocol_names
        ]
        timer_block = "\n".join(
            [
                "void messageConvert::onCheckDataTimer()",
                "{",
                *checks,
                "}",
                "",
                "void messageConvert::checkData(QString name, int time)",
                "{",
                "    QMutexLocker lock(&dataMutex);",
                "    for (int i = 0; i < dataInfo.size(); ++i) {",
                "        int ll = static_cast<int>(QDateTime::currentMSecsSinceEpoch() - dataInfo[i]->time.last());",
                "        if (ll > time && normalizeRuntimeMessageName(name) == normalizeRuntimeMessageName(dataInfo[i]->name)) {",
                "            dataInfo.remove(i);",
                "            return;",
                "        }",
                "    }",
                "}",
                "",
            ]
        )
    joint_include = '#include "to_code_Choreography.h"\n' if joint else ""
    joint_start = ""
    joint_stop = ""
    if joint:
        joint_start = (
            "    connect(&checkDataTimer, &QTimer::timeout, this, &messageConvert::onCheckDataTimer);\n"
            f"    checkDataTimer.start({check_data_interval_ms});\n"
        )
        joint_stop = "    checkDataTimer.stop();\n"
        push_data_reset_line = "                dataInfo[i]->state.clear();"
        push_data_duplicate_lines = "\n".join(
            [
                "                dataInfo[i]->num++;",
                "                dataInfo[i]->time.append(data->time.last());",
                "                dataInfo[i]->state.clear();",
            ]
        )
        get_data_condition = 'if (name == item->name && (num == item->num) && item->state.indexOf(name) == -1) {'
        get_data_mark = "            item->state.append(name);"
    else:
        push_data_reset_line = "                dataInfo[i]->state = 0;"
        push_data_duplicate_lines = "\n".join(
            [
                "                dataInfo[i]->num++;",
                "                dataInfo[i]->state = 0;",
            ]
        )
        get_data_condition = "if (name == item->name && (num <= item->num) && item->state == 0) {"
        get_data_mark = "            item->state = 1;"
    joint_flush_dispatch = ""
    joint_direct_dispatch = ""
    if joint:
        joint_flush_dispatch = """    if (jointControlled) {
        code_test check;
        int sflag = check.getStatus_41(targetName);
        if (0 == sflag) onSendMessage(selected->data);
        else cacheGeneratedTarget(cacheName, cacheNum, selected->data);
        return;
    }
"""
        joint_direct_dispatch = """    if (jointControlled) {
        code_test check;
        int sflag = check.getStatus_41(targetName);
        if (0 == sflag) onSendMessage(data);
        else cacheGeneratedTarget(cacheName, cacheNum, data);
        return;
    }
"""
    batch_aggregation_entries: list[tuple[str, str]] = []
    for conversion in conversions:
        if not _uses_batch_loop_fill(conversion, protocol_lookup):
            continue
        source = conversion.sources[0]
        runtime_source = _fetch_runtime_source(conversion, source.alias)
        message_name = (
            runtime_source.message_name
            if runtime_source and runtime_source.message_name
            else source_cache_keys.get(source.protocol, source.protocol)
        )
        batch_aggregation_entries.append((source.protocol, message_name))
    batch_aggregation_lines = []
    for protocol_name, message_name in batch_aggregation_entries:
        batch_aggregation_lines.append(
            "    if (normalizedProtocol == normalizeRuntimeMessageName("
            + _quoted(protocol_name)
            + ") && normalizedMessage == normalizeRuntimeMessageName("
            + _quoted(message_name)
            + ")) return true;"
        )
    if not batch_aggregation_lines:
        batch_aggregation_lines.append("    Q_UNUSED(normalizedProtocol);")
        batch_aggregation_lines.append("    Q_UNUSED(normalizedMessage);")
    batch_aggregation_helper = "\n".join(
        [
            "bool messageConvert::isLoopAggregationSource(const QString& protocolName, const QString& messageName) const",
            "{",
            "    const QString normalizedProtocol = normalizeRuntimeMessageName(",
            "        resolveCanonicalRuntimeMessageName(protocolName, protocolName)",
            "    );",
            "    const QString normalizedMessage = normalizeRuntimeMessageName(",
            "        resolveCanonicalRuntimeMessageName(protocolName, messageName)",
            "    );",
            *batch_aggregation_lines,
            "    return false;",
            "}",
            "",
        ]
    )
    process_calls = "\n".join(f"        {method}();" for method in process_methods)
    return f"""{_msvc_utf8_preamble()}
#include "messageconvert.h"
#include "codec.h"
#include <algorithm>
#include <QDateTime>
#include <QDebug>
#include <QMutexLocker>
#include <QThread>
#include <QtConcurrent>
{joint_include}
{helper_block}

static void logUdpSendResult(
    const QString& tag,
    const QString& protocolName,
    const QString& targetName,
    const QString& endpointName,
    const QString& ip,
    int port,
    int size,
    qint64 written,
    const QString& errorText)
{{
    if (written < 0) {{
        qDebug() << tag
                 << "protocol" << protocolName
                 << "target" << targetName
                 << "endpoint" << endpointName
                 << "to" << ip << port
                 << "size" << size
                 << "written" << written
                 << "error" << errorText;
        return;
    }}
    qDebug() << tag
             << "protocol" << protocolName
             << "target" << targetName
             << "endpoint" << endpointName
             << "to" << ip << port
             << "size" << size
             << "written" << written;
}}

messageConvert::messageConvert(QObject* parent)
    : QObject(parent)
{{
}}

int messageConvert::start(QVector<std::shared_ptr<NetInfo>> netlist, QVector<std::shared_ptr<MessageRuleInfo>> ruleList, int maxThread)
{{
    _maxThread = maxThread;
    messageRuleList = ruleList;
    for (auto serv : netlist) {{
        if (serv->bRecvTag == false) {{
            udpSendList.push_back(serv);
        }} else {{
            std::shared_ptr<QUdpSocket> soc(new QUdpSocket);
            connect(soc.get(), &QUdpSocket::readyRead, [serv, soc, this]() {{
                while (soc->hasPendingDatagrams()) {{
                    QHostAddress sender;
                    quint16 senderPort = 0;
                    qint64 size = soc->pendingDatagramSize();
                    QByteArray buffer(size, 0);
                    soc->readDatagram(buffer.data(), size, &sender, &senderPort);
                    readPendingDatagrams(serv->name, sender, senderPort, buffer);
                }}
            }});
            if (!soc->bind(QHostAddress::Any, serv->port)) return -1;
            udpRecvList.push_back(soc);
        }}
    }}
    QtConcurrent::run([this]() {{ this->msgConvertThread(); }});
{joint_start}    return 0;
}}

QString messageConvert::computeCrc16Hex(const QString& raw) const
{{
    QByteArray bytes = raw.toUtf8();
    quint16 crc = 0xFFFF;
    for (unsigned char byte : bytes) {{
        crc ^= static_cast<quint16>(byte);
        for (int i = 0; i < 8; ++i) {{
            if (crc & 0x0001) crc = static_cast<quint16>((crc >> 1) ^ 0xA001);
            else crc = static_cast<quint16>(crc >> 1);
        }}
    }}
    return QStringLiteral("%1").arg(crc, 4, 16, QChar('0')).toUpper();
}}

void messageConvert::cacheCrcValue(const QString& messageName, const QString& bindElement, const QString& rawValue)
{{
    const QString crc = computeCrc16Hex(rawValue);
    const QString key = messageName + QStringLiteral(":") + bindElement;
    crcValueMap.insert(key, crc);
    qDebug() << "CRC_VALUE" << key << crc;
}}

int messageConvert::stop()
{{
    _threadExit = 1;
{joint_stop}    for (auto var : udpRecvList) {{
        if (var->isOpen()) var->close();
    }}
    udpRecvList.clear();
    return 0;
}}

void messageConvert::onSendMessage(QByteArray msg)
{{
    QUdpSocket sender;
    for (auto var : udpSendList) {{
        if (!var) continue;
        qint64 written = sender.writeDatagram(msg, QHostAddress(var->ip), var->port);
        logUdpSendResult(QStringLiteral("[UDP SEND OK]"), QString(), QString(), var->name, var->ip, var->port, msg.size(), written, sender.errorString());
    }}
}}

void messageConvert::onSendMessage(const QString& protocolName, const QString& targetName, QByteArray msg)
{{
    const QString normalizedProtocol = normalizeRuntimeMessageName(
        resolveCanonicalRuntimeMessageName(protocolName, protocolName)
    );
    const QString normalizedTarget = normalizeRuntimeMessageName(
        resolveCanonicalRuntimeMessageName(protocolName, targetName)
    );
    QUdpSocket sender;
    bool matched = false;
    for (auto var : udpSendList) {{
        if (!var) continue;
        const QString normalizedEndpoint = normalizeRuntimeMessageName(var->name);
        const bool protocolMatch = !normalizedProtocol.isEmpty() && (
            normalizedEndpoint == normalizedProtocol
            || normalizedEndpoint.contains(normalizedProtocol)
            || normalizedProtocol.contains(normalizedEndpoint)
        );
        const bool targetMatch = !normalizedTarget.isEmpty() && (
            normalizedEndpoint == normalizedTarget
            || normalizedEndpoint.contains(normalizedTarget)
            || normalizedTarget.contains(normalizedEndpoint)
        );
        if (!protocolMatch && !targetMatch) continue;
        qint64 written = sender.writeDatagram(msg, QHostAddress(var->ip), var->port);
        logUdpSendResult(QStringLiteral("[UDP SEND MATCHED]"), protocolName, targetName, var->name, var->ip, var->port, msg.size(), written, sender.errorString());
        matched = true;
    }}
    if (matched) return;
    for (auto var : udpSendList) {{
        if (!var) continue;
        qint64 written = sender.writeDatagram(msg, QHostAddress(var->ip), var->port);
        logUdpSendResult(QStringLiteral("[UDP SEND FALLBACK]"), protocolName, targetName, var->name, var->ip, var->port, msg.size(), written, sender.errorString());
    }}
}}

void messageConvert::readPendingDatagrams(QString name, QHostAddress ip, quint16 port, QByteArray data)
{{
    const QString resolvedProtocol = resolveInboundProtocolName(name, data);
    const QString resolvedMessage = normalizeRuntimeMessageName(resolvedProtocol) == normalizeRuntimeMessageName(name)
        ? resolveCanonicalRuntimeMessageName(resolvedProtocol, name)
        : resolvedProtocol;
    std::shared_ptr<msgDataInfo> d(new msgDataInfo);
    d->time.append(QDateTime::currentMSecsSinceEpoch());
    d->name = resolvedMessage;
    d->protocolName = resolvedProtocol;
    d->num = 1;
    d->data = data;
    d->ip = ip.toString();
    d->port = port;
    pushData(d);
}}

void messageConvert::pushData(std::shared_ptr<msgDataInfo> data)
{{
    QMutexLocker lock(&dataMutex);
    const auto rule = findMessageRule(data->protocolName, data->name);
    QString aggregationMode = isLoopAggregationSource(data->protocolName, data->name)
        ? (rule ? rule->aggregation.mode.trimmed().toUpper() : QStringLiteral("SINGLE"))
        : QStringLiteral("SINGLE");
    if (aggregationMode == QStringLiteral("COUNT")) aggregationMode = QStringLiteral("BY_COUNT");
    if (aggregationMode == QStringLiteral("TIME")) aggregationMode = QStringLiteral("BY_TIME");
    if (aggregationMode == QStringLiteral("BY_COUNT") || aggregationMode == QStringLiteral("BY_TIME")) {{
        dataInfo.push_back(data);
        trimQueue(dataInfo, data->name, 256);
        return;
    }}
    const QString normalizedName = normalizeRuntimeMessageName(
        resolveCanonicalRuntimeMessageName(data->protocolName, data->name)
    );
    for (int index = 0; index < dataInfo.size(); ++index) {{
        if (normalizeRuntimeMessageName(
                resolveCanonicalRuntimeMessageName(dataInfo[index]->protocolName, dataInfo[index]->name)
            ) != normalizedName) continue;
        if (data->data != dataInfo[index]->data) {{
            dataInfo[index] = data;
            dataInfo[index]->state.clear();
        }} else {{
            dataInfo[index]->num++;
            if (!data->time.isEmpty()) dataInfo[index]->time.append(data->time.last());
            dataInfo[index]->state.clear();
        }}
        return;
    }}
    dataInfo.push_back(data);
    trimQueue(dataInfo, data->name, 256);
}}

QString messageConvert::normalizeRuntimeMessageName(const QString& value) const
{{
    QString normalized = value.trimmed().toLower();
    normalized.replace('.', '_');
    normalized.replace('-', '_');
    normalized.replace(' ', '_');
    const QStringList suffixes = {{
        QStringLiteral("_recv"),
        QStringLiteral("_send"),
        QStringLiteral("_cache"),
        QStringLiteral("_input"),
        QStringLiteral("_output")
    }};
    bool changed = true;
    while (changed) {{
        changed = false;
        for (const QString& suffix : suffixes) {{
            if (normalized.endsWith(suffix)) {{
                normalized.chop(suffix.size());
                changed = true;
                break;
            }}
        }}
    }}
    QString compact;
    compact.reserve(normalized.size());
    for (const QChar& ch : normalized) {{
        if (ch.isLetterOrNumber()) compact.append(ch);
    }}
    return compact;
}}

{route_runtime_helpers}
{batch_aggregation_helper}

std::shared_ptr<messageConvert::MessageRuleInfo> messageConvert::findMessageRule(const QString& protocolName, const QString& messageName) const
{{
    const QString normalizedProtocol = normalizeRuntimeMessageName(
        resolveCanonicalRuntimeMessageName(protocolName, protocolName)
    );
    const QString normalizedMessage = normalizeRuntimeMessageName(
        resolveCanonicalRuntimeMessageName(protocolName, messageName)
    );
    for (const auto& rule : messageRuleList) {{
        if (!rule) continue;
        const QString normalizedRuleName = normalizeRuntimeMessageName(
            resolveCanonicalRuntimeMessageName(protocolName, rule->messageName)
        );
        if (normalizedRuleName == normalizedMessage || normalizedRuleName == normalizedProtocol) {{
            return rule;
        }}
    }}
    return nullptr;
}}

QString messageConvert::resolveAggregationBindValue(const QString& protocolName, const QString& bindElement, const QByteArray& data) const
{{
    QString value = extractRuntimeFieldValue(protocolName, bindElement, data);
    if (!value.isEmpty()) return value;
    return QString::fromLatin1(data.toHex());
}}

bool messageConvert::compareRuleConditionValue(const QString& operatorName, const QString& actualValue, const QString& expectedValue) const
{{
    bool actualOk = false;
    bool expectedOk = false;
    const double actualNumber = actualValue.toDouble(&actualOk);
    const double expectedNumber = expectedValue.toDouble(&expectedOk);
    if (!actualOk || !expectedOk) return false;
    if (operatorName == QStringLiteral("GT")) return actualNumber > expectedNumber;
    if (operatorName == QStringLiteral("LT")) return actualNumber < expectedNumber;
    if (operatorName == QStringLiteral("EQ")) return actualNumber == expectedNumber;
    if (operatorName == QStringLiteral("GTE")) return actualNumber >= expectedNumber;
    if (operatorName == QStringLiteral("LTE")) return actualNumber <= expectedNumber;
    if (operatorName == QStringLiteral("NEQ")) return actualNumber != expectedNumber;
    return false;
}}

bool messageConvert::shouldApplyRuleCondition(const QString& protocolName, const QString& messageName, const QByteArray& data) const
{{
    const auto rule = findMessageRule(protocolName, messageName);
    if (!rule) return true;

    const QString operatorName = rule->aggregation.compareOperator.trimmed().toUpper();
    const QString expectedValue = rule->aggregation.compareValue.trimmed();
    if (operatorName.isEmpty() || expectedValue.isEmpty()) return true;

    const QString bindElement = rule->aggregationType.bindElement.trimmed();
    if (bindElement.isEmpty()) return false;

    const QString actualValue = extractRuntimeFieldValue(protocolName, bindElement, data).trimmed();
    if (actualValue.isEmpty()) return false;

    return compareRuleConditionValue(operatorName, actualValue, expectedValue);
}}

QVector<int> messageConvert::collectReadyBatchIndexes(
    const QVector<std::shared_ptr<msgDataInfo>>& queue,
    const QString& protocolName,
    const QString& messageName,
    int time,
    int num,
    bool forceTimeWindow) const
{{
    QVector<int> matchingIndexes;
    const QString normalizedName = normalizeRuntimeMessageName(
        resolveCanonicalRuntimeMessageName(protocolName, messageName)
    );
    for (int index = 0; index < queue.size(); ++index) {{
        const auto& item = queue[index];
        if (!item) continue;
        if (normalizeRuntimeMessageName(
                resolveCanonicalRuntimeMessageName(item->protocolName, item->name)
            ) == normalizedName) matchingIndexes.push_back(index);
    }}
    if (matchingIndexes.isEmpty()) return {{}};

    const auto rule = findMessageRule(protocolName, messageName);
    QString aggregationMode = rule ? rule->aggregation.mode.trimmed().toUpper() : QStringLiteral("SINGLE");
    if (aggregationMode == QStringLiteral("COUNT")) aggregationMode = QStringLiteral("BY_COUNT");
    if (aggregationMode == QStringLiteral("TIME")) aggregationMode = QStringLiteral("BY_TIME");

    int requiredCount = num > 0 ? num : 1;
    if (rule && rule->aggregation.count > requiredCount) requiredCount = rule->aggregation.count;

    if (aggregationMode == QStringLiteral("BY_COUNT")) {{
        if (matchingIndexes.size() < requiredCount) return {{}};
        return matchingIndexes.mid(0, requiredCount);
    }}

    if (aggregationMode == QStringLiteral("BY_TIME")) {{
        int windowMs = 0;
        if (rule && rule->aggregation.timeMs > 0) windowMs = rule->aggregation.timeMs;
        else if (time > 0) windowMs = time;
        if (windowMs <= 0) windowMs = 1;

        const qint64 firstTime = queue[matchingIndexes.first()]->time.isEmpty() ? 0 : queue[matchingIndexes.first()]->time.last();
        const qint64 now = QDateTime::currentMSecsSinceEpoch();
        if (!forceTimeWindow && (now - firstTime) < windowMs) return {{}};

        QVector<int> windowIndexes;
        for (int index : matchingIndexes) {{
            const qint64 itemTime = queue[index]->time.isEmpty() ? firstTime : queue[index]->time.last();
            if ((itemTime - firstTime) <= windowMs) windowIndexes.push_back(index);
            else break;
        }}
        if (windowIndexes.size() < requiredCount) return {{}};
        return windowIndexes;
    }}

    if (matchingIndexes.size() < requiredCount) return {{}};
    return matchingIndexes.mid(0, requiredCount);
}}

QVector<int> messageConvert::normalizeAggregatedBatchIndexes(
    const QVector<std::shared_ptr<msgDataInfo>>& queue,
    const QVector<int>& batchIndexes,
    const QString& protocolName,
    const QString& messageName) const
{{
    if (batchIndexes.size() <= 1) return batchIndexes;
    const auto rule = findMessageRule(protocolName, messageName);
    QString aggregationType = rule ? rule->aggregationType.type.trimmed().toUpper() : QStringLiteral("TIME");
    const QString bindElement = rule ? rule->aggregationType.bindElement.trimmed() : QString();
    QVector<int> normalizedIndexes = batchIndexes;

    if (aggregationType == QStringLiteral("ORDER") && !bindElement.isEmpty()) {{
        std::sort(normalizedIndexes.begin(), normalizedIndexes.end(), [this, &queue, &protocolName, &bindElement](int left, int right) {{
            const QString leftValue = resolveAggregationBindValue(protocolName, bindElement, queue[left]->data);
            const QString rightValue = resolveAggregationBindValue(protocolName, bindElement, queue[right]->data);
            bool leftOk = false;
            bool rightOk = false;
            const qlonglong leftNumber = leftValue.toLongLong(&leftOk);
            const qlonglong rightNumber = rightValue.toLongLong(&rightOk);
            if (leftOk && rightOk) return leftNumber < rightNumber;
            return leftValue < rightValue;
        }});
        return normalizedIndexes;
    }}

    if (aggregationType == QStringLiteral("DISTINCT") && !bindElement.isEmpty()) {{
        QStringList seenValues;
        QVector<int> uniqueIndexes;
        for (int index : normalizedIndexes) {{
            const QString value = resolveAggregationBindValue(protocolName, bindElement, queue[index]->data);
            if (seenValues.indexOf(value) != -1) continue;
            seenValues.append(value);
            uniqueIndexes.append(index);
        }}
        return uniqueIndexes;
    }}

    return normalizedIndexes;
}}

void messageConvert::removeQueueIndexes(QVector<std::shared_ptr<msgDataInfo>>& queue, const QVector<int>& indexes)
{{
    QVector<int> ordered = indexes;
    std::sort(ordered.begin(), ordered.end(), std::greater<int>());
    for (int index : ordered) {{
        if (index >= 0 && index < queue.size()) queue.remove(index);
    }}
}}

void messageConvert::trimQueue(QVector<std::shared_ptr<msgDataInfo>>& queue, const QString& messageName, int maxEntries)
{{
    QVector<int> matchingIndexes;
    const QString normalizedName = normalizeRuntimeMessageName(messageName);
    for (int index = 0; index < queue.size(); ++index) {{
        const auto& item = queue[index];
        if (!item) continue;
        if (normalizeRuntimeMessageName(item->name) == normalizedName) matchingIndexes.push_back(index);
    }}
    const int overflow = matchingIndexes.size() - maxEntries;
    if (overflow <= 0) return;
    QVector<int> staleIndexes = matchingIndexes.mid(0, overflow);
    removeQueueIndexes(queue, staleIndexes);
}}

void messageConvert::getDataBatch(QString protocolName, QString name, QString consumerKey, int time, int num, int maxBatchSize, QVector<QByteArray>& batchData, QVector<QString>& batchIps, QVector<int>& batchPorts, QVector<int>& batchTimes)
{{
    batchData.clear();
    batchIps.clear();
    batchPorts.clear();
    batchTimes.clear();
    if (maxBatchSize <= 0) maxBatchSize = 1;

    const auto rule = findMessageRule(protocolName, name);
    QString aggregationMode = rule ? rule->aggregation.mode.trimmed().toUpper() : QStringLiteral("SINGLE");
    if (aggregationMode == QStringLiteral("COUNT")) aggregationMode = QStringLiteral("BY_COUNT");
    if (aggregationMode == QStringLiteral("TIME")) aggregationMode = QStringLiteral("BY_TIME");
    if (aggregationMode != QStringLiteral("BY_COUNT") && aggregationMode != QStringLiteral("BY_TIME")) {{
        QByteArray singleData;
        QString singleIp;
        int singlePort = 0;
        int singleTime = 0;
        getData(protocolName, name, consumerKey, time, num, singleData, singleIp, singlePort, singleTime);
        if (singleData.isEmpty()) return;
        batchData.append(singleData);
        batchIps.append(singleIp);
        batchPorts.append(singlePort);
        batchTimes.append(singleTime);
        return;
    }}

    QMutexLocker lock(&dataMutex);
    QVector<int> batchIndexes = collectReadyBatchIndexes(dataInfo, protocolName, name, time, num, false);
    if (batchIndexes.isEmpty()) return;
    QVector<int> normalizedIndexes = normalizeAggregatedBatchIndexes(dataInfo, batchIndexes, protocolName, name);
    if (normalizedIndexes.isEmpty()) return;
    if (normalizedIndexes.size() > maxBatchSize) normalizedIndexes = normalizedIndexes.mid(0, maxBatchSize);
    for (int index : normalizedIndexes) {{
        const auto& item = dataInfo[index];
        if (!item) continue;
        batchData.append(item->data);
        batchIps.append(item->ip);
        batchPorts.append(item->port);
        batchTimes.append(item->time.isEmpty() ? 0 : static_cast<int>(item->time.first()));
    }}
    removeQueueIndexes(dataInfo, batchIndexes);
}}

void messageConvert::getData(QString protocolName, QString name, QString consumerKey, int time, int num, QByteArray& data, QString& ip, int& port, int& outTime)
{{
    data.clear();
    ip.clear();
    port = 0;
    outTime = 0;
    QMutexLocker lock(&dataMutex);
    const QString normalizedName = normalizeRuntimeMessageName(
        resolveCanonicalRuntimeMessageName(protocolName, name)
    );
    const QString normalizedStateKey = normalizeRuntimeMessageName(
        consumerKey.isEmpty() ? normalizedName : consumerKey
    );
    const int requiredCount = num > 0 ? num : 1;
    for (const auto& item : dataInfo) {{
        if (!item) continue;
        if (normalizeRuntimeMessageName(
                resolveCanonicalRuntimeMessageName(item->protocolName, item->name)
            ) != normalizedName) continue;
        if (item->num != requiredCount) continue;
        if (item->state.indexOf(normalizedStateKey) != -1) continue;
        for (int index = item->time.size() - 1; index >= 1; --index) {{
            if (item->time[index] - item->time[index - 1] <= static_cast<qulonglong>(time)) return;
        }}
        ip = item->ip;
        port = item->port;
        data = item->data;
        item->state.append(normalizedStateKey);
        outTime = item->time.isEmpty() ? 0 : static_cast<int>(item->time.first());
        return;
    }}
}}

void messageConvert::cacheGeneratedTarget(const QString& targetName, int num, const QByteArray& data)
{{
    std::shared_ptr<msgDataInfo> d(new msgDataInfo);
    d->time.append(QDateTime::currentMSecsSinceEpoch());
    d->name = targetName;
    d->protocolName = targetName;
    d->cacheName = targetName;
    d->num = num;
    d->cacheNum = num;
    d->cacheOnly = true;
    d->data = data;
    d->ip = QStringLiteral("127.0.0.1");
    d->port = 0;
    pushData(d);
}}

void messageConvert::routeGeneratedTarget(const QString& protocolName, const QString& targetName, const QString& cacheName, int cacheNum, bool jointControlled, bool cacheOnly, const QByteArray& data)
{{
    Q_UNUSED(protocolName);
    Q_UNUSED(targetName);
    Q_UNUSED(jointControlled);
{joint_direct_dispatch}    if (cacheOnly) cacheGeneratedTarget(cacheName, cacheNum, data);
    else onSendMessage(protocolName, targetName, data);
}}

{timer_block}{chr(10).join(process_blocks)}

void messageConvert::msgConvertThread()
{{
    while (0 == _threadExit) {{
{process_calls}
        QThread::msleep(static_cast<unsigned long>({loop_sleep_ms}));
    }}
}}
"""


def render_generator_readme() -> str:
    """Renders generator usage documentation."""

    return """# Python 协议转换项目生成器

## 用法

```bash
python -m project_generator build --protocol-dir input/protocols --mappings input/mappings.json --output output/demo_project
```

联合转换模式：

```bash
python -m project_generator build --protocol-dir input/protocols --mappings input/mappings.json --choreography input/choreography.json --output output/demo_project
```

## 输入说明

- XML: 协议结构与 `MessCode` 序列
- `mappings.json`: 字段公式、运行时抓取策略、端口配置
- `choreography.json`: 联合转换目标窗口、时序矩阵、缓存发送策略

## 当前能力

- 解析 `Item/StructMess/Field/Group/MessCode`
- 生成 `*_def.h`、`codec.*`、`messageconvert.*`、映射文件、`main.cpp`、`config.xml`、`peach.pro`
- 联合模式生成 `to_code_Choreography.*`
- `codec.cpp` 按 AST 递归生成分支和循环读写逻辑
- `process_method / message_name / display_name / cache_name / cache_num` 支持自动推导或默认补齐
"""
