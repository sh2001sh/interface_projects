"""JSON loading and validation for generator inputs."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from project_generator.models import (
    AggregationSpec,
    AggregationTypeSpec,
    ChoreographySource,
    ChoreographySpec,
    ChoreographyTarget,
    ConstraintAssignment,
    ConstraintSpec,
    ConversionRuntime,
    ConversionSpec,
    CrcCheckSpec,
    EndpointSpec,
    FetchAttempt,
    JointGroup,
    LoopConfigSpec,
    MappingRule,
    MappingSpec,
    MatrixSpec,
    MessageRuleDetailSpec,
    ProtocolVerifySpec,
    ResponseActionSpec,
    RuntimeSpec,
    SourceAlias,
    SourceRuntime,
    TransportSpec,
    VerifyRuleSpec,
)


_FORMULA_ALLOWED_RE = re.compile(r"^[A-Za-z0-9_\s\.\+\-\*\/%\(\),<>=!&\|\?:]+$")
_FUNCTION_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_ALLOWED_FUNCTIONS = {
    "abs",
    "min",
    "max",
    "pow",
    "round",
    "floor",
    "ceil",
    "clamp",
    "int",
    "float",
    "double",
    "long",
}
_RESERVED_TOKENS = {"if", "for", "while", "return", "include"}


def _strip_balanced_outer_parentheses(text: str) -> str:
    normalized = str(text or "").strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        balanced = True
        quote = None
        escape = False
        for index, char in enumerate(normalized):
            if quote:
                if escape:
                    escape = False
                    continue
                if char == "\\":
                    escape = True
                    continue
                if char == quote:
                    quote = None
                continue
            if char in {"'", '"'}:
                quote = char
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    balanced = False
                    break
                if depth == 0 and index != len(normalized) - 1:
                    balanced = False
                    break
        if not balanced or depth != 0:
            break
        normalized = normalized[1:-1].strip()
    return normalized


def _strip_self_referential_target_guard(expression: str, target_token: str | None) -> str:
    normalized_expression = str(expression or "").strip()
    normalized_target = str(target_token or "").strip()
    if not normalized_expression or not normalized_target:
        return normalized_expression

    def _split_top_level_ternary(text: str) -> tuple[str, str, str] | None:
        candidate = _strip_balanced_outer_parentheses(text)
        depth = 0
        question_index = -1
        nested_ternary_depth = 0
        quote = None
        escape = False
        for index, char in enumerate(candidate):
            if quote:
                if escape:
                    escape = False
                    continue
                if char == "\\":
                    escape = True
                    continue
                if char == quote:
                    quote = None
                continue
            if char in {"'", '"'}:
                quote = char
                continue
            if char in "([{":
                depth += 1
                continue
            if char in ")]}":
                depth = max(depth - 1, 0)
                continue
            if depth != 0:
                continue
            if char == "?":
                if question_index < 0:
                    question_index = index
                else:
                    nested_ternary_depth += 1
                continue
            if char == ":" and question_index >= 0:
                if nested_ternary_depth == 0:
                    return (
                        candidate[:question_index].strip(),
                        candidate[question_index + 1:index].strip(),
                        candidate[index + 1:].strip(),
                    )
                nested_ternary_depth -= 1
        return None

    def _true_branch_matches_compared_value(true_branch: str, compared_value: str) -> str | None:
        true_unwrapped = _strip_balanced_outer_parentheses(true_branch)
        compared_unwrapped = _strip_balanced_outer_parentheses(compared_value)
        if true_unwrapped == compared_unwrapped or compared_unwrapped in true_unwrapped:
            return true_unwrapped
        return None

    escaped_target = re.escape(normalized_target)
    ternary_parts = _split_top_level_ternary(normalized_expression)
    if ternary_parts is not None:
        condition, when_true, when_false = ternary_parts
        if re.fullmatch(r"0(?:\.0|U|L)?", _strip_balanced_outer_parentheses(when_false)):
            condition_patterns = [
                re.compile(rf"^(?P<left>.+?)\s*==\s*{escaped_target}$", flags=re.IGNORECASE),
                re.compile(rf"^{escaped_target}\s*==\s*(?P<right>.+?)$", flags=re.IGNORECASE),
            ]
            for condition_pattern in condition_patterns:
                condition_match = condition_pattern.fullmatch(_strip_balanced_outer_parentheses(condition))
                if not condition_match:
                    continue
                compared_value = (
                    condition_match.groupdict().get("left")
                    or condition_match.groupdict().get("right")
                    or ""
                ).strip()
                matched_true = _true_branch_matches_compared_value(when_true, compared_value)
                if matched_true:
                    return matched_true

    same_value_then_zero_patterns = [
        re.compile(
            rf"^\(?\s*(?P<value>.+?)\s*==\s*{escaped_target}\s*\?\s*(?P=value)\s*:\s*0(?:\.0|U|L)?\s*\)?$"
        ),
        re.compile(
            rf"^\(?\s*{escaped_target}\s*==\s*(?P<value>.+?)\s*\?\s*(?P=value)\s*:\s*0(?:\.0|U|L)?\s*\)?$"
        ),
    ]
    for pattern in same_value_then_zero_patterns:
        match = pattern.fullmatch(normalized_expression)
        if match:
            return match.group("value").strip()

    python_same_value_then_zero_pattern = re.compile(
        rf"^\(?\s*(?P<value>.+?)\s+if\s+(?P<condition>.+?)\s+else\s+0(?:\.0)?\s*\)?$",
        flags=re.IGNORECASE,
    )
    match = python_same_value_then_zero_pattern.fullmatch(normalized_expression)
    if not match:
        return normalized_expression

    value = match.group("value").strip()
    condition = match.group("condition").strip()
    condition_patterns = [
        re.compile(rf"^(?P<left>.+?)\s*==\s*{escaped_target}$", flags=re.IGNORECASE),
        re.compile(rf"^{escaped_target}\s*==\s*(?P<right>.+?)$", flags=re.IGNORECASE),
    ]
    for condition_pattern in condition_patterns:
        condition_match = condition_pattern.fullmatch(condition)
        if not condition_match:
            continue
        compared_value = (
            condition_match.groupdict().get("left")
            or condition_match.groupdict().get("right")
            or ""
        ).strip()
        matched_true = _true_branch_matches_compared_value(value, compared_value)
        if matched_true:
            return matched_true
    return normalized_expression


def _normalize_legacy_expression(expression: str | None) -> str | None:
    if expression is None:
        return None
    normalized = str(expression).strip()
    normalized = _normalize_assignment_python_block(normalized)
    assignment_lhs = None
    single_line_assign = re.fullmatch(
        r"[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.]*\s*=\s*(.+)",
        normalized,
    )
    if single_line_assign and "\n" not in normalized:
        assignment_lhs = normalized.split("=", 1)[0].strip()
        normalized = single_line_assign.group(1).strip()
    normalized = _strip_self_referential_target_guard(normalized, "__target__")
    if assignment_lhs:
        normalized = _strip_self_referential_target_guard(normalized, assignment_lhs)
        escaped_lhs = re.escape(assignment_lhs)
        same_value_then_zero_patterns = [
            re.compile(
                rf"^\(?\s*(?P<value>.+?)\s*==\s*{escaped_lhs}\s*\?\s*(?P=value)\s*:\s*0(?:\.0|U|L)?\s*\)?$"
            ),
            re.compile(
                rf"^\(?\s*{escaped_lhs}\s*==\s*(?P<value>.+?)\s*\?\s*(?P=value)\s*:\s*0(?:\.0|U|L)?\s*\)?$"
            ),
        ]
        for pattern in same_value_then_zero_patterns:
            match = pattern.fullmatch(normalized)
            if match:
                normalized = match.group("value").strip()
                break
    return normalized


def _assignment_target_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return None


def _collapse_assignment_branch(
    statements: list[ast.stmt],
    expected_target: str | None,
) -> tuple[str | None, str | None]:
    if len(statements) != 1:
        return None, expected_target
    statement = statements[0]
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target_name = _assignment_target_name(statement.targets[0])
        if not target_name:
            return None, expected_target
        if expected_target and target_name != expected_target:
            return None, expected_target
        return ast.unparse(statement.value).strip(), target_name
    if isinstance(statement, ast.If):
        return _collapse_assignment_if(statement, expected_target)
    return None, expected_target


def _collapse_assignment_if(
    node: ast.If,
    expected_target: str | None,
) -> tuple[str | None, str | None]:
    then_expr, target_name = _collapse_assignment_branch(node.body, expected_target)
    if not then_expr:
        return None, expected_target
    else_expr, target_name = _collapse_assignment_branch(node.orelse, target_name)
    if not else_expr:
        return None, expected_target
    condition = ast.unparse(node.test).strip()
    return f"({then_expr} if {condition} else {else_expr})", target_name


def _normalize_assignment_python_block(expression: str | None) -> str | None:
    if expression is None:
        return None
    normalized = str(expression).strip()
    if not normalized or "\n" not in normalized:
        return normalized
    try:
        parsed = ast.parse(normalized, mode="exec")
    except SyntaxError:
        return normalized
    if len(parsed.body) != 1 or not isinstance(parsed.body[0], ast.If):
        return normalized
    collapsed, _target_name = _collapse_assignment_if(parsed.body[0], None)
    return collapsed or normalized


def _load_json(path: Path) -> dict:
    """Loads a JSON object from disk."""

    return json.loads(path.read_text(encoding="utf-8-sig"))


def _validate_expression(expression: str | None, aliases: set[str]) -> None:
    """Validates one formula or condition expression."""

    expression = _normalize_legacy_expression(expression)
    if expression is None:
        return
    if not _FORMULA_ALLOWED_RE.fullmatch(expression):
        raise ValueError(f"非法表达式字符: {expression}")
    lowered = expression.lower()
    for token in _RESERVED_TOKENS:
        if token in lowered:
            raise ValueError(f"表达式包含禁止关键字 '{token}': {expression}")
    for function_name in _FUNCTION_RE.findall(expression):
        if function_name not in _ALLOWED_FUNCTIONS and function_name not in aliases:
            raise ValueError(f"未授权函数或标识符 '{function_name}': {expression}")


def _load_fetches(payload: list[dict] | None) -> list[FetchAttempt]:
    """Loads one fetch-attempt list."""

    fetches = payload or []
    result = [FetchAttempt(count=int(item["count"]), cycle_ms=int(item["cycle_ms"])) for item in fetches]
    return result or [FetchAttempt(count=1, cycle_ms=0)]


def _load_runtime(payload: dict | None, source_aliases: list[SourceAlias]) -> ConversionRuntime:
    """Loads conversion runtime metadata."""

    runtime_payload = payload or {}
    runtime_sources: list[SourceRuntime] = []
    source_lookup = {source.alias: source for source in source_aliases}
    for alias, source in source_lookup.items():
        runtime_sources.append(
            SourceRuntime(
                alias=alias,
                message_name=None,
                display_name=None,
                fetches=[FetchAttempt(count=1, cycle_ms=0)],
            )
        )
    configured_sources = {item["alias"]: item for item in runtime_payload.get("sources", [])}
    for index, runtime_source in enumerate(runtime_sources):
        configured = configured_sources.get(runtime_source.alias)
        if configured is None:
            continue
        runtime_sources[index] = SourceRuntime(
            alias=runtime_source.alias,
            message_name=configured.get("message_name", runtime_source.message_name),
            display_name=configured.get("display_name", runtime_source.display_name),
            fetches=_load_fetches(configured.get("fetches")),
        )
    return ConversionRuntime(
        process_method=runtime_payload.get("process_method"),
        usage_key=runtime_payload.get("usage_key"),
        sources=runtime_sources,
        response_enabled=runtime_payload.get("response_enabled", True),
        send_mode=runtime_payload.get("send_mode", "direct"),
        cache_name=runtime_payload.get("cache_name"),
        cache_num=int(runtime_payload.get("cache_num", 3)),
    )


def _load_endpoints(payload: list[dict] | None) -> list[EndpointSpec]:
    """Loads config.xml endpoint settings."""

    result: list[EndpointSpec] = []
    for item in payload or []:
        result.append(
            EndpointSpec(
                ip=item.get("ip", "127.0.0.1"),
                port=int(item["port"]),
                net_type=item.get("type", "udp"),
                recv=bool(int(item["recv"])) if isinstance(item.get("recv"), str) else bool(item["recv"]),
                feedback_port=int(item.get("feed_back_port", item.get("feedBackPort", item["port"]))),
                name=item["name"],
            )
        )
    return result


def _require_port(port_value: object, field_name: str) -> int:
    """Validates and converts one port value."""

    port = int(port_value)
    if port < 1 or port > 65535:
        raise ValueError(f"{field_name} 端口非法: {port}")
    return port


def _load_message_rule_detail(payload: dict) -> MessageRuleDetailSpec:
    """Loads one runtime message-rule detail item."""

    message_name = str(payload.get("message_name") or payload.get("messageName") or "").strip()
    if not message_name:
        raise ValueError("messageRuleDetailList[].messageName 不能为空")
    filter_payload = payload.get("filterConfig") or payload.get("filter_config") or {}
    if filter_payload and not isinstance(filter_payload, dict):
        raise ValueError("messageRuleDetailList[].filterConfig 必须是对象")
    crc_payload = (
        payload.get("crc_check")
        or payload.get("crcCheck")
        or filter_payload.get("crc_check")
        or filter_payload.get("crcCheck")
        or {}
    )
    loop_payload = (
        payload.get("loop_config")
        or payload.get("loopConfig")
        or filter_payload.get("loop_config")
        or filter_payload.get("loopConfig")
        or {}
    )
    aggregation_payload = (
        payload.get("aggregation")
        or filter_payload.get("aggregation")
        or {}
    )
    aggregation_type_payload = (
        payload.get("aggregation_type")
        or payload.get("aggregationType")
        or filter_payload.get("aggregation_type")
        or filter_payload.get("aggregationType")
        or {}
    )
    compare_operator = str(aggregation_payload.get("operator") or "").strip().upper() or None
    if compare_operator and compare_operator not in {"GT", "LT", "EQ", "GTE", "LTE", "NEQ"}:
        raise ValueError("messageRuleDetailList[].filterConfig.aggregation.operator 非法")
    compare_value = None if aggregation_payload.get("value") in (None, "") else str(aggregation_payload.get("value")).strip()
    if compare_value == "":
        compare_value = None

    return MessageRuleDetailSpec(
        message_name=message_name,
        delay_requirement=int(payload.get("delay_requirement", payload.get("delayRequirement", 0)) or 0),
        crc_check=CrcCheckSpec(
            enabled=bool(crc_payload.get("enabled", False)),
            bind_element=str(crc_payload.get("bind_element") or crc_payload.get("bindElement") or "").strip() or None,
        ),
        loop_config=LoopConfigSpec(
            type=str(loop_payload.get("type") or "NONE").strip() or "NONE",
        ),
        aggregation=AggregationSpec(
            mode=str(aggregation_payload.get("mode") or "SINGLE").strip() or "SINGLE",
            count=(None if aggregation_payload.get("count") in (None, "") else int(aggregation_payload.get("count"))),
            time_ms=(None if aggregation_payload.get("time_ms", aggregation_payload.get("timeMs")) in (None, "") else int(aggregation_payload.get("time_ms", aggregation_payload.get("timeMs")))),
            operator=compare_operator,
            value=compare_value,
        ),
        aggregation_type=AggregationTypeSpec(
            type=str(aggregation_type_payload.get("type") or "TIME").strip() or "TIME",
            bind_element=str(aggregation_type_payload.get("bind_element") or aggregation_type_payload.get("bindElement") or "").strip() or None,
        ),
    )


def _load_transport(payload: dict | None) -> TransportSpec | None:
    """Loads transport configuration from runtime payload."""

    if not payload:
        return None
    message_type = str(payload.get("message_type") or payload.get("messageType") or "").strip() or "bundle"
    recv_ip = str(payload.get("recv_ip") or payload.get("recvIp") or "127.0.0.1").strip() or "127.0.0.1"
    send_ip = str(payload.get("send_ip") or payload.get("sendIp") or "127.0.0.1").strip() or "127.0.0.1"
    recv_port = payload.get("recv_port", payload.get("recvPort"))
    send_port = payload.get("send_port", payload.get("sendPort"))
    if recv_port is None:
        raise ValueError("runtime.transport.recvPort 不能为空")
    if send_port is None:
        raise ValueError("runtime.transport.sendPort 不能为空")
    message_rules_payload = payload.get("message_rules") or payload.get("messageRuleDetailList") or []
    if not isinstance(message_rules_payload, list):
        raise ValueError("runtime.transport.messageRuleDetailList 必须是数组")
    return TransportSpec(
        message_type=message_type,
        recv_ip=recv_ip,
        recv_port=_require_port(recv_port, "recvPort"),
        send_ip=send_ip,
        send_port=_require_port(send_port, "sendPort"),
        message_rules=[_load_message_rule_detail(item) for item in message_rules_payload],
    )


def _load_protocol_verifies(payload: dict | None) -> list[ProtocolVerifySpec]:
    """Loads protocol-level verify/response state-machine settings."""

    if not payload:
        return []
    result: list[ProtocolVerifySpec] = []
    for protocol_name, item in payload.items():
        constraints: list[ConstraintSpec] = []
        for index, constraint in enumerate(item.get("constraints", []), start=1):
            constraints.append(
                ConstraintSpec(
                    name=constraint.get("name", f"Constraint{index}"),
                    check=constraint.get("check"),
                    assignments=[
                        ConstraintAssignment(field=assignment["field"], value=str(assignment["value"]))
                        for assignment in constraint.get("set", [])
                    ],
                )
            )
        verify_rules: list[VerifyRuleSpec] = []
        for index, rule in enumerate(item.get("verify_rules", []), start=1):
            default_constraint = constraints[index - 1].name if index - 1 < len(constraints) else None
            verify_rules.append(
                VerifyRuleSpec(
                    name=rule.get("name", f"verify{index}"),
                    when_seq=rule["when_seq"],
                    constraint=rule.get("constraint", default_constraint),
                )
            )
        response_actions: list[ResponseActionSpec] = []
        for index, action in enumerate(item.get("response_actions", []), start=1):
            default_verify = verify_rules[index - 1].name if index - 1 < len(verify_rules) else None
            if action.get("on_verify", default_verify) is None:
                raise ValueError(f"协议 '{protocol_name}' 的 response_actions[{index}] 缺少 on_verify，且无法按顺序推导")
            response_actions.append(
                ResponseActionSpec(
                    on_verify=action.get("on_verify", default_verify),
                    set_constraint=action.get("set_constraint"),
                    encode_seq=action.get("encode_seq"),
                    return_code=int(action.get("return_code", 0)),
                )
            )
        result.append(
            ProtocolVerifySpec(
                protocol=protocol_name,
                constraints=constraints,
                verify_rules=verify_rules,
                response_actions=response_actions,
                default_verify=item.get("default_verify"),
                default_return_code=int(item.get("default_return_code", -1)),
            )
        )
    return result


def _resolve_conversion_mode(raw_mode: Any, source_count: int) -> str:
    """Normalizes conversion mode and falls back to source-count inference."""

    mode = str(raw_mode or "").strip().lower()
    if mode in {"joint", "simple"}:
        return mode
    return "joint" if int(source_count or 0) > 1 else "simple"


def _resolve_choreography_mode(payload: dict) -> str:
    """Normalizes choreography mode and falls back to joint-group inference."""

    mode = str(payload.get("mode") or "").strip().lower()
    if mode in {"joint", "simple"}:
        return mode
    return "joint" if payload.get("joint_groups") else "simple"


def load_mappings(path: Path) -> MappingSpec:
    """Loads and validates mappings.json."""

    payload = _load_json(path)
    conversions: list[ConversionSpec] = []
    seen_names: set[str] = set()
    for item in payload.get("conversions", []):
        name = item["name"]
        if name in seen_names:
            raise ValueError(f"重复的转换名称: {name}")
        seen_names.add(name)
        sources = [
            SourceAlias(alias=src["alias"], protocol=src["protocol"])
            for src in item.get("sources", [])
        ]
        alias_names = {source.alias for source in sources}
        if len(alias_names) != len(sources):
            raise ValueError(f"转换 '{name}' 中存在重复别名")
        rules: list[MappingRule] = []
        seen_target_fields: set[str] = set()
        for rule in item.get("rules", []):
            target_field = rule["target_field"]
            if target_field in seen_target_fields:
                raise ValueError(f"转换 '{name}' 中重复赋值字段: {target_field}")
            seen_target_fields.add(target_field)
            normalized_formula = _normalize_legacy_expression(rule["formula"])
            normalized_when = _normalize_legacy_expression(rule.get("when"))
            _validate_expression(normalized_formula, alias_names)
            _validate_expression(normalized_when, alias_names)
            rules.append(
                MappingRule(
                    target_field=target_field,
                    formula=normalized_formula or "",
                    source_fields=rule.get("source_fields", []),
                    rule_type=rule["rule_type"],
                    when=normalized_when,
                    default_value=(
                        None if rule.get("default_value") is None else str(rule.get("default_value"))
                    ),
                    description=rule.get("description"),
                )
            )
        conversions.append(
            ConversionSpec(
                name=name,
                mode=_resolve_conversion_mode(item.get("mode"), len(sources)),
                sources=sources,
                target_protocol=item["target"]["protocol"],
                rules=rules,
                runtime=_load_runtime(item.get("runtime"), sources),
            )
        )
    runtime_payload = payload.get("runtime", {})
    transport = _load_transport(runtime_payload.get("transport"))
    return MappingSpec(
        version=payload.get("version", "1.0"),
        project_name=payload["project_name"],
        conversions=conversions,
        runtime=RuntimeSpec(
            endpoints=_load_endpoints(runtime_payload.get("endpoints")),
            transport=transport,
            loop_sleep_ms=int(runtime_payload.get("loop_sleep_ms", 2)),
            check_data_interval_ms=int(runtime_payload.get("check_data_interval_ms", 5000)),
            protocol_verifies=_load_protocol_verifies(runtime_payload.get("protocol_verifies")),
        ),
    )


def load_choreography(path: Path) -> ChoreographySpec:
    """Loads and validates choreography.json."""

    payload = _load_json(path)
    sources = [
        ChoreographySource(
            source_id=item["id"],
            protocol=item["protocol"],
            message_type=item["message_type"],
            cache_key=item["cache_key"],
            required=item.get("required", True),
        )
        for item in payload.get("sources", [])
    ]
    targets = [
        ChoreographyTarget(
            target_id=item["id"],
            protocol=item["protocol"],
            message_type=item["message_type"],
            template_name=item["template_name"],
            receive_window_ms=int(item["receive_window_ms"]),
            initial_status=item.get("initial_status", "direct"),
        )
        for item in payload.get("targets", [])
    ]
    source_ids = {item.source_id for item in sources}
    target_ids = {item.target_id for item in targets}
    joint_groups: list[JointGroup] = []
    for group in payload.get("joint_groups", []):
        if group["target_id"] not in target_ids:
            raise ValueError(f"编排组引用了未知目标: {group['target_id']}")
        for source_name in group.get("sources", []):
            if source_name not in source_ids:
                raise ValueError(f"编排组引用了未知源消息: {source_name}")
        matrix_payload = group["matrix"]
        rows = matrix_payload["rows"]
        cols = matrix_payload["cols"]
        values = matrix_payload["values"]
        if len(values) != len(rows):
            raise ValueError(f"编排组 '{group['group_id']}' 的矩阵行数不匹配")
        for row in values:
            if len(row) != len(cols):
                raise ValueError(f"编排组 '{group['group_id']}' 的矩阵列数不匹配")
        for row_index, row in enumerate(values):
            for col_index, value in enumerate(row):
                if row_index == col_index and value != 0:
                    raise ValueError("时序矩阵对角线必须为 0")
                if value is not None and int(value) < 0:
                    raise ValueError("时序矩阵值必须为非负数或 null")
        joint_groups.append(
            JointGroup(
                group_id=group["group_id"],
                target_id=group["target_id"],
                sources=group["sources"],
                trigger_policy=group["trigger_policy"],
                matrix=MatrixSpec(
                    unit=matrix_payload["unit"],
                    rows=rows,
                    cols=cols,
                    values=values,
                ),
            )
        )
    return ChoreographySpec(
        version=payload.get("version", "1.0"),
        mode=_resolve_choreography_mode(payload),
        project_name=payload["project_name"],
        sources=sources,
        targets=targets,
        joint_groups=joint_groups,
    )
