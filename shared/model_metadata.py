"""Model metadata helpers shared across interface projects."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from safetensors import safe_open


_PARAMETER_SUFFIX = re.compile(r"(?<![0-9])(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[BMK])\b", re.IGNORECASE)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _format_parameter_count(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    magnitude = abs(int(value))
    if magnitude >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if magnitude >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if magnitude >= 1_000:
        return f"{value / 1_000:.2f}K"
    return str(int(value))


def _expand_model_cache_roots(model_cache_dir: Any) -> Tuple[Path, ...]:
    raw_candidates = [
        _clean_text(model_cache_dir),
        _clean_text(os.getenv("MODEL_CACHE_DIR")),
        _clean_text(Path.home() / "model_cache"),
    ]
    roots = []
    seen = set()
    for raw in raw_candidates:
        if not raw:
            continue
        base = Path(os.path.expanduser(raw)).resolve()
        for candidate in (base, base / "modelscope"):
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            roots.append(candidate)
    return tuple(roots)


def _model_name_variants(model_name: Any) -> Tuple[Path, ...]:
    text = _clean_text(model_name)
    if not text:
        return tuple()
    normalized = text.replace("\\", "/")
    lower = normalized.lower()
    basename = Path(normalized).name
    variants = [Path(normalized), Path(basename)]
    if "." in basename:
        variants.append(Path(basename.replace(".", "___")))
    if "/" not in normalized:
        variants.append(Path("Qwen") / basename)
        if "." in basename:
            variants.append(Path("Qwen") / basename.replace(".", "___"))
    if "qwen3" in lower and "embedding" in lower:
        variants.extend(
            [
                Path("Qwen") / "Qwen3-Embedding-0.6B",
                Path("Qwen") / "Qwen3-Embedding-0___6B",
                Path("Qwen3-Embedding-0.6B"),
                Path("Qwen3-Embedding-0___6B"),
            ]
        )
    if "qwen3" in lower and "reranker" in lower:
        variants.extend(
            [
                Path("Qwen") / "Qwen3-Reranker-0.6B",
                Path("Qwen") / "Qwen3-Reranker-0___6B",
                Path("Qwen3-Reranker-0.6B"),
                Path("Qwen3-Reranker-0___6B"),
            ]
        )
    if "qwen3" in lower and "4b" in lower:
        variants.extend(
            [
                Path("Qwen") / "Qwen3-4B",
                Path("Qwen3-4B"),
            ]
        )
    deduped = []
    seen = set()
    for item in variants:
        key = str(item)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return tuple(deduped)


def _resolve_model_dir(model_name: Any = None, model_dir: Any = None, model_cache_dir: Any = None) -> Optional[Path]:
    explicit_dir = _clean_text(model_dir)
    if explicit_dir:
        candidate = Path(os.path.expanduser(explicit_dir)).resolve()
        if candidate.is_dir():
            return candidate

    explicit_name = _clean_text(model_name)
    if explicit_name:
        candidate = Path(os.path.expanduser(explicit_name)).resolve()
        if candidate.is_dir():
            return candidate

    for root in _expand_model_cache_roots(model_cache_dir):
        for variant in _model_name_variants(model_name):
            candidate = (root / variant).resolve()
            if candidate.is_dir():
                return candidate
    return None


@lru_cache(maxsize=32)
def _read_model_config(model_dir: str) -> Dict[str, Any]:
    config_path = Path(model_dir) / "config.json"
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@lru_cache(maxsize=32)
def _read_tensor_shapes(model_dir: str) -> Tuple[Tuple[str, Tuple[int, ...]], ...]:
    root = Path(model_dir)
    index_path = root / "model.safetensors.index.json"
    safetensor_files = []
    if index_path.exists():
        index_payload = json.loads(index_path.read_text(encoding="utf-8"))
        weight_map = index_payload.get("weight_map") or {}
        safetensor_files = [root / name for name in sorted(set(weight_map.values()))]
    else:
        single_path = root / "model.safetensors"
        if single_path.exists():
            safetensor_files = [single_path]

    shapes = []
    for safetensor_path in safetensor_files:
        with safe_open(str(safetensor_path), framework="pt", device="cpu") as handle:
            for name in handle.keys():
                shape = tuple(int(dim) for dim in handle.get_slice(name).get_shape())
                shapes.append((name, shape))
    return tuple(shapes)


def _parameter_count_from_shapes(shapes: Iterable[Tuple[str, Sequence[int]]]) -> int:
    total = 0
    for _, shape in shapes:
        count = 1
        for dimension in shape:
            count *= int(dimension)
        total += count
    return total


def _nominal_parameter_count_from_name(model_name: Any) -> Optional[int]:
    matches = list(_PARAMETER_SUFFIX.finditer(_clean_text(model_name)))
    if not matches:
        return None
    match = matches[-1]
    value = float(match.group("value"))
    unit = match.group("unit").upper()
    multiplier = {"B": 1_000_000_000, "M": 1_000_000, "K": 1_000}[unit]
    return int(round(value * multiplier))


def _resolve_display_name(model_name: Any, resolved_dir: Optional[Path]) -> Optional[str]:
    explicit = _clean_text(model_name)
    if explicit:
        return explicit
    if resolved_dir is not None:
        return resolved_dir.name
    return None


def resolve_model_metadata(
    *,
    model_name: Any = None,
    model_dir: Any = None,
    model_cache_dir: Any = None,
) -> Dict[str, Any]:
    """Resolve model name, directory, and parameter count metadata."""

    resolved_dir = _resolve_model_dir(model_name=model_name, model_dir=model_dir, model_cache_dir=model_cache_dir)
    parameter_count = None
    parameter_count_source = "unavailable"
    if resolved_dir is not None:
        shapes = _read_tensor_shapes(str(resolved_dir))
        if shapes:
            parameter_count = _parameter_count_from_shapes(shapes)
            parameter_count_source = "exact_safetensors"

    if parameter_count is None:
        nominal = _nominal_parameter_count_from_name(model_name or resolved_dir)
        if nominal is not None:
            parameter_count = nominal
            parameter_count_source = "nominal_from_name"

    return {
        "model_name": _resolve_display_name(model_name, resolved_dir),
        "model_dir": str(resolved_dir) if resolved_dir is not None else None,
        "parameter_count": parameter_count,
        "parameter_count_display": _format_parameter_count(parameter_count),
        "parameter_count_source": parameter_count_source,
    }


def _count_lora_parameters_from_shapes(
    shapes: Iterable[Tuple[str, Sequence[int]]],
    *,
    target_modules: Sequence[str],
    rank: int,
) -> int:
    suffixes = tuple(f".{module}.weight" for module in target_modules)
    total = 0
    for name, shape in shapes:
        if not name.endswith(suffixes) or len(shape) < 2:
            continue
        out_features = int(shape[0])
        in_features = int(shape[1])
        total += rank * (out_features + in_features)
    return total


def _estimate_lora_parameter_count_from_config(
    config_payload: Dict[str, Any],
    *,
    target_modules: Sequence[str],
    rank: int,
) -> Optional[int]:
    hidden_size = config_payload.get("hidden_size")
    num_hidden_layers = config_payload.get("num_hidden_layers")
    if not hidden_size or not num_hidden_layers:
        return None
    per_projection = rank * (int(hidden_size) + int(hidden_size))
    return int(num_hidden_layers) * len(tuple(target_modules)) * per_projection


def resolve_lora_finetune_metadata(
    *,
    base_model_name: Any,
    model_cache_dir: Any = None,
    lora_rank: Any = 16,
    target_modules: Sequence[str] = ("q_proj", "k_proj", "v_proj", "o_proj"),
) -> Dict[str, Any]:
    """Resolve base-model and LoRA trainable-parameter metadata."""

    normalized_rank = max(int(lora_rank or 0), 0)
    base_info = resolve_model_metadata(
        model_name=base_model_name,
        model_cache_dir=model_cache_dir,
    )
    finetune_parameter_count = None
    finetune_parameter_count_source = "unavailable"
    resolved_dir = _resolve_model_dir(model_name=base_model_name, model_cache_dir=model_cache_dir)
    if resolved_dir is not None and normalized_rank > 0:
        shapes = _read_tensor_shapes(str(resolved_dir))
        if shapes:
            finetune_parameter_count = _count_lora_parameters_from_shapes(
                shapes,
                target_modules=tuple(target_modules),
                rank=normalized_rank,
            )
            finetune_parameter_count_source = "exact_lora_shapes"
        if finetune_parameter_count is None:
            estimate = _estimate_lora_parameter_count_from_config(
                _read_model_config(str(resolved_dir)),
                target_modules=tuple(target_modules),
                rank=normalized_rank,
            )
            if estimate is not None:
                finetune_parameter_count = estimate
                finetune_parameter_count_source = "estimated_from_config"

    total_parameter_count = base_info.get("parameter_count")
    finetune_ratio = None
    finetune_ratio_percent = None
    finetune_ratio_display = None
    if total_parameter_count and finetune_parameter_count is not None:
        finetune_ratio = finetune_parameter_count / float(total_parameter_count)
        finetune_ratio_percent = finetune_ratio * 100.0
        finetune_ratio_display = f"{finetune_ratio_percent:.4f}%"

    return {
        "base_model_name": base_info.get("model_name"),
        "base_model_dir": base_info.get("model_dir"),
        "total_parameter_count": total_parameter_count,
        "total_parameter_count_display": base_info.get("parameter_count_display"),
        "total_parameter_count_source": base_info.get("parameter_count_source"),
        "finetune_parameter_count": finetune_parameter_count,
        "finetune_parameter_count_display": _format_parameter_count(finetune_parameter_count),
        "finetune_parameter_count_source": finetune_parameter_count_source,
        "finetune_ratio": None if finetune_ratio is None else round(finetune_ratio, 8),
        "finetune_ratio_percent": None if finetune_ratio_percent is None else round(finetune_ratio_percent, 4),
        "finetune_ratio_display": finetune_ratio_display,
        "lora_rank": normalized_rank,
        "target_modules": list(target_modules),
    }
