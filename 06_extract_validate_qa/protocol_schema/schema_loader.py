"""Protocol schema loading helpers — minimal stub (schema JSON files not deployed)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_DIR = ROOT_DIR / "data" / "protocol_schemas"
FALLBACK_SCHEMA_KEY = "default"


def guess_message_code(text: str) -> Optional[str]:
    """Guess message code like J12.0 from free text."""
    if not text:
        return None
    match = re.search(r"(J\d+\.\d+)", text, flags=re.IGNORECASE)
    return match.group(1).upper() if match else None


def load_protocol_schema(protocol_type: Optional[str]) -> Dict[str, Any]:
    """Load protocol schema JSON; returns {} when no schema files exist."""
    key = (protocol_type or "").strip().lower() or "default"
    candidates = [
        DEFAULT_SCHEMA_DIR / f"{key}.json",
        DEFAULT_SCHEMA_DIR / f"{FALLBACK_SCHEMA_KEY}.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


def resolve_message_schema(
    protocol_type: Optional[str],
    message_code: Optional[str] = None,
    field_name: Optional[str] = None,
) -> Tuple[Dict[str, Any], Optional[str], Optional[Dict[str, Any]]]:
    """Resolve protocol schema and best-effort message schema."""
    schema = load_protocol_schema(protocol_type)
    if not schema:
        return {}, None, None
    normalized_code = str(message_code).upper() if message_code else None
    messages = schema.get("messages")
    if isinstance(messages, dict) and normalized_code:
        msg = messages.get(normalized_code)
        if isinstance(msg, dict):
            return schema, normalized_code, msg
    return schema, normalized_code, None


def build_schema_prompt_context(
    protocol_schema: Dict[str, Any],
    message_schema: Optional[Dict[str, Any]],
    message_code: Optional[str],
    max_fields: int = 12,
) -> str:
    """Build compact schema context for extraction prompt."""
    if not protocol_schema:
        return ""
    lines = [f"protocol={protocol_schema.get('protocol_type', 'unknown')}"]
    if message_code:
        lines.append(f"message_code={message_code}")
    if message_schema:
        fields = message_schema.get("fields") or []
        if isinstance(fields, list) and fields:
            lines.append("allowed_fields:")
            for field in fields[:max_fields]:
                if not isinstance(field, dict):
                    continue
                name = str(field.get("name") or "").strip()
                if not name:
                    continue
                parts = [name]
                if field.get("bit_start") is not None:
                    parts.append(f"start={field.get('bit_start')}")
                if field.get("bit_length") is not None:
                    parts.append(f"len={field.get('bit_length')}")
                if field.get("unit"):
                    parts.append(f"unit={field.get('unit')}")
                lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


def validate_with_schema(
    extracted_info: Dict[str, Any],
    message_schema: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Schema-based validation (no-op when no schema files deployed)."""
    if not message_schema:
        return []
    return []
