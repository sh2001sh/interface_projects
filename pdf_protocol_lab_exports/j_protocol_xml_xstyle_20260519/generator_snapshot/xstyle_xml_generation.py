"""Generate J-series protocol XML in the same structural style as X XML."""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from .settings import OUTPUT_ROOT, SOURCE_PDF
from .xml_generation import _indent_xml, _write_xml


BASE_RE = re.compile(r"^(J\d+\.\d+)")
VARIANT_RE = re.compile(r"^(J\d+\.\d+)(?:\(\d+\))?(I|E\d+|C\d+)$")
EXACT_VARIANT_TOKEN_RE = re.compile(r"\b(J\d+\.\d+(?:\(\d+\))?(?:I|E\d+|C\d+))\b")
SECTION_TOKEN_RE = re.compile(r"\b(E\d+|C\d+)\b")
FIELD_BITS_RE = re.compile(r"(?P<name>[^;,.()]+?)\s*\((?P<bits>\d+)\s*bits?\)", re.IGNORECASE)
FIELD_PARENS_BITS_RE = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*?)\s*\((?P<bits>\d+)\)", re.IGNORECASE)
FIELD_PARENS_RANGE_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*?)\s*\((?P<start>\d+)\s*(?P<sep>[-/])\s*(?P<end>\d+)\)",
    re.IGNORECASE,
)
CHARACTER_GROUP_BITS_RE = re.compile(
    r"(?P<name>Characters?\s*#?(?P<start_idx>\d+)\s*-\s*#?(?P<end_idx>\d+))\s*\((?P<bits>\d+)\s*bits?\s+each\)",
    re.IGNORECASE,
)
FIELD_RANGE_RE = re.compile(r"(?P<start>\d+)\s*-\s*(?P<end>\d+):\s*(?P<name>[^;,.]+)")
BIT_SEGMENT_RE = re.compile(
    r"Bits?\s*(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?\s*:\s*(?P<name>.+?)(?:\s*\((?P<bits>\d+)\s*bits?\))?$",
    re.IGNORECASE,
)
INLINE_BIT_SEGMENT_RE = re.compile(
    r"(?:(?:Bit|Bits)\s*)?(?P<start>\d+)\s*-\s*(?P<end>\d+)\s+(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*)$",
    re.IGNORECASE,
)
NAMED_IS_BITS_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*?)(?:\s*\([^)]*\))?\s+(?:is|uses|use|contains)\s+(?P<bits>\d+)\s*-\s*bits?\b",
    re.IGNORECASE,
)
NAMED_BITS_AFTER_COLON_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*?)\s*:\s*(?P<bits>\d+)\s*-\s*bit\b",
    re.IGNORECASE,
)
BITS_FOR_NAME_RE = re.compile(
    r"(?P<bits>\d+)\s*-\s*bit(?:s)?\s+(?:field\s+)?for\s+(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*)",
    re.IGNORECASE,
)
NAME_COMMA_BITS_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*?)\s*,\s*(?P<bits>\d+)\s*bits?\b",
    re.IGNORECASE,
)
NAME_PLAIN_BITS_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*?)\s+(?P<bits>\d+)\s*bits?\b",
    re.IGNORECASE,
)
NAME_BITS_DUI_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*?)\s*\([^)]*\)\s*,?\s*(?P<bits>\d+)\s*-\s*bit\b",
    re.IGNORECASE,
)
FIELD_IS_BITS_SIMPLE_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*?)\s+field\s*\([^)]*\)\s+is\s+(?P<bits>\d+)\s*bits?\b",
    re.IGNORECASE,
)
NAME_DUI_BITS_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*?)\.\s*DUI\s+\d+\s*\((?P<bits>\d+)\s*bit\)",
    re.IGNORECASE,
)
FLAG_NAME_BITS_RE = re.compile(
    r"(?P<bits>\d+)\s*-\s*bit\s+flag:\s*0\s*=\s*No Statement,\s*1\s*=\s*(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*)",
    re.IGNORECASE,
)
ENUM_NAME_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*?)\s*(?:codes?|values?)\s*:\s*(?P<body>.+)$",
    re.IGNORECASE,
)
ENUM_NAME_INLINE_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*?)\s+(?:codes?|values?)\s+(?P<body>.+)$",
    re.IGNORECASE,
)
ENUM_AFTER_COLON_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 /#,+-]*?)\s*:\s*(?P<body>.+)$",
    re.IGNORECASE,
)
ENUM_CODE_RE = re.compile(r"(?<![A-Za-z])(\d+)(?:\s*-\s*(\d+))?\s*[:=]")
BIT_PREFIX_RE = re.compile(r"^Bits?\s*\d+(?:\s*-\s*\d+)?(?:\s*:\s*|\s+)", re.IGNORECASE)
TRAILING_BITS_SUFFIX_RE = re.compile(r"\s*\((?:\d+\s*bits?|\d+)\)\s*$", re.IGNORECASE)
WORD_CONTAINS_PREFIX_RE = re.compile(r"^Word\s+[A-Z0-9/#+-]+(?:\s*\([^)]*\))?\s+contains\s+", re.IGNORECASE)
LEADING_NOISE_PREFIX_RE = re.compile(
    r"^(?:Contains|Containing|Mentions|Data elements?|Field layout|Bit layout for|Word map for|layout for|Fields?:)\s+",
    re.IGNORECASE,
)
FIELD_LIST_SPLIT_RE = re.compile(r",|\band\b", re.IGNORECASE)
FIELD_NAME_ALIAS_REPLACEMENTS = (
    (re.compile(r"\bMsg\b", re.IGNORECASE), "Message"),
    (re.compile(r"\bFreq\b", re.IGNORECASE), "Frequency"),
    (re.compile(r"\bMult\b", re.IGNORECASE), "Multiplier"),
    (re.compile(r"\bTN\b", re.IGNORECASE), "Track Number"),
    (re.compile(r"\bJU\b", re.IGNORECASE), ""),
)
FIELD_NAME_STOP_PATTERNS = (
    re.compile(r"\b(transmitted|repeated|required|missing|record|creation|processing|rules?|response|trigger)\b", re.IGNORECASE),
    re.compile(r"\b(up to \d+ times|as required|based on|for J\d+\.\d+)\b", re.IGNORECASE),
    re.compile(r"^(?:word|sheet|table|detailed field coding)\b", re.IGNORECASE),
    re.compile(r"^Over-the-Air Rekeying Extension Word \d+$", re.IGNORECASE),
    re.compile(r"^etc$", re.IGNORECASE),
    re.compile(r"^J\d+\.\d+(?:\(\d+\))?[A-Z]?\b.*\b(?:Initial|Extension|Continuation)\s+Word\b", re.IGNORECASE),
    re.compile(r"^(?:codes?|values?|enum|indicator|bit flag|dui)$", re.IGNORECASE),
    re.compile(r"^(?:\d+\s+bits?(?:,\s*enum)?|bit enum)$", re.IGNORECASE),
    re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE),
    re.compile(r"\b(?:has|have|uses?|mapped?|maps?|set to|restricted to|var(?:y|ies)|continues?)\b", re.IGNORECASE),
    re.compile(r"\b(?:enumerated|enumeration|explicit enum|complete field definitions|summary lists)\b", re.IGNORECASE),
    re.compile(r"\b(?:numeric|illegal|invalid|alphanumeric|specific integer)\b", re.IGNORECASE),
    re.compile(r"\b(?:bit code|bit codes|degrees? to bit|feet(?:/sec)? to bit|validity to bit)\b", re.IGNORECASE),
    re.compile(r"\b(?:field contains|fields restricted|field set to|value is an enumerated)\b", re.IGNORECASE),
    re.compile(r"^\w+\s+valid$", re.IGNORECASE),
    re.compile(r"^through\s+[A-Z0-9]+$", re.IGNORECASE),
)
PURE_CODE_NAME_RE = re.compile(r"^(?:DFI|DUI)\s+\d+(?:\s*/\s*\d+)?(?:\s+\d+)?$", re.IGNORECASE)


