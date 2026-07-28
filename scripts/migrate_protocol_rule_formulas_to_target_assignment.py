#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
INTERFACE7_ROOT = REPO_ROOT / "07_protocol_generate_rules"
INTERFACE7_APP = INTERFACE7_ROOT / "api_03_extract_validate" / "app.py"


def _load_interface7_module():
    if str(INTERFACE7_ROOT) not in sys.path:
        sys.path.insert(0, str(INTERFACE7_ROOT))
    spec = importlib.util.spec_from_file_location("interface7_formula_migration", INTERFACE7_APP)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {INTERFACE7_APP}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse_source_fields(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in text.split(",") if item.strip()]


def _build_rule_payload(rule: Any, new_formula: str) -> dict[str, Any]:
    return {
        "concept_name": getattr(rule, "concept_name", None),
        "field_name": getattr(rule, "field_name", None),
        "source_fields": list(getattr(rule, "source_fields", None) or []),
        "protocol_type": getattr(rule, "protocol_type", None),
        "message_code": getattr(rule, "message_code", None),
        "target_field": getattr(rule, "target_field", None),
        "target_protocol_type": getattr(rule, "target_protocol_type", None),
        "target_message_code": getattr(rule, "target_message_code", None),
        "conversion_mode": getattr(rule, "conversion_mode", None),
        "formula": new_formula,
        "description": getattr(rule, "description", None),
        "confidence": getattr(rule, "confidence", None),
        "unit": getattr(rule, "unit", None),
        "bit_length": getattr(rule, "bit_length", None),
        "status": getattr(rule, "status", None),
        "source": getattr(rule, "source", None),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate KG rule formulas to explicit target-field assignments.")
    parser.add_argument("--apply", action="store_true", help="Apply migration instead of dry-run.")
    args = parser.parse_args()

    module = _load_interface7_module()
    ProtocolConversionKnowledgeBase = module.ProtocolConversionKnowledgeBase

    seed_kb = ProtocolConversionKnowledgeBase.load("K1_6")
    protocol_rows = seed_kb._run_cypher(
        """
        MATCH ()-[r:MAP_TO]->()
        WHERE coalesce(r.source_protocol_type, '') <> ''
        RETURN r.source_protocol_type AS protocol_type, count(DISTINCT r.rule_id) AS rule_count
        ORDER BY protocol_type
        """
    )
    rule_rows = seed_kb._run_cypher(
        """
        MATCH ()-[r:MAP_TO]->()
        WHERE coalesce(r.rule_id, '') <> ''
        WITH r.rule_id AS rule_id, head(collect(r)) AS rel
        RETURN rule_id,
               rel.source_protocol_type AS protocol_type,
               rel.source_message_code AS message_code,
               rel.target_protocol_type AS target_protocol_type,
               rel.target_message_code AS target_message_code,
               rel.target_field AS target_field,
               rel.source_fields_json AS source_fields_json,
               rel.formula AS formula,
               rel.desc AS description,
               rel.conversion_mode AS conversion_mode,
               rel.concept_name AS concept_name,
               rel.source AS source,
               rel.status AS status,
               rel.confidence AS confidence
        ORDER BY protocol_type, rule_id
        """
    )
    rules_by_protocol: dict[str, list[Any]] = defaultdict(list)
    for row in rule_rows:
        protocol_type = str(row.get("protocol_type") or "").strip()
        if not protocol_type:
            continue
        rules_by_protocol[protocol_type].append(
            SimpleNamespace(
                edge_id=row.get("rule_id"),
                protocol_type=protocol_type,
                message_code=row.get("message_code"),
                target_protocol_type=row.get("target_protocol_type"),
                target_message_code=row.get("target_message_code"),
                target_field=row.get("target_field"),
                source_fields=_parse_source_fields(row.get("source_fields_json")),
                formula=row.get("formula"),
                description=row.get("description"),
                conversion_mode=row.get("conversion_mode"),
                concept_name=row.get("concept_name"),
                source=row.get("source"),
                status=row.get("status"),
                confidence=row.get("confidence"),
                field_name=((_parse_source_fields(row.get("source_fields_json")) or [None])[0]),
            )
        )

    report: dict[str, Any] = {
        "apply": bool(args.apply),
        "protocols": [],
        "total_rules": 0,
        "changed_rules": 0,
        "deleted_legacy_rules": 0,
    }

    for row in protocol_rows:
        protocol_type = str(row.get("protocol_type") or "").strip()
        if not protocol_type:
            continue
        kb = ProtocolConversionKnowledgeBase.load(protocol_type)
        rules = list(rules_by_protocol.get(protocol_type) or [])
        report["total_rules"] += len(rules)

        changed = 0
        deleted = 0
        samples: list[dict[str, Any]] = []
        scope_cache: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for existing_rule in rules:
            scope_key = (
                str(getattr(existing_rule, "message_code", None) or "").strip(),
                str(getattr(existing_rule, "target_protocol_type", None) or "").strip(),
                str(getattr(existing_rule, "target_message_code", None) or "").strip(),
            )
            scope_cache[scope_key].append(existing_rule)

        for rule in rules:
            target_token = module._build_explicit_formula_target_token(
                getattr(rule, "target_protocol_type", None),
                getattr(rule, "target_field", None),
                None,
                getattr(rule, "target_field", None),
            )
            new_formula = module._ensure_explicit_target_formula(getattr(rule, "formula", None), target_token)
            old_formula = str(getattr(rule, "formula", None) or "").strip()
            if old_formula == new_formula:
                continue

            changed += 1
            if len(samples) < 5:
                samples.append(
                    {
                        "rule_id": getattr(rule, "edge_id", None),
                        "target_field": getattr(rule, "target_field", None),
                        "source_fields": list(getattr(rule, "source_fields", None) or []),
                        "old_formula": old_formula,
                        "new_formula": new_formula,
                    }
                )

            if not args.apply:
                continue

            scope_key = (
                str(getattr(rule, "message_code", None) or "").strip(),
                str(getattr(rule, "target_protocol_type", None) or "").strip(),
                str(getattr(rule, "target_message_code", None) or "").strip(),
            )
            payload = _build_rule_payload(rule, new_formula)
            written_rules = kb.upsert_generated_rules(
                [payload],
                protocol_type=protocol_type,
                message_code=getattr(rule, "message_code", None),
                target_protocol_type=getattr(rule, "target_protocol_type", None),
                target_message_code=getattr(rule, "target_message_code", None),
                source=getattr(rule, "source", None) or "manual_review",
            )
            if not written_rules:
                continue

            written_rule = written_rules[0]
            legacy_rule_ids = module._collect_legacy_formula_rule_ids(scope_cache[scope_key], written_rule)
            deleted += module._delete_knowledge_rule_ids(kb, legacy_rule_ids)
            scope_cache[scope_key] = [
                item
                for item in scope_cache[scope_key]
                if str(getattr(item, "edge_id", None) or "").strip() not in legacy_rule_ids
            ] + [written_rule]

        report["changed_rules"] += changed
        report["deleted_legacy_rules"] += deleted
        report["protocols"].append(
            {
                "protocol_type": protocol_type,
                "rule_count": len(rules),
                "changed_rules": changed,
                "deleted_legacy_rules": deleted,
                "samples": samples,
            }
        )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
