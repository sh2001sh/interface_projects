from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from protocol_conversion.generator import (
    _build_deterministic_candidate_rules,
    _expand_generated_rules_to_target_instances,
    _filter_valid_generated_rules,
    _build_kg_writeback_payload,
    _normalize_formula_expression_syntax,
    _prune_ranked_source_candidates,
    _score_source_catalog_entry,
    _score_source_candidate,
    _validate_rule_semantic_alignment,
)


def test_kg_writeback_payload_assigns_default_confidence():
    payload = _build_kg_writeback_payload(
        generated_rules=[
            {
                "target_field": "威胁类型",
                "source_fields": ["威胁类型"],
                "conversion_mode": "transcoding",
                "formula_kind": "python_expr",
                "rule": "威胁类型",
                "source": "llm_generated",
                "status": "candidate",
            }
        ],
        source_protocol={"protocol_type": "K1.6", "message_code": "K1.6"},
        target_protocol={"protocol_type": "K1.7", "message_code": "K1.7"},
    )

    rules = payload["rules"]
    assert len(rules) == 1
    assert isinstance(rules[0]["confidence"], float)
    assert 0.0 < rules[0]["confidence"] <= 1.0


def test_control_field_cannot_map_to_business_field():
    target_spec = {
        "field_name": "威胁类型",
        "label": "威胁类型",
        "description": "业务字段",
        "path_parts": ["消息", "威胁类型"],
    }
    assert _score_source_candidate(target_spec, "FPI11") == 0.0

    valid, reason = _validate_rule_semantic_alignment(
        {
            "target_field": "威胁类型",
            "source_fields": ["FPI11"],
            "rule": "FPI11",
            "formula_kind": "python_expr",
        },
        target_spec_map={"威胁类型": target_spec},
        candidate_map={"威胁类型": {"FPI11"}},
    )

    assert valid is False
    assert "控制位字段" in str(reason)


def test_numbered_target_prefers_exact_numbered_source_candidate():
    target_spec = {
        "field_name": "经度1",
        "label": "经度1",
        "description": "循环目标经度1",
        "path_parts": ["循环1_1", "经度1"],
    }
    generic_source = {
        "field_name": "K1_6_经度",
        "display_field": "经度",
        "actual_field": "u5206u652f4_u7ecfu5ea6",
        "source_path": "分支4/经度",
    }
    exact_source = {
        "field_name": "K1_6_经度1",
        "display_field": "经度1",
        "actual_field": "u5206u652f8_u7ecfu5ea61",
        "source_path": "分支8/经度1",
    }

    generic_score = _score_source_catalog_entry(target_spec, generic_source)
    exact_score = _score_source_catalog_entry(target_spec, exact_source)

    assert exact_score > generic_score

    ranked = sorted(
        [
            {**generic_source, "score": generic_score},
            {**exact_source, "score": exact_score},
        ],
        key=lambda item: (-float(item["score"]), item["field_name"]),
    )
    pruned = _prune_ranked_source_candidates(target_spec, ranked, top_k=2)
    assert len(pruned) == 1
    assert pruned[0]["field_name"] == "K1_6_经度1"


def test_time_target_prefers_exact_bit_length_source_candidate():
    target_spec = {
        "field_name": "飞临时间",
        "label": "飞临时间",
        "description": "K1.6::飞临时间",
        "path_parts": ["分支1", "飞临时间"],
        "bit_length": 5,
    }
    exact_source = {
        "field_name": "X0_5_时间1",
        "display_field": "时间1",
        "actual_field": "origin_u65f6u95f41",
        "source_path": "时间1",
        "bit_length": 5,
    }
    fuzzy_source = {
        "field_name": "X0_5_时间2",
        "display_field": "时间2",
        "actual_field": "origin_u65f6u95f42",
        "source_path": "时间2",
        "bit_length": 6,
    }

    exact_score = _score_source_catalog_entry(target_spec, exact_source)
    fuzzy_score = _score_source_catalog_entry(target_spec, fuzzy_source)

    assert exact_score > fuzzy_score


def test_deterministic_rule_builder_generates_direct_rule_for_exact_match():
    tasks = [
        {
            "field_name": "经度1",
            "label": "经度1",
            "description": "循环目标经度1",
            "path_parts": ["循环1_1", "经度1"],
            "candidate_source_fields": [
                {
                    "field_name": "K1_6_经度1",
                    "display_field": "经度1",
                    "actual_field": "u5206u652f8_u7ecfu5ea61",
                    "source_path": "分支8/经度1",
                    "score": 118.0,
                }
            ],
        }
    ]

    rules = _build_deterministic_candidate_rules(tasks)
    assert len(rules) == 1
    assert rules[0]["target_field"] == "经度1"
    assert rules[0]["source_fields"] == ["K1_6_经度1"]
    assert rules[0]["rule"] == "K1_6_经度1"
    assert rules[0]["source"] == "deterministic_match"