HEADER_FIELDS = {
    "WORD FORMAT",
    "LABEL",
    "SUBLABEL",
    "MESSAGE LENGTH INDICATOR",
    "CONTINUATION WORD LABEL",
}
ORIGIN_FALLBACK_FIELDS = (
    ("Word Format", 2),
    ("Label", 5),
    ("Sublabel", 3),
    ("Message Length Indicator", 3),
)


@dataclass
class XField:
    """One scalar field in one X-style section."""

    name: str
    bit_length: int = 0
    pages: set[int] = field(default_factory=set)


@dataclass
class XSection:
    """One X-style section such as Origin/Prolong/Continue1."""

    name: str
    fields: dict[str, XField] = field(default_factory=dict)
    pages: set[int] = field(default_factory=set)


@dataclass
class XProtocol:
    """One base J protocol grouped into X-style sections."""

    base_name: str
    variants: set[str] = field(default_factory=set)
    pages: set[int] = field(default_factory=set)
    sections: dict[str, XSection] = field(default_factory=dict)


def generate_xstyle_xml_outputs(
    *,
    parsed_dir: Path,
    output_dir: Path,
) -> dict[str, object]:
    """Generate one J.xml head plus X-style per-message XML files."""

    protocols = _collect_protocols(sorted(parsed_dir.glob("batch_*.json")))
    output_dir.mkdir(parents=True, exist_ok=True)
    _clear_previous_outputs(output_dir)

    head_path = output_dir / "J.xml"
    _write_xml(head_path, build_head_xml(sorted(protocols)))

    message_files = []
    for base_name in sorted(protocols):
        xml_path = output_dir / f"{base_name}.xml"
        _write_xml(xml_path, build_protocol_xml(protocols[base_name]))
        message_files.append(xml_path)

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_pdf": str(SOURCE_PDF),
        "base_protocol_count": len(protocols),
        "head_xml_path": str(head_path),
        "output_dir": str(output_dir),
        "message_files_generated": len(message_files),
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def build_head_xml(base_names: list[str]) -> ET.Element:
    """Build the J head XML analogous to X.xml."""

    root = ET.Element("NameSpace", {"xmlns": "J/head"})
    ET.SubElement(root, "Dimen", {"packHeadLength": "0", "endian": "1", "wordLength": "70"})

    head_origin = ET.SubElement(root, "Head_Origin:NameSpace", {"xmlns:Head_Origin": "J/head/Origin"})
    _append_scalar(head_origin, "Head_Origin:StructMess", "字标识", 2, default_value="3", declared_type="int")
    _append_scalar(head_origin, "Head_Origin:StructMess", "消息标识", 5, declared_type="int")
    _append_scalar(head_origin, "Head_Origin:StructMess", "子标识", 3, declared_type="int")
    _append_scalar(head_origin, "Head_Origin:StructMess", "消息长度", 3, declared_type="int")

    head_prolong = ET.SubElement(root, "Head_Prolong:NameSpace", {"xmlns:Head_Prolong": "J/head/Prolong"})
    _append_scalar(head_prolong, "Head_Prolong:StructMess", "字标识", 2, default_value="1", declared_type="int")

    head_continue = ET.SubElement(root, "Head_Continue:NameSpace", {"xmlns:Head_Continue": "J/head/Continue"})
    _append_scalar(head_continue, "Head_Continue:StructMess", "字标识", 2, default_value="2", declared_type="int")
    _append_scalar(head_continue, "Head_Continue:StructMess", "标识符", 5, declared_type="int")

    for base_name in base_names:
        message_id, sub_id = _message_numbers(base_name)
        route = ET.SubElement(
            root,
            "Field",
            {
                "corr": "Head_Origin:StructMess.子标识,Head_Origin:StructMess.消息标识",
                "value": f"{sub_id},{message_id}",
            },
        )
        route.text = base_name
    return root


