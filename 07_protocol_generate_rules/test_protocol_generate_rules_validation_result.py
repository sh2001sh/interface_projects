from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import io


PROJECT_ROOT = Path(__file__).resolve().parent
API_APP_PATH = PROJECT_ROOT / "app.py"


def _load_api_module():
    spec = importlib.util.spec_from_file_location("interface_project_07_api_app", API_APP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {API_APP_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare_protocol_dirs(tmp_path: Path) -> tuple[Path, Path]:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "temp_sensor.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="temperature" defaultValue="0">12</Item>\n  <Item name="status" defaultValue="0">4</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (target_dir / "temp_report.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="temperature_c" defaultValue="0">10</Item>\n  <Item name="alarm" defaultValue="0">6</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    return source_dir, target_dir


def _prepare_k1_6_k1_7_graph_hit_dirs(tmp_path: Path) -> tuple[Path, Path]:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "k1.6.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="高程1" defaultValue="0">16</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (target_dir / "k1.7.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="高度1" defaultValue="0">16</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    return source_dir, target_dir


def test_resolve_protocol_message_specs_filters_abstract_descriptor_xml(tmp_path):
    module = _load_api_module()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "k.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="说明字段">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (source_dir / "k1.6.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="小时">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (source_dir / "k1.7.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="高度1">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )

    specs = module.resolve_protocol_message_specs([str(source_dir)], "source_protocol_dirs")

    protocol_names = [spec["protocol_name"] for spec in specs]
    assert "K" not in protocol_names
    assert "K1_6" in protocol_names
    assert "K1_7" in protocol_names


def test_resolve_protocol_message_specs_filters_route_descriptor_even_without_name_prefix_match(tmp_path):
    module = _load_api_module()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "route_header.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="路由字段">8</Item>\n  <Field corr="消息标识" value="1">K1.6</Field>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (source_dir / "k1.6.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="小时">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )

    specs = module.resolve_protocol_message_specs([str(source_dir)], "source_protocol_dirs")

    protocol_names = [spec["protocol_name"] for spec in specs]
    assert "Route_Header" not in protocol_names
    assert protocol_names == ["K1_6"]


def test_resolve_protocol_type_names_filters_route_descriptor_even_without_name_prefix_match(tmp_path):
    module = _load_api_module()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "route_header.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="路由字段">8</Item>\n  <Field corr="消息标识" value="1">K1.6</Field>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (source_dir / "k1.6.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="小时">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )

    protocol_names = module.resolve_protocol_type_names(str(source_dir), "source_protocol_dir")

    assert protocol_names == ["K1_6"]


def test_resolve_protocol_type_names_keeps_numbered_protocol_with_message_identifier_field(tmp_path):
    module = _load_api_module()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "k.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="说明字段">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (source_dir / "k1.6.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="消息标识">2</Item>\n  <Item name="小时">5</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )

    protocol_names = module.resolve_protocol_type_names(str(source_dir), "source_protocol_dir")
    specs = module.resolve_protocol_message_specs(str(source_dir), "source_protocol_dir")

    assert protocol_names == ["K1_6"]
    assert [spec["protocol_name"] for spec in specs] == ["K1_6"]


def test_resolve_protocol_message_specs_preserves_directory_per_protocol(tmp_path):
    module = _load_api_module()
    source_a = tmp_path / "source_a"
    source_b = tmp_path / "source_b"
    source_a.mkdir()
    source_b.mkdir()
    (source_a / "k1.6.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="小时">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (source_b / "k1.7.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="高度1">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )

    specs = module.resolve_protocol_message_specs(
        [str(source_a), str(source_b)],
        "source_protocol_dirs",
    )

    directory_map = {item["protocol_name"]: item["directory"] for item in specs}
    assert directory_map["K1_6"] == str(source_a)
    assert directory_map["K1_7"] == str(source_b)


