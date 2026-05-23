from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import _save_protocol_rules_files


def test_save_protocol_rules_files_writes_json_and_yaml(tmp_path):
    payload = {
        "version": "1.0",
        "project_name": "demo",
        "relations": [{"relation_id": "r1", "rules": [{"target_field": "A", "formula": "1"}]}],
    }

    saved_paths = _save_protocol_rules_files(
        {
            "rules_output_dir": str(tmp_path),
            "rules_file_name": "nested/demo_rules.json",
        },
        payload,
    )

    json_path = Path(saved_paths["conversion_rules_json"])
    yaml_path = Path(saved_paths["conversion_rules_yaml"])
    assert json_path.exists()
    assert yaml_path.exists()
    assert json.loads(json_path.read_text(encoding="utf-8")) == payload
    assert yaml.safe_load(yaml_path.read_text(encoding="utf-8")) == payload