def build_protocol_xml(protocol: XProtocol) -> ET.Element:
    """Build one per-base J protocol XML in X-style."""

    ns_token = _namespace_token(protocol.base_name)
    root = ET.Element(f"{ns_token}:NameSpace", {f"xmlns:{ns_token}": f"J/{ns_token}"})

    mess_code = ET.SubElement(root, "MessCode")
    pre_seq = ET.SubElement(mess_code, "PreSeq", {"cycle": "0", "times": "1", "name": "Seq_1"})
    ET.SubElement(pre_seq, "Member", {"corr": "Head_Origin:StructMess.字标识"}).text = "3"
    if "Prolong" in protocol.sections:
        ET.SubElement(pre_seq, "Member", {"corr": "Head_Prolong:StructMess.字标识"}).text = "1"
    continue_sections = sorted(
        [name for name in protocol.sections if name.startswith("Continue")],
        key=_continue_sort_key,
    )
    for section_name in continue_sections:
        continue_index = section_name.replace("Continue", "") or "1"
        ET.SubElement(
            pre_seq,
            "Member",
            {"corr": "Head_Continue:StructMess.字标识,Head_Continue:StructMess.标识符"},
        ).text = f"2,{continue_index}"

    for section_name in _ordered_section_names(protocol.sections):
        section = protocol.sections[section_name]
        tag_name = f"{ns_token}_{section_name}:NameSpace"
        section_el = ET.SubElement(root, tag_name, {f"xmlns:{ns_token}_{section_name}": f"J/{ns_token}/{section_name}"})
        fields = sorted(section.fields.values(), key=lambda item: (_field_sort_rank(item.name), item.name.upper()))
        if not fields:
            if section_name == "Origin":
                for field_name, bit_length in ORIGIN_FALLBACK_FIELDS:
                    _append_scalar(section_el, f"{ns_token}_{section_name}:StructMess", field_name, bit_length)
                continue
            _append_scalar(section_el, f"{ns_token}_{section_name}:Item", "payload", 0, default_value="0")
            continue
        for field_item in fields:
            local_tag = "StructMess" if _is_struct_field(section_name, field_item.name) else "Item"
            tag = f"{ns_token}_{section_name}:{local_tag}"
            kwargs = {"default_value": "0"} if _default_zero(field_item.name) else {}
            _append_scalar(section_el, tag, field_item.name, field_item.bit_length, **kwargs)
    return root


def _collect_protocols(batch_paths: list[Path]) -> dict[str, XProtocol]:
    """Collect base J protocols and their sections from parsed batches."""

    protocols: dict[str, XProtocol] = {}
    known_variants: dict[str, set[str]] = {}
    local_length_votes: dict[tuple[str, str], Counter[int]] = defaultdict(Counter)
    global_length_votes: dict[str, Counter[int]] = defaultdict(Counter)

    for path in batch_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("message_candidates", []) or []:
            for raw in (item.get("normalized_name"), item.get("raw_name")):
                base_name = _base_name(raw)
                if not base_name:
                    continue
                protocol = protocols.setdefault(base_name, XProtocol(base_name=base_name))
                protocol.pages.update(_to_pages(item.get("evidence_pages", [])))
                token = _variant_token(raw)
                if token:
                    protocol.variants.add(token)
                    known_variants.setdefault(base_name, set()).add(token)

    for path in batch_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        batch_pages = _to_pages(
            [
                (payload.get("page_range", {}) or {}).get("start"),
                (payload.get("page_range", {}) or {}).get("end"),
            ]
        )
        for signal in payload.get("xml_signals", []) or []:
            if not _is_structural_signal(signal):
                continue
            signal_type = str(signal.get("signal_type", "")).strip()
            signal_message = str(signal.get("message", "")).strip()
            signal_pages = _to_pages(signal.get("evidence_pages", [])) or batch_pages
            details = str(signal.get("details", "")).strip()
            targets = _resolve_signal_targets(signal_message, details, known_variants)
            if not targets:
                continue
            fields = _extract_fields_from_signal(details, signal_type)
            if not fields:
                continue
            for base_name, section_name in targets:
                protocol = protocols.setdefault(base_name, XProtocol(base_name=base_name))
                protocol.pages.update(signal_pages)
                section = protocol.sections.setdefault(section_name, XSection(name=section_name))
                section.pages.update(signal_pages)
                for field_name, bit_length in fields:
                    key = _field_key(field_name)
                    field_item = section.fields.setdefault(key, XField(name=field_name, bit_length=bit_length))
                    if bit_length and not field_item.bit_length:
                        field_item.bit_length = bit_length
                    if bit_length:
                        local_length_votes[(base_name, key)][bit_length] += 1
                        global_length_votes[key][bit_length] += 1
                    field_item.pages.update(signal_pages)

    _backfill_missing_bit_lengths(protocols, local_length_votes, global_length_votes)
    _prune_redundant_fields(protocols)
    _apply_protocol_corrections(protocols)

    for protocol in protocols.values():
        if not protocol.sections:
            protocol.sections["Origin"] = XSection(name="Origin")
        if "Origin" not in protocol.sections:
            protocol.sections["Origin"] = XSection(name="Origin")
    return protocols


def _resolve_signal_targets(
    signal_message: str,
    details: str,
    known_variants: dict[str, set[str]],
) -> list[tuple[str, str]]:
    """Resolve which base protocol section a signal belongs to."""

    resolved: list[tuple[str, str]] = []
    explicit_variants = {match.group(1) for match in EXACT_VARIANT_TOKEN_RE.finditer(details)}
    if VARIANT_RE.match(signal_message):
        explicit_variants.add(signal_message)

    for variant in sorted(explicit_variants):
        base_name = _base_name(variant)
        if not base_name:
            continue
        resolved.append((base_name, _section_name_for_variant(variant)))

    if resolved:
        return _unique_targets(resolved)

    base_name = _base_name(signal_message)
    if base_name:
        section_tokens = {match.group(1) for match in SECTION_TOKEN_RE.finditer(details)}
        if section_tokens:
            for token in sorted(section_tokens):
                resolved.append((base_name, _section_name_for_variant(f"{base_name}{token}")))
            return _unique_targets(resolved)
        variants = known_variants.get(base_name, set())
        if f"{base_name}I" in variants:
            return [(base_name, "Origin")]
        if variants:
            return [(base_name, _section_name_for_variant(sorted(variants)[0]))]
        return [(base_name, "Origin")]
    return []


