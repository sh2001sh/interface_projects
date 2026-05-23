from __future__ import annotations

import json
import sys
from pathlib import Path

import torch
from safetensors.torch import save_file


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared.model_metadata import resolve_lora_finetune_metadata, resolve_model_metadata


def test_resolve_model_metadata_counts_parameters_from_safetensors(tmp_path):
    model_dir = tmp_path / "demo-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text(json.dumps({"hidden_size": 4}), encoding="utf-8")
    save_file(
        {
            "linear.weight": torch.zeros((3, 4), dtype=torch.float32),
            "linear.bias": torch.zeros((3,), dtype=torch.float32),
        },
        str(model_dir / "model.safetensors"),
    )

    metadata = resolve_model_metadata(model_name="Demo-Unit", model_dir=str(model_dir))

    assert metadata["model_name"] == "Demo-Unit"
    assert metadata["parameter_count"] == 15
    assert metadata["parameter_count_display"] == "15"
    assert metadata["parameter_count_source"] == "exact_safetensors"


def test_resolve_lora_finetune_metadata_counts_target_module_parameters(tmp_path):
    model_dir = tmp_path / "demo-lora"
    model_dir.mkdir()
    (model_dir / "config.json").write_text(
        json.dumps({"hidden_size": 4, "num_hidden_layers": 1}),
        encoding="utf-8",
    )
    save_file(
        {
            "model.layers.0.self_attn.q_proj.weight": torch.zeros((8, 4), dtype=torch.float32),
            "model.layers.0.self_attn.k_proj.weight": torch.zeros((8, 4), dtype=torch.float32),
            "model.layers.0.self_attn.v_proj.weight": torch.zeros((8, 4), dtype=torch.float32),
            "model.layers.0.self_attn.o_proj.weight": torch.zeros((4, 8), dtype=torch.float32),
            "model.embed_tokens.weight": torch.zeros((10, 4), dtype=torch.float32),
        },
        str(model_dir / "model.safetensors"),
    )

    metadata = resolve_lora_finetune_metadata(
        base_model_name=str(model_dir),
        lora_rank=2,
    )

    assert metadata["total_parameter_count"] == 168
    assert metadata["finetune_parameter_count"] == 96
    assert metadata["finetune_parameter_count_source"] == "exact_lora_shapes"
    assert metadata["finetune_ratio"] == round(96 / 168, 8)
    assert metadata["finetune_ratio_display"] == f"{(96 / 168) * 100:.4f}%"

