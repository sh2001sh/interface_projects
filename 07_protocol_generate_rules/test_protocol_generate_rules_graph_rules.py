from __future__ import annotations

from pathlib import Path

from code_generation_adapter import build_generator_rules_payload
from protocol_conversion.generator import (
    _apply_source_field_catalog_to_rules,
    _expand_generated_rules_to_target_instances,
    _filter_valid_generated_rules,
    _knowledge_rule_to_generated_rule,
)
from protocol_conversion.knowledge_base import KnowledgeRule, ProtocolConversionKnowledgeBase


def _prepare_protocol_dir(tmp_path: Path) -> Path:
    protocol_dir = tmp_path / "protocols"
    protocol_dir.mkdir()
    (protocol_dir / "k1.6.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="time_field">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (protocol_dir / "k1.7.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="height_field">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (protocol_dir / "x0.5.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="time_value">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    return protocol_dir


def test_knowledge_rule_to_generated_rule_preserves_source_metadata():
    rule = KnowledgeRule(
        protocol_type="K1_6",
        message_code="K1.6",
        field_name="K1_6_TIME_FIELD",
        source_fields=["K1_6_TIME_FIELD"],
        conversion_mode="mapping",
        formula="x_0_5_time_value = K1_6_TIME_FIELD + 0",
        target_field="TIME_VALUE",
        unit=None,
        aliases=[],
        source="manual_review",
        target_protocol_type="X0_5",
        target_message_code="X0.5",
        concept_name="TIME_VALUE",
        formula_kind="python_expr",
    )

    generated = _knowledge_rule_to_generated_rule(rule)

    assert generated["source_protocol_type"] == "K1_6"
    assert generated["source_protocol_name"] == "K1_6"
    assert generated["source_message_code"] == "K1.6"
    assert generated["target_protocol_type"] == "X0_5"
    assert generated["target_message_code"] == "X0.5"
    assert generated["rule"].replace(" ", "") == "K1_6_TIME_FIELD+0"


def test_knowledge_rule_to_generated_rule_removes_self_referential_target_guard():
    rule = KnowledgeRule(
        protocol_type="X0_5",
        message_code="X0.5",
        field_name="X0_5_纬度",
        source_fields=["X0_5_纬度"],
        conversion_mode="mapping",
        formula=(
            "k_1_7_循环1_1_纬度1 = "
            "((X0_5_纬度 == k_1_7_循环1_1_纬度1) ? int(X0_5_纬度) : 0)"
        ),
        target_field="纬度1",
        unit=None,
        aliases=[],
        source="manual_review",
        target_protocol_type="K1_7",
        target_message_code="K1.7",
        concept_name="纬度1",
        formula_kind="python_expr",
    )

    generated = _knowledge_rule_to_generated_rule(rule)

    assert generated["rule"] == "int(X0_5_纬度)"
    assert "循环1" not in generated["rule"]
    assert "k_1_7" not in generated["rule"].lower()


def test_filter_valid_generated_rules_rejects_target_dependency_in_expression():
    rules, filtered = _filter_valid_generated_rules(
        generated_rules=[
            {
                "target_field": "纬度1",
                "target_protocol_type": "K1_7",
                "source_fields": ["X0_5_纬度"],
                "formula_kind": "python_expr",
                "rule": "X0_5_纬度 + k_1_7_循环1_1_纬度1",
            }
        ],
        available_source_fields=["X0_5_纬度"],
    )

    assert rules == []
    assert len(filtered) == 1
    assert "目标字段" in filtered[0]["filtered_reason"]


def test_apply_source_field_catalog_to_rules_enriches_graph_rule_bindings():
    rule = KnowledgeRule(
        protocol_type="K1_6",
        message_code="K1.6",
        field_name="K1_6_TIME_FIELD",
        source_fields=["K1_6_TIME_FIELD"],
        conversion_mode="mapping",
        formula="x_0_5_time_value = K1_6_TIME_FIELD + 0",
        target_field="TIME_VALUE",
        unit=None,
        aliases=[],
        source="manual_review",
        target_protocol_type="X0_5",
        target_message_code="X0.5",
        concept_name="TIME_VALUE",
        formula_kind="python_expr",
    )

    enriched = _apply_source_field_catalog_to_rules(
        [_knowledge_rule_to_generated_rule(rule)],
        source_field_catalog=[
            {
                "field_name": "K1_6_TIME_FIELD",
                "protocol": "K1_6",
                "message_code": "K1.6",
                "actual_field": "time_field",
                "display_field": "time_field",
                "source_path": "time_field",
            }
        ],
    )[0]

    assert enriched["source_protocol_type"] == "K1_6"
    assert enriched["source_protocol_name"] == "K1_6"
    assert enriched["source_message_code"] == "K1.6"
    assert enriched["source_actual_fields"] == ["time_field"]
    assert enriched["source_paths"] == ["time_field"]
    assert enriched["source_bindings"] == [
        {
            "alias_name": "K1_6_TIME_FIELD",
            "protocol": "K1_6",
            "message_code": "K1.6",
            "actual_field": "time_field",
            "display_field": "time_field",
            "source_path": "time_field",
        }
    ]


def test_build_generator_rules_payload_keeps_python_expr_mapping_rule_formula(tmp_path):
    protocol_dir = _prepare_protocol_dir(tmp_path)

    payload = build_generator_rules_payload(
        raw_rules={
            "normalized_rules": [
                {
                    "target_field": "TIME_VALUE",
                    "target_actual_field": "time_value",
                    "target_path": "time_value",
                    "source_fields": ["K1_6_TIME_FIELD"],
                    "source_bindings": [
                        {
                            "alias_name": "K1_6_TIME_FIELD",
                            "protocol": "K1_6",
                            "message_code": "K1.6",
                            "actual_field": "time_field",
                            "display_field": "time_field",
                            "source_path": "time_field",
                        }
                    ],
                    "source_actual_fields": ["time_field"],
                    "source_paths": ["time_field"],
                    "source_protocol_type": "K1_6",
                    "source_protocol_name": "K1_6",
                    "source_message_code": "K1.6",
                    "conversion_mode": "mapping",
                    "formula_kind": "python_expr",
                    "formula": "x_0_5_time_value = K1_6_TIME_FIELD + 0",
                    "rule": "x_0_5_time_value = K1_6_TIME_FIELD + 0",
                }
            ]
        },
        protocol_dir=protocol_dir,
        target_protocol_name="X0_5",
        preserve_display_names=True,
    )

    rule = payload["conversions"][0]["rules"][0]

    assert "?" not in rule["formula"]
    assert "==" not in rule["formula"]
    assert "time_field + 0" in rule["formula"]
    assert rule["source_actual_fields"] == ["k1_6.time_field"]
    assert rule["source_paths"] == ["time_field"]


def test_knowledge_base_normalize_rule_input_strips_protocol_prefixes(tmp_path):
    kb = ProtocolConversionKnowledgeBase(
        protocol_type="K1_6",
        payload={
            "protocol_type": "K1_6",
            "concepts": [],
            "field_nodes": [],
            "edges": [],
        },
        file_path=tmp_path / "kb.json",
    )

    rule = kb._normalize_rule_input(
        {
            "protocol_type": "K1_6",
            "target_protocol_type": "X0_5",
            "field_name": "K1_6_小时",
            "source_fields": ["K1_6_小时", "K1_6_分钟", "K1_6_秒"],
            "target_field": "x_0_5_时间1",
            "formula": "x_0_5_时间1 = (K1_6_小时 * 3600) + (K1_6_分钟 * 60) + K1_6_秒",
        }
    )

    assert rule is not None
    assert rule.field_name == "小时"
    assert rule.source_fields == ["小时", "分钟", "秒"]
    assert rule.target_field == "时间1"
    assert rule.formula == "(小时 * 3600) + (分钟 * 60) + 秒"


def test_expand_generated_rules_to_target_instances_falls_back_to_unique_field_match():
    expanded = _expand_generated_rules_to_target_instances(
        [
            {
                "target_field": "俯仰2",
                "target_path": "俯仰2",
                "source_fields": ["俯仰角"],
                "formula_kind": "python_expr",
                "rule": "俯仰角",
            }
        ],
        [
            {
                "field_name": "俯仰2",
                "label": "俯仰2",
                "target_path": "循环2_1/俯仰2",
                "actual_field": "u5faau73af2_1_u4fefu4ef02",
                "path_parts": ["循环2_1", "俯仰2"],
            }
        ],
    )

    assert len(expanded) == 1
    assert expanded[0]["target_field"] == "俯仰2"
    assert expanded[0]["target_path"] == "循环2_1/俯仰2"
    assert expanded[0]["target_actual_field"] == "u5faau73af2_1_u4fefu4ef02"
