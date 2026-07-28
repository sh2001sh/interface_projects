from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from code_generation_adapter import (  # noqa: E402
    _build_choreography_payload,
    _normalize_formula_for_response,
    _normalize_manifest_for_response,
    _normalize_conversion_rules_payload,
    _normalize_relations_rules_payload,
    _normalize_formula_for_generator,
    resolve_protocol_field_specs,
    resolve_protocol_type_names,
    _normalize_protocol_names,
    build_code_generation_payload,
    build_generator_rules_payload,
    normalize_port_config,
)
from project_generator.loaders import _normalize_legacy_expression  # noqa: E402


class CodeGenerationAdapterTest(unittest.TestCase):
    """Covers code-generation adapter compatibility cases."""

    def test_normalize_formula_for_response_rewrites_generator_ternary_to_python(self) -> None:
        normalized = _normalize_formula_for_response(
            "((k1_7.pitch1 != 0) ? k1_7.pitch1 : ((k1_7.pitch2 != 0) ? k1_7.pitch2 : 0))"
        )

        self.assertEqual(
            normalized,
            "(k1_7.pitch1 if k1_7.pitch1 != 0 else (k1_7.pitch2 if k1_7.pitch2 != 0 else 0))",
        )

    def test_normalize_manifest_for_response_only_changes_formula_display(self) -> None:
        manifest = {
            "conversions": [
                {
                    "name": "demo",
                    "rules": [
                        {
                            "formula": "((src.a != 0) ? src.a : src.b)",
                            "when": "((src.flag == 1) && !(src.mask == 0))",
                            "target_field": "dst.c",
                        }
                    ],
                }
            ],
            "runtime": {
                "transport": {"message_type": "joint_bundle"},
                "protocol_verifies": [
                    {
                        "constraints": [
                            {"check": "((value.a == 1) && !(value.b == 0))"},
                        ]
                    }
                ],
            },
            "protocols": [
                {
                    "nodes": [
                        {"condition": "!0"},
                    ]
                }
            ],
        }

        normalized = _normalize_manifest_for_response(manifest)

        self.assertEqual(
            normalized["conversions"][0]["rules"][0]["formula"],
            "(src.a if src.a != 0 else src.b)",
        )
        self.assertEqual(
            normalized["conversions"][0]["rules"][0]["when"],
            "(src.flag == 1) and not (src.mask == 0)",
        )
        self.assertEqual(
            normalized["runtime"]["protocol_verifies"][0]["constraints"][0]["check"],
            "(value.a == 1) and not (value.b == 0)",
        )
        self.assertEqual(
            normalized["protocols"][0]["nodes"][0]["condition"],
            "not 0",
        )
        self.assertEqual(
            manifest["conversions"][0]["rules"][0]["formula"],
            "((src.a != 0) ? src.a : src.b)",
        )

    def test_normalize_protocol_names_excludes_route_descriptor_parent_xml(self) -> None:
        protocol_dir = Path(self.id().replace(".", "_"))

        with patch("code_generation_adapter.load_protocols") as mocked_load_protocols:
            mocked_load_protocols.return_value = [
                type(
                    "Protocol",
                    (),
                    {
                        "type_name": "Route_Header",
                        "file_stem": "route_header",
                        "source_path": protocol_dir / "route_header.xml",
                        "routes": [type("Route", (), {"target_protocol": "K1.6"})()],
                    },
                )(),
                type(
                    "Protocol",
                    (),
                    {
                        "type_name": "K1_6",
                        "file_stem": "k1_6",
                        "source_path": protocol_dir / "k1.6.xml",
                        "routes": [],
                    },
                )(),
                type(
                    "Protocol",
                    (),
                    {
                        "type_name": "K1_7",
                        "file_stem": "k1_7",
                        "source_path": protocol_dir / "k1.7.xml",
                        "routes": [],
                    },
                )(),
            ]

            protocol_names = _normalize_protocol_names(protocol_dir)

        self.assertEqual(protocol_names, ["K1_6", "K1_7"])

    def test_resolve_protocol_specs_strip_copy_suffix_from_xml_filename(self) -> None:
        protocol_dir = Path(self.id().replace(".", "_"))
        protocol_dir.mkdir(parents=True, exist_ok=True)
        (protocol_dir / "X.xml").write_text(
            """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="说明字段">8</Item>\n</NameSpace>\n""",
            encoding="utf-8",
        )
        (protocol_dir / "X0.5 - 副本.xml").write_text(
            """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="高度">16</Item>\n</NameSpace>\n""",
            encoding="utf-8",
        )

        protocol_names = resolve_protocol_type_names([str(protocol_dir)], "source_protocol_dirs")
        field_specs = resolve_protocol_field_specs([str(protocol_dir)], "source_protocol_dirs")

        self.assertEqual(protocol_names, ["X", "X0_5"])
        self.assertIn("X0_5", {item["protocol"] for item in field_specs})
        self.assertIn("高度", {item["field_name"] for item in field_specs if item["protocol"] == "X0_5"})

    def test_normalize_port_config_auto_generates_one_send_endpoint_per_target(self) -> None:
        normalized = normalize_port_config(
            {
                "recvIp": "127.0.0.1",
                "recvPort": 4300,
                "sendIp": "127.0.0.1",
                "sendPort": 5300,
                "messageRuleDetailList": [
                    {"messageName": "X0.5"},
                    {"messageName": "K1.6"},
                    {"messageName": "K1.7"},
                ],
            },
            conversions=[
                {
                    "sources": [{"alias": "x0_5", "protocol": "X0_5"}],
                    "target": {"protocol": "K1_6"},
                },
                {
                    "sources": [{"alias": "x0_5", "protocol": "X0_5"}],
                    "target": {"protocol": "K1_7"},
                },
            ],
        )

        endpoints = normalized["endpoints"]
        self.assertEqual(len(endpoints), 3)
        self.assertEqual(endpoints[0]["name"], "x0_5")
        self.assertEqual(endpoints[1]["name"], "k1_6")
        self.assertEqual(endpoints[1]["port"], 5300)
        self.assertEqual(endpoints[2]["name"], "k1_7")
        self.assertEqual(endpoints[2]["port"], 5301)

    def test_build_choreography_payload_auto_derives_joint_groups_when_missing_matrix(self) -> None:
        payload = _build_choreography_payload(
            None,
            mappings_payload={
                "project_name": "demo_project",
                "conversions": [
                    {
                        "name": "K1.7_K1.6_to_X0.5",
                        "mode": "joint",
                        "sources": [
                            {"alias": "k1_7", "protocol": "K1_7"},
                            {"alias": "k1_6", "protocol": "K1_6"},
                        ],
                        "target": {"protocol": "X0_5"},
                    }
                ],
            },
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["mode"], "joint")
        self.assertEqual(len(payload["sources"]), 2)
        self.assertEqual(payload["targets"][0]["protocol"], "X0_5")
        self.assertEqual(payload["joint_groups"][0]["sources"], ["k1_7", "k1_6"])
        self.assertEqual(payload["joint_groups"][0]["matrix"]["values"], [[0, 1], [1, 0]])

    def test_build_code_generation_payload_reports_qt_generation_time(self) -> None:
        temp_root = Path(self.id().replace(".", "_"))
        protocol_dir = temp_root / "protocols"
        output_dir = temp_root / "generated"
        protocol_dir.mkdir(parents=True, exist_ok=True)

        def fake_render_project(output_path, _protocols, _mappings, _choreography):
            output_path.mkdir(parents=True, exist_ok=True)
            (output_path / "demo_to_x.cpp").write_text("// generated\n", encoding="utf-8")
            (output_path / "protocol_manifest.json").write_text("{}", encoding="utf-8")

        with (
            patch("code_generation_adapter._materialize_protocol_dirs", return_value=(protocol_dir, None)),
            patch(
                "code_generation_adapter.build_runtime_mappings_payload",
                return_value={"project_name": "demo", "conversions": [{"mode": "simple"}]},
            ),
            patch("code_generation_adapter._build_choreography_payload", return_value=None),
            patch("code_generation_adapter.load_protocols", return_value=[object()]),
            patch("code_generation_adapter.load_mappings", return_value=object()),
            patch("code_generation_adapter.render_project", side_effect=fake_render_project),
            patch("code_generation_adapter._validate_generated_cpp_syntax", return_value={"passed": True}),
        ):
            result = build_code_generation_payload(
                source_protocol_dir="source",
                target_protocol_dir="target",
                conversion_rules_json="rules.json",
                conversion_matrix_json=None,
                port_config_json="ports.json",
                output_dir=output_dir,
            )

        self.assertIn("qt_project_generation_time_ms", result["summary"])
        self.assertGreaterEqual(result["summary"]["qt_project_generation_time_ms"], 0.0)
        self.assertTrue(result["summary"]["qt_project_generation_time_display"].endswith("ms"))

    def test_relations_skip_abstract_conversions_without_matching_xml_protocols(self) -> None:
        raw_rules = {
            "version": "1.0",
            "project_name": "demo",
            "relations": [
                {
                    "relation_id": "K1.7_K1.6_to_X0.5",
                    "source_protocols": ["K1_7", "K1_6"],
                    "target_protocol": "X0_5",
                    "rules": [
                        {
                            "target_field": "origin_u65f6u95f41",
                            "formula": "k1_6.origin_u5c0fu65f6",
                            "source_fields": ["K1_6_小时"],
                            "source_actual_fields": ["k1_6.origin_u5c0fu65f6"],
                            "source_paths": ["小时"],
                        }
                    ],
                },
                {
                    "relation_id": "K_to_X",
                    "source_protocols": ["K"],
                    "target_protocol": "X",
                    "rules": [
                        {
                            "target_field": "origin_u6d88u606fu6807u8bc6",
                            "formula": "int(k.消息标识)",
                            "source_fields": ["k.消息标识"],
                            "source_actual_fields": ["k.origin_u6d88u606fu6807u8bc6"],
                            "source_paths": ["消息标识"],
                        }
                    ],
                },
            ],
        }

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "K1_6":
                return (
                    {
                        "origin_u5c0fu65f6": {
                            "actual_field": "origin_u5c0fu65f6",
                            "path_parts": ["小时"],
                        }
                    },
                    {"小时": "origin_u5c0fu65f6"},
                )
            if protocol_name == "X0_5":
                return (
                    {
                        "origin_u65f6u95f41": {
                            "actual_field": "origin_u65f6u95f41",
                            "path_parts": ["时间1"],
                        }
                    },
                    {"时间1": "origin_u65f6u95f41"},
                )
            return ({}, {})

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["K1_7", "K1_6", "X0_5"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "时间1": [("X0_5", "origin_u65f6u95f41")],
                    "小时": [("K1_6", "origin_u5c0fu65f6")],
                },
            ),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            payload = build_generator_rules_payload(raw_rules, protocol_dir=Path("/tmp/fake_protocol_dir"))

        conversions = payload.get("conversions") or []
        self.assertEqual(len(conversions), 1)
        self.assertEqual(conversions[0]["name"], "K1.7_K1.6_to_X0.5")

    def test_relations_drop_conversions_that_end_up_with_empty_rules(self) -> None:
        raw_rules = {
            "version": "1.0",
            "project_name": "demo",
            "relations": [
                {
                    "relation_id": "K1.7_to_X0.5",
                    "source_protocols": ["K1_7"],
                    "target_protocol": "X0_5",
                    "rules": [
                        {
                            "target_field": "x_0_5_时间1",
                            "target_actual_field": "origin_u65f6u95f41",
                            "target_path": "时间1",
                            "formula": "k1_7.origin_u5c0fu65f6",
                            "source_fields": ["k1_7.小时"],
                            "source_actual_fields": ["k1_7.origin_u5c0fu65f6"],
                            "source_paths": ["小时"],
                        }
                    ],
                }
            ],
        }

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["K1_7"]),
            patch("code_generation_adapter._protocol_field_index", return_value={}),
            patch("code_generation_adapter._build_protocol_spec_maps", return_value=({}, {})),
        ):
            with self.assertRaisesRegex(ValueError, "没有与当前 source/target XML 匹配的可生成转换关系"):
                build_generator_rules_payload(raw_rules, protocol_dir=Path("/tmp/fake_protocol_dir"))

    def test_relations_strip_target_protocol_prefix_from_target_field(self) -> None:
        raw_rules = {
            "version": "1.0",
            "project_name": "demo",
            "relations": [
                {
                    "relation_id": "K1.7_to_X0.5",
                    "source_protocols": ["K1_7"],
                    "target_protocol": "X0_5",
                    "rules": [
                        {
                            "target_field": "x_0_5_时间1",
                            "formula": "k1_7_时间1",
                            "source_fields": ["k1_7_时间1"],
                        }
                    ],
                }
            ],
        }

        normalized = _normalize_relations_rules_payload(raw_rules)
        conversion = normalized["conversions"][0]
        self.assertEqual(conversion["rules"][0]["target_field"], "时间1")

    def test_relations_strip_legacy_target_assignment_formula(self) -> None:
        raw_rules = {
            "version": "1.0",
            "project_name": "demo",
            "relations": [
                {
                    "relation_id": "K1.7_to_X0.5",
                    "source_protocols": ["K1_7"],
                    "target_protocol": "X0_5",
                    "rules": [
                        {
                            "target_field": "x_0_5_俯仰角",
                            "target_actual_field": "continue3_u4fefu4ef0u89d2",
                            "target_path": "俯仰角",
                            "formula": (
                                "if k1_7_俯仰1 == 5:\n"
                                "    x0_5.X0_5_俯仰角 = 13\n"
                                "else:\n"
                                "    x_0_5_俯仰角 = k1_7_俯仰1"
                            ),
                            "source_fields": ["k1_7_俯仰1"],
                        }
                    ],
                }
            ],
        }

        normalized = _normalize_relations_rules_payload(raw_rules)
        formula = normalized["conversions"][0]["rules"][0]["formula"]
        self.assertNotIn("x0_5.X0_5_俯仰角", formula)
        self.assertNotIn("x_0_5_俯仰角 =", formula)
        self.assertIn("?", formula)

    def test_relations_fallback_to_target_field_when_legacy_target_actual_field_mismatches(self) -> None:
        raw_rules = {
            "version": "1.0",
            "project_name": "demo",
            "relations": [
                {
                    "relation_id": "X0.5_to_K1.7",
                    "source_protocols": ["X0_5"],
                    "target_protocol": "K1_7",
                    "rules": [
                        {
                            "target_field": "经度1",
                            "target_actual_field": "origin_u7ecfu5ea61",
                            "target_path": "经度1",
                            "formula": "int(x0_5.经度) if x0_5.经度 is not None else 1",
                            "source_fields": ["x0_5.经度"],
                            "source_actual_fields": ["x0_5.prolong_u7ecfu5ea6"],
                            "source_paths": ["经度"],
                        }
                    ],
                }
            ],
        }

        relation_payload = _normalize_relations_rules_payload(raw_rules)

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "X0_5":
                return (
                    {
                        "prolong_u7ecfu5ea6": {
                            "actual_field": "prolong_u7ecfu5ea6",
                            "path_parts": ["经度"],
                        }
                    },
                    {"经度": "prolong_u7ecfu5ea6"},
                )
            if protocol_name == "K1_7":
                return (
                    {
                        "origin_u5faau73af1_u7ecfu5ea61": {
                            "actual_field": "origin_u5faau73af1_u7ecfu5ea61",
                            "path_parts": ["循环1_1", "经度1"],
                        }
                    },
                    {"循环1_1/经度1": "origin_u5faau73af1_u7ecfu5ea61"},
                )
            return ({}, {})

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["X0_5", "K1_7"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "经度": [("X0_5", "prolong_u7ecfu5ea6")],
                    "经度1": [("K1_7", "origin_u5faau73af1_u7ecfu5ea61")],
                },
            ),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            payload = _normalize_conversion_rules_payload(
                relation_payload,
                protocol_dir=Path("/tmp/fake_protocol_dir"),
            )

        rules = payload["conversions"][0]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["target_field"], "origin_u5faau73af1_u7ecfu5ea61")
        self.assertEqual(rules[0]["target_actual_field"], "origin_u5faau73af1_u7ecfu5ea61")
        self.assertIn("x0_5.prolong_u7ecfu5ea6", rules[0]["formula"])

    def test_relations_compound_source_prefix_maps_to_actual_source_field(self) -> None:
        raw_rules = {
            "version": "1.0",
            "project_name": "demo",
            "relations": [
                {
                    "relation_id": "K1.7_K1.6_to_X0.5",
                    "source_protocols": ["K1_7", "K1_6"],
                    "target_protocol": "X0_5",
                    "rules": [
                        {
                            "target_field": "x_0_5_纬度",
                            "target_actual_field": "prolong_u7eacu5ea6",
                            "target_path": "纬度",
                            "formula": "x_0_5_纬度 = 16 if k1_7_k1_6_纬度 == 16 else (15 if k1_7_k1_6_纬度 == 15 else 0)",
                            "source_fields": ["k1_7_k1_6_纬度"],
                            "source_actual_fields": ["k1_7_k1_6.纬度"],
                            "source_paths": ["纬度"],
                        }
                    ],
                }
            ],
        }

        relation_payload = _normalize_relations_rules_payload(raw_rules)
        relation_rule = relation_payload["conversions"][0]["rules"][0]
        self.assertEqual(relation_rule["source_fields"], ["k1_6.纬度"])
        self.assertIn("k1_6.纬度", relation_rule["formula"])

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "K1_6":
                return (
                    {
                        "origin_u5206u652f4_u7eacu5ea6": {
                            "actual_field": "origin_u5206u652f4_u7eacu5ea6",
                            "path_parts": ["纬度"],
                        }
                    },
                    {"纬度": "origin_u5206u652f4_u7eacu5ea6"},
                )
            if protocol_name == "X0_5":
                return (
                    {
                        "prolong_u7eacu5ea6": {
                            "actual_field": "prolong_u7eacu5ea6",
                            "path_parts": ["纬度"],
                        }
                    },
                    {"纬度": "prolong_u7eacu5ea6"},
                )
            return ({}, {})

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["K1_7", "K1_6", "X0_5"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "纬度": [
                        ("K1_6", "origin_u5206u652f4_u7eacu5ea6"),
                        ("X0_5", "prolong_u7eacu5ea6"),
                    ],
                },
            ),
            patch(
                "code_generation_adapter._build_target_protocol_field_lookup",
                return_value=({"纬度": "prolong_u7eacu5ea6"}, {"prolong_u7eacu5ea6"}, set()),
            ),
            patch("code_generation_adapter._build_target_concept_spec_lookup", return_value={}),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            payload = _normalize_conversion_rules_payload(
                relation_payload,
                protocol_dir=Path("/tmp/fake_protocol_dir"),
            )

        rules = payload["conversions"][0]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["source_fields"], ["k1_6.origin_u5206u652f4_u7eacu5ea6"])
        self.assertEqual(rules[0]["target_field"], "prolong_u7eacu5ea6")
        self.assertEqual(rules[0]["target_actual_field"], "prolong_u7eacu5ea6")
        self.assertIn("k1_6.origin_u5206u652f4_u7eacu5ea6", rules[0]["formula"])

    def test_conversion_payload_maps_legacy_loop_target_actual_names_to_slot_fields(self) -> None:
        payload = {
            "version": "1.0",
            "project_name": "demo",
            "conversions": [
                {
                    "name": "X0_5_to_K1_7",
                    "mode": "simple",
                    "sources": [{"alias": "x0_5", "protocol": "X0_5"}],
                    "target": {"protocol": "K1_7"},
                    "rules": [
                        {
                            "target_field": "origin_u5faau73af1_u7ecfu5ea61",
                            "formula": "x0_5.prolong_u7ecfu5ea6",
                            "source_fields": ["x0_5.prolong_u7ecfu5ea6"],
                            "rule_type": "direct",
                        }
                    ],
                }
            ],
        }

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "X0_5":
                return (
                    {
                        "prolong_u7ecfu5ea6": {
                            "actual_field": "prolong_u7ecfu5ea6",
                            "path_parts": ["经度"],
                        }
                    },
                    {"经度": "prolong_u7ecfu5ea6"},
                )
            if protocol_name == "K1_7":
                return (
                    {
                        "u5faau73af1_1_u7ecfu5ea61": {
                            "actual_field": "u5faau73af1_1_u7ecfu5ea61",
                            "field_name": "经度1",
                            "label": "经度1",
                            "path_parts": ["循环1_1", "经度1"],
                        }
                    },
                    {"循环1_1/经度1": "u5faau73af1_1_u7ecfu5ea61"},
                )
            return ({}, {})

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["X0_5", "K1_7"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "经度": [("X0_5", "prolong_u7ecfu5ea6")],
                    "经度1": [("K1_7", "u5faau73af1_1_u7ecfu5ea61")],
                },
            ),
            patch("code_generation_adapter.resolve_protocol_field_specs", side_effect=lambda *_args, **_kwargs: [
                {
                    "protocol": "K1_7",
                    "field_name": "经度1",
                    "actual_field": "u5faau73af1_1_u7ecfu5ea61",
                    "label": "经度1",
                    "path_parts": ["循环1_1", "经度1"],
                },
                {
                    "protocol": "X0_5",
                    "field_name": "经度",
                    "actual_field": "prolong_u7ecfu5ea6",
                    "label": "经度",
                    "path_parts": ["经度"],
                },
            ]),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            normalized = _normalize_conversion_rules_payload(payload, protocol_dir=Path("/tmp/fake_protocol_dir"))

        rules = normalized["conversions"][0]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["target_field"], "u5faau73af1_1_u7ecfu5ea61")
        self.assertEqual(rules[0]["target_actual_field"], "u5faau73af1_1_u7ecfu5ea61")

    def test_relations_resolve_protocol_prefixed_hierarchical_target_fields(self) -> None:
        raw_rules = {
            "version": "1.0",
            "project_name": "demo",
            "relations": [
                {
                    "relation_id": "X0.5_to_K5.1",
                    "source_protocols": ["X0_5"],
                    "target_protocol": "K5_1",
                    "rules": [
                        {
                            "target_field": "k_5_1_循环1_1_循环2_1_分支1_高程",
                            "formula": "k_5_1_循环1_1_循环2_1_分支1_高程 = x0_5_高度",
                            "source_fields": ["x0_5_高度"],
                            "source_actual_fields": ["x0_5.prolong_u9ad8u5ea6"],
                            "source_paths": ["高度"],
                        }
                    ],
                },
                {
                    "relation_id": "X0.5_to_K1.7",
                    "source_protocols": ["X0_5"],
                    "target_protocol": "K1_7",
                    "rules": [
                        {
                            "target_field": "k_1_7_循环2_1_俯仰2",
                            "formula": "k_1_7_循环2_1_俯仰2 = x0_5_俯仰角",
                            "source_fields": ["x0_5_俯仰角"],
                            "source_actual_fields": ["x0_5.continue3_u4fefu4ef0u89d2"],
                            "source_paths": ["俯仰角"],
                        }
                    ],
                },
            ],
        }

        relation_payload = _normalize_relations_rules_payload(raw_rules)

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "X0_5":
                return (
                    {
                        "prolong_u9ad8u5ea6": {
                            "actual_field": "prolong_u9ad8u5ea6",
                            "path_parts": ["高度"],
                        },
                        "continue3_u4fefu4ef0u89d2": {
                            "actual_field": "continue3_u4fefu4ef0u89d2",
                            "path_parts": ["俯仰角"],
                        },
                    },
                    {
                        "高度": "prolong_u9ad8u5ea6",
                        "俯仰角": "continue3_u4fefu4ef0u89d2",
                    },
                )
            if protocol_name == "K5_1":
                return (
                    {
                        "u5faau73af1_1_u5faau73af2_1_u5206u652f1_u9ad8u7a0b": {
                            "actual_field": "u5faau73af1_1_u5faau73af2_1_u5206u652f1_u9ad8u7a0b",
                            "field_name": "高程",
                            "label": "高程",
                            "path_parts": ["循环1_1", "循环2_1", "分支1", "高程"],
                        }
                    },
                    {
                        "循环1_1/循环2_1/分支1/高程": "u5faau73af1_1_u5faau73af2_1_u5206u652f1_u9ad8u7a0b",
                    },
                )
            if protocol_name == "K1_7":
                return (
                    {
                        "u5faau73af2_1_u4fefu4ef02": {
                            "actual_field": "u5faau73af2_1_u4fefu4ef02",
                            "field_name": "俯仰2",
                            "label": "俯仰2",
                            "path_parts": ["循环2_1", "俯仰2"],
                        }
                    },
                    {
                        "循环2_1/俯仰2": "u5faau73af2_1_u4fefu4ef02",
                    },
                )
            return ({}, {})

        protocol_specs = [
            {
                "protocol": "K5_1",
                "field_name": "高程",
                "actual_field": "u5faau73af1_1_u5faau73af2_1_u5206u652f1_u9ad8u7a0b",
                "label": "高程",
                "path_parts": ["循环1_1", "循环2_1", "分支1", "高程"],
            },
            {
                "protocol": "K1_7",
                "field_name": "俯仰2",
                "actual_field": "u5faau73af2_1_u4fefu4ef02",
                "label": "俯仰2",
                "path_parts": ["循环2_1", "俯仰2"],
            },
        ]

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["X0_5", "K5_1", "K1_7"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "高度": [("X0_5", "prolong_u9ad8u5ea6")],
                    "俯仰角": [("X0_5", "continue3_u4fefu4ef0u89d2")],
                },
            ),
            patch("code_generation_adapter.resolve_protocol_field_specs", return_value=protocol_specs),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            normalized = _normalize_conversion_rules_payload(
                relation_payload,
                protocol_dir=Path("/tmp/fake_protocol_dir"),
            )

        conversions = normalized["conversions"]
        self.assertEqual(len(conversions), 2)
        self.assertEqual(
            conversions[0]["rules"][0]["target_field"],
            "u5faau73af1_1_u5faau73af2_1_u5206u652f1_u9ad8u7a0b",
        )
        self.assertEqual(
            conversions[1]["rules"][0]["target_field"],
            "u5faau73af2_1_u4fefu4ef02",
        )

    def test_simple_matrix_payload_is_expanded_to_joint_choreography(self) -> None:
        mappings_payload = {
            "project_name": "demo_project",
            "conversions": [
                {
                    "name": "K1.7_K1.6_to_X0.5",
                    "sources": [
                        {"alias": "k1_7", "protocol": "K1_7"},
                        {"alias": "k1_6", "protocol": "K1_6"},
                    ],
                    "target": {"protocol": "X0_5"},
                }
            ],
        }
        choreography = _build_choreography_payload(
            {"matrix": [[3, 3], [3, 3]], "headers": ["K1_7", "K1_6"]},
            mappings_payload=mappings_payload,
        )

        self.assertIsNotNone(choreography)
        assert choreography is not None
        self.assertEqual(choreography["project_name"], "demo_project")
        self.assertEqual(choreography["mode"], "joint")
        self.assertEqual(len(choreography["sources"]), 2)
        self.assertEqual(choreography["targets"][0]["protocol"], "X0_5")
        self.assertEqual(choreography["joint_groups"][0]["matrix"]["values"], [[0, 3], [3, 0]])

    def test_floor_division_formula_is_normalized_for_cpp_generation(self) -> None:
        normalized = _normalize_formula_for_generator("(k1_6.origin_u79d2 // 1)")
        self.assertNotIn("//", normalized)
        self.assertIn("/", normalized)

    def test_legacy_target_assignment_formula_is_simplified(self) -> None:
        normalized = _normalize_formula_for_generator(
            "x_0_5_纬度 = (k1_6.origin_u5206u652f4_u7eacu5ea6 == x_0_5_纬度 ? "
            "k1_6.origin_u5206u652f4_u7eacu5ea6 : 0)"
        )
        self.assertEqual(normalized, "k1_6.origin_u5206u652f4_u7eacu5ea6")

    def test_legacy_target_guard_with_cast_is_simplified(self) -> None:
        formula = (
            "k_1_7_循环1_1_纬度1 = ((x0_5.prolong_u7eacu5ea6 == "
            "k_1_7_循环1_1_纬度1) ? int(x0_5.prolong_u7eacu5ea6) : 0)"
        )

        normalized = _normalize_formula_for_generator(formula)

        self.assertEqual(normalized, "int(x0_5.prolong_u7eacu5ea6)")

    def test_generator_loader_strips_legacy_target_guard_with_cast(self) -> None:
        expression = (
            "k_1_7_循环1_1_纬度1 = ((x0_5.prolong_u7eacu5ea6 == "
            "k_1_7_循环1_1_纬度1) ? int(x0_5.prolong_u7eacu5ea6) : 0)"
        )

        normalized = _normalize_legacy_expression(expression)

        self.assertEqual(normalized, "int(x0_5.prolong_u7eacu5ea6)")

    def test_multiline_target_assignment_python_block_is_simplified(self) -> None:
        normalized = _normalize_formula_for_generator(
            "if k1_7.origin_u5faau73af1_u4fefu4ef01 == 5:\n"
            "    x_0_5_俯仰角 = 13\n"
            "elif k1_7.origin_u5faau73af2_u4fefu4ef02 == 6:\n"
            "    x_0_5_俯仰角 = 13\n"
            "else:\n"
            "    x_0_5_俯仰角 = k1_7.origin_u5faau73af1_u4fefu4ef01 if "
            "k1_7.origin_u5faau73af1_u4fefu4ef01 != 5 else k1_7.origin_u5faau73af2_u4fefu4ef02"
        )
        self.assertNotIn("x_0_5_俯仰角 =", normalized)
        self.assertNotIn("\n", normalized)
        self.assertIn("?", normalized)

    def test_conversion_rules_payload_sanitizes_dotted_legacy_target_reference(self) -> None:
        rules_payload = {
            "version": "1.0",
            "project_name": "demo",
            "conversions": [
                {
                    "name": "K1.7_to_X0.5",
                    "sources": [{"alias": "k1_7", "protocol": "K1_7"}],
                    "target": {"protocol": "X0_5"},
                    "rules": [
                        {
                            "target_field": "俯仰角",
                            "target_actual_field": "continue3_u4fefu4ef0u89d2",
                            "target_path": "俯仰角",
                            "formula": "x0_5.X0_5_俯仰角 = k1_7.origin_u4fefu4ef01",
                            "source_fields": ["k1_7.俯仰1"],
                            "source_actual_fields": ["k1_7.origin_u4fefu4ef01"],
                            "source_paths": ["俯仰1"],
                        }
                    ],
                }
            ],
        }

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "K1_7":
                return (
                    {
                        "origin_u4fefu4ef01": {
                            "actual_field": "origin_u4fefu4ef01",
                            "path_parts": ["俯仰1"],
                        }
                    },
                    {"俯仰1": "origin_u4fefu4ef01"},
                )
            if protocol_name == "X0_5":
                return (
                    {
                        "continue3_u4fefu4ef0u89d2": {
                            "actual_field": "continue3_u4fefu4ef0u89d2",
                            "path_parts": ["俯仰角"],
                        }
                    },
                    {"俯仰角": "continue3_u4fefu4ef0u89d2"},
                )
            return ({}, {})

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["K1_7", "X0_5"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "俯仰1": [("K1_7", "origin_u4fefu4ef01")],
                    "俯仰角": [("X0_5", "continue3_u4fefu4ef0u89d2")],
                },
            ),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            normalized = _normalize_conversion_rules_payload(
                rules_payload,
                protocol_dir=Path("/tmp/fake_protocol_dir"),
            )

        formula = normalized["conversions"][0]["rules"][0]["formula"]
        self.assertEqual(formula, "k1_7.origin_u4fefu4ef01")

    def test_invalid_source_fields_are_filtered_without_failing_generation(self) -> None:
        rules_payload = {
            "version": "1.0",
            "project_name": "demo",
            "conversions": [
                {
                    "name": "K1.7_to_X0.5",
                    "sources": [{"alias": "k1_7", "protocol": "K1_7"}],
                    "target": {"protocol": "X0_5"},
                    "rules": [
                        {
                            "target_field": "时间1",
                            "formula": "k1_7.经度1",
                            "source_fields": ["k1_7.经度1", "k1_7.不存在字段"],
                            "source_actual_fields": ["k1_7.origin_u7ecfu5ea61", "k1_7.invalid_field"],
                            "source_paths": ["循环1_1/经度1", "不存在路径"],
                        }
                    ],
                }
            ],
        }

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "K1_7":
                return (
                    {
                        "origin_u7ecfu5ea61": {
                            "actual_field": "origin_u7ecfu5ea61",
                            "path_parts": ["循环1_1", "经度1"],
                        }
                    },
                    {"循环1_1/经度1": "origin_u7ecfu5ea61"},
                )
            if protocol_name == "X0_5":
                return (
                    {
                        "origin_u65f6u95f41": {
                            "actual_field": "origin_u65f6u95f41",
                            "path_parts": ["时间1"],
                        }
                    },
                    {"时间1": "origin_u65f6u95f41"},
                )
            return ({}, {})

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["K1_7", "X0_5"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "经度1": [("K1_7", "origin_u7ecfu5ea61")],
                    "时间1": [("X0_5", "origin_u65f6u95f41")],
                },
            ),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            payload = _normalize_conversion_rules_payload(
                rules_payload,
                protocol_dir=Path("/tmp/fake_protocol_dir"),
            )

        rules = payload["conversions"][0]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["source_fields"], ["k1_7.origin_u7ecfu5ea61"])
        self.assertEqual(rules[0]["source_actual_fields"], ["k1_7.origin_u7ecfu5ea61"])

    def test_rules_with_invalid_target_fields_are_filtered_out(self) -> None:
        rules_payload = {
            "version": "1.0",
            "project_name": "demo",
            "conversions": [
                {
                    "name": "K1.7_to_X0.5",
                    "sources": [{"alias": "k1_7", "protocol": "K1_7"}],
                    "target": {"protocol": "X0_5"},
                    "rules": [
                        {
                            "target_field": "不存在目标字段",
                            "formula": "k1_7.经度1",
                            "source_fields": ["k1_7.经度1"],
                        },
                        {
                            "target_field": "时间1",
                            "formula": "k1_7.经度1",
                            "source_fields": ["k1_7.经度1"],
                        },
                    ],
                }
            ],
        }

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "K1_7":
                return (
                    {
                        "origin_u7ecfu5ea61": {
                            "actual_field": "origin_u7ecfu5ea61",
                            "path_parts": ["循环1_1", "经度1"],
                        }
                    },
                    {"循环1_1/经度1": "origin_u7ecfu5ea61"},
                )
            if protocol_name == "X0_5":
                return (
                    {
                        "origin_u65f6u95f41": {
                            "actual_field": "origin_u65f6u95f41",
                            "path_parts": ["时间1"],
                        }
                    },
                    {"时间1": "origin_u65f6u95f41"},
                )
            return ({}, {})

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["K1_7", "X0_5"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "经度1": [("K1_7", "origin_u7ecfu5ea61")],
                    "时间1": [("X0_5", "origin_u65f6u95f41")],
                },
            ),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            payload = _normalize_conversion_rules_payload(
                rules_payload,
                protocol_dir=Path("/tmp/fake_protocol_dir"),
            )

        rules = payload["conversions"][0]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["target_field"], "origin_u65f6u95f41")

    def test_build_generator_rules_payload_resolves_aggregate_protocol_legacy_source_tokens(self) -> None:
        raw_rules = [
            {
                "conversion_mode": "mapping",
                "formula": "纬度 = 16 if k1_7.k1_6_纬度 == 16 else (15 if k1_7.k1_6_纬度 == 15 else 0)",
                "source_fields": ["k1_6_纬度", "k1_6_纬度1", "k1_6_纬度1"],
                "source_actual_fields": ["k1_7_k1_6.纬度", "k1_7_k1_6.纬度1", "k1_7_k1_6.纬度1"],
                "source_paths": [None, None, None],
                "source_protocol_type": "K1_7+K1_6",
                "source_protocol_name": "K1_7+K1_6",
                "target_field": "纬度",
                "target_actual_field": "prolong_u7eacu5ea6",
                "target_path": "纬度",
                "target_protocol_type": "X0_5",
            }
        ]

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "K1_6":
                return (
                    {
                        "origin_u7eacu5ea6": {
                            "actual_field": "origin_u7eacu5ea6",
                            "path_parts": ["纬度"],
                        },
                        "origin_u7eacu5ea61": {
                            "actual_field": "origin_u7eacu5ea61",
                            "path_parts": ["纬度1"],
                        },
                    },
                    {
                        "纬度": "origin_u7eacu5ea6",
                        "纬度1": "origin_u7eacu5ea61",
                    },
                )
            if protocol_name == "K1_7":
                return (
                    {
                        "origin_u7eacu5ea61": {
                            "actual_field": "origin_u7eacu5ea61",
                            "path_parts": ["纬度1"],
                        }
                    },
                    {"纬度1": "origin_u7eacu5ea61"},
                )
            if protocol_name == "X0_5":
                return (
                    {
                        "prolong_u7eacu5ea6": {
                            "actual_field": "prolong_u7eacu5ea6",
                            "path_parts": ["纬度"],
                        }
                    },
                    {"纬度": "prolong_u7eacu5ea6"},
                )
            return ({}, {})

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["K1_6", "K1_7", "X0_5"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "纬度": [("K1_6", "origin_u7eacu5ea6"), ("X0_5", "prolong_u7eacu5ea6")],
                    "纬度1": [("K1_6", "origin_u7eacu5ea61"), ("K1_7", "origin_u7eacu5ea61")],
                },
            ),
            patch(
                "code_generation_adapter._protocol_field_display_index",
                return_value={
                    ("K1_6", "ORIGIN_U7EACU5EA6"): "纬度",
                    ("K1_6", "ORIGIN_U7EACU5EA61"): "纬度1",
                    ("K1_7", "ORIGIN_U7EACU5EA61"): "纬度1",
                    ("X0_5", "PROLONG_U7EACU5EA6"): "纬度",
                },
            ),
            patch(
                "code_generation_adapter._build_target_protocol_field_lookup",
                return_value=({"纬度": "prolong_u7eacu5ea6"}, {"prolong_u7eacu5ea6"}, set()),
            ),
            patch("code_generation_adapter._build_target_concept_spec_lookup", return_value={}),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            payload = build_generator_rules_payload(
                raw_rules,
                protocol_dir=Path("/tmp/fake_protocol_dir"),
                target_protocol_name="X0_5",
                project_name="demo",
            )

        rules = payload["conversions"][0]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["target_field"], "prolong_u7eacu5ea6")
        self.assertEqual(rules[0]["source_fields"][0], "k1_6.origin_u7eacu5ea6")
        self.assertEqual(rules[0]["source_actual_fields"][0], "k1_6.origin_u7eacu5ea6")
        self.assertNotIn("纬度", rules[0]["formula"])
        self.assertNotIn("k1_7.k1_6_纬度", rules[0]["formula"])
        self.assertIn("k1_6.origin_u7eacu5ea6", rules[0]["formula"])

    def test_conversion_payload_ignores_aggregate_source_protocol_without_xml(self) -> None:
        rules_payload = {
            "version": "1.0",
            "project_name": "demo",
            "conversions": [
                {
                    "name": "K1.7_K5.1_to_X0.5",
                    "mode": "joint",
                    "sources": [
                        {"alias": "k1_7", "protocol": "K1_7"},
                        {"alias": "k1_7_k5_1", "protocol": "K1_7+K5_1"},
                    ],
                    "target": {"protocol": "X0_5"},
                    "rules": [
                        {
                            "target_field": "x_0_5_高度",
                            "target_actual_field": "prolong_u9ad8u5ea6",
                            "target_path": "高度",
                            "source_fields": ["k1_7.高度1"],
                            "source_actual_fields": ["k1_7.origin_u9ad8u5ea61"],
                            "source_paths": ["循环1_1/高度1"],
                            "formula": "x_0_5_高度 = k1_7.高度1",
                        }
                    ],
                }
            ],
        }

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "K1_7":
                return (
                    {
                        "origin_u9ad8u5ea61": {
                            "actual_field": "origin_u9ad8u5ea61",
                            "path_parts": ["循环1_1", "高度1"],
                        }
                    },
                    {"循环1_1/高度1": "origin_u9ad8u5ea61", "高度1": "origin_u9ad8u5ea61"},
                )
            if protocol_name == "X0_5":
                return (
                    {
                        "prolong_u9ad8u5ea6": {
                            "actual_field": "prolong_u9ad8u5ea6",
                            "path_parts": ["高度"],
                        }
                    },
                    {"高度": "prolong_u9ad8u5ea6"},
                )
            return ({}, {})

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["K1_7", "X0_5"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "高度1": [("K1_7", "origin_u9ad8u5ea61")],
                    "高度": [("X0_5", "prolong_u9ad8u5ea6")],
                },
            ),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            payload = _normalize_conversion_rules_payload(
                rules_payload,
                protocol_dir=Path("/tmp/fake_protocol_dir"),
            )

        conversions = payload["conversions"]
        self.assertEqual(len(conversions), 1)
        self.assertEqual(conversions[0]["sources"], [{"alias": "k1_7", "protocol": "K1_7"}])
        self.assertEqual(conversions[0]["rules"][0]["target_field"], "prolong_u9ad8u5ea6")
        self.assertEqual(conversions[0]["rules"][0]["source_fields"], ["k1_7.origin_u9ad8u5ea61"])

    def test_mapping_python_expr_is_not_converted_to_mapping_table_ternary(self) -> None:
        raw_rules = [
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
            }
        ]

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "K1_6":
                return (
                    {"time_field": {"actual_field": "time_field", "path_parts": ["time_field"]}},
                    {"TIME_FIELD": "time_field"},
                )
            if protocol_name == "X0_5":
                return (
                    {"time_value": {"actual_field": "time_value", "path_parts": ["time_value"]}},
                    {"TIME_VALUE": "time_value"},
                )
            return ({}, {})

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["K1_6", "X0_5"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "TIME_VALUE": [("X0_5", "time_value")],
                    "K1_6_TIME_FIELD": [("K1_6", "time_field")],
                },
            ),
            patch(
                "code_generation_adapter._protocol_field_display_index",
                return_value={
                    ("K1_6", "TIME_FIELD"): "time_field",
                    ("K1_6", "TIME_VALUE"): "time_value",
                    ("K1_6", "K1_6_TIME_FIELD"): "time_field",
                    ("X0_5", "TIME_VALUE"): "time_value",
                },
            ),
            patch(
                "code_generation_adapter._build_target_protocol_field_lookup",
                return_value=({"TIME_VALUE": "time_value"}, {"time_value"}, set()),
            ),
            patch("code_generation_adapter._build_target_concept_spec_lookup", return_value={}),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            payload = build_generator_rules_payload(
                raw_rules,
                protocol_dir=Path("/tmp/fake_protocol_dir"),
                target_protocol_name="X0_5",
                project_name="demo",
                preserve_display_names=True,
            )

        formula = payload["conversions"][0]["rules"][0]["formula"]
        self.assertNotIn("?", formula)
        self.assertNotIn("==", formula)
        self.assertEqual(formula.replace(" ", ""), "time_field+0")

    def test_python_expr_self_referential_target_guard_is_stripped(self) -> None:
        raw_rules = [
            {
                "target_field": "LATITUDE_1",
                "target_actual_field": "latitude_1",
                "target_path": "group_1/latitude_1",
                "source_fields": ["X0_5_LATITUDE"],
                "source_bindings": [
                    {
                        "alias_name": "X0_5_LATITUDE",
                        "protocol": "X0_5",
                        "message_code": "X0.5",
                        "actual_field": "latitude",
                        "display_field": "latitude",
                        "source_path": "latitude",
                    }
                ],
                "source_actual_fields": ["latitude"],
                "source_paths": ["latitude"],
                "source_protocol_type": "X0_5",
                "source_protocol_name": "X0_5",
                "source_message_code": "X0.5",
                "conversion_mode": "transcoding",
                "formula_kind": "python_expr",
                "formula": "k_1_7_group_1_latitude_1 = int(X0_5_LATITUDE) if X0_5_LATITUDE == k_1_7_group_1_latitude_1 else 0",
            }
        ]

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "X0_5":
                return (
                    {"latitude": {"actual_field": "latitude", "path_parts": ["latitude"]}},
                    {"LATITUDE": "latitude", "X0_5_LATITUDE": "latitude"},
                )
            if protocol_name == "K1_7":
                return (
                    {"latitude_1": {"actual_field": "latitude_1", "path_parts": ["group_1", "latitude_1"]}},
                    {"LATITUDE_1": "latitude_1"},
                )
            return ({}, {})

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["X0_5", "K1_7"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "LATITUDE_1": [("K1_7", "latitude_1")],
                    "X0_5_LATITUDE": [("X0_5", "latitude")],
                },
            ),
            patch(
                "code_generation_adapter._protocol_field_display_index",
                return_value={
                    ("X0_5", "LATITUDE"): "latitude",
                    ("X0_5", "X0_5_LATITUDE"): "latitude",
                    ("K1_7", "LATITUDE_1"): "latitude_1",
                },
            ),
            patch(
                "code_generation_adapter._build_target_protocol_field_lookup",
                return_value=({"LATITUDE_1": "latitude_1"}, {"latitude_1"}, set()),
            ),
            patch("code_generation_adapter._build_target_concept_spec_lookup", return_value={}),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            payload = build_generator_rules_payload(
                raw_rules,
                protocol_dir=Path("/tmp/fake_protocol_dir"),
                target_protocol_name="K1_7",
                project_name="demo",
                preserve_display_names=True,
            )

        formula = payload["conversions"][0]["rules"][0]["formula"]
        self.assertNotIn("__target__", formula)
        self.assertNotIn("?", formula)
        self.assertNotIn("==", formula)
        self.assertEqual(formula.replace(" ", ""), "int(latitude)")

    def test_mapping_table_formula_still_converts_to_ternary(self) -> None:
        raw_rules = [
            {
                "target_field": "STATUS",
                "target_actual_field": "status",
                "target_path": "status",
                "source_fields": ["K1_6_STATUS"],
                "source_bindings": [
                    {
                        "alias_name": "K1_6_STATUS",
                        "protocol": "K1_6",
                        "message_code": "K1.6",
                        "actual_field": "status",
                        "display_field": "status",
                        "source_path": "status",
                    }
                ],
                "source_actual_fields": ["status"],
                "source_paths": ["status"],
                "source_protocol_type": "K1_6",
                "source_protocol_name": "K1_6",
                "source_message_code": "K1.6",
                "conversion_mode": "mapping",
                "formula_kind": "mapping_table",
                "formula": "1=10, 2=20",
            }
        ]

        def fake_spec_maps(_protocol_dir: Path, protocol_name: str):
            if protocol_name == "K1_6":
                return (
                    {"status": {"actual_field": "status", "path_parts": ["status"]}},
                    {"STATUS": "status"},
                )
            if protocol_name == "X0_5":
                return (
                    {"status": {"actual_field": "status", "path_parts": ["status"]}},
                    {"STATUS": "status"},
                )
            return ({}, {})

        with (
            patch("code_generation_adapter._normalize_protocol_names", return_value=["K1_6", "X0_5"]),
            patch(
                "code_generation_adapter._protocol_field_index",
                return_value={
                    "STATUS": [("K1_6", "status"), ("X0_5", "status")],
                    "K1_6_STATUS": [("K1_6", "status")],
                },
            ),
            patch(
                "code_generation_adapter._protocol_field_display_index",
                return_value={
                    ("K1_6", "STATUS"): "status",
                    ("K1_6", "K1_6_STATUS"): "status",
                    ("X0_5", "STATUS"): "status",
                },
            ),
            patch(
                "code_generation_adapter._build_target_protocol_field_lookup",
                return_value=({"STATUS": "status"}, {"status"}, set()),
            ),
            patch("code_generation_adapter._build_target_concept_spec_lookup", return_value={}),
            patch("code_generation_adapter._build_protocol_spec_maps", side_effect=fake_spec_maps),
        ):
            payload = build_generator_rules_payload(
                raw_rules,
                protocol_dir=Path("/tmp/fake_protocol_dir"),
                target_protocol_name="X0_5",
                project_name="demo",
                preserve_display_names=True,
            )

        formula = payload["conversions"][0]["rules"][0]["formula"]
        self.assertIn("?", formula)
        self.assertIn("status == 1", formula)
        self.assertIn("20", formula)


if __name__ == "__main__":
    unittest.main()
