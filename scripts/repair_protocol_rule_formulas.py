from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[1]
RULES_ROOT = ROOT / "07_protocol_generate_rules"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from protocol_conversion.generator import (  # noqa: E402
    _filter_valid_generated_rules,
    _formula_references_target_field,
    _infer_formula_kind,
    _normalize_formula_expression_syntax,
    _normalize_python_block_target_assignments,
    _normalize_rule_field_token,
    _strip_explicit_target_assignment,
    _strip_self_referential_target_guard_at_source,
)
from protocol_conversion.knowledge_base import ProtocolConversionKnowledgeBase  # noqa: E402


FORMULA_KEYS = ("formula", "expr", "expression")


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _parse_source_fields(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in text.replace("；", ";").split(";") if item.strip()]
    return []


def _source_fields_from_mapping(item: Dict[str, Any]) -> List[str]:
    for key in ("source_fields", "source_fields_json", "fullLabelFrom", "full_label_from"):
        fields = _parse_source_fields(item.get(key))
        if fields:
            return fields
    field_name = str(item.get("field_name") or item.get("source_field") or "").strip()
    return [field_name] if field_name else []


def _extract_formula_value(item: Dict[str, Any]) -> str:
    for key in FORMULA_KEYS:
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _repair_formula(
    formula: str,
    target_field: Any,
    target_protocol_type: Optional[str],
    source_fields: Iterable[Any],
) -> Dict[str, Any]:
    original = str(formula or "").strip()
    if not original:
        return {"formula": original, "changed": False, "target_dependency": False}

    stripped_assignment = _strip_explicit_target_assignment(
        original,
        target_field=target_field,
        target_protocol_type=target_protocol_type,
    )
    normalized = stripped_assignment
    formula_kind = _infer_formula_kind(normalized)
    if formula_kind == "python_expr":
        normalized = _normalize_formula_expression_syntax(normalized)
        normalized = _strip_self_referential_target_guard_at_source(
            normalized,
            target_field=target_field,
            target_protocol_type=target_protocol_type,
            source_fields=source_fields,
        )
    elif formula_kind == "python_block":
        normalized = _normalize_python_block_target_assignments(
            normalized,
            target_field=target_field,
            target_protocol_type=target_protocol_type,
        )
    target_dependency = _formula_references_target_field(
        normalized,
        target_field=target_field,
        target_protocol_type=target_protocol_type,
        source_fields=source_fields,
    )
    return {
        "formula": normalized,
        "changed": normalized != original,
        "target_dependency": target_dependency,
    }


def _backup(path: Path) -> Path:
    backup_path = path.with_suffix(path.suffix + f".bak_{_timestamp()}")
    shutil.copy2(path, backup_path)
    return backup_path