def _extract_fields_from_signal(details: str, signal_type: str) -> list[tuple[str, int]]:
    """Extract field names and bit lengths from one signal text."""

    cleaned = _prepare_signal_details(details)
    seen: set[str] = set()
    result: list[tuple[str, int]] = []
    for match in CHARACTER_GROUP_BITS_RE.finditer(cleaned):
        field_name = _normalize_field_name(match.group("name"))
        if not field_name:
            continue
        key = _field_key(field_name)
        if key in seen:
            continue
        start_idx = int(match.group("start_idx"))
        end_idx = int(match.group("end_idx"))
        bits_each = int(match.group("bits"))
        count = abs(end_idx - start_idx) + 1
        bit_length = count * bits_each
        if bit_length <= 0:
            continue
        seen.add(key)
        result.append((field_name, bit_length))
    for match in FIELD_PARENS_RANGE_RE.finditer(cleaned):
        field_name = _normalize_field_name(match.group("name"))
        if not field_name:
            continue
        key = _field_key(field_name)
        if key in seen:
            continue
        start = int(match.group("start"))
        end = int(match.group("end"))
        if signal_type in {"field_layout", "word_map"} and max(start, end) > 8:
            bit_length = abs(end - start) + 1
        else:
            bit_length = max(start, end).bit_length()
        if bit_length <= 0:
            continue
        seen.add(key)
        result.append((field_name, bit_length))
    for raw_segment in re.split(r"[.;]\s*", cleaned):
        segment = raw_segment.strip()
        if not segment:
            continue
        match = BIT_SEGMENT_RE.match(segment)
        if not match:
            continue
        field_name = _normalize_field_name(match.group("name"))
        if not field_name:
            continue
        key = _field_key(field_name)
        if key in seen:
            continue
        start = int(match.group("start"))
        end = int(match.group("end") or match.group("start"))
        bit_length = int(match.group("bits") or (abs(end - start) + 1))
        if bit_length <= 0:
            continue
        seen.add(key)
        result.append((field_name, bit_length))
    for raw_segment in re.split(r"[.;]\s*", cleaned):
        segment = raw_segment.strip()
        if not segment:
            continue
        match = INLINE_BIT_SEGMENT_RE.match(segment)
        if not match:
            continue
        field_name = _normalize_field_name(match.group("name"))
        if not field_name:
            continue
        key = _field_key(field_name)
        if key in seen:
            continue
        start = int(match.group("start"))
        end = int(match.group("end"))
        bit_length = abs(end - start) + 1
        if bit_length <= 0:
            continue
        seen.add(key)
        result.append((field_name, bit_length))
    for match in FIELD_BITS_RE.finditer(cleaned):
        field_name = _normalize_field_name(match.group("name"))
        if not field_name:
            continue
        key = _field_key(field_name)
        if key in seen:
            continue
        bit_length = int(match.group("bits"))
        if bit_length <= 0:
            continue
        seen.add(key)
        result.append((field_name, bit_length))
    for pattern in (
        FIELD_PARENS_BITS_RE,
        NAMED_IS_BITS_RE,
        NAMED_BITS_AFTER_COLON_RE,
        BITS_FOR_NAME_RE,
        NAME_COMMA_BITS_RE,
        NAME_PLAIN_BITS_RE,
        NAME_BITS_DUI_RE,
        FIELD_IS_BITS_SIMPLE_RE,
        NAME_DUI_BITS_RE,
    ):
        for match in pattern.finditer(cleaned):
            field_name = _normalize_field_name(match.group("name"))
            if not field_name:
                continue
            key = _field_key(field_name)
            if key in seen:
                continue
            bit_length = int(match.group("bits"))
            if bit_length <= 0:
                continue
            seen.add(key)
            result.append((field_name, bit_length))
    for match in FLAG_NAME_BITS_RE.finditer(cleaned):
        field_name = _normalize_field_name(match.group("name"))
        if not field_name:
            continue
        key = _field_key(field_name)
        if key in seen:
            continue
        bit_length = int(match.group("bits"))
        if bit_length <= 0:
            continue
        seen.add(key)
        result.append((field_name, bit_length))
    for enum_name, enum_bits in _extract_enum_field_lengths(cleaned):
        key = _field_key(enum_name)
        if key in seen:
            continue
        seen.add(key)
        result.append((enum_name, enum_bits))
    for chunk in cleaned.split(";"):
        range_match = FIELD_RANGE_RE.search(chunk)
        if not range_match:
            continue
        field_name = _normalize_field_name(range_match.group("name"))
        if not field_name:
            continue
        key = _field_key(field_name)
        if key in seen:
            continue
        start = int(range_match.group("start"))
        end = int(range_match.group("end"))
        bit_length = abs(end - start) + 1
        if bit_length <= 0:
            continue
        seen.add(key)
        result.append((field_name, bit_length))
    if _looks_like_field_list(details, signal_type):
        for token in FIELD_LIST_SPLIT_RE.split(cleaned):
            field_name = _normalize_field_name(token)
            if not field_name:
                continue
            key = _field_key(field_name)
            if key in seen:
                continue
            seen.add(key)
            result.append((field_name, 0))
    return result


def _append_scalar(
    parent: ET.Element,
    tag: str,
    name: str,
    bit_length: int,
    *,
    default_value: str | None = None,
    declared_type: str | None = None,
) -> None:
    """Append one scalar field node."""

    attrs = {"name": name}
    if default_value is not None:
        attrs["defaultValue"] = default_value
    if declared_type is not None:
        attrs["type"] = declared_type
    node = ET.SubElement(parent, tag, attrs)
    node.text = str(bit_length)


def _base_name(raw: object) -> str:
    """Return J base name like J7.2."""

    text = str(raw or "").strip()
    match = BASE_RE.match(text)
    return match.group(1) if match else ""


def _variant_token(raw: object) -> str:
    """Return exact variant token when the text is a direct word id."""

    text = str(raw or "").strip()
    if VARIANT_RE.match(text):
        return text
    return ""


def _section_name_for_variant(variant: str) -> str:
    """Map one word variant token to X-style section name."""

    match = VARIANT_RE.match(variant)
    if not match:
        return "Origin"
    suffix = match.group(2)
    if suffix == "I":
        return "Origin"
    if suffix.startswith("E"):
        return "Prolong"
    if suffix.startswith("C"):
        return f"Continue{suffix[1:] or '1'}"
    return "Origin"


def _message_numbers(base_name: str) -> tuple[str, str]:
    """Split J7.2 into message id and sub id strings."""

    _, rest = base_name[0], base_name[1:]
    message_id, sub_id = rest.split(".", 1)
    return message_id, sub_id


def _namespace_token(base_name: str) -> str:
    """Return a compact XML namespace token like J72."""

    return base_name.replace(".", "")


def _ordered_section_names(sections: dict[str, XSection]) -> list[str]:
    """Return stable X-style section order."""

    ordered = []
    if "Origin" in sections:
        ordered.append("Origin")
    if "Prolong" in sections:
        ordered.append("Prolong")
    ordered.extend(sorted([name for name in sections if name.startswith("Continue")], key=_continue_sort_key))
    ordered.extend(sorted([name for name in sections if name not in ordered]))
    return ordered


def _continue_sort_key(section_name: str) -> tuple[int, str]:
    """Sort Continue sections numerically."""

    suffix = section_name.replace("Continue", "")
    try:
        return (int(suffix), section_name)
    except ValueError:
        return (999, section_name)


