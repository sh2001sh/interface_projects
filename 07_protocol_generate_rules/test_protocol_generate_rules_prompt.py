from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from protocol_conversion.generator import build_protocol_rule_generation_prompt  # noqa: E402


def test_build_protocol_rule_generation_prompt_omits_misleading_values():
    source_protocol = {
        "name": "X0_5",
        "protocol_type": "X0_5",
        "message_code": "X0.5",
        "bundle_id": "X0.5_to_K1.6",
        "content": "<source-xml />",
    }
    target_protocol = {
        "name": "K1_6",
        "protocol_type": "K1_6",
        "message_code": "K1.6",
        "content": "<target-xml />",
    }
    source_message = {
        "经度": 12345,
        "纬度": 67890,
        "时间1": 1,
    }
    target_tasks = [
        {
            "field_name": "经度",
            "label": "经度",
            "description": "经度字段",
            "bit_length": 24,
            "default_value": 0,
            "candidate_source_fields": [
                {
                    "field_name": "经度",
                    "display_field": "经度",
                    "source_message_code": "X0.5",
                    "source_protocol_type": "X0_5",
                    "source_path": "经度",
                    "score": 118.0,
                    "sample_value": 12345,
                }
            ],
        }
    ]

    _system_prompt, user_prompt = build_protocol_rule_generation_prompt(
        source_protocol=source_protocol,
        target_protocol=target_protocol,
        source_message=source_message,
        target_tasks=target_tasks,
    )

    assert "sample_value=" not in user_prompt
    assert "bit_length=" not in user_prompt
    assert "default_value=" not in user_prompt
    assert "12345" not in user_prompt
    assert "67890" not in user_prompt
    assert "原报文字段清单（仅字段名，不含示例值）" in user_prompt
    assert "\"经度\"" in user_prompt
    assert "\"纬度\"" in user_prompt