def repair_json_kb(paths: Iterable[Path], dry_run: bool) -> List[Dict[str, Any]]:
    reports: List[Dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        changed = 0
        target_dependencies = 0
        samples: List[Dict[str, Any]] = []
        for index, edge in enumerate(data.get("edges") or []):
            if not isinstance(edge, dict):
                continue
            formula = _extract_formula_value(edge)
            if not formula:
                continue
            source_fields = _source_fields_from_mapping(edge)
            target_field = edge.get("target_field") or edge.get("target_actual_field") or edge.get("field_name")
            target_protocol_type = str(edge.get("target_protocol_type") or "").strip() or None
            repaired = _repair_formula(formula, target_field, target_protocol_type, source_fields)
            if repaired["target_dependency"]:
                target_dependencies += 1
            if not repaired["changed"]:
                continue
            changed += 1
            samples.append(
                {
                    "index": index,
                    "target_field": target_field,
                    "source_fields": source_fields,
                    "before": formula,
                    "after": repaired["formula"],
                    "target_dependency": repaired["target_dependency"],
                }
            )
            edge["formula"] = repaired["formula"]
            if str(edge.get("expr") or "").strip() == formula:
                edge["expr"] = repaired["formula"]
            edge["formula_repair_note"] = "normalized_by_repair_protocol_rule_formulas"
        backup_path = None
        if changed and not dry_run:
            backup_path = _backup(path)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        reports.append(
            {
                "path": str(path),
                "changed": changed,
                "target_dependencies": target_dependencies,
                "backup_path": str(backup_path) if backup_path else None,
                "samples": samples[:10],
            }
        )
    return reports


def _neo4j_rows(kb: ProtocolConversionKnowledgeBase) -> List[Dict[str, Any]]:
    return kb._run_cypher(
        """
        MATCH ()-[r]->()
        WHERE r.formula IS NOT NULL OR r.expr IS NOT NULL OR r.expression IS NOT NULL
        RETURN elementId(r) AS rel_element_id,
               type(r) AS rel_type,
               r.rule_id AS rule_id,
               r.formula AS formula,
               r.expr AS expr,
               r.expression AS expression,
               r.target_field AS target_field,
               r.target_actual_field AS target_actual_field,
               r.target_protocol_type AS target_protocol_type,
               r.source_fields AS source_fields,
               r.source_fields_json AS source_fields_json,
               r.fullLabelFrom AS fullLabelFrom,
               r.status AS status
        """
    )


def repair_neo4j(protocol_type: str, dry_run: bool) -> Dict[str, Any]:
    kb = ProtocolConversionKnowledgeBase.load(protocol_type)
    summary = kb.to_summary()
    rows = _neo4j_rows(kb)
    changed_rows: List[Dict[str, Any]] = []
    target_dependency_rows: List[Dict[str, Any]] = []
    for row in rows:
        formula = str(row.get("formula") or row.get("expr") or row.get("expression") or "").strip()
        if not formula:
            continue
        if ":" in formula and not str(row.get("formula") or "").strip():
            _prefix, formula = formula.split(":", 1)
            formula = formula.strip()
        source_fields = (
            _parse_source_fields(row.get("source_fields_json"))
            or _parse_source_fields(row.get("source_fields"))
            or _parse_source_fields(row.get("fullLabelFrom"))
        )
        target_field = row.get("target_field") or row.get("target_actual_field")
        target_protocol_type = str(row.get("target_protocol_type") or "").strip() or None
        repaired = _repair_formula(formula, target_field, target_protocol_type, source_fields)
        if repaired["target_dependency"]:
            target_dependency_rows.append(
                {
                    "rule_id": row.get("rule_id"),
                    "rel_type": row.get("rel_type"),
                    "target_field": target_field,
                    "source_fields": source_fields,
                    "formula": repaired["formula"],
                }
            )
            continue
        if not repaired["changed"]:
            continue
        changed_rows.append(
            {
                "rule_id": row.get("rule_id"),
                "rel_element_id": row.get("rel_element_id"),
                "rel_type": row.get("rel_type"),
                "target_field": target_field,
                "source_fields": source_fields,
                "before": formula,
                "after": repaired["formula"],
            }
        )
        if dry_run:
            continue
        expr = str(row.get("expr") or "").strip()
        new_expr = expr
        if expr == formula:
            new_expr = repaired["formula"]
        elif ":" in expr and expr.rsplit(":", 1)[-1].strip() == formula:
            prefix = expr.rsplit(":", 1)[0]
            new_expr = f"{prefix}:{repaired['formula']}"
        kb._run_cypher(
            """
            MATCH ()-[r]->()
            WHERE elementId(r) = $rel_element_id
            SET r.formula = CASE WHEN r.formula IS NULL THEN r.formula ELSE $formula END,
                r.expr = CASE WHEN r.expr IS NULL THEN r.expr ELSE $expr END,
                r.expression = CASE WHEN r.expression IS NULL THEN r.expression ELSE $formula END,
                r.formula_repair_note = $repair_note,
                r.formula_repaired_at = datetime()
            """,
            {
                "rel_element_id": row.get("rel_element_id"),
                "formula": repaired["formula"],
                "expr": new_expr,
                "repair_note": "normalized_by_repair_protocol_rule_formulas",
            },
        )
    if changed_rows and not dry_run:
        kb._invalidate_external_cache()
    return {
        "summary": summary,
        "scanned": len(rows),
        "changed": len(changed_rows),
        "target_dependencies": len(target_dependency_rows),
        "changed_rows": changed_rows[:20],
        "target_dependency_rows": target_dependency_rows[:20],
    }


def validate_neo4j(protocol_type: str) -> Dict[str, Any]:
    kb = ProtocolConversionKnowledgeBase.load(protocol_type)
    rules = kb.list_rules()
    invalid: List[Dict[str, Any]] = []
    for rule in rules:
        generated = {
            "target_field": rule.target_field,
            "target_protocol_type": rule.target_protocol_type,
            "source_fields": list(rule.source_fields or []),
            "formula_kind": rule.formula_kind or _infer_formula_kind(rule.formula),
            "rule": rule.formula,
        }
        valid, filtered = _filter_valid_generated_rules(generated_rules=[generated], available_source_fields=rule.source_fields or [])
        if not valid:
            invalid.append(
                {
                    "rule_id": rule.edge_id,
                    "target_field": rule.target_field,
                    "source_fields": list(rule.source_fields or []),
                    "formula": rule.formula,
                    "reason": filtered[0].get("filtered_reason") if filtered else "unknown",
                }
            )
    return {"rules": len(rules), "invalid": len(invalid), "invalid_samples": invalid[:20]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--protocol-type", default="K1_7")
    parser.add_argument("--report", default=str(ROOT / "test" / "output" / f"protocol_rule_formula_repair_{_timestamp()}.json"))
    args = parser.parse_args()

    json_paths = [
        ROOT / "07_protocol_generate_rules" / "data" / "protocol_conversion_kb" / "link16_value_graph.json",
        ROOT / "05_generate_qa" / "data" / "protocol_conversion_kb" / "link16_value_graph.json",
        ROOT / "10_rule_evaluate" / "data" / "protocol_conversion_kb" / "link16_value_graph.json",
    ]
    report = {
        "dry_run": args.dry_run,
        "json": repair_json_kb(json_paths, dry_run=args.dry_run),
        "neo4j": repair_neo4j(args.protocol_type, dry_run=args.dry_run),
        "validation": validate_neo4j(args.protocol_type),
        "env": {
            "PROTOCOL_CONVERSION_GRAPH_BACKEND": os.getenv("PROTOCOL_CONVERSION_GRAPH_BACKEND"),
            "PROTOCOL_CONVERSION_NEO4J_URI": os.getenv("PROTOCOL_CONVERSION_NEO4J_URI"),
            "PROTOCOL_CONVERSION_NEO4J_SCHEMA_MODE": os.getenv("PROTOCOL_CONVERSION_NEO4J_SCHEMA_MODE"),
        },
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