def _field_sort_rank(name: str) -> tuple[int, str]:
    """Prioritize structural header fields first."""

    normalized = _field_key(name)
    if normalized in {"WORD FORMAT", "LABEL", "SUBLABEL", "MESSAGE LENGTH INDICATOR"}:
        return (0, normalized)
    if "SPARE" in normalized or "RESERVED" in normalized:
        return (2, normalized)
    return (1, normalized)


def _is_struct_field(section_name: str, field_name: str) -> bool:
    """Decide whether one field should be emitted as StructMess."""

    return section_name == "Origin" and _field_key(field_name) in HEADER_FIELDS


def _default_zero(field_name: str) -> bool:
    """Whether one field should default to zero."""

    normalized = _field_key(field_name)
    return "SPARE" in normalized or "RESERVED" in normalized


def _prepare_signal_details(details: str) -> str:
    """Trim common prefixes before field parsing."""

    cleaned = str(details or "").strip()
    cleaned = re.sub(r"^(?:Word\s+)?J\d+\.\d+(?:[A-Z]\d*)?\s+contains\s+(?:fields:\s*)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Fields?:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Fields?\s+include\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Data elements?:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Word\s+[A-Z0-9/#+-]+(?:\s*\([^)]*\))?\s+contains\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Mentions\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Contains\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Bit layout for\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^layout for\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"^Word\s+(?:[A-Z0-9/#+-]+\s+)?structure\s+defined\s+with\s+fields?(?:\s+like)?\s*:?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^Word\s+(?:[A-Z0-9/#+-]+\s+)?fields?:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"^Word\s+contains\s+\d+\s+bits?\s*\(\d+\s*-\s*\d+\)\s+split\s+into\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^Word map (?:on page \d+\s+)?(?:provides|shows|defines)\s+(?:exact\s+)?(?:bit\s+(?:offsets|positions|ranges)\s+)?for\s+fields?\s+like\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Word map (?:on page \d+\s+)?(?:provides|shows|defines)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Bit map (?:showing|shows)\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned


def _normalize_field_name(name: str) -> str:
    """Normalize one field name string."""

    cleaned = str(name or "").strip(" ,.;:")
    cleaned = BIT_PREFIX_RE.sub("", cleaned)
    cleaned = WORD_CONTAINS_PREFIX_RE.sub("", cleaned)
    cleaned = LEADING_NOISE_PREFIX_RE.sub("", cleaned)
    cleaned = re.sub(r"^(?:and|or)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^summary\s+lists\s+fields?\s+like\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Fields?\s+include\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^for\s+fields?\s+like\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:is|are|was|were)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:DFI|DUI)\s+\d+(?:\s*/\s*\d+)?(?:\s*,\s*(?:DFI|DUI)\s+\d+(?:\s*/\s*\d+)?)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^bit\s+layout\s+for\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^bit\s+map\s+layout:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^bit\s+map\s+(?:for\s+)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^bit\s+layout\s+defined:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^bit\s+counts?\s+for\s+(?:elements?\s+like\s+)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^layout\s+for\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\d+-bit\s+word\.\s*Fields?:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[A-Za-z0-9 /-]+?\.\s*Fields?:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^DFI\s+\d+\s+fields?:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Text\s+Extension\s+Word\s+containing\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Initial word\s+[A-Z0-9.()/-]+\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Extension Word \d+\s+is\s+[A-Z0-9.()/-]+\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Continuation word \d+\s+is\s+[A-Z0-9.()/-]+\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Data element summary for\s+[A-Z0-9.()/-]+\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^WORD C(?: structure)?(?: defined with fields like)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:[IQE]\d*\s+)?WORD\s+[CQ]\s+contains\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\d+\s*-\s*bit\s+enum\s+for\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\d+\s*-\s*bit\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Detailed bit for\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Detailed bit and\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = TRAILING_BITS_SUFFIX_RE.sub("", cleaned)
    cleaned = re.sub(r"\binclude(?:s|d)?\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bdefined\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bcoding\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bdata element\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bemission\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bindication\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+values?\s+listed$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+values?\s+defined$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+value\s+defined$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+value$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+\d+\s*-\s*bit\s+enum$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*\(bits?\s*\d+\s*-\s*\d+\)\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\.\s*Defaults provided$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\s+contains\s+(?:extensive\s+)?(?:enum(?:eration)?\s+list|enumerated\s+values?|enumeration\s+of|list\s+of|di\s+bit\s+codes?|bit\s+codes?).*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+fields?$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+field$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+through\s+", "-", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+\d+\s*-\s*\d+$", "", cleaned)
    cleaned = re.sub(r"^ACT TSR$", "Action Time Slot Request", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Track Number,?\s*Associated\s+(\d+)$", r"Track Number Associated \1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Track Number,?\s*Subject$", "Track Number Subject", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Subject;\s*Track Number$", "Track Number Subject", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Association;\s*Track Number$", "Track Number Associated", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Associated\s+(\d+)$", r"Track Number Associated \1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Track Numbers for Addressees$", "Track Number Addressees", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Character #11-#20\. Default value 32 \(Blank\)$", "Character #11-#20", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Character #1-#10\. Default value 32 \(Blank\)$", "Character #1-#10", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Characters$", "Characters 1-2", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Associated\s+(\d+);\s*Track Number$", r"Track Number Associated \1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^JRS LVL$", "Jammer Received Signal Level", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^ASR/ASP Indicator$", "Antenna Scan Rate/Period Indicator", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^PRI IND$", "PRF/PRI Indicator", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^PRF or PRI value$", "PRF/PRI Value", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Track Number,\s*Associated$", "Track Number Associated", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Local Discrete ID$", "Local Discrete Identifier", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Track Number,\s*Origin$", "Track Number", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Track Number Addresssee$", "Track Number Addressee", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Num Associated DMPIS$", "Number of Associated DMPIs", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^NUMBER OF ASSOCIATED DMPIS$", "Number of Associated DMPIs", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Length$", "Message Length Indicator", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Track Numbers$", "Track Number", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^Track Number Reference$", "Track Number", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+is$", "", cleaned, flags=re.IGNORECASE)
    for pattern, replacement in FIELD_NAME_ALIAS_REPLACEMENTS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(" ,.;:")
    if PURE_CODE_NAME_RE.match(cleaned):
        return ""
    if cleaned and cleaned[0].islower():
        return ""
    if not re.search(r"[A-Za-z]", cleaned):
        return ""
    if re.search(r"\b(page|provides|shows|defines|offsets|positions|ranges)\b", cleaned, re.IGNORECASE):
        return ""
    if cleaned.lower() in {"field", "fields", "data element", "data elements"}:
        return ""
    if cleaned.lower() in {
        "specific types",
        "etc.)",
        "kinematic quality continuation word including position uncertainty",
        "bit",
        "bits",
        "is",
        "ontains",
        "undefined",
        "for status",
        "dfi",
        "t",
        "sp",
        "contains enum",
        "enumerated",
        "field name",
        "with di bit",
        "for field layout",
        "each uses a 2-bit",
        "each uses a 2-bit code",
        "contains multiple duis",
        "with standard 2-bit status",
        "enum values",
        "es frequency bands use",
        "standard 2-bit status",
        "standard 2-bit status codes",
        "missile inventory fields with specific",
        "missile inventory fields with specific value codes",
        "with bit",
        "j14.2c5 word structure",
        "ewac values",
        "r/c values",
        "dfi/dui",
        "extensive list of di bit",
    }:
        return ""
    if re.match(r"^DUI\s+\d+(?:/\d+)?\b", cleaned, flags=re.IGNORECASE):
        return ""
    if re.search(r"\b(?:values|codes)\b$", cleaned, re.IGNORECASE):
        return ""
    for pattern in FIELD_NAME_STOP_PATTERNS:
        if pattern.search(cleaned):
            return ""
    return cleaned


def _field_key(name: str) -> str:
    """Build a dedup key for a field label."""

    return re.sub(r"[^A-Z0-9]+", " ", _normalize_field_name(name).upper()).strip()


def _is_structural_signal(signal: dict[str, object]) -> bool:
    """Keep only field/word-map signals that really encode structure."""

    signal_type = str(signal.get("signal_type", "")).strip()
    details = str(signal.get("details", "")).strip()
    if signal_type not in {"field_layout", "word_map", "field_coding", "enum"}:
        return False
    if signal_type in {"enum", "field_coding"}:
        if re.search(r"\b(?:codes?|values?)\b", details, re.IGNORECASE):
            return True
        if re.search(r"\bDUI\s+\d+\s*\(\d+\s*bit", details, re.IGNORECASE):
            return True
        if re.search(r"\b\d+\s*(?:=|:|/|-)\s*\d+", details):
            return True
        if re.search(r"\b\d+\s*-\s*bit\b", details, re.IGNORECASE):
            return True
    if re.search(r"Bits?\s*\d+", details):
        return True
    if re.search(r"\b[A-Za-z][A-Za-z0-9 /,-]*\(\d+\s*bits?\)", details):
        return True
    if re.search(r"\b[A-Za-z][A-Za-z0-9 /#,+-]*\(\d+\)", details):
        return True
    if re.search(r"\b\d+\s*-\s*bit\b", details, re.IGNORECASE):
        return True
    if re.search(r"\b\d+\s*bits?\b", details, re.IGNORECASE) and re.search(r"\b[A-Za-z]{3,}\b", details):
        return True
    if re.search(r"\bFields?:", details):
        return True
    if re.search(r"\bcontains\s+fields?:", details, re.IGNORECASE):
        return True
    if re.search(r"\bData elements?:", details, re.IGNORECASE):
        return True
    if re.search(r"\bfields?\s+like\s+", details, re.IGNORECASE):
        return True
    if re.search(r"^Bit layout for\b", details, re.IGNORECASE):
        return True
    if re.search(r"^Word\s+[A-Z0-9/#+-]+(?:\s*\([^)]*\))?\s+contains\b", details, re.IGNORECASE):
        return True
    if re.search(r"^Mentions\b", details, re.IGNORECASE):
        return True
    return False


def _looks_like_field_list(details: str, signal_type: str) -> bool:
    """Whether the signal text is a plain field-name list without bit lengths."""

    if signal_type in {"field_coding", "enum"}:
        return False
    return bool(
        re.search(r"\bData elements?:", details, re.IGNORECASE)
        or re.search(r"\bFields?:", details, re.IGNORECASE)
        or re.search(r"\bFields?\s+include\b", details, re.IGNORECASE)
        or re.search(r"\bdefined\s+with\s+fields?(?:\s+like)?\b", details, re.IGNORECASE)
        or re.search(r"^Bit layout for\b", details, re.IGNORECASE)
        or re.search(r"^Word\s+[A-Z0-9/#+-]+(?:\s*\([^)]*\))?\s+contains\b", details, re.IGNORECASE)
        or re.search(r"^Mentions\b", details, re.IGNORECASE)
    )


def _extract_enum_field_lengths(cleaned: str) -> list[tuple[str, int]]:
    """Infer field lengths from enum/code ranges when bit width is not explicit."""

    result: list[tuple[str, int]] = []
    for pattern in (ENUM_NAME_RE, ENUM_AFTER_COLON_RE, ENUM_NAME_INLINE_RE):
        match = pattern.search(cleaned)
        if not match:
            continue
        field_name = _normalize_field_name(match.group("name"))
        if not field_name:
            continue
        body = match.group("body")
        max_value = -1
        for number_match in re.finditer(r"(?<![A-Za-z])(\d+)(?![A-Za-z])", body):
            max_value = max(max_value, int(number_match.group(1)))
        if max_value < 1:
            continue
        inferred_bits = max_value.bit_length()
        if inferred_bits <= 0:
            continue
        result.append((field_name, inferred_bits))
        break
    return result


def _backfill_missing_bit_lengths(
    protocols: dict[str, XProtocol],
    local_length_votes: dict[tuple[str, str], Counter[int]],
    global_length_votes: dict[str, Counter[int]],
) -> None:
    """Backfill zero-length fields from same-message then global observations."""

    for base_name, protocol in protocols.items():
        for section in protocol.sections.values():
            for field_item in section.fields.values():
                if field_item.bit_length:
                    continue
                field_key = _field_key(field_item.name)
                local_vote = _select_vote(local_length_votes.get((base_name, field_key)))
                if local_vote:
                    field_item.bit_length = local_vote
                    continue
                global_vote = _select_vote(global_length_votes.get(field_key))
                if global_vote:
                    field_item.bit_length = global_vote
                    continue
                fuzzy_local_vote = _select_fuzzy_vote(field_key, {k[1]: v for k, v in local_length_votes.items() if k[0] == base_name})
                if fuzzy_local_vote:
                    field_item.bit_length = fuzzy_local_vote
                    continue
                fuzzy_global_vote = _select_fuzzy_vote(field_key, global_length_votes)
                if fuzzy_global_vote:
                    field_item.bit_length = fuzzy_global_vote
                    continue
                manual_length = _manual_field_length(base_name, field_key)
                if manual_length:
                    field_item.bit_length = manual_length


def _manual_field_length(base_name: str, field_key: str) -> int:
    """Apply evidence-backed fallback lengths for fields the parser cannot infer generically."""

    manual = {
        ("J10.5", "HANDOVER INDICATOR"): 1,
        ("J13.3", "FLIGHT DECK"): 3,
        ("J13.3", "DIRECTION FINDERS"): 2,
        ("J14.0", "AGILE EMITTER"): 1,
        ("J14.0", "SPI"): 1,
        ("J14.0", "SQUARE CIRCLE SWITCH"): 2,
        ("J14.0", "ANTENNA SCAN RATE PERIOD INDICATOR"): 2,
        ("J14.0", "ANTENNA SCAN RATE"): 10,
        ("J14.0", "ANTENNA SCAN PERIOD"): 10,
        ("J14.0", "JAMMER RECEIVED SIGNAL LEVEL"): 3,
        ("J14.0", "PULSE WIDTH"): 14,
        ("J14.0", "LOCAL DISCRETE IDENTIFIER"): 12,
        ("J14.2", "PRF PRI VALUE"): 23,
        ("J14.2", "PRF PRI INDICATOR"): 1,
        ("J14.2", "TIME DURATION"): 4,
        ("J14.2", "EW COORDINATOR INDICATOR"): 1,
        ("J14.2", "WARTIME RESERVE MODE INDICATOR"): 1,
        ("J16.0", "IMAGE PACKET NUMBER"): 16,
        ("J16.1", "TRACK NUMBER"): 19,
        ("J16.1", "ROUTE NUMBER"): 3,
        ("J16.1", "ROUTE DESCRIPTOR"): 4,
        ("J16.1", "ROUTE POINT TYPE"): 2,
        ("J16.1", "SEQUENCE NUMBER"): 5,
        ("J16.1", "TOTAL POINTS"): 5,
        ("J16.1", "MINUTE"): 6,
        ("J16.1", "HOUR"): 5,
        ("J16.1", "TIME FUNCTION"): 3,
        ("J16.1", "LATITUDE"): 23,
        ("J16.1", "LONGITUDE"): 24,
        ("J16.1", "ALTITUDE"): 13,
        ("J16.1", "ALTITUDE AMPLIFICATION"): 3,
        ("J28.2", "CHARACTERS 3 13"): 66,
        ("J3.0", "ESTIMATED YIELD"): 2,
        ("J3.0", "TYPE OF BURST"): 3,
        ("J3.2", "EMERGENCY"): 1,
        ("J3.2", "FORCE TELL"): 1,
        ("J3.2", "PPLI TRACK NUMBER"): 1,
        ("J3.2", "SIMULATION"): 1,
        ("J3.2", "TRACK QUALITY"): 4,
        ("J3.2", "PASSIVE ACTIVE INDICATOR"): 1,
        ("J3.2", "PPLI IFF SIF"): 2,
        ("J3.4", "LAUNCH CAPABILITY"): 2,
        ("J31.0", "CRYPTOPERIOD DESIGNATOR"): 1,
        ("J7.1", "SPACE AMBIGUITY MATRIX REQUEST INDICATOR"): 1,
        ("J7.3", "TEXT INDICATOR"): 2,
        ("J7.3", "CHARACTER 11 20"): 60,
        ("J7.7", "TRACK NUMBER SUBJECT"): 19,
    }
    return manual.get((base_name, field_key), 0)


def _prune_redundant_fields(protocols: dict[str, XProtocol]) -> None:
    """Drop aggregate placeholders once more specific child fields exist."""

    for protocol in protocols.values():
        for section in protocol.sections.values():
            keys = set(section.fields)
            for key, field_item in list(section.fields.items()):
                if field_item.bit_length and key not in {"CHARACTERS 1 2"}:
                    continue
                if key == "CHARACTERS 1 2" and {"CHARACTER 1", "CHARACTER 2"} <= keys:
                    section.fields.pop(key, None)
                elif key == "TRACK NUMBER ADDRESSEES" and any("ADDRESSEE" in candidate for candidate in keys):
                    section.fields.pop(key, None)
                elif key == "CONTRAIL HEIGHTS" and {
                    "CONTRAIL HEIGHT LOWER LIMIT",
                    "CONTRAIL HEIGHT UPPER LIMIT",
                } <= keys:
                    section.fields.pop(key, None)
                elif key == "ICING HEIGHTS" and {
                    "ICING FREEZING RAIN HEIGHT LOWER LIMIT",
                    "ICING FREEZING RAIN HEIGHT UPPER LIMIT",
                } <= keys:
                    section.fields.pop(key, None)
                elif key == "VOICE GROUP CHANNELS" and {
                    "VOICE GROUP A CHANNEL",
                    "VOICE GROUP B CHANNEL",
                } <= keys:
                    section.fields.pop(key, None)
                elif protocol.base_name == "J13.3" and key == "WEAPON SYSTEMS":
                    section.fields.pop(key, None)
                elif protocol.base_name == "J14.2" and key == "J14 2C5 WORD STRUCTURE":
                    section.fields.pop(key, None)


def _ensure_field(section: XSection, field_name: str, bit_length: int) -> None:
    """Ensure one field exists with at least the requested bit length."""

    key = _field_key(field_name)
    current = section.fields.get(key)
    if current is None:
        section.fields[key] = XField(name=field_name, bit_length=bit_length)
        return
    current.name = field_name
    if bit_length and (not current.bit_length or current.bit_length != bit_length):
        current.bit_length = bit_length


def _drop_field(section: XSection, field_name: str) -> None:
    """Drop one field by normalized key."""

    section.fields.pop(_field_key(field_name), None)


def _rename_field(section: XSection, old_name: str, new_name: str, bit_length: int = 0) -> None:
    """Rename one field while preserving pages and stronger bit length."""

    old_key = _field_key(old_name)
    field_item = section.fields.pop(old_key, None)
    if field_item is None:
        if bit_length:
            _ensure_field(section, new_name, bit_length)
        return
    field_item.name = new_name
    if bit_length and (not field_item.bit_length or field_item.bit_length != bit_length):
        field_item.bit_length = bit_length
    new_key = _field_key(new_name)
    existing = section.fields.get(new_key)
    if existing is None:
        section.fields[new_key] = field_item
        return
    existing.pages.update(field_item.pages)
    if field_item.bit_length and (not existing.bit_length or existing.bit_length != field_item.bit_length):
        existing.bit_length = field_item.bit_length


def _apply_protocol_corrections(protocols: dict[str, XProtocol]) -> None:
    """Apply message-specific corrections where PDF evidence is already known."""

    for protocol in protocols.values():
        if protocol.base_name == "J12.0":
            _fix_j120(protocol)
        elif protocol.base_name == "J14.2":
            _fix_j142(protocol)
        elif protocol.base_name == "J16.1":
            _fix_j161(protocol)
        elif protocol.base_name == "J3.4":
            _fix_j34(protocol)


def _fix_j120(protocol: XProtocol) -> None:
    """Clean J12.0 explanatory duplicates."""

    origin = protocol.sections.get("Origin")
    if origin is not None:
        _drop_field(origin, "I WORD C contains Track Number, Mission Assignment Discrete")
        _drop_field(origin, "MAD values listed")
        _drop_field(origin, "MAD")
        _drop_field(origin, "Track Number")
        _rename_field(origin, "Track Number Addresssee", "Track Number Addressee", 15)
    for section_name in ("Continue3", "Continue4", "Continue6"):
        section = protocol.sections.get(section_name)
        if section is None:
            continue
        _rename_field(section, "2-bit enum for Point Number", "Point Number", 2)
        _rename_field(section, "TARGET TYPE 5-bit enum", "Target Type", 5)
        _drop_field(section, "Detailed bit and")
        _rename_field(section, "Detailed bit for Laser Illuminator", "Laser Illuminator", 5)


def _fix_j142(protocol: XProtocol) -> None:
    """Resolve J14.2 overlay/noise fields to the physical word view."""

    origin = protocol.sections.get("Origin")
    if origin is not None:
        _rename_field(origin, "EWC IND value", "EWC IND", 1)
        _drop_field(origin, "Sector/Area/Location Indicator")
        _drop_field(origin, "Sector Width")
        _drop_field(origin, "STN")
        _drop_field(origin, "Time Duration")
    continue8 = protocol.sections.get("Continue8")
    if continue8 is not None:
        _rename_field(continue8, "PRF or PRI value", "PRF/PRI Value", 23)
        if _field_key("PRF/PRI Value") in continue8.fields:
            _drop_field(continue8, "PRI")
            _drop_field(continue8, "Pulse Repetition Frequency")
            _drop_field(continue8, "Pulse Repetition Interval")
    prolong = protocol.sections.get("Prolong")
    if prolong is not None:
        _drop_field(prolong, "Automatic EW Attack Negation")
        _rename_field(prolong, "EW Coordinator Indicator", "EWC IND", 1)
        _ensure_field(prolong, "Time Duration", 4)


def _fix_j161(protocol: XProtocol) -> None:
    """Repair J16.1 core word structure from known PDF evidence."""

    origin = protocol.sections.setdefault("Origin", XSection(name="Origin"))
    prolong = protocol.sections.setdefault("Prolong", XSection(name="Prolong"))
    for field_name, bit_length in (
        ("Track Number", 19),
        ("Route Number", 3),
        ("Route Descriptor", 3),
        ("Route Point Type", 2),
        ("Sequence Number", 5),
        ("Total Points", 5),
        ("Minute", 6),
        ("Hour", 5),
        ("Time Function", 3),
    ):
        _ensure_field(origin, field_name, bit_length)
    for field_name, bit_length in (
        ("Latitude", 23),
        ("Longitude", 24),
        ("Altitude", 13),
        ("Altitude Amplification", 3),
    ):
        _ensure_field(prolong, field_name, bit_length)
    _drop_field(prolong, "Amplification")
    _drop_field(origin, "Addressee Track Number")
    _drop_field(origin, "STN")
    _drop_field(origin, "Route Number (referenced in reception constraints)")


def _fix_j34(protocol: XProtocol) -> None:
    """Clean J3.4 continuation-word field names."""

    prolong = protocol.sections.get("Prolong")
    if prolong is not None:
        _rename_field(prolong, "Latitude (bits 02-23)", "Latitude", 22)
        _rename_field(prolong, "Longitude (bits 25-48)", "Longitude", 24)
        _rename_field(prolong, "Speed (bits 50-60)", "Speed", 11)
        _rename_field(prolong, "Course (bits 61-69)", "Course", 9)
        _rename_field(prolong, "Speed ASW. Defaults provided", "Speed", 11)
        _rename_field(prolong, "Speed ASW", "Speed", 11)
    continue2 = protocol.sections.get("Continue2")
    if continue2 is not None:
        _rename_field(
            continue2,
            "Subsurface Specific Type contains extensive list of submarine class",
            "Subsurface Specific Type",
            12,
        )


def _select_vote(counter: Counter[int] | None) -> int:
    """Choose one likely bit length from observed votes."""

    if not counter:
        return 0
    ranked = counter.most_common()
    if len(ranked) == 1:
        return ranked[0][0]
    top_value, top_count = ranked[0]
    next_count = ranked[1][1]
    if top_count >= next_count * 2:
        return top_value
    return 0


def _select_fuzzy_vote(field_key: str, votes: dict[str, Counter[int]]) -> int:
    """Choose one likely bit length from nearby field-name variants."""

    target_tokens = tuple(token for token in field_key.split() if token)
    if not target_tokens:
        return 0
    best_score = 0
    best_vote = 0
    target_set = set(target_tokens)
    for candidate_key, counter in votes.items():
        candidate_tokens = tuple(token for token in candidate_key.split() if token)
        if not candidate_tokens:
            continue
        candidate_set = set(candidate_tokens)
        overlap = len(target_set & candidate_set)
        if overlap == 0:
            continue
        if not (target_set <= candidate_set or candidate_set <= target_set):
            continue
        score = overlap * 10 - abs(len(candidate_tokens) - len(target_tokens))
        vote = _select_vote(counter)
        if vote and score > best_score:
            best_score = score
            best_vote = vote
    return best_vote


def _clear_previous_outputs(output_dir: Path) -> None:
    """Remove stale generated files so validation only sees the latest pass."""

    for path in output_dir.glob("J*.xml"):
        path.unlink(missing_ok=True)
    (output_dir / "summary.json").unlink(missing_ok=True)
    messages_dir = output_dir / "messages"
    if messages_dir.exists():
        shutil.rmtree(messages_dir)


def _to_pages(values: object) -> set[int]:
    """Convert one scalar/list into page refs."""

    items = values if isinstance(values, list) else [values]
    result: set[int] = set()
    for value in items:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            result.add(number)
    return result


def _unique_targets(values: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Preserve order while deduplicating target tuples."""

    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