def test_deterministic_rule_builder_accepts_semantic_single_candidate_match():
    tasks = [
        {
            "field_name": "高度1",
            "label": "高度1",
            "description": "循环目标高度1",
            "path_parts": ["循环1_1", "高度1"],
            "candidate_source_fields": [
                {
                    "field_name": "K1_6_高程1",
                    "display_field": "高程1",
                    "actual_field": "u5206u652f8_u5206u652f9_u9ad8u7a0b1",
                    "source_path": "分支8/分支9/高程1",
                    "score": 77.3333,
                }
            ],
        },
        {
            "field_name": "数量",
            "label": "数量",
            "description": "目标数量",
            "path_parts": ["数量"],
            "candidate_source_fields": [
                {
                    "field_name": "K1_6_目标数量",
                    "display_field": "目标数量",
                    "actual_field": "u5206u652f3_u76eeu6807u6570u91cf",
                    "source_path": "分支3/目标数量",
                    "score": 75.3333,
                }
            ],
        },
    ]

    rules = _build_deterministic_candidate_rules(tasks)
    assert [rule["target_field"] for rule in rules] == ["高度1", "数量"]
    assert [rule["source_fields"][0] for rule in rules] == ["K1_6_高程1", "K1_6_目标数量"]


def test_deterministic_rule_builder_accepts_time_single_candidate_with_same_bit_length():
    tasks = [
        {
            "field_name": "飞临时间",
            "label": "飞临时间",
            "description": "K1.6::飞临时间",
            "path_parts": ["分支1", "飞临时间"],
            "bit_length": 5,
            "candidate_source_fields": [
                {
                    "field_name": "X0_5_时间1",
                    "display_field": "时间1",
                    "actual_field": "origin_u65f6u95f41",
                    "source_path": "时间1",
                    "bit_length": 5,
                    "score": 78.0,
                }
            ],
        }
    ]

    rules = _build_deterministic_candidate_rules(tasks)
    assert len(rules) == 1
    assert rules[0]["target_field"] == "飞临时间"
    assert rules[0]["source_fields"] == ["X0_5_时间1"]
    assert rules[0]["rule"] == "X0_5_时间1"


def test_filter_valid_generated_rules_rejects_target_dependent_formula():
    rules, filtered = _filter_valid_generated_rules(
        generated_rules=[
            {
                "target_field": "高程",
                "source_fields": ["X0_5_高度"],
                "formula_kind": "python_expr",
                "rule": "(X0_5_高度 == 高程 ? 1=20 : 0)",
            }
        ],
        available_source_fields=["X0_5_高度"],
        target_tasks=[
            {
                "field_name": "高程",
                "label": "高程",
                "description": "K1.6::高程",
                "path_parts": ["分支5", "高程"],
                "candidate_source_fields": [
                    {
                        "field_name": "X0_5_高度",
                        "display_field": "高度",
                        "bit_length": 16,
                        "score": 60.0,
                    }
                ],
            }
        ],
    )

    assert rules == []
    assert len(filtered) == 1
    assert "目标字段" in filtered[0]["filtered_reason"]


def test_normalize_formula_expression_syntax_rewrites_c_style_ternary_to_python() -> None:
    normalized = _normalize_formula_expression_syntax(
        "(K1_7_俯仰1 == 5 ? 5 : (K1_7_俯仰1 == 6 ? 6 : 0))"
    )

    assert normalized == "(5 if K1_7_俯仰1 == 5 else (6 if K1_7_俯仰1 == 6 else 0))"


def test_prune_ranked_source_candidates_keeps_cross_protocol_preferred_candidates() -> None:
    target_spec = {
        "field_name": "融合字段",
        "label": "融合字段",
        "description": "需要跨协议联合",
        "path_parts": ["融合字段"],
        "preferred_source_candidates": [
            {
                "field_name": "K1_6_小时",
                "source_protocol_type": "K1_6",
                "source_message_code": "K1.6",
            },
            {
                "field_name": "K1_7_俯仰1",
                "source_protocol_type": "K1_7",
                "source_message_code": "K1.7",
            },
        ],
    }
    ranked = [
        {
            "field_name": "K1_6_小时",
            "display_field": "小时",
            "source_protocol_type": "K1_6",
            "source_message_code": "K1.6",
            "score": 118.0,
        },
        {
            "field_name": "K1_7_俯仰1",
            "display_field": "俯仰1",
            "source_protocol_type": "K1_7",
            "source_message_code": "K1.7",
            "score": 97.0,
        },
    ]

    pruned = _prune_ranked_source_candidates(target_spec, ranked, top_k=5)

    assert [item["field_name"] for item in pruned] == ["K1_6_小时", "K1_7_俯仰1"]


