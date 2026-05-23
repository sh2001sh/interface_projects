from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import _build_finetune_model_info


def test_build_finetune_model_info_forwards_lora_rank():
    with patch("app.resolve_lora_finetune_metadata") as mocked_resolve:
        mocked_resolve.return_value = {"finetune_parameter_count": 123}

        result = _build_finetune_model_info(
            {
                "base_model_path": "Qwen/Qwen3-4B",
                "parameters": {"lora_rank": 32},
            }
        )

    assert result == {"finetune_parameter_count": 123}
    mocked_resolve.assert_called_once()
    kwargs = mocked_resolve.call_args.kwargs
    assert kwargs["base_model_name"] == "Qwen/Qwen3-4B"
    assert kwargs["lora_rank"] == 32
