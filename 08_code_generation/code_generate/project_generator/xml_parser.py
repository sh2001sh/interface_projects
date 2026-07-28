"""XML parsing support for protocol definition files."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter

from project_generator.models import (
    BranchNode,
    ConstraintAssignment,
    ConstraintSpec,
    DimenSpec,
    FieldSpec,
    GroupNode,
    ProtocolNode,
    ProtocolSpec,
    ProtocolVerifySpec,
    ResponseActionSpec,
    RouteSpec,
    ScalarNode,
    SectionSpec,
    SequenceMember,
    SequenceSpec,
    VerifyRuleSpec,
)
from project_generator.utils import normalize_token, to_snake_name, to_type_name


_EXPR_TOKEN_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_NUMERIC_LITERAL_RE = re.compile(r"^-?\d+(\.\d+)?$")
_FAMILY_KEY_RE = re.compile(r"^[A-Za-z]+")
_COPY_SUFFIX_RE = re.compile(
    r"(?:\s*[-_]\s*|\s+)?(?:副本|copy)(?:\s*[\(\[]?\d+[\)\]]?)?\s*$",
    flags=re.IGNORECASE,
)


def _local_name(tag: str) -> str:
    """Extracts the local tag name from one XML element tag."""

    if "}" in tag:
        return tag.split("}", 1)[1]
    if ":" in tag:
        return tag.split(":", 1)[1]
    return tag


def _tag_prefix(tag: str) -> str:
    """Returns the tag prefix when present."""

    if "}" in tag:
        raw = tag.split("}", 1)[1]
    else:
        raw = tag
    if ":" in raw:
        return raw.split(":", 1)[0]
    return raw


def _namespace_uri(tag: str) -> str:
    """Returns the XML namespace URI when present."""

    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return ""


def _sanitize_protocol_file_stem(stem: str) -> str:
    """Normalizes copied XML filenames back to their canonical protocol stem."""

    normalized = _COPY_SUFFIX_RE.sub("", str(stem or "").strip()).strip()
    return normalized or str(stem or "").strip()


def _build_field_name(path_parts: tuple[str, ...]) -> str:
    """Builds a stable C++ field identifier from one nested XML path."""

    tokens = [normalize_token(part) for part in path_parts if part]
    return "_".join(token for token in tokens if token) or "field"


def _binding_field_name(raw: str | None, path_parts: tuple[str, ...]) -> str:
    """Builds the backing C++ field identifier for one XML scalar node."""

    if raw is not None and raw.strip():
        return normalize_token(raw.strip())
    return _build_field_name(path_parts)


def _normalize_declared_type(raw: str | None) -> str | None:
    """Normalizes one XML declared type value."""

    if raw is None:
        return None
    declared_type = raw.strip().lower()
    return declared_type or None


def _cpp_type_for_declared_type(declared_type: str | None) -> str:
    """Maps one XML declared type to a generated C++ field type."""

    mapping = {
        "char": "char",
        "float": "float",
        "double": "double",
        "int": "long",
    }
    return mapping.get(declared_type or "", "long")


def _parse_int(raw: str | None, fallback: int | None = None) -> int | None:
    """Parses an integer when possible."""

    if raw is None:
        return fallback
    text = raw.strip()
    if not text:
        return fallback
    try:
        return int(text)
    except ValueError:
        return fallback


def _parse_bit_length(node: ET.Element) -> int | None:
    """Parses a bit-length value from one XML node."""

    candidates = [
        (node.text or "").strip(),
        node.attrib.get("bitLength", "").strip(),
        node.attrib.get("length", "").strip(),
    ]
    for candidate in candidates:
        value = _parse_int(candidate)
        if value is not None:
            return value
    return None


def _append_feature(features: list[str], feature: str) -> None:
    """Appends one feature once."""

    if feature not in features:
        features.append(feature)


def _corr_fields(raw_corr: str | None) -> tuple[str, ...]:
    """Extracts referenced control-field names from one corr expression."""

    if not raw_corr:
        return ()
    parts: list[str] = []
    for chunk in raw_corr.split(","):
        token = chunk.strip()
        if not token:
            continue
        parts.append(token.rsplit(".", 1)[-1].strip())
    return tuple(parts)


def _match_control_default(
    control_fields: tuple[str, ...],
    label_defaults: dict[str, str | None],
) -> int | None:
    """Returns a numeric default value from referenced control fields."""

    for label in control_fields:
        default_value = label_defaults.get(label)
        parsed = _parse_int(default_value)
        if parsed is not None:
            return parsed
    return None


def _flatten_nodes(
    nodes: list[ProtocolNode],
    label_defaults: dict[str, str | None],
    features: list[str],
    path_parts: tuple[str, ...] = (),
    bit_offset: int = 0,
) -> tuple[list[FieldSpec], int]:
    """Flattens AST nodes into field manifest entries."""

    fields: list[FieldSpec] = []
    current_offset = bit_offset
    for node in nodes:
        if isinstance(node, ScalarNode):
            fields.append(
                FieldSpec(
                    label=node.label,
                    cpp_name=node.cpp_name,
                    path="/".join(path_parts + (node.label,)),
                    path_parts=path_parts + (node.label,),
                    bit_length=node.bit_length,
                    bit_offset=current_offset,
                    default_value=node.default_value,
                    source_tag=node.source_tag,
                    source_field=node.source_field,
                    declared_type=node.declared_type,
                    cpp_type=_cpp_type_for_declared_type(node.declared_type),
                )
            )
            if node.bit_length is not None:
                current_offset += node.bit_length
            continue
        if isinstance(node, BranchNode):
            _append_feature(features, "branch")
            nested_fields, current_offset = _flatten_nodes(
                node.children,
                label_defaults,
                features,
                path_parts + (node.label,),
                current_offset,
            )
            fields.extend(nested_fields)
            continue

        _append_feature(features, "loop")
        repeat_count = node.repeat_count
        if node.max_repeat is not None and node.repeat_count > 1:
            _append_feature(features, "group_max_repeat")
        if node.max_repeat is None and repeat_count > 1:
            _append_feature(features, "group_default_repeat")
        for index in range(repeat_count):
            group_label = f"{node.label}_{index + 1}" if repeat_count > 1 else node.label
            nested_fields, current_offset = _flatten_nodes(
                node.children,
                label_defaults,
                features,
                path_parts + (group_label,),
                current_offset,
            )
            fields.extend(nested_fields)
    return fields, current_offset


def _parse_children(
    container: ET.Element,
    label_defaults: dict[str, str | None],
    features: list[str],
    path_parts: tuple[str, ...] = (),
) -> list[ProtocolNode]:
    """Parses one XML subtree into protocol nodes."""

    nodes: list[ProtocolNode] = []
    for child in list(container):
        local = _local_name(child.tag)
        label = child.attrib.get("name", local)
        node_path = path_parts + (label,)
        if local in {"Atomic", "Constraint", "Condition", "ObjMaps", "ObjMap", "Verify", "PreSeq", "Member"}:
            continue
        if local in {"Item", "StructMess"} or (not list(child) and local not in {"Field", "Group", "MessCode", "Dimen"}):
            default_value = child.attrib.get("defaultValue")
            label_defaults[label] = default_value
            declared_type = _normalize_declared_type(child.attrib.get("type"))
            nodes.append(
                ScalarNode(
                    label=label,
                    cpp_name=_binding_field_name(child.attrib.get("field"), node_path),
                    path="/".join(node_path),
                    path_parts=node_path,
                    bit_length=_parse_bit_length(child),
                    default_value=default_value,
                    source_tag=local,
                    source_field=child.attrib.get("source"),
                    declared_type=declared_type,
                    cpp_type=_cpp_type_for_declared_type(declared_type),
                )
            )
            continue
        if local == "Field":
            nodes.append(
                BranchNode(
                    label=label,
                    path="/".join(node_path),
                    path_parts=node_path,
                    corr=child.attrib.get("corr"),
                    value=child.attrib.get("value"),
                    control_fields=_corr_fields(child.attrib.get("corr")),
                    children=_parse_children(child, label_defaults, features, node_path),
                )
            )
            continue
        if local == "Group":
            control_fields = _corr_fields(child.attrib.get("corr"))
            max_repeat = _parse_int(child.attrib.get("max"))
            repeat_count = (
                max_repeat
                if max_repeat is not None
                else (_match_control_default(control_fields, label_defaults) or 1)
            )
            nodes.append(
                GroupNode(
                    label=label,
                    path="/".join(node_path),
                    path_parts=node_path,
                    corr=child.attrib.get("corr"),
                    condition=child.attrib.get("condition"),
                    max_repeat=max_repeat,
                    repeat_count=max(1, repeat_count),
                    control_fields=control_fields,
                    children=_parse_children(child, label_defaults, features, node_path),
                )
            )
            continue
        if local == "MessCode":
            _append_feature(features, "sequence")
            continue
        if local == "Dimen":
            continue
        nodes.extend(_parse_children(child, label_defaults, features, path_parts))
    return nodes


def _section_name_from_element(node: ET.Element) -> str:
    """Builds one section name from a top-level XML element."""

    raw_name = node.attrib.get("name", "").strip()
    if raw_name:
        return raw_name
    namespace_uri = _namespace_uri(node.tag).strip().rstrip("/")
    if namespace_uri:
        tail = namespace_uri.split("/")[-1].strip()
        if tail:
            return tail
    prefix = _tag_prefix(node.tag)
    if "_" in prefix:
        return prefix.split("_")[-1]
    return prefix or _local_name(node.tag)


def _parse_sections(
    root: ET.Element,
    label_defaults: dict[str, str | None],
    features: list[str],
) -> list[SectionSpec]:
    """Parses top-level section containers."""

    sections: list[SectionSpec] = []
    for child in list(root):
        local = _local_name(child.tag)
        if local != "NameSpace":
            continue
        section_name = _section_name_from_element(child)
        raw_key = child.attrib.get("key")
        sections.append(
            SectionSpec(
                name=section_name,
                cpp_name=normalize_token(section_name),
                tag_name=_tag_prefix(child.tag),
                path=section_name,
                key=_parse_int(raw_key),
                nodes=_parse_children(child, label_defaults, features, (section_name,)),
            )
        )
    return sections


def _parse_dimen(root: ET.Element) -> DimenSpec:
    """Parses the optional Dimen node."""

    for child in list(root):
        if _local_name(child.tag) != "Dimen":
            continue
        endian_value = child.attrib.get("endian", "").strip()
        endian = "little" if endian_value in {"2", "little", "Little", "LE", "le"} else "big"
        return DimenSpec(
            pack_head_length=_parse_int(child.attrib.get("packHeadLength"), 0) or 0,
            endian=endian,
            word_length=_parse_int(child.attrib.get("wordLength"), -1) or -1,
        )
    return DimenSpec()


def _parse_sequences(root: ET.Element) -> list[SequenceSpec]:
    """Parses MessCode/PreSeq definitions."""

    sequences: list[SequenceSpec] = []
    for child in list(root):
        if _local_name(child.tag) != "MessCode":
            continue
        for pre_seq in list(child):
            if _local_name(pre_seq.tag) != "PreSeq":
                continue
            members: list[SequenceMember] = []
            for member in list(pre_seq):
                if _local_name(member.tag) != "Member":
                    continue
                members.append(
                    SequenceMember(
                        corr=member.attrib.get("corr"),
                        value=(member.text or "").strip() or member.attrib.get("value"),
                        control_fields=_corr_fields(member.attrib.get("corr")),
                    )
                )
            sequences.append(
                SequenceSpec(
                    name=pre_seq.attrib.get("name", f"Seq_{len(sequences) + 1}"),
                    cycle=_parse_int(pre_seq.attrib.get("cycle"), 0) or 0,
                    times=_parse_int(pre_seq.attrib.get("times"), 1) or 1,
                    members=members,
                )
            )
    return sequences


def _parse_routes(root: ET.Element) -> list[RouteSpec]:
    """Parses root-level route selectors such as k.xml."""

    routes: list[RouteSpec] = []
    for child in list(root):
        if _local_name(child.tag) != "Field":
            continue
        target_protocol = (child.text or "").strip()
        if not target_protocol:
            continue
        routes.append(
            RouteSpec(
                corr=child.attrib.get("corr"),
                value=child.attrib.get("value"),
                target_protocol=target_protocol,
                control_fields=_corr_fields(child.attrib.get("corr")),
            )
        )
    return routes


def _resolve_verify_field_name(protocol: ProtocolSpec, raw_field: str) -> str | None:
    """Resolves one XML verify expression field reference."""

    field_token = raw_field.rsplit(".", 1)[-1].strip()
    normalized = normalize_token(field_token)
    for field in protocol.fields:
        label_normalized = normalize_token(field.label)
        if field.label == field_token or field.cpp_name == field_token:
            return field.cpp_name
        if label_normalized == normalized or field.cpp_name.endswith(normalized):
            return field.cpp_name
        if normalized.endswith(label_normalized) or label_normalized.endswith(normalized):
            return field.cpp_name
    return None


def _translate_atomic_check(raw_text: str, protocol: ProtocolSpec) -> str:
    """Translates one Atomic XML expression into a C++ condition."""

    parts = [part.strip() for part in raw_text.split(",", 2)]
    if len(parts) != 3:
        return "true"
    left, operator, right = parts
    field_name = _resolve_verify_field_name(protocol, left)
    if field_name is None:
        return "true"
    cpp_operator = {"=": "==", "==": "==", "!=": "!=", "<>": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<="}.get(
        operator,
        operator,
    )
    return f"value.{field_name} {cpp_operator} {right}"


def _parse_atomic_assignment(raw_text: str, protocol: ProtocolSpec) -> ConstraintAssignment | None:
    """Extracts one assignment from an equality-style Atomic XML expression."""

    parts = [part.strip() for part in raw_text.split(",", 2)]
    if len(parts) != 3:
        return None
    left, operator, right = parts
    if operator not in {"=", "=="}:
        return None
    field_name = _resolve_verify_field_name(protocol, left)
    if field_name is None:
        return None
    return ConstraintAssignment(field=field_name, value=right)


def _translate_constraint_expression(raw_text: str, atomic_checks: dict[str, str]) -> str:
    """Translates one Constraint XML expression into a C++ boolean expression."""

    text = raw_text.replace("&", " && ").replace("|", " || ")

    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        return f"({atomic_checks[token]})" if token in atomic_checks else token

    return _EXPR_TOKEN_RE.sub(repl, text)


def _parse_xml_protocol_verify(root: ET.Element, protocol: ProtocolSpec) -> ProtocolVerifySpec | None:
    """Builds one protocol verify spec from XML-native rule nodes."""

    atomic_checks: dict[str, str] = {}
    atomic_assignments: dict[str, ConstraintAssignment] = {}
    for child in list(root):
        if _local_name(child.tag) != "Atomic":
            continue
        name = child.attrib.get("name")
        raw_text = (child.text or "").strip()
        if not name or not raw_text:
            continue
        atomic_checks[name] = _translate_atomic_check(raw_text, protocol)
        assignment = _parse_atomic_assignment(raw_text, protocol)
        if assignment is not None:
            atomic_assignments[name] = assignment

    constraints: list[ConstraintSpec] = []
    for index, child in enumerate(list(root), start=1):
        if _local_name(child.tag) != "Constraint":
            continue
        raw_text = (child.text or "").strip()
        name = child.attrib.get("name", f"Constraint_{index}")
        expression = _translate_constraint_expression(raw_text, atomic_checks)
        assignments: list[ConstraintAssignment] = []
        if "|" not in raw_text and "||" not in raw_text:
            assignment_by_field: dict[str, ConstraintAssignment] = {}
            for token in _EXPR_TOKEN_RE.findall(raw_text):
                assignment = atomic_assignments.get(token)
                if assignment is None:
                    continue
                assignment_by_field[assignment.field] = assignment
            assignments = list(assignment_by_field.values())
        constraints.append(ConstraintSpec(name=name, check=expression, assignments=assignments))

    verify_rules: list[VerifyRuleSpec] = []
    verify_lookup: dict[str, VerifyRuleSpec] = {}
    for child in list(root):
        if _local_name(child.tag) != "Condition":
            continue
        for index, verify_node in enumerate(list(child), start=1):
            if _local_name(verify_node.tag) != "Verify":
                continue
            payload = [part.strip() for part in (verify_node.text or "").split(",") if part.strip()]
            if not payload:
                continue
            rule = VerifyRuleSpec(
                name=verify_node.attrib.get("name", f"Verify_{index}"),
                when_seq=payload[0],
                constraint=payload[1] if len(payload) > 1 else None,
            )
            verify_rules.append(rule)
            verify_lookup[rule.name] = rule

    response_actions: list[ResponseActionSpec] = []
    for child in list(root):
        if _local_name(child.tag) != "ObjMaps":
            continue
        for obj_map in list(child):
            if _local_name(obj_map.tag) != "ObjMap":
                continue
            recv_verify = (obj_map.attrib.get("recv") or "").strip()
            feedback_verify = (obj_map.attrib.get("feedBack") or obj_map.attrib.get("feedback") or "").strip()
            if not recv_verify:
                continue
            encode_seq = None
            set_constraint = None
            if feedback_verify and feedback_verify.upper() != "NULL":
                feedback_rule = verify_lookup.get(feedback_verify)
                if feedback_rule is not None:
                    encode_seq = feedback_rule.when_seq
                    set_constraint = feedback_rule.constraint
            response_actions.append(
                ResponseActionSpec(
                    on_verify=recv_verify,
                    set_constraint=set_constraint,
                    encode_seq=encode_seq,
                    return_code=0,
                )
            )

    if not constraints and not verify_rules and not response_actions:
        return None
    return ProtocolVerifySpec(
        protocol=protocol.type_name,
        constraints=constraints,
        verify_rules=verify_rules,
        response_actions=response_actions,
        default_verify=None,
        default_return_code=-1,
    )


def _determine_structure_kind(features: list[str]) -> str:
    """Determines the XML structure kind from collected features."""

    if "loop" in features:
        return "loop"
    if "branch" in features:
        return "branch"
    return "fixed_length"


def _refresh_protocol(protocol: ProtocolSpec, label_defaults: dict[str, str | None], features: list[str]) -> None:
    """Rebuilds flattened fields after node mutation."""

    protocol.nodes = [node for section in protocol.sections for node in section.nodes]
    protocol.fields, protocol.total_bits = _flatten_nodes(protocol.nodes, label_defaults, features)
    if not protocol.fields:
        protocol.fields = [
            FieldSpec(
                label="payload",
                cpp_name="payload",
                path="payload",
                path_parts=("payload",),
                bit_length=None,
                bit_offset=0,
                default_value="0",
                source_tag="Generated",
            )
        ]
    _uniquify_protocol_bindings(protocol)
    protocol.label_to_cpp = {field.label: field.cpp_name for field in protocol.fields}
    protocol.structure_kind = _determine_structure_kind(features)


def _placeholder_class(section_name: str) -> str | None:
    """Maps one section name to a placeholder category."""

    token = normalize_token(section_name)
    if token.startswith("continue"):
        return "continue"
    if token.startswith("prolong"):
        return "prolong"
    if token.startswith("origin"):
        return "origin"
    return None


def _collect_placeholder_lengths(protocol: ProtocolSpec) -> dict[str, list[int]]:
    """Collects placeholder bit lengths grouped by section class."""

    grouped: dict[str, list[int]] = {}
    for section in protocol.sections:
        category = _placeholder_class(section.name)
        if category is None:
            continue
        for node in section.nodes:
            for scalar in _iter_scalar_nodes([node]):
                if scalar.label != "**":
                    continue
                if scalar.bit_length is None:
                    continue
                grouped.setdefault(category, []).append(scalar.bit_length)
    return grouped


def _iter_scalar_nodes(nodes: list[ProtocolNode]) -> list[ScalarNode]:
    """Flattens scalar nodes from one node tree."""

    result: list[ScalarNode] = []
    for node in nodes:
        if isinstance(node, ScalarNode):
            result.append(node)
        else:
            result.extend(_iter_scalar_nodes(node.children))
    return result


def _uniquify_protocol_bindings(protocol: ProtocolSpec) -> None:
    """Makes generated C++ field bindings unique while preserving traversal order."""

    totals = Counter(field.cpp_name for field in protocol.fields)
    for field in protocol.fields:
        base_name = field.cpp_name
        if totals[base_name] <= 1:
            continue
        field.cpp_name = _build_field_name(field.path_parts)


def _apply_placeholder_lengths(protocol: ProtocolSpec, template_protocol: ProtocolSpec) -> bool:
    """Applies template placeholder lengths onto zero-length concrete fields."""

    placeholders = _collect_placeholder_lengths(template_protocol)
    if not placeholders:
        return False

    cursors = {key: 0 for key in placeholders}
    changed = False
    for section in protocol.sections:
        category = _placeholder_class(section.name)
        if category is None or category not in placeholders:
            continue
        values = placeholders[category]
        for scalar in _iter_scalar_nodes(section.nodes):
            if scalar.bit_length != 0:
                continue
            index = min(cursors[category], len(values) - 1)
            scalar.bit_length = values[index]
            cursors[category] += 1
            changed = True
    return changed


def _protocol_family_key(path: Path) -> str:
    """Builds a family key used to match template XML with concrete XML."""

    token = _sanitize_protocol_file_stem(path.stem).replace(".", "_")
    match = _FAMILY_KEY_RE.match(token)
    return match.group(0).lower() if match else token.lower()


def parse_protocol_file(path: Path) -> ProtocolSpec:
    """Parses one protocol XML file into a protocol specification."""

    root = ET.fromstring(path.read_text(encoding="utf-8"))
    namespace = root.attrib.get("xmlns", "")
    raw_name = _sanitize_protocol_file_stem(path.stem).replace(".", "_")
    type_name = to_type_name(raw_name)
    label_defaults: dict[str, str | None] = {}
    features: list[str] = []
    sections = _parse_sections(root, label_defaults, features)
    nodes = [node for section in sections for node in section.nodes]
    if not sections:
        nodes = _parse_children(root, label_defaults, features)
        sections = [
            SectionSpec(
                name="Origin",
                cpp_name="origin",
                tag_name="Origin",
                path="Origin",
                nodes=nodes,
            )
        ]
    fields, total_bits = _flatten_nodes(nodes, label_defaults, features)
    if not fields:
        fields = [
            FieldSpec(
                label="payload",
                cpp_name="payload",
                path="payload",
                path_parts=("payload",),
                bit_length=None,
                bit_offset=0,
                default_value="0",
                source_tag="Generated",
            )
        ]
    protocol = ProtocolSpec(
        type_name=type_name,
        file_stem=to_snake_name(raw_name),
        source_path=path,
        namespace=namespace,
        dimen=_parse_dimen(root),
        total_bits=total_bits,
        structure_kind=_determine_structure_kind(features),
        codec_supported=True,
        unsupported_features=features,
        fields=fields,
        nodes=nodes,
        sections=sections,
        sequences=_parse_sequences(root),
        routes=_parse_routes(root),
        label_to_cpp={},
    )
    _uniquify_protocol_bindings(protocol)
    protocol.label_to_cpp = {field.label: field.cpp_name for field in protocol.fields}
    protocol.xml_protocol_verify = _parse_xml_protocol_verify(root, protocol)
    return protocol


def load_protocols(protocol_dir: Path) -> list[ProtocolSpec]:
    """Loads all XML protocol definitions from a directory."""

    files = sorted(protocol_dir.glob("*.xml"))
    if not files:
        raise ValueError(f"未在目录中找到 XML 协议文件: {protocol_dir}")

    protocols = [parse_protocol_file(file_path) for file_path in files]
    family_templates: dict[str, ProtocolSpec] = {}
    for protocol in protocols:
        if _collect_placeholder_lengths(protocol):
            family_templates.setdefault(_protocol_family_key(protocol.source_path), protocol)

    for protocol in protocols:
        template_protocol = family_templates.get(_protocol_family_key(protocol.source_path))
        if template_protocol is None or template_protocol.source_path == protocol.source_path:
            continue
        label_defaults = {
            scalar.label: scalar.default_value
            for scalar in _iter_scalar_nodes(protocol.nodes)
            if scalar.default_value is not None
        }
        features = list(protocol.unsupported_features)
        if _apply_placeholder_lengths(protocol, template_protocol):
            _refresh_protocol(protocol, label_defaults, features)
    return protocols