def test_filter_valid_generated_rules_rejects_time_component_direct_copy_for_aggregate_time() -> None:
    rules, filtered = _filter_valid_generated_rules(
        generated_rules=[
            {
                "target_field": "时间1",
                "source_fields": ["K1_6_小时"],
                "formula_kind": "python_expr",
                "rule": "K1_6_小时",
            }
        ],
        available_source_fields=["K1_6_小时", "K1_6_分钟", "K1_6_秒"],
        target_tasks=[
            {
                "field_name": "时间1",
                "label": "时间1",
                "description": "X0.5::时间1",
                "path_parts": ["时间1"],
                "candidate_source_fields": [
                    {"field_name": "K1_6_小时", "display_field": "小时", "score": 84.0},
                    {"field_name": "K1_6_分钟", "display_field": "分钟", "score": 72.0},
                    {"field_name": "K1_6_秒", "display_field": "秒", "score": 72.0},
                ],
            }
        ],
    )

    assert rules == []
    assert len(filtered) == 1
    assert filtered[0]["filtered_reason"] == "时间聚合字段不能直接映射到单个时间分量"


def test_filter_valid_generated_rules_rejects_f_string_formula() -> None:
    rules, filtered = _filter_valid_generated_rules(
        generated_rules=[
            {
                "target_field": "时间1",
                "source_fields": ["K1_6_小时", "K1_6_分钟", "K1_6_秒"],
                "formula_kind": "python_expr",
                "rule": "f'{K1_6_小时:02d}:{K1_6_分钟:02d}:{K1_6_秒:02d}'",
            }
        ],
        available_source_fields=["K1_6_小时", "K1_6_分钟", "K1_6_秒"],
        target_tasks=[
            {
                "field_name": "时间1",
                "label": "时间1",
                "description": "X0.5::时间1",
                "path_parts": ["时间1"],
                "candidate_source_fields": [
                    {"field_name": "K1_6_小时", "display_field": "小时", "score": 84.0},
                    {"field_name": "K1_6_分钟", "display_field": "分钟", "score": 72.0},
                    {"field_name": "K1_6_秒", "display_field": "秒", "score": 72.0},
                ],
            }
        ],
    )

    assert rules == []
    assert len(filtered) == 1
    assert filtered[0]["filtered_reason"] == "公式不能使用字符串模板或 f-string"


def test_filter_valid_generated_rules_rejects_ambiguous_time_constant_selector_formula():
    rules, filtered = _filter_valid_generated_rules(
        generated_rules=[
            {
                "target_field": "秒",
                "source_fields": ["X0_5_时间1", "X0_5_时间2", "X0_5_时间3", "X0_5_时间4"],
                "formula_kind": "python_expr",
                "rule": "(5 if X0_5_时间1 == 1 else (6 if X0_5_时间2 == 1 else 1))",
            }
        ],
        available_source_fields=["X0_5_时间1", "X0_5_时间2", "X0_5_时间3", "X0_5_时间4"],
        target_tasks=[
            {
                "field_name": "秒",
                "label": "秒",
                "description": "K1.6::秒",
                "path_parts": ["分支7", "秒"],
                "candidate_source_fields": [
                    {"field_name": "X0_5_时间1", "display_field": "时间1", "score": 32.0},
                    {"field_name": "X0_5_时间2", "display_field": "时间2", "score": 32.0},
                    {"field_name": "X0_5_时间3", "display_field": "时间3", "score": 32.0},
                    {"field_name": "X0_5_时间4", "display_field": "时间4", "score": 32.0},
                ],
            }
        ],
    )

    assert rules == []
    assert len(filtered) == 1
    assert "时间类字段候选歧义过高" in str(filtered[0]["filtered_reason"])


def test_expand_generated_rules_reuses_loop_formula_for_all_instances():
    generated_rules = [
        {
            "target_field": "经度1",
            "source_fields": ["K1_6_经度1"],
            "conversion_mode": "transcoding",
            "formula_kind": "python_expr",
            "rule": "K1_6_经度1",
        }
    ]
    required_target_fields = [
        {
            "field_name": "经度1",
            "actual_field": "loop_1_lon_1",
            "target_path": "循环1_1/经度1",
        },
        {
            "field_name": "经度1",
            "actual_field": "loop_1_lon_2",
            "target_path": "循环1_2/经度1",
        },
    ]

    expanded = _expand_generated_rules_to_target_instances(generated_rules, required_target_fields)
    assert len(expanded) == 2
    assert {item["target_actual_field"] for item in expanded} == {"loop_1_lon_1", "loop_1_lon_2"}
    assert {item["rule"] for item in expanded} == {"K1_6_经度1"}
