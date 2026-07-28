from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ParsedElement:
    kind: str
    page_num: int
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    rows: List[List[str]] = field(default_factory=list)
    top_ratio: Optional[float] = None
    bottom_ratio: Optional[float] = None
    left_ratio: Optional[float] = None
    right_ratio: Optional[float] = None
    center_ratio: Optional[float] = None
    width_ratio: Optional[float] = None
    height_ratio: Optional[float] = None
    source_index: Optional[int] = None
    label: Optional[str] = None
    column_role: Optional[str] = None


__all__ = ["ParsedElement"]