def test_resolve_protocol_message_specs_accepts_copy_suffix_xml_filename(tmp_path):
    module = _load_api_module()
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "X.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="说明字段">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (source_dir / "X0.5 - 副本.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="高度">16</Item>\n  <Item name="纬度">15</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (target_dir / "k1.6.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="高程">16</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )

    protocol_names = module.resolve_protocol_type_names([str(source_dir)], "source_protocol_dirs")
    specs = module.resolve_protocol_message_specs([str(source_dir)], "source_protocol_dirs")
    field_specs = module.resolve_protocol_field_specs([str(source_dir)], "source_protocol_dirs")

    assert protocol_names == ["X0_5"]
    assert [item["protocol_name"] for item in specs] == ["X0_5"]
    assert field_specs
    assert any(item["field_name"] == "高度" for item in field_specs)


def test_protocol_generate_rules_returns_four_boolean_checks(tmp_path):
    module = _load_api_module()
    source_dir, target_dir = _prepare_protocol_dirs(tmp_path)

    module.generate_protocol_field_rules = lambda **_: {
        "generated_rules": [
            {
                "target_field": "TEMPERATURE_C",
                "source_fields": ["TEMP_SENSOR_temperature"],
                "source_bindings": [
                    {
                        "alias_name": "TEMP_SENSOR_temperature",
                        "protocol": "Temp_Sensor",
                        "message_code": "TEMP_SENSOR",
                        "actual_field": "temperature",
                        "display_field": "temperature",
                        "source_path": "temperature",
                    }
                ],
                "formula": "TEMPERATURE",
                "rule": "TEMP_SENSOR_temperature",
                "conversion_mode": "transcoding",
                "description": "温度直传",
                "message_bundle_id": "TEMP_SENSOR_to_TEMP_REPORT",
            }
        ],
        "normalized_rules": [
            {
                "target_field": "TEMPERATURE_C",
                "source_fields": ["TEMP_SENSOR_temperature"],
                "source_bindings": [
                    {
                        "alias_name": "TEMP_SENSOR_temperature",
                        "protocol": "Temp_Sensor",
                        "message_code": "TEMP_SENSOR",
                        "actual_field": "temperature",
                        "display_field": "temperature",
                        "source_path": "temperature",
                    }
                ],
                "formula": "TEMP_SENSOR_temperature",
                "rule": "TEMP_SENSOR_temperature",
                "conversion_mode": "transcoding",
                "description": "温度直传",
                "message_bundle_id": "TEMP_SENSOR_to_TEMP_REPORT",
            }
        ],
        "kg_writeback_payload": {},
        "summary": {"total_rules": 1},
        "raw_output": "mocked",
    }
    module.discover_message_bundle_candidates = lambda **_: [
        {
            "bundle_id": "TEMP_SENSOR_to_TEMP_REPORT",
            "source_messages": ["TEMP_SENSOR"],
            "target_message": "TEMP_REPORT",
            "selected": True,
        }
    ]
    module.build_bundle_generation_payload = lambda *_, **__: {
        "bundle_id": "TEMP_SENSOR_to_TEMP_REPORT",
        "source_specs": [{"message_code": "TEMP_SENSOR", "directory": str(source_dir)}],
        "target_spec": {"protocol_name": "Temp_Report", "message_code": "TEMP_REPORT", "directory": str(target_dir)},
        "source_protocol": {
            "name": "Temp_Sensor",
            "protocol_type": "Temp_Sensor",
            "message_code": "TEMP_SENSOR",
            "content": "source",
            "bundle_id": "TEMP_SENSOR_to_TEMP_REPORT",
        },
        "target_protocol": {
            "name": "Temp_Report",
            "protocol_type": "Temp_Report",
            "message_code": "TEMP_REPORT",
            "content": "target",
        },
        "source_message": {"TEMP_SENSOR_temperature": 0},
        "source_field_catalog": [
            {
                "field_name": "TEMP_SENSOR_temperature",
                "protocol": "Temp_Sensor",
                "message_code": "TEMP_SENSOR",
                "actual_field": "temperature",
                "display_field": "temperature",
                "source_path": "temperature",
                "sample_value": 0,
            }
        ],
        "required_target_fields": [
            {
                "protocol": "Temp_Report",
                "field_name": "temperature_c",
                "actual_field": "temperature_c",
                "path_parts": ["temperature_c"],
            }
        ],
    }
    module._score_relation_conversion = lambda **_: {
        "field_match_accuracy": 88.0,
        "semantic_fidelity": 84.5,
        "structure_integrity": 91.0,
        "overall_correctness_score": 87.125,
    }

    client = module.app.test_client()
    response = client.post(
        "/api/knowledge/protocol_generate_rules",
        json={
            "source_protocol_dirs": [str(source_dir)],
            "target_protocol_dir": str(target_dir),
            "rules_output_dir": str(tmp_path / "output"),
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    rules_json_path = payload["data"]["conversion_rules_json"]
    assert isinstance(rules_json_path, str)
    assert Path(rules_json_path).exists()
    saved_rules = json.loads(Path(rules_json_path).read_text(encoding="utf-8"))
    assert saved_rules["relations"][0]["relation_id"] == "TEMP_SENSOR_to_TEMP_REPORT"
    assert saved_rules["relations"][0]["target_protocol"] == "Temp_Report"
    assert saved_rules["relations"][0]["rules"][0]["target_field"] == "temp_report_temperature_c"
    assert saved_rules["relations"][0]["rules"][0]["target_var"] == "temp_report_temperature_c"
    assert saved_rules["relations"][0]["rules"][0]["formula"] == "temp_report_temperature_c = temp_sensor_temperature"
    assert saved_rules["relations"][0]["rules"][0]["source_fields"] == ["temp_sensor_temperature"]
    assert saved_rules["relations"][0]["scores"] == {
        "field_match_accuracy": 88.0,
        "semantic_fidelity": 84.5,
        "structure_integrity": 91.0,
        "overall_correctness_score": 87.125,
    }
    assert payload["data"]["relations"][0]["relation_id"] == "TEMP_SENSOR_to_TEMP_REPORT"
    assert payload["data"]["relations"][0]["scores"] == {
        "field_match_accuracy": 88.0,
        "semantic_fidelity": 84.5,
        "conversion_rate": 0.0,
        "structure_integrity": 91.0,
        "overall_correctness_score": 87.125,
    }
    expected_summary = {
        "semantic_match_query_count": 0,
        "semantic_match_avg_query_time_ms": None,
        "semantic_match_time_target_met": None,
        "rule_generation_target_count": 0,
        "rule_generation_avg_time_ms": None,
        "rule_generation_time_target_met": None,
        "knowledge_graph_field_count": 0,
        "candidate_assisted_target_count": 0,
        "deterministic_field_count": 0,
        "converted_field_count": 0,
        "llm_converted_field_count": 0,
        "sub_message_relation_count": 1,
        "trained_doc_registry_hit": False,
        "trained_doc_registry_info": {},
        "selected_bundle_count": 1,
    }
    for key, value in expected_summary.items():
        assert payload["data"]["summary"][key] == value
    assert payload["data"]["summary"]["evidence_snippet_count"] == 0
    assert payload["data"]["pageindex_audit"]["evidence_snippet_count"] == 0
    assert "_meta" not in payload["data"]
    assert payload["data"]["relations"][0]["relation_id"] == "TEMP_SENSOR_to_TEMP_REPORT"
    assert payload["data"]["validation_result"] == {
        "field_legality": True,
        "position_accuracy": True,
        "conversion_logic": True,
        "protocol_compliance": True,
    }


def test_protocol_generate_rules_default_message_bundle_flow_works_without_external_module(tmp_path):
    module = _load_api_module()
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "k1.6.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="小时" defaultValue="0">8</Item>\n  <Item name="分钟" defaultValue="0">8</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (source_dir / "k1.7.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="高度1" defaultValue="0">16</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (target_dir / "x0.5.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Item name="时间1" defaultValue="0">32</Item>\n  <Item name="高度" defaultValue="0">16</Item>\n</NameSpace>\n""",
        encoding="utf-8",
    )

    module.generate_protocol_field_rules = lambda **_: {
        "generated_rules": [
            {
                "target_field": "时间1",
                "target_actual_field": "u65f6u95f41",
                "target_path": "时间1",
                "source_fields": ["K1_6_小时", "K1_6_分钟"],
                "formula": "K1_6_小时 * 60 + K1_6_分钟",
                "rule": "K1_6_小时 * 60 + K1_6_分钟",
                "conversion_mode": "transcoding",
                "description": "时间拼接",
                "message_bundle_id": "K1.6_K1.7_to_X0.5",
            }
        ],
        "normalized_rules": [
            {
                "target_field": "时间1",
                "target_actual_field": "u65f6u95f41",
                "target_path": "时间1",
                "source_fields": ["K1_6_小时", "K1_6_分钟"],
                "formula": "K1_6_小时 * 60 + K1_6_分钟",
                "rule": "K1_6_小时 * 60 + K1_6_分钟",
                "conversion_mode": "transcoding",
                "description": "时间拼接",
                "message_bundle_id": "K1.6_K1.7_to_X0.5",
            }
        ],
        "kg_writeback_payload": {
            "rules": [
                {
                    "target_field": "时间1",
                    "target_actual_field": "u65f6u95f41",
                    "target_path": "时间1",
                    "source_fields": ["K1_6_小时", "K1_6_分钟"],
                    "formula": "K1_6_小时 * 60 + K1_6_分钟",
                    "conversion_mode": "transcoding",
                    "description": "时间拼接",
                }
            ]
        },
        "summary": {
            "semantic_match_query_count": 3,
            "semantic_match_avg_query_time_ms": 12.5,
            "semantic_match_time_target_met": True,
            "rule_generation_target_count": 1,
            "rule_generation_avg_time_ms": 18.0,
            "rule_generation_time_target_met": True,
            "knowledge_graph_rule_count": 0,
            "candidate_target_count": 0,
            "deterministic_rule_count": 0,
            "llm_rule_count": 1,
        },
        "raw_output": "mocked",
    }
    module._score_relation_conversion = lambda **_: {
        "field_match_accuracy": 100.0,
        "semantic_fidelity": 100.0,
        "structure_integrity": 100.0,
        "overall_correctness_score": 100.0,
    }

    client = module.app.test_client()
    response = client.post(
        "/api/knowledge/protocol_generate_rules",
        json={
            "source_protocol_dirs": [str(source_dir)],
            "target_protocol_dir": str(target_dir),
            "use_trained_docs": False,
            "rules_output_dir": str(tmp_path / "output"),
        },
    )

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["summary"]["selected_bundle_count"] == 1
    assert payload["summary"]["semantic_match_query_count"] == 3
    assert payload["summary"]["semantic_match_avg_query_time_ms"] == 12.5
    assert payload["summary"]["semantic_match_time_target_met"] is True
    assert payload["summary"]["rule_generation_target_count"] == 1
    assert payload["summary"]["rule_generation_avg_time_ms"] == 18.0
    assert payload["summary"]["rule_generation_time_target_met"] is True
    assert payload["summary"]["knowledge_graph_avg_rule_time_ms"] == 18.0
    assert payload["summary"]["knowledge_graph_rule_time_target_met"] is True
    assert payload["relations"][0]["relation_id"] == "K1.6_K1.7_to_X0.5"
    assert payload["relations"][0]["source_protocols"] == ["K1_6"]


def test_protocol_generate_rules_hits_local_knowledge_graph_rule_edge(tmp_path, monkeypatch):
    source_dir, target_dir = _prepare_k1_6_k1_7_graph_hit_dirs(tmp_path)
    monkeypatch.setenv("PROTOCOL_CONVERSION_GRAPH_BACKEND", "local_json")
    monkeypatch.setenv("PROTOCOL_CONVERSION_NEO4J_ENABLED", "0")
    monkeypatch.setenv("PROTOCOL_CONVERSION_JSON_FALLBACK", "0")
    module = _load_api_module()
    module.ProtocolConversionKnowledgeBase._INSTANCE_CACHE.clear()

    client = module.app.test_client()
    response = client.post(
        "/api/knowledge/protocol_generate_rules",
        json={
            "source_protocol_dirs": [str(source_dir)],
            "target_protocol_dir": str(target_dir),
            "use_trained_docs": False,
            "rules_output_dir": str(tmp_path / "output"),
            "force_regenerate": True,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()["data"]
    summary = payload["summary"]
    assert summary["semantic_match_query_count"] >= 1
    assert summary["semantic_match_avg_query_time_ms"] is not None
    assert summary["semantic_match_avg_query_time_ms"] < 20.0
    assert summary["semantic_match_time_target_met"] is True
    assert summary["rule_generation_target_count"] == 1
    assert summary["rule_generation_avg_time_ms"] is not None
    assert summary["rule_generation_avg_time_ms"] < 50.0
    assert summary["rule_generation_time_target_met"] is True
    assert summary["knowledge_graph_field_count"] == 1
    assert summary["knowledge_graph_avg_rule_time_ms"] == summary["rule_generation_avg_time_ms"]
    assert summary["knowledge_graph_rule_time_target_met"] is True
    assert summary["llm_converted_field_count"] == 0
    assert payload["relations"][0]["relation_id"] == "K1.6_to_K1.7"
    assert payload["relations"][0]["source_protocols"] == ["K1_6"]
    assert payload["relations"][0]["rules"][0]["target_field"] == "高度1"
    assert payload["relations"][0]["rules"][0]["source_fields"] == ["k1_6_高程1"]
    assert payload["relations"][0]["rules"][0]["formula"] == "k_1_7_高度1 = int(k1_6_高程1)"


def test_resolve_trained_doc_provider_passes_explicit_index_registry_path(monkeypatch):
    module = _load_api_module()
    captured = {}

    def fake_get_trained_doc_evidence_provider(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            registry={
                "project_id": "proj",
                "dataset_id": "ds",
                "doc_set_id": "docset",
                "index_ref": "idx",
                "document_count": 1,
                "indexed_shard_count": 2,
                "registry_count": 1,
                "registry_paths": [str(kwargs.get("index_registry_path"))],
            }
        )

    monkeypatch.setattr(module, "get_trained_doc_evidence_provider", fake_get_trained_doc_evidence_provider)

    _provider, registry_hit, registry_info = module._resolve_trained_doc_provider(
        {
            "project_id": "proj",
            "dataset_id": "ds",
            "doc_set_id": "docset",
            "index_ref": "idx",
            "index_registry_path": "/tmp/pageindex_registry/docset.json",
        }
    )

    assert registry_hit is True
    assert captured["index_registry_path"] == "/tmp/pageindex_registry/docset.json"
    assert registry_info["index_registry_paths"] == ["/tmp/pageindex_registry/docset.json"]


def test_protocol_generate_rules_returns_404_for_missing_index_registry_path(tmp_path):
    module = _load_api_module()
    source_dir, target_dir = _prepare_protocol_dirs(tmp_path)
    client = module.app.test_client()

    response = client.post(
        "/api/knowledge/protocol_generate_rules",
        json={
            "source_protocol_dirs": [str(source_dir)],
            "target_protocol_dir": str(target_dir),
            "index_registry_path": str(tmp_path / "missing" / "docset.json"),
        },
    )

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["code"] == 404
    assert "index_registry_path 不存在" in payload["message"]


def test_protocol_generate_rules_returns_400_for_malformed_json():
    module = _load_api_module()
    client = module.app.test_client()

    response = client.post(
        "/api/knowledge/protocol_generate_rules",
        data="{",
        content_type="application/json",
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["code"] == 400
    assert payload["message"] == "请求体必须是JSON对象"


def test_resolve_trained_doc_provider_accepts_pageindex_registry_alias(monkeypatch):
    module = _load_api_module()
    captured = {}

    def fake_get_trained_doc_evidence_provider(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(registry={})

    monkeypatch.setattr(module, "get_trained_doc_evidence_provider", fake_get_trained_doc_evidence_provider)

    module._resolve_trained_doc_provider(
        {
            "pageindex_registry_path": "/tmp/pageindex_registry/alias.json",
        }
    )

    assert captured["index_registry_path"] == "/tmp/pageindex_registry/alias.json"


def test_build_relation_payload_uses_actual_rule_source_protocols():
    module = _load_api_module()

    relation = module._build_relation_payload(
        candidate={
            "bundle_id": "K1.6_K1.7_to_X0.5",
            "source_protocols": ["K1_6", "K1_7"],
        },
        conversion={
            "target": {"protocol": "X0_5"},
            "rules": [
                {
                    "target_field": "时间1",
                    "source_fields": ["K1_6_小时", "K1_6_分钟"],
                    "formula": "K1_6_小时 * 60 + K1_6_分钟",
                }
            ],
        },
        bundle_payload={
            "source_specs": [
                {"protocol_name": "K1_6"},
                {"protocol_name": "K1_7"},
            ],
            "target_spec": {"protocol_name": "X0_5"},
        },
    )

    assert relation["source_protocols"] == ["K1_6"]


def test_manual_writeback_normalizes_c_style_ternary_to_python() -> None:
    module = _load_api_module()

    normalized_rules = module._normalize_manual_writeback_rules(  # type: ignore[attr-defined]
        [
            {
                "target_field": "x_0_5_俯仰角",
                "source_fields": ["K1_7_俯仰1"],
                "source_protocol_type": "K1_7",
                "target_protocol_type": "X0_5",
                "formula": "x_0_5_俯仰角 = (K1_7_俯仰1 == 5 ? 5 : 0)",
            }
        ]
    )

    assert normalized_rules[0]["target_field"] == "俯仰角"
    assert normalized_rules[0]["source_fields"] == ["俯仰1"]
    assert normalized_rules[0]["formula"] == "俯仰角 = (5 if 俯仰1 == 5 else 0)"


def test_filter_display_writeback_rules_rejects_bit_length_mapping_noise():
    module = _load_api_module()

    rules = [
        {
            "target_field": "k_1_6_经度",
            "target_actual_field": "origin_u7ecfu5ea6",
            "target_path": "经度",
            "source_fields": ["X0_5_经度"],
            "formula": "k_1_6_经度 = 24=24",
        },
        {
            "target_field": "k_1_6_纬度",
            "target_actual_field": "origin_u7eacu5ea6",
            "target_path": "纬度",
            "source_fields": ["X0_5_纬度"],
            "formula": "k_1_6_纬度 = X0_5_纬度",
        },
    ]
    bundle_payload = {
        "required_target_fields": [
            {
                "field_name": "经度",
                "actual_field": "origin_u7ecfu5ea6",
                "target_path": "经度",
                "bit_length": 24,
                "default_value": None,
            },
            {
                "field_name": "纬度",
                "actual_field": "origin_u7eacu5ea6",
                "target_path": "纬度",
                "bit_length": 24,
                "default_value": None,
            },
        ]
    }

    filtered = module._filter_display_writeback_rules(rules, bundle_payload=bundle_payload)

    assert len(filtered) == 1
    assert filtered[0]["target_field"] == "k_1_6_纬度"


def test_score_relation_conversion_matches_prefixed_target_field_via_target_path():
    module = _load_api_module()
    bundle_payload = {
        "source_field_catalog": [
            {
                "actual_field": "temperature",
                "display_field": "temperature",
                "field_name": "temperature",
                "label": "temperature",
                "source_path": "temperature",
            }
        ],
        "required_target_fields": [
            {
                "field_name": "temperature_c",
                "actual_field": "temperature_c",
                "label": "temperature_c",
                "path_parts": ["temperature_c"],
                "preferred_source_candidates": [
                    {
                        "field_name": "temperature",
                        "actual_field": "temperature",
                    }
                ],
            }
        ],
    }
    conversion = {
        "rules": [
            {
                "target_field": "temp_report_temperature_c",
                "target_actual_field": "temperature_c",
                "target_path": "temperature_c",
                "source_fields": ["temperature"],
                "formula": "temperature",
                "rule_type": "direct",
            }
        ]
    }

    scores = module._score_relation_conversion({}, bundle_payload, conversion)

    assert scores["field_match_accuracy"] == 100.0
    assert scores["conversion_rate"] == 100.0
    assert scores["structure_integrity"] == 100.0
    assert scores["overall_correctness_score"] > 0.0


def test_score_relation_conversion_expression_rule_not_penalized_by_text_only():
    module = _load_api_module()
    bundle_payload = {
        "source_field_catalog": [
            {"actual_field": "小时", "display_field": "小时", "field_name": "小时", "label": "小时", "source_path": "小时"},
            {"actual_field": "分钟", "display_field": "分钟", "field_name": "分钟", "label": "分钟", "source_path": "分钟"},
            {"actual_field": "秒", "display_field": "秒", "field_name": "秒", "label": "秒", "source_path": "秒"},
        ],
        "required_target_fields": [
            {
                "field_name": "时间1",
                "actual_field": "时间1",
                "label": "时间1",
                "path_parts": ["时间1"],
                "preferred_source_candidates": [
                    {"field_name": "小时", "actual_field": "小时"},
                    {"field_name": "分钟", "actual_field": "分钟"},
                    {"field_name": "秒", "actual_field": "秒"},
                ],
            }
        ],
    }
    conversion = {
        "rules": [
            {
                "target_field": "x_0_5_时间1",
                "target_actual_field": "时间1",
                "target_path": "时间1",
                "source_fields": ["小时", "分钟", "秒"],
                "formula": "(小时*3600 + 分钟*60 + 秒)",
                "rule_type": "expression",
            }
        ]
    }

    scores = module._score_relation_conversion({}, bundle_payload, conversion)

    assert scores["field_match_accuracy"] == 100.0
    assert scores["semantic_fidelity"] >= 80.0
    assert scores["conversion_rate"] == 100.0


def test_build_relation_rule_payload_normalizes_legacy_target_assignment_formula():
    module = _load_api_module()

    payload = module._build_relation_rule_payload(
        {
            "target_field": "俯仰角",
            "target_actual_field": "continue3_u4fefu4ef0u89d2",
            "target_path": "俯仰角",
            "source_fields": ["k1_7.俯仰1"],
            "source_actual_fields": ["k1_7.origin_u4fefu4ef01"],
            "formula": (
                "if k1_7.origin_u4fefu4ef01 == 5:\n"
                "    x_0_5_俯仰角 = 13\n"
                "else:\n"
                "    x0_5.X0_5_俯仰角 = k1_7.origin_u4fefu4ef01"
            ),
        },
        "X0_5",
    )

    assert payload["target_field"] == "俯仰角"
    assert payload["target_var"] == "x_0_5_俯仰角"
    assert payload["formula"] == "x_0_5_俯仰角 = 13 if k1_7_俯仰1 == 5 else k1_7_俯仰1"


def test_build_relation_rule_payload_keeps_target_field_semantic_when_target_path_has_loops():
    module = _load_api_module()

    payload = module._build_relation_rule_payload(
        {
            "target_field": "高程",
            "target_actual_field": "u5faau73af1_1_u5faau73af2_1_u5206u652f1_u9ad8u7a0b",
            "target_path": "循环1_1/循环2_1/分支1/高程",
            "source_fields": ["x0_5.高度"],
            "source_actual_fields": ["x0_5.prolong_u9ad8u5ea6"],
            "formula": "k_5_1_循环1_1_循环2_1_分支1_高程 = x0_5_高度",
        },
        "K5_1",
    )

    assert payload["target_field"] == "高程"
    assert payload["target_var"] == "k_5_1_高程"
    assert payload["target_path"] == "高程"
    assert payload["formula"] == "k_5_1_高程 = x0_5_高度"


def test_score_relation_conversion_prefixed_source_field_names_are_resolved():
    module = _load_api_module()
    bundle_payload = {
        "source_field_catalog": [
            {"actual_field": "小时", "display_field": "小时", "field_name": "小时", "label": "小时", "source_path": "小时"},
            {"actual_field": "分钟", "display_field": "分钟", "field_name": "分钟", "label": "分钟", "source_path": "分钟"},
            {"actual_field": "秒", "display_field": "秒", "field_name": "秒", "label": "秒", "source_path": "秒"},
        ],
        "required_target_fields": [
            {
                "field_name": "时间1",
                "actual_field": "时间1",
                "label": "时间1",
                "path_parts": ["时间1"],
                "preferred_source_candidates": [
                    {"field_name": "小时", "actual_field": "小时"},
                    {"field_name": "分钟", "actual_field": "分钟"},
                    {"field_name": "秒", "actual_field": "秒"},
                ],
            }
        ],
    }
    conversion = {
        "rules": [
            {
                "target_field": "x_0_5_时间1",
                "target_actual_field": "时间1",
                "target_path": "时间1",
                "source_fields": ["k1_6_小时", "k1_6_分钟", "k1_6_秒"],
                "formula": "(k1_6_小时*3600 + k1_6_分钟*60 + k1_6_秒)",
                "rule_type": "expression",
            }
        ]
    }

    scores = module._score_relation_conversion({}, bundle_payload, conversion)

    assert scores["field_match_accuracy"] == 100.0
    assert scores["conversion_rate"] == 100.0
    assert scores["structure_integrity"] == 100.0


def test_score_relation_conversion_accuracy_counts_successful_fields_only():
    module = _load_api_module()
    bundle_payload = {
        "source_field_catalog": [
            {"actual_field": "A", "display_field": "A", "field_name": "A", "label": "A", "source_path": "A"},
        ],
        "required_target_fields": [
            {
                "field_name": "FIELD_A",
                "actual_field": "FIELD_A",
                "label": "FIELD_A",
                "path_parts": ["FIELD_A"],
                "preferred_source_candidates": [{"field_name": "A", "actual_field": "A"}],
            },
            {
                "field_name": "FIELD_B",
                "actual_field": "FIELD_B",
                "label": "FIELD_B",
                "path_parts": ["FIELD_B"],
                "preferred_source_candidates": [{"field_name": "B", "actual_field": "B"}],
            },
        ],
    }
    conversion = {
        "rules": [
            {
                "target_field": "x_0_5.FIELD_A",
                "target_actual_field": "FIELD_A",
                "target_path": "FIELD_A",
                "source_fields": ["proto_A"],
                "formula": "proto_A",
                "rule_type": "direct",
            },
            {
                "target_field": "x_0_5.FIELD_B",
                "target_actual_field": "FIELD_B",
                "target_path": "FIELD_B",
                "source_fields": ["proto_B"],
                "formula": "proto_B",
                "rule_type": "direct",
            },
        ]
    }

    scores = module._score_relation_conversion({}, bundle_payload, conversion)

    assert scores["field_match_accuracy"] == 50.0
    assert scores["conversion_rate"] == 50.0


def test_finalize_relation_rule_target_fields_appends_suffix_for_duplicates():
    module = _load_api_module()

    finalized = module._finalize_relation_rule_target_fields(
        [
            {"target_field": "x_0_5_备用", "target_var": "x_0_5_备用", "formula": "x_0_5_备用 = a", "target_actual_field": "origin_u5907u7528"},
            {"target_field": "x_0_5_备用", "target_var": "x_0_5_备用", "formula": "x_0_5_备用 = b", "target_actual_field": "continue1_u5907u7528"},
            {"target_field": "x_0_5_备用", "target_var": "x_0_5_备用", "formula": "x_0_5_备用 = c", "target_actual_field": "continue2_u5907u7528"},
        ]
    )

    assert [item["target_field"] for item in finalized] == [
        "x_0_5_备用",
        "x_0_5_备用",
        "x_0_5_备用",
    ]
    assert [item["target_var"] for item in finalized] == [
        "x_0_5_备用",
        "x_0_5_备用_2",
        "x_0_5_备用_3",
    ]
    assert [item["formula"] for item in finalized] == [
        "x_0_5_备用 = a",
        "x_0_5_备用_2 = b",
        "x_0_5_备用_3 = c",
    ]


def test_displayize_rule_records_preserves_rule_alignment_for_writeback(tmp_path):
    module = _load_api_module()
    source_dir, target_dir = _prepare_protocol_dirs(tmp_path)

    protocol_dir = module._merge_protocol_dirs([str(source_dir)], str(target_dir))
    display_rules = module._displayize_rule_records(
        [
            {
                "concept_name": "温度",
                "source_fields": ["TEMP_SENSOR_temperature"],
                "source_bindings": [
                    {
                        "alias_name": "TEMP_SENSOR_temperature",
                        "protocol": "Temp_Sensor",
                        "message_code": "TEMP_SENSOR",
                        "actual_field": "temperature",
                        "display_field": "temperature",
                        "source_path": "temperature",
                    }
                ],
                "target_field": "TEMPERATURE_C",
                "target_actual_field": "temperature_c",
                "target_path": "temperature_c",
                "conversion_mode": "transcoding",
                "formula": "TEMP_SENSOR_temperature",
                "source": "llm_generated",
                "status": "candidate",
            },
            {
                "concept_name": "无效候选",
                "source_fields": [],
                "target_field": "STATUS",
                "conversion_mode": "transcoding",
                "formula": "0",
                "source": "llm_generated",
                "status": "candidate",
            },
        ],
        protocol_dir=protocol_dir,
        target_protocol_name="Temp_Report",
        source_protocol_name="Temp_Sensor",
    )

    assert len(display_rules) == 2
    assert display_rules[0]["target_field"] == "temp_report_temperature_c"
    assert display_rules[0]["formula"] == "temp_report_temperature_c = temp_sensor_temperature"
    assert display_rules[1]["target_field"] == "temp_report_temperature_c"
    assert display_rules[1]["formula"] == "temp_report_temperature_c = 0"


def test_displayize_rule_records_can_plainify_writeback_fields(tmp_path):
    module = _load_api_module()
    source_dir, target_dir = _prepare_protocol_dirs(tmp_path)

    protocol_dir = module._merge_protocol_dirs([str(source_dir)], str(target_dir))
    display_rules = module._displayize_rule_records(
        [
            {
                "concept_name": "温度",
                "source_fields": ["TEMP_SENSOR_temperature"],
                "source_bindings": [
                    {
                        "alias_name": "TEMP_SENSOR_temperature",
                        "protocol": "Temp_Sensor",
                        "message_code": "TEMP_SENSOR",
                        "actual_field": "temperature",
                        "display_field": "temperature",
                        "source_path": "temperature",
                    }
                ],
                "target_field": "TEMPERATURE_C",
                "target_actual_field": "temperature_c",
                "target_path": "temperature_c",
                "target_protocol_type": "Temp_Report",
                "target_message_code": "TEMP_REPORT",
                "conversion_mode": "transcoding",
                "formula": "TEMP_SENSOR_temperature",
                "source": "llm_generated",
                "status": "candidate",
            }
        ],
        protocol_dir=protocol_dir,
        target_protocol_name="Temp_Report",
        source_protocol_name="Temp_Sensor",
        plain_writeback_fields=True,
    )

    assert len(display_rules) == 1
    assert display_rules[0]["target_field"] == "temperature_c"
    assert display_rules[0]["source_fields"] == ["temperature"]
    assert display_rules[0]["formula"] == "temperature_c = temperature"
    assert display_rules[0]["source_bindings"] == [
        {
            "alias_name": "temperature",
            "protocol": "Temp_Sensor",
            "message_code": "TEMP_SENSOR",
            "actual_field": "temperature",
            "display_field": "temperature",
            "source_path": "temperature",
        }
    ]


def test_manual_writeback_accepts_rules_only_and_mixed_source_protocols():
    module = _load_api_module()
    store = [
        {
            "protocol_type": "K1_6",
            "message_code": "K1.6",
            "target_protocol_type": "X0_5",
            "target_message_code": "X0.5",
            "target_field": "时间1",
            "source_fields": ["K1_6_小时", "K1_6_分钟", "K1_6_秒"],
            "formula": "result = (K1_6_小时* 60 +K1_6_分钟) * 60 +K1_6_秒",
        }
    ]

    class FakeKnowledgeBase:
        def __init__(self, protocol_type: str):
            self.protocol_type = protocol_type

        def list_rules(self, message_code=None, target_protocol_type=None, target_message_code=None):
            matched = []
            for item in store:
                if item["protocol_type"] != self.protocol_type:
                    continue
                if message_code and item["message_code"] != message_code:
                    continue
                if target_protocol_type and item["target_protocol_type"] != target_protocol_type:
                    continue
                if target_message_code and item["target_message_code"] != target_message_code:
                    continue
                matched.append(
                    SimpleNamespace(
                        target_field=item["target_field"],
                        source_fields=item["source_fields"],
                        formula=item["formula"],
                    )
                )
            return matched

        def upsert_generated_rules(
            self,
            rules,
            protocol_type=None,
            message_code=None,
            target_protocol_type=None,
            target_message_code=None,
            source="manual_review",
        ):
            written = []
            for item in rules:
                record = {
                    "protocol_type": str(item.get("protocol_type") or protocol_type),
                    "message_code": str(item.get("message_code") or message_code or ""),
                    "target_protocol_type": str(item.get("target_protocol_type") or target_protocol_type),
                    "target_message_code": str(item.get("target_message_code") or target_message_code or ""),
                    "target_field": str(item.get("target_field")),
                    "source_fields": list(item.get("source_fields") or []),
                    "formula": str(item.get("formula")),
                }
                store.append(record)
                written.append(
                    SimpleNamespace(
                        target_field=record["target_field"],
                        source_fields=record["source_fields"],
                        formula=record["formula"],
                        status="approved",
                        source=source,
                        edge_id=f"{record['protocol_type']}::{record['target_field']}",
                    )
                )
            return written

        def to_summary(self):
            return {"protocol_type": self.protocol_type}

    module.ProtocolConversionKnowledgeBase.load = lambda protocol_type: FakeKnowledgeBase(str(protocol_type))

    client = module.app.test_client()
    response = client.post(
        "/api/knowledge/protocol_rules/manual_writeback",
        json={
            "rules": [
                {
                    "target_field": "时间1",
                    "source_fields": ["K1_6_小时", "K1_6_分钟", "K1_6_秒"],
                    "formula": "result = (K1_6_小时* 60 +K1_6_分钟) * 60 +K1_6_秒",
                    "source_protocol_type": "K1_6",
                    "target_protocol_type": "X0_5",
                    "target_message_code": "X0.5",
                    "source_bindings": [
                        {"message_code": "K1.6"},
                        {"message_code": "K1.6"},
                    ],
                },
                {
                    "target_field": "高度",
                    "source_fields": ["K1_7_高度1"],
                    "formula": "int(K1_7_高度1) if K1_7_高度1 is not None else 1",
                    "source_protocol_type": "K1_7",
                    "target_protocol_type": "X0_5",
                    "target_message_code": "X0.5",
                    "source_bindings": [
                        {"message_code": "K1.7"},
                    ],
                },
            ]
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"]["total"] == 2
    assert payload["data"]["written"] == 2
    assert payload["data"]["created"] == 2
    assert payload["data"]["updated"] == 0
    assert payload["data"]["failed"] == 0
    assert sorted(payload["data"]["knowledge_graph"]["protocol_types"]) == ["K1_6", "K1_7"]
    assert len(payload["data"]["knowledge_graphs"]) == 2
    assert payload["data"]["results"][0]["action"] == "created"
    assert payload["data"]["results"][0]["protocol_type"] == "K1_6"
    assert payload["data"]["results"][0]["source_message_code"] == "K1.6"
    assert payload["data"]["results"][1]["action"] == "created"
    assert payload["data"]["results"][1]["protocol_type"] == "K1_7"
    assert payload["data"]["results"][1]["source_message_code"] == "K1.7"
    assert payload["data"]["results"][0]["target_field"] == "时间1"
    assert payload["data"]["results"][0]["source_fields"] == ["小时", "分钟", "秒"]
    assert payload["data"]["results"][0]["formula"] == "时间1 = (小时* 60 +分钟) * 60 +秒"


def test_ensure_explicit_target_formula_uses_prefixed_target_field():
    module = _load_api_module()

    assert module._build_explicit_formula_target_token(
        "X0_5",
        "时间1",
    ) == "x_0_5_时间1"
    assert module._ensure_explicit_target_formula(
        "K1_6_飞临时间",
        "x_0_5_时间1",
    ) == "x_0_5_时间1 = K1_6_飞临时间"
    assert module._ensure_explicit_target_formula(
        "result = (K1_6_小时* 3600 +K1_6_分钟* 60 +K1_6_秒)",
        "x_0_5_时间2",
    ) == "x_0_5_时间2 = K1_6_小时* 3600 +K1_6_分钟* 60 +K1_6_秒"
    assert module._ensure_explicit_target_formula(
        "x_0_5_纬度 = (k1_6_纬度 == x_0_5_纬度 ? k1_6_纬度 : 0)",
        "x_0_5_纬度",
    ) == "x_0_5_纬度 = k1_6_纬度"
    assert module._ensure_explicit_target_formula(
        "k_1_7_纬度1 = ((x0_5_纬度 == k_1_7_纬度1) ? int(x0_5_纬度) : 0)",
        "k_1_7_纬度1",
    ) == "k_1_7_纬度1 = int(x0_5_纬度)"


def test_normalize_manual_writeback_rules_strips_self_referential_target_guard():
    module = _load_api_module()

    normalized = module._normalize_manual_writeback_rules(
        [
            {
                "target_field": "纬度",
                "target_actual_field": "prolong_u7eacu5ea6",
                "target_path": "纬度",
                "source_fields": ["K1_6_纬度"],
                "formula": "x_0_5_纬度 = (K1_6_纬度 == x_0_5_纬度 ? K1_6_纬度 : 0)",
                "source_protocol_type": "K1_6",
                "target_protocol_type": "X0_5",
                "target_message_code": "X0.5",
            }
        ]
    )

    assert normalized[0]["target_field"] == "纬度"
    assert normalized[0]["source_fields"] == ["纬度"]
    assert normalized[0]["formula"] == "纬度 = 纬度"


def test_normalize_manual_writeback_rules_uses_last_target_path_segment_for_graph_field():
    module = _load_api_module()

    normalized = module._normalize_manual_writeback_rules(
        [
            {
                "target_field": "k_5_1_循环1_1_循环2_1_分支1_高程",
                "target_actual_field": "u5faau73af1_1_u5faau73af2_1_u5206u652f1_u9ad8u7a0b",
                "target_path": "循环1_1/循环2_1/分支1/高程",
                "source_fields": ["X0_5_高度"],
                "formula": "k_5_1_循环1_1_循环2_1_分支1_高程 = X0_5_高度",
                "source_protocol_type": "X0_5",
                "target_protocol_type": "K5_1",
                "target_message_code": "K5.1",
            }
        ]
    )

    assert normalized[0]["target_field"] == "高程"
    assert normalized[0]["source_fields"] == ["高度"]
    assert normalized[0]["formula"] == "高程 = 高度"


def test_normalize_cached_protocol_rules_response_plainifies_graph_target_fields(tmp_path):
    module = _load_api_module()
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "x0.5.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Field name="高度" type="uint">0</Field>\n</NameSpace>\n""",
        encoding="utf-8",
    )
    (target_dir / "k5.1.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>\n<NameSpace>\n  <Field name="循环1_1">\n    <Field name="循环2_1">\n      <Field name="分支1">\n        <Field name="高程" type="uint">0</Field>\n      </Field>\n    </Field>\n  </Field>\n</NameSpace>\n""",
        encoding="utf-8",
    )

    normalized_payload = module._normalize_cached_protocol_rules_response(
        {
            "conversion_rules_json": str(tmp_path / "rules.json"),
            "conversion_rules_yaml": str(tmp_path / "rules.yaml"),
            "kg_writeback_payload": {
                "rules": [
                    {
                        "concept_name": "高程",
                        "field_name": "高度",
                        "source_fields": ["高度"],
                        "source_bindings": [
                            {
                                "alias_name": "高度",
                                "protocol": "X0_5",
                                "message_code": "X0.5",
                                "actual_field": "高度",
                                "display_field": "高度",
                                "source_path": "高度",
                            }
                        ],
                        "source_protocol_type": "X0_5",
                        "source_protocol_name": "X0_5",
                        "source_message_code": "X0.5",
                        "target_protocol_type": "K5_1",
                        "target_message_code": "K5.1",
                        "target_field": "循环1_1/循环2_1/分支1/高程",
                        "target_path": "循环1_1/循环2_1/分支1/高程",
                        "target_actual_field": "u5faau73af1_1_u5faau73af2_1_u5206u652f1_u9ad8u7a0b",
                        "conversion_mode": "transcoding",
                        "formula": "循环1_1/循环2_1/分支1/高程 = 高度",
                        "source": "deterministic_match",
                        "status": "candidate",
                    }
                ]
            },
            "summary": {"knowledge_graph_field_count": 1},
        },
        [str(source_dir)],
        str(target_dir),
    )

    assert normalized_payload["summary"]["knowledge_graph_field_count"] == 1
    normalized_rule = normalized_payload["kg_writeback_payload"]["rules"][0]
    assert normalized_rule["target_field"] == "高程"
    assert normalized_rule["source_fields"] == ["高度"]
    assert normalized_rule["formula"] == "高程 = 高度"


def test_protocol_generate_rules_table_mode_extracts_csv_rules(tmp_path):
    module = _load_api_module()
    csv_path = tmp_path / "mapping_rules.csv"
    csv_path.write_text(
        "\n".join(
            [
                "目标字段,源字段,转换公式,说明",
                "时间1,小时|分钟|秒,\"(小时 * 3600) + (分钟 * 60) + 秒\",时间拼接",
                "状态,状态码,\"1=待机, 2=工作\",状态映射",
            ]
        ),
        encoding="utf-8",
    )

    client = module.app.test_client()
    response = client.post(
        "/api/knowledge/protocol_generate_rules",
        json={
            "input_mode": "table_rule",
            "table_rule_files": [str(csv_path)],
            "source_protocol_type": "K1_6",
            "source_message_code": "K1.6",
            "target_protocol_type": "X0_5",
            "target_message_code": "X0.5",
            "rules_output_dir": str(tmp_path / "output"),
        },
    )

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert Path(payload["conversion_rules_json"]).exists()
    assert payload["summary"]["input_mode"] == "table_rule"
    assert payload["summary"]["table_rule_count"] == 2
    assert payload["summary"]["table_file_count"] == 1
    assert payload["summary"]["parsed_table_count"] == 1
    assert payload["kg_writeback_payload"]["rules"][0]["target_field"] == "时间1"
    assert payload["kg_writeback_payload"]["rules"][0]["source_fields"] == ["小时", "分钟", "秒"]
    assert payload["kg_writeback_payload"]["rules"][0]["formula"] == "时间1 = (小时 * 3600) + (分钟 * 60) + 秒"
    assert payload["kg_writeback_payload"]["rules"][0]["protocol_type"] == "K1_6"
    assert payload["kg_writeback_payload"]["rules"][0]["target_protocol_type"] == "X0_5"
    assert payload["kg_writeback_payload"]["rules"][1]["conversion_mode"] == "mapping"
    assert payload["relations"][0]["relation_id"] == "mapping_rules"
    assert payload["relations"][0]["target_protocol"] == "X0_5"
    assert payload["validation_result"] == {
        "field_legality": True,
        "position_accuracy": True,
        "conversion_logic": True,
        "protocol_compliance": True,
    }


def test_protocol_generate_rules_table_mode_extracts_inline_csv_rules(tmp_path):
    module = _load_api_module()
    csv_content = "\n".join(
        [
            "目标字段,源字段,转换公式,说明",
            "时间1,小时|分钟|秒,\"(小时 * 3600) + (分钟 * 60) + 秒\",时间拼接",
            "状态,状态码,\"1=待机, 2=工作\",状态映射",
        ]
    )

    client = module.app.test_client()
    response = client.post(
        "/api/knowledge/protocol_generate_rules",
        json={
            "input_mode": "table_rule",
            "table_rule_files": [
                {
                    "file_name": "mapping_rules.csv",
                    "content": csv_content,
                }
            ],
            "source_protocol_type": "K1_6",
            "source_message_code": "K1.6",
            "target_protocol_type": "X0_5",
            "target_message_code": "X0.5",
            "rules_output_dir": str(tmp_path / "output"),
        },
    )

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert Path(payload["conversion_rules_json"]).exists()
    assert payload["summary"]["input_mode"] == "table_rule"
    assert payload["summary"]["table_rule_count"] == 2
    assert payload["summary"]["table_file_count"] == 1
    assert payload["summary"]["parsed_table_count"] == 1
    assert payload["kg_writeback_payload"]["rules"][0]["target_field"] == "时间1"
    assert payload["kg_writeback_payload"]["rules"][0]["source_fields"] == ["小时", "分钟", "秒"]
    assert payload["kg_writeback_payload"]["rules"][1]["conversion_mode"] == "mapping"


def test_upload_table_rule_returns_server_path_and_generated_request(tmp_path):
    module = _load_api_module()
    csv_content = "\n".join(
        [
            "目标字段,源字段,转换公式,说明",
            "时间1,小时|分钟|秒,\"(小时 * 3600) + (分钟 * 60) + 秒\",时间拼接",
            "状态,状态码,\"1=待机, 2=工作\",状态映射",
        ]
    ).encode("utf-8")

    client = module.app.test_client()
    upload_response = client.post(
        "/api/knowledge/upload_table_rule",
        data={
            "file": (io.BytesIO(csv_content), "mapping_rules.csv"),
            "source_protocol_type": "K1_6",
            "source_message_code": "K1.6",
            "target_protocol_type": "X0_5",
            "target_message_code": "X0.5",
        },
        content_type="multipart/form-data",
    )

    assert upload_response.status_code == 200
    upload_payload = upload_response.get_json()["data"]
    saved_path = Path(upload_payload["file_path"])
    assert saved_path.exists()
    assert upload_payload["next_request"]["path"] == "/api/knowledge/protocol_generate_rules"
    assert upload_payload["next_request"]["body"]["table_rule_files"] == [str(saved_path)]

    generate_response = client.post(
        "/api/knowledge/protocol_generate_rules",
        json={
            **upload_payload["next_request"]["body"],
            "rules_output_dir": str(tmp_path / "output"),
        },
    )
    assert generate_response.status_code == 200
    generate_payload = generate_response.get_json()["data"]
    assert generate_payload["summary"]["table_rule_count"] == 2
    assert generate_payload["kg_writeback_payload"]["rules"][0]["target_field"] == "时间1"


def test_protocol_generate_rules_table_mode_extracts_docx_table(tmp_path):
    module = _load_api_module()
    docx_path = tmp_path / "word_rules.docx"

    from docx import Document

    document = Document()
    table = document.add_table(rows=3, cols=4)
    table.rows[0].cells[0].text = "目标字段"
    table.rows[0].cells[1].text = "源字段"
    table.rows[0].cells[2].text = "转换公式"
    table.rows[0].cells[3].text = "说明"
    table.rows[1].cells[0].text = "俯仰角"
    table.rows[1].cells[1].text = "俯仰1"
    table.rows[1].cells[2].text = "(5 if 俯仰1 == 5 else 0)"
    table.rows[1].cells[3].text = "姿态修正"
    table.rows[2].cells[0].text = "经度"
    table.rows[2].cells[1].text = "经度原值"
    table.rows[2].cells[2].text = "经度原值"
    table.rows[2].cells[3].text = "直接映射"
    document.save(docx_path)

    client = module.app.test_client()
    response = client.post(
        "/api/knowledge/protocol_generate_rules",
        json={
            "input_mode": "table_rule",
            "table_rule_files": [str(docx_path)],
            "source_protocol_type": "K1_7",
            "target_protocol_type": "X0_5",
            "target_message_code": "X0.5",
            "rules_output_dir": str(tmp_path / "output"),
        },
    )

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["summary"]["table_rule_count"] == 2
    assert payload["kg_writeback_payload"]["rules"][0]["target_field"] == "俯仰角"
    assert payload["kg_writeback_payload"]["rules"][0]["source_fields"] == ["俯仰1"]
    assert payload["kg_writeback_payload"]["rules"][0]["formula"] == "俯仰角 = 5 if 俯仰1 == 5 else 0"
    assert payload["kg_writeback_payload"]["rules"][1]["formula"] == "经度 = 经度原值"
