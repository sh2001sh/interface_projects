from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import torch


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from protocol_conversion.rule_evaluation import evaluate_protocol_rules
from protocol_conversion.evaluation import EvaluationBackend, ModelLoadResult, _candidate_paths, evaluate_protocol_conversion
from retrieval.reranker import inspect_reranker_model_dir


def _write_xml(directory: Path, filename: str, field_names: list[str]) -> None:
    items = "\n".join(f'    <Item name="{field_name}">8</Item>' for field_name in field_names)
    xml = f"<Root>\n  <NameSpace name=\"消息\">\n{items}\n  </NameSpace>\n</Root>\n"
    (directory / filename).write_text(xml, encoding="utf-8")


def test_inspect_reranker_model_dir_accepts_qwen3_causallm_reranker(tmp_path):
    model_dir = tmp_path / "Qwen3-Reranker-0___6B"
    model_dir.mkdir()
    (model_dir / "config.json").write_text(
        json.dumps(
            {
                "architectures": ["Qwen3ForCausalLM"],
                "model_type": "qwen3",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (model_dir / "README.md").write_text(
        "# Qwen3-Reranker-0.6B\n\nThis is a text reranking model.\n",
        encoding="utf-8",
    )

    inspection = inspect_reranker_model_dir(model_dir)

    assert inspection["compatible"] is True
    assert inspection["loader_type"] == "causal_lm_reranker"


def test_candidate_paths_prefer_configured_underscore_model_dirs(tmp_path):
    embed_dir = tmp_path / "Qwen" / "Qwen3-Embedding-0___6B"
    rerank_dir = tmp_path / "Qwen3-Reranker-0___6B"
    embed_dir.mkdir(parents=True)
    rerank_dir.mkdir(parents=True)

    with patch("config.EMBED_MODEL_DIR", str(embed_dir)), patch("config.RERANK_MODEL_DIR", str(rerank_dir)):
        embed_candidates = _candidate_paths("embed")
        rerank_candidates = _candidate_paths("rerank")

    assert str(embed_candidates[0]) == str(embed_dir)
    assert str(rerank_candidates[0]) == str(rerank_dir)


def test_rule_metrics_accuracy_only_counts_convertible_fields(tmp_path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    _write_xml(source_dir, "source.xml", ["FIELD_A"])
    _write_xml(target_dir, "target.xml", ["FIELD_A", "FIELD_B"])

    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "target_field": "x_0_5_FIELD_A",
                    "target_actual_field": "FIELD_A",
                    "target_path": "FIELD_A",
                    "source_fields": ["FIELD_A"],
                    "formula": "FIELD_A",
                    "rule_type": "direct",
                },
                {
                    "target_field": "x_0_5_FIELD_B",
                    "target_actual_field": "FIELD_B",
                    "target_path": "FIELD_B",
                    "source_fields": [],
                    "formula": "0",
                    "rule_type": "const",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = evaluate_protocol_rules(
        source_protocol_dirs=[str(source_dir)],
        target_protocol_dir=str(target_dir),
        conversion_rules=str(rules_path),
        coarse_top_k=5,
        coarse_similarity_threshold=0.0,
        fine_similarity_threshold=0.0,
        use_model_inference=False,
        allow_modelscope_download=False,
    )

    assert result["summary"]["target_field_count"] == 2
    assert result["summary"]["convertible_field_count"] == 1
    assert result["summary"]["successful_converted_field_count"] == 1
    assert result["summary"]["non_zero_rule_count"] == 1
    assert result["scores"]["field_match_accuracy"] == 100.0
    assert result["scores"]["field_coverage_rate"] == 100.0
    assert result["scores"]["final_conversion_rate"] == 50.0
    fallback = next(item for item in result["field_results"] if item["status"] == "fallback_zero")
    assert fallback["semantic_fidelity"] == 0.0
    assert fallback["structure_integrity"] == 0.0


def test_rule_metrics_field_match_accuracy_uses_success_over_convertible(tmp_path):
    source_dir = tmp_path / "source_multi"
    target_dir = tmp_path / "target_multi"
    source_dir.mkdir()
    target_dir.mkdir()
    _write_xml(source_dir, "source.xml", ["FIELD_A"])
    _write_xml(target_dir, "target.xml", ["FIELD_A", "FIELD_B"])

    rules_path = tmp_path / "rules_multi.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "target_field": "x_0_5_FIELD_A",
                    "target_actual_field": "FIELD_A",
                    "target_path": "FIELD_A",
                    "source_fields": ["FIELD_A"],
                    "formula": "FIELD_A",
                    "rule_type": "direct",
                },
                {
                    "target_field": "x_0_5_FIELD_B",
                    "target_actual_field": "FIELD_B",
                    "target_path": "FIELD_B",
                    "source_fields": ["FIELD_B_SOURCE"],
                    "formula": "FIELD_B_SOURCE",
                    "rule_type": "direct",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = evaluate_protocol_rules(
        source_protocol_dirs=[str(source_dir)],
        target_protocol_dir=str(target_dir),
        conversion_rules=str(rules_path),
        coarse_top_k=5,
        coarse_similarity_threshold=0.0,
        fine_similarity_threshold=0.0,
        use_model_inference=False,
        allow_modelscope_download=False,
    )

    assert result["summary"]["convertible_field_count"] == 2
    assert result["summary"]["successful_converted_field_count"] == 1
    assert result["scores"]["field_match_accuracy"] == 50.0
    assert result["scores"]["final_conversion_rate"] == 50.0


def test_expression_rule_passes_without_fine_rerank_hits(tmp_path):
    source_dir = tmp_path / "source_expr"
    target_dir = tmp_path / "target_expr"
    source_dir.mkdir()
    target_dir.mkdir()
    _write_xml(source_dir, "k1.6.xml", ["小时", "分钟", "秒"])
    _write_xml(target_dir, "x0.5.xml", ["时间1"])

    rules_path = tmp_path / "expr_rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "target_field": "x_0_5_时间1",
                    "target_actual_field": "时间1",
                    "target_path": "时间1",
                    "source_fields": ["小时", "分钟", "秒"],
                    "formula": "(小时*3600 + 分钟*60 + 秒)",
                    "rule_type": "expression",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = evaluate_protocol_rules(
        source_protocol_dirs=[str(source_dir)],
        target_protocol_dir=str(target_dir),
        conversion_rules=str(rules_path),
        coarse_top_k=5,
        coarse_similarity_threshold=0.0,
        fine_similarity_threshold=1.1,
        use_model_inference=False,
        allow_modelscope_download=False,
    )

    field_result = result["field_results"][0]
    assert field_result["status"] == "pass"
    assert field_result["field_match_correctness"] == 100.0
    assert field_result["semantic_fidelity"] >= 80.0
    assert result["summary"]["successful_converted_field_count"] == 1


def test_rule_metrics_include_embedding_and_reranker_parameter_counts(tmp_path):
    source_dir = tmp_path / "source_meta"
    target_dir = tmp_path / "target_meta"
    source_dir.mkdir()
    target_dir.mkdir()
    _write_xml(source_dir, "k1.6.xml", ["小时"])
    _write_xml(target_dir, "x0.5.xml", ["时间1"])

    rules_path = tmp_path / "meta_rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "target_field": "x_0_5_时间1",
                    "target_actual_field": "时间1",
                    "target_path": "时间1",
                    "source_fields": ["小时"],
                    "formula": "小时",
                    "rule_type": "direct",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with patch(
        "protocol_conversion.rule_evaluation.resolve_model_metadata",
        side_effect=[
            {
                "model_name": "embed-demo",
                "parameter_count": 11,
                "parameter_count_display": "11",
                "parameter_count_source": "test",
                "model_dir": "/tmp/embed",
            },
            {
                "model_name": "rerank-demo",
                "parameter_count": 22,
                "parameter_count_display": "22",
                "parameter_count_source": "test",
                "model_dir": "/tmp/rerank",
            },
        ],
    ):
        result = evaluate_protocol_rules(
            source_protocol_dirs=[str(source_dir)],
            target_protocol_dir=str(target_dir),
            conversion_rules=str(rules_path),
            coarse_top_k=5,
            coarse_similarity_threshold=0.0,
            fine_similarity_threshold=1.1,
            use_model_inference=False,
            allow_modelscope_download=False,
        )

    assert result["summary"]["embedding_parameter_count"] == 11
    assert result["summary"]["reranker_parameter_count"] == 22
    assert result["strategy"]["embedding_model_meta"]["model_name"] == "embed-demo"
    assert result["strategy"]["reranker_model_meta"]["model_name"] == "rerank-demo"


def test_rule_metrics_resolve_prefixed_source_field_names(tmp_path):
    source_dir = tmp_path / "source_prefixed"
    target_dir = tmp_path / "target_prefixed"
    source_dir.mkdir()
    target_dir.mkdir()
    _write_xml(source_dir, "k1.6.xml", ["小时", "分钟", "秒"])
    _write_xml(target_dir, "x0.5.xml", ["时间1"])

    rules_path = tmp_path / "prefixed_rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "target_field": "x_0_5_时间1",
                    "target_actual_field": "时间1",
                    "target_path": "时间1",
                    "source_fields": ["k1_6_小时", "k1_6_分钟", "k1_6_秒"],
                    "formula": "(k1_6_小时*3600 + k1_6_分钟*60 + k1_6_秒)",
                    "rule_type": "expression",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = evaluate_protocol_rules(
        source_protocol_dirs=[str(source_dir)],
        target_protocol_dir=str(target_dir),
        conversion_rules=str(rules_path),
        coarse_top_k=5,
        coarse_similarity_threshold=0.0,
        fine_similarity_threshold=1.1,
        use_model_inference=False,
        allow_modelscope_download=False,
    )

    field_result = result["field_results"][0]
    assert field_result["status"] == "pass"
    assert field_result["resolved_source_fields"] == ["小时", "分钟", "秒"]
    assert result["scores"]["field_match_accuracy"] == 100.0
    assert result["scores"]["final_conversion_rate"] == 100.0


def test_rule_evaluate_can_export_payload_and_return_path(tmp_path):
    source_dir = tmp_path / "source_export"
    target_dir = tmp_path / "target_export"
    source_dir.mkdir()
    target_dir.mkdir()
    _write_xml(source_dir, "k1.6.xml", ["小时"])
    _write_xml(target_dir, "x0.5.xml", ["时间1"])

    rules_path = tmp_path / "export_rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "target_field": "x_0_5_时间1",
                    "target_actual_field": "时间1",
                    "target_path": "时间1",
                    "source_fields": ["小时"],
                    "formula": "小时",
                    "rule_type": "direct",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class FakeEmbedding:
        def encode(self, texts, max_length=512):
            rows = []
            for index, _ in enumerate(texts):
                rows.append([float(index + 1), float(index + 2), float(index + 3)])
            return torch.tensor(rows, dtype=torch.float32)

    fake_backend = EvaluationBackend(
        embedding=FakeEmbedding(),
        embedding_info=ModelLoadResult("qwen3_embedding", True, "/tmp/embed", False),
        reranker=None,
        reranker_info=ModelLoadResult("fallback_text_similarity", True, None, False),
    )

    export_root = tmp_path / "milvus_exports"
    with patch("protocol_conversion.rule_evaluation._load_backends", return_value=fake_backend), patch(
        "protocol_conversion.rule_evaluation.resolve_model_metadata",
        side_effect=[
            {
                "model_name": "embed-demo",
                "parameter_count": 3,
                "parameter_count_display": "3",
                "parameter_count_source": "test",
                "model_dir": "/tmp/embed",
            },
            {
                "model_name": "rerank-demo",
                "parameter_count": 0,
                "parameter_count_display": "0",
                "parameter_count_source": "test",
                "model_dir": None,
            },
        ],
    ), patch("protocol_conversion.rule_evaluation._default_export_root", return_value=export_root):
        result = evaluate_protocol_rules(
            source_protocol_dirs=[str(source_dir)],
            target_protocol_dir=str(target_dir),
            conversion_rules=str(rules_path),
            coarse_top_k=5,
            coarse_similarity_threshold=0.0,
            fine_similarity_threshold=1.1,
            use_model_inference=True,
            allow_modelscope_download=False,
            export_payload=True,
            export_name="rule_eval_test",
        )

    export_path = Path(result["export_path"])
    assert export_path.exists()

    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload["format"] == "milvus_insert_rows_v1"
    assert payload["collection_name"] == "rule_eval_test"
    assert payload["row_count"] == 2
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["semantic_type"] in {"source_field", "target_field"}
    assert len(payload["rows"][0]["embedding"]) == 3


def test_rule_evaluate_export_requires_real_embedding(tmp_path):
    source_dir = tmp_path / "source_export_disabled"
    target_dir = tmp_path / "target_export_disabled"
    source_dir.mkdir()
    target_dir.mkdir()
    _write_xml(source_dir, "k1.6.xml", ["小时"])
    _write_xml(target_dir, "x0.5.xml", ["时间1"])

    rules_path = tmp_path / "export_disabled_rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "target_field": "x_0_5_时间1",
                    "target_actual_field": "时间1",
                    "target_path": "时间1",
                    "source_fields": ["小时"],
                    "formula": "小时",
                    "rule_type": "direct",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    try:
        evaluate_protocol_rules(
            source_protocol_dirs=[str(source_dir)],
            target_protocol_dir=str(target_dir),
            conversion_rules=str(rules_path),
            coarse_top_k=5,
            coarse_similarity_threshold=0.0,
            fine_similarity_threshold=1.1,
            use_model_inference=False,
            allow_modelscope_download=False,
            export_payload=True,
        )
    except ValueError as exc:
        assert "export_payload=true 时必须启用真实 embedding 模型推理" in str(exc)
    else:
        raise AssertionError("expected ValueError when export_milvus_payload is enabled without embedding")


def test_conversion_correctness_only_aggregates_converted_fields():
    result = evaluate_protocol_conversion(
        converted_message={"FIELD_A": "100"},
        reference_message={"FIELD_A": "100", "FIELD_B": "200"},
        source_message={"FIELD_A": "100"},
        use_model_inference=False,
        allow_modelscope_download=False,
    )

    assert result["summary"]["expected_field_count"] == 2
    assert result["summary"]["converted_field_count"] == 1
    assert result["summary"]["missing_field_count"] == 1
    assert result["correctness_score"] == 100.0
    assert result["semantic_similarity"] == 100.0
    assert result["rerank_score"] == 100.0
    assert result["information_loss_score"] == 50.0
    assert result["conversion_rate"] == 50.0
