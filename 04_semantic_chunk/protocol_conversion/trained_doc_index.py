from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from utils.file_store import FileStore

from .pageindex_adapter import PageIndexEvidenceProvider, _default_pageindex_client_factory


ROOT_DIR = Path(__file__).resolve().parents[1]
PAGEINDEX_WORKSPACE_ROOT = ROOT_DIR.parent / "data" / "pageindex_workspace"
PAGEINDEX_DOC_ROOT = ROOT_DIR.parent / "data" / "pageindex_docs"
DEFAULT_SHARD_MAX_BLOCKS = 180
DEFAULT_SHARD_MAX_PAGES = 60
DEFAULT_SHARD_MAX_CHARS = 90000
DEFAULT_SCAN_SHARD_LIMIT = 12
DEFAULT_SCAN_SHARDS_PER_FILE = 4


def _slugify(value: Any, default: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z._-]+", "-", str(value or "").strip()).strip("-").lower()
    return slug or default


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _stable_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _normalize_message_codes(raw_codes: Any) -> List[str]:
    if isinstance(raw_codes, str):
        values = [raw_codes]
    elif isinstance(raw_codes, list):
        values = raw_codes
    else:
        values = []
    result: List[str] = []
    seen = set()
    for item in values:
        code = str(item or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return result


def _normalize_tags(raw_tags: Any) -> List[str]:
    if isinstance(raw_tags, str):
        values = [raw_tags]
    elif isinstance(raw_tags, list):
        values = raw_tags
    else:
        values = []
    tags: List[str] = []
    seen = set()
    for item in values:
        tag = str(item or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tags


def _normalize_document_paths(raw_paths: Any) -> List[str]:
    if isinstance(raw_paths, str):
        values = [raw_paths]
    elif isinstance(raw_paths, list):
        values = raw_paths
    else:
        values = []
    paths: List[str] = []
    seen = set()
    for item in values:
        raw_path = str(item or "").strip()
        if not raw_path:
            continue
        normalized_path = str(Path(raw_path).expanduser())
        if normalized_path in seen:
            continue
        seen.add(normalized_path)
        paths.append(normalized_path)
    return paths


def _build_document_path_lookup(document_paths: List[str]) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    for raw_path in document_paths:
        file_name = Path(raw_path).name.strip()
        if not file_name or file_name in lookup:
            continue
        lookup[file_name] = raw_path
    return lookup


def _format_protocol_field_item(raw_field: Any) -> str:
    """Render one protocol-field metadata entry into stable markdown text."""
    if isinstance(raw_field, str):
        return _compact_text(raw_field)
    if isinstance(raw_field, dict):
        field_name = _compact_text(raw_field.get("field_name"))
        meaning = _compact_text(raw_field.get("meaning"))
        formula = _compact_text(raw_field.get("formula"))
        parts = [part for part in [field_name, meaning, formula] if part]
        if parts:
            return " | ".join(parts)
        return _compact_text(json.dumps(raw_field, ensure_ascii=False, sort_keys=True))
    return _compact_text(raw_field)


def _format_protocol_fields(raw_fields: Any) -> str:
    """Normalize protocol_fields metadata into a readable single-line string."""
    if not isinstance(raw_fields, list) or not raw_fields:
        return "N/A"
    rendered: List[str] = []
    seen = set()
    for item in raw_fields:
        text = _format_protocol_field_item(item)
        if not text or text in seen:
            continue
        seen.add(text)
        rendered.append(text)
    return ", ".join(rendered) if rendered else "N/A"


def _filter_blocks(
    blocks: Iterable[Any],
    file_names: Optional[List[str]] = None,
    source_block_ids: Optional[List[int]] = None,
) -> List[Any]:
    allowed_files = {str(item).strip() for item in (file_names or []) if str(item).strip()}
    allowed_block_ids = {int(item) for item in (source_block_ids or [])}
    filtered: List[Any] = []
    for block in blocks:
        if allowed_files and str(getattr(block, "file_name", "") or "").strip() not in allowed_files:
            continue
        block_id = getattr(block, "block_id", None)
        if allowed_block_ids and int(block_id or 0) not in allowed_block_ids:
            continue
        filtered.append(block)
    filtered.sort(
        key=lambda item: (
            str(getattr(item, "file_name", "") or ""),
            int(getattr(item, "page_num", 0) or 0),
            int(getattr(item, "block_id", 0) or 0),
        )
    )
    return filtered


def _group_blocks_by_file(blocks: Iterable[Any]) -> Dict[str, List[Any]]:
    grouped: Dict[str, List[Any]] = {}
    for block in blocks:
        file_name = str(getattr(block, "file_name", "") or "unnamed").strip() or "unnamed"
        grouped.setdefault(file_name, []).append(block)
    return grouped


def _block_content(block: Any) -> str:
    return str(getattr(block, "cleaned_content", None) or getattr(block, "content", "") or "").strip()


def _build_block_shards(
    blocks: List[Any],
    max_blocks: int = DEFAULT_SHARD_MAX_BLOCKS,
    max_pages: int = DEFAULT_SHARD_MAX_PAGES,
    max_chars: int = DEFAULT_SHARD_MAX_CHARS,
) -> List[List[Any]]:
    if not blocks:
        return []

    shards: List[List[Any]] = []
    current: List[Any] = []
    current_pages: set[int] = set()
    current_chars = 0

    for block in blocks:
        content = _block_content(block)
        block_chars = max(len(content), 1)
        page_num = int(getattr(block, "page_num", 0) or 0)
        next_pages = set(current_pages)
        if page_num > 0:
            next_pages.add(page_num)

        should_split = bool(current) and (
            len(current) >= max(1, max_blocks)
            or current_chars + block_chars > max(1, max_chars)
            or len(next_pages) > max(1, max_pages)
        )
        if should_split:
            shards.append(current)
            current = []
            current_pages = set()
            current_chars = 0

        current.append(block)
        if page_num > 0:
            current_pages.add(page_num)
        current_chars += block_chars

    if current:
        shards.append(current)
    return shards


def _extract_field_terms_from_blocks(blocks: List[Any], limit: int = 80) -> List[str]:
    seen = set()
    result: List[str] = []

    def push(raw_value: Any) -> None:
        text = _compact_text(raw_value).upper()
        if not text or text in seen:
            return
        seen.add(text)
        result.append(text)

    for block in blocks:
        metadata = getattr(block, "metadata", {}) or {}
        protocol_fields = metadata.get("protocol_fields") if isinstance(metadata, dict) else None
        if not isinstance(protocol_fields, list):
            continue
        for item in protocol_fields:
            if isinstance(item, dict):
                push(item.get("field_name"))
                push(item.get("meaning"))
            else:
                push(item)
            if len(result) >= limit:
                return result
    return result


def _build_sample_text(blocks: List[Any], limit: int = 1200) -> str:
    parts: List[str] = []
    total = 0
    for block in blocks:
        content = _block_content(block)
        if not content:
            continue
        remaining = limit - total
        if remaining <= 0:
            break
        snippet = content[:remaining]
        parts.append(snippet)
        total += len(snippet)
        if total >= limit:
            break
    return "\n".join(parts).strip()


def _load_json_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as file_obj:
        payload = json.load(file_obj)
    if not isinstance(payload, dict):
        raise ValueError(f"PageIndex registry 文件内容必须是 JSON 对象: {path}")
    return payload


def _is_pageindex_registry_payload(payload: Dict[str, Any]) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("documents"), list) and bool(
        str(payload.get("workspace_dir") or "").strip() or str(payload.get("docs_dir") or "").strip()
    )


def _iter_registry_candidate_files(index_registry_path: Any) -> List[Path]:
    if isinstance(index_registry_path, (list, tuple)):
        collected: List[Path] = []
        seen = set()
        for item in index_registry_path:
            for candidate in _iter_registry_candidate_files(item):
                resolved = str(candidate.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                collected.append(candidate)
        return collected

    raw_path = str(index_registry_path or "").strip()
    if not raw_path:
        raise ValueError("index_registry_path 不能为空")
    raw_relative_path = Path(raw_path).expanduser()
    candidate_paths: List[Path] = []
    if raw_relative_path.is_absolute():
        candidate_paths.append(raw_relative_path)
    else:
        candidate_paths.extend(
            [
                Path.cwd() / raw_relative_path,
                ROOT_DIR / raw_relative_path,
                ROOT_DIR.parent / raw_relative_path,
                ROOT_DIR.parent / "04_semantic_chunk" / raw_relative_path,
            ]
        )

    path = next((candidate for candidate in candidate_paths if candidate.exists()), None)
    if path is None:
        searched = ", ".join(str(candidate) for candidate in candidate_paths) or str(raw_relative_path)
        raise FileNotFoundError(f"index_registry_path 不存在: {raw_relative_path}; 已尝试: {searched}")
    if path.is_file():
        return [path]
    return sorted(candidate for candidate in path.rglob("*.json") if candidate.is_file())


def _load_pageindex_registries_from_path(index_registry_path: Any) -> List[Dict[str, Any]]:
    registries: List[Dict[str, Any]] = []
    for candidate in _iter_registry_candidate_files(index_registry_path):
        try:
            payload = _load_json_file(candidate)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not _is_pageindex_registry_payload(payload):
            continue
        enriched_payload = dict(payload)
        enriched_payload["_registry_path"] = str(candidate.resolve())
        _normalize_registry_storage_paths(enriched_payload)
        registries.append(enriched_payload)
    if not registries:
        raise ValueError(f"未在 index_registry_path 中找到可用的 PageIndex registry: {index_registry_path}")
    return registries


def _workspace_has_registry_documents(workspace_dir: Path, registry: Dict[str, Any]) -> bool:
    documents = list(registry.get("documents") or [])
    if not documents:
        return workspace_dir.exists()
    for document in documents:
        if not isinstance(document, dict):
            continue
        doc_id = str(document.get("doc_id") or "").strip()
        if not doc_id:
            continue
        if not (workspace_dir / f"{doc_id}.json").exists():
            return False
    return True


def _docs_has_registry_documents(docs_dir: Path, registry: Dict[str, Any]) -> bool:
    documents = list(registry.get("documents") or [])
    if not documents:
        return docs_dir.exists()
    for document in documents:
        if not isinstance(document, dict):
            continue
        normalized_path = str(document.get("normalized_path") or "").strip()
        if not normalized_path:
            continue
        if not (docs_dir / Path(normalized_path).name).exists():
            return False
    return True


def _normalize_registry_storage_paths(registry: Dict[str, Any]) -> None:
    project_id = str(registry.get("project_id") or "").strip()
    doc_set_id = str(registry.get("doc_set_id") or "").strip()
    if not project_id or not doc_set_id:
        return

    desired_roots: List[tuple[Path, Path]] = [(PAGEINDEX_WORKSPACE_ROOT, PAGEINDEX_DOC_ROOT)]
    registry_path_raw = str(registry.get("_registry_path") or "").strip()
    if registry_path_raw:
        registry_path = Path(registry_path_raw).expanduser()
        registry_parent = registry_path.parent
        registry_root = registry_parent.parent if registry_parent.name == project_id else registry_parent
        if registry_root.name == "pageindex_registry":
            data_root = registry_root.parent
            desired_roots.insert(
                0,
                (data_root / "pageindex_workspace", data_root / "pageindex_docs"),
            )

    current_workspace_raw = str(registry.get("workspace_dir") or "").strip()
    current_docs_raw = str(registry.get("docs_dir") or "").strip()
    current_workspace = Path(current_workspace_raw).expanduser() if current_workspace_raw else None
    current_docs = Path(current_docs_raw).expanduser() if current_docs_raw else None

    for workspace_root, docs_root in desired_roots:
        desired_workspace = workspace_root / project_id / doc_set_id
        desired_docs = docs_root / project_id / doc_set_id

        if desired_workspace.exists() and (
            current_workspace is None
            or not _workspace_has_registry_documents(current_workspace, registry)
        ) and _workspace_has_registry_documents(desired_workspace, registry):
            registry["workspace_dir"] = str(desired_workspace)
            current_workspace = desired_workspace

        if desired_docs.exists() and (
            current_docs is None
            or not _docs_has_registry_documents(current_docs, registry)
        ) and _docs_has_registry_documents(desired_docs, registry):
            registry["docs_dir"] = str(desired_docs)
            current_docs = desired_docs

    resolved_docs_dir = str(registry.get("docs_dir") or "").strip()
    if not resolved_docs_dir:
        return
    docs_dir = Path(resolved_docs_dir)
    for document in registry.get("documents") or []:
        if not isinstance(document, dict):
            continue
        normalized_path = str(document.get("normalized_path") or "").strip()
        if normalized_path:
            candidate = docs_dir / Path(normalized_path).name
            if candidate.exists():
                document["normalized_path"] = str(candidate)


def _merge_pageindex_registries(registries: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not registries:
        return {}

    normalized_registries = [dict(item) for item in registries if isinstance(item, dict)]
    registry_paths: List[str] = []
    message_codes: List[str] = []
    tags: List[str] = []
    documents: List[Dict[str, Any]] = []
    logical_document_keys = set()
    first_registry = normalized_registries[0]

    for registry in normalized_registries:
        registry_path = str(registry.get("_registry_path") or "").strip() or None
        if registry_path and registry_path not in registry_paths:
            registry_paths.append(registry_path)
        message_codes.extend(_normalize_message_codes(registry.get("message_codes")))
        tags.extend(_normalize_tags(registry.get("tags")))
        workspace_dir = str(registry.get("workspace_dir") or "").strip() or None
        docs_dir = str(registry.get("docs_dir") or "").strip() or None
        for document in registry.get("documents") or []:
            if not isinstance(document, dict):
                continue
            doc_item = dict(document)
            doc_item["_workspace_dir"] = workspace_dir
            doc_item["_docs_dir"] = docs_dir
            doc_item["_registry_path"] = registry_path
            doc_item["_registry_doc_set_id"] = str(registry.get("doc_set_id") or "").strip() or None
            doc_item["_registry_index_ref"] = str(registry.get("index_ref") or "").strip() or None
            documents.append(doc_item)
            logical_key = str(
                doc_item.get("logical_document_id")
                or f"{doc_item.get('file_name')}::{doc_item.get('source_document_path')}"
            ).strip()
            if logical_key:
                logical_document_keys.add(logical_key)

    def _shared_value(key: str) -> Optional[str]:
        values = {
            str(item.get(key) or "").strip()
            for item in normalized_registries
            if str(item.get(key) or "").strip()
        }
        if len(values) == 1:
            return next(iter(values))
        return None

    return {
        "project_id": _shared_value("project_id"),
        "dataset_id": _shared_value("dataset_id"),
        "doc_set_id": _shared_value("doc_set_id"),
        "index_ref": _shared_value("index_ref"),
        "status": "ready",
        "protocol_type": _shared_value("protocol_type"),
        "message_codes": _normalize_message_codes(message_codes),
        "tags": _normalize_tags(tags),
        "workspace_dir": str(first_registry.get("workspace_dir") or "").strip() or None,
        "docs_dir": str(first_registry.get("docs_dir") or "").strip() or None,
        "document_count": len(logical_document_keys) or sum(int(item.get("document_count") or 0) for item in normalized_registries),
        "indexed_shard_count": len(documents),
        "documents": documents,
        "registry_count": len(normalized_registries),
        "registry_paths": registry_paths,
        "source_registries": [
            {
                "project_id": item.get("project_id"),
                "dataset_id": item.get("dataset_id"),
                "doc_set_id": item.get("doc_set_id"),
                "index_ref": item.get("index_ref"),
                "workspace_dir": item.get("workspace_dir"),
                "docs_dir": item.get("docs_dir"),
                "document_count": item.get("document_count"),
                "indexed_shard_count": item.get("indexed_shard_count"),
                "registry_path": item.get("_registry_path"),
            }
            for item in normalized_registries
        ],
    }


def _render_document_markdown(
    file_name: str,
    blocks: List[Any],
    protocol_type: str,
    message_codes: List[str],
    tags: List[str],
    shard_index: int = 1,
    shard_count: int = 1,
) -> str:
    page_range = sorted({int(getattr(block, "page_num", 0) or 0) for block in blocks if int(getattr(block, "page_num", 0) or 0) > 0})
    block_ids = [int(getattr(block, "block_id", 0) or 0) for block in blocks if int(getattr(block, "block_id", 0) or 0) > 0]
    shard_title = file_name if shard_count <= 1 else f"{file_name} (Part {shard_index}/{shard_count})"
    body: List[str] = [
        f"# {shard_title}",
        "",
        f"- protocol_type: {protocol_type or 'N/A'}",
        f"- message_codes: {', '.join(message_codes) if message_codes else 'N/A'}",
        f"- tags: {', '.join(tags) if tags else 'N/A'}",
        f"- shard_index: {shard_index}",
        f"- shard_count: {shard_count}",
        f"- page_range: {', '.join(str(item) for item in page_range) if page_range else 'N/A'}",
        f"- source_block_ids: {', '.join(str(item) for item in block_ids) if block_ids else 'N/A'}",
        "",
    ]
    for block in blocks:
        page_num = int(getattr(block, "page_num", 0) or 0)
        block_type = str(getattr(block, "block_type", "") or "text").strip() or "text"
        block_id = int(getattr(block, "block_id", 0) or 0)
        metadata = getattr(block, "metadata", {}) or {}
        protocol_fields = metadata.get("protocol_fields") if isinstance(metadata, dict) else None
        content = str(getattr(block, "cleaned_content", None) or getattr(block, "content", "") or "").strip()
        if not content:
            continue
        body.extend(
            [
                f"## Page {page_num or 'N/A'} / Block {block_id or 'N/A'}",
                "",
        f"- block_type: {block_type}",
        f"- protocol_fields: {_format_protocol_fields(protocol_fields)}",
        "",
        content,
        "",
            ]
        )
    return "\n".join(body).strip() + "\n"


def _score_document_shard(document: Dict[str, Any], queries: List[str]) -> float:
    haystack_parts = [
        str(document.get("file_name") or ""),
        str(document.get("sample_text") or ""),
        " ".join(str(item) for item in (document.get("field_terms") or [])),
        " ".join(str(item) for item in (document.get("message_codes") or [])),
    ]
    haystack = "\n".join(part for part in haystack_parts if part).upper()
    score = 0.0
    for query in queries:
        normalized = _compact_text(query).upper()
        if not normalized:
            continue
        if normalized in haystack:
            score += 3.0
        for token in re.split(r"[_\W]+", normalized):
            if len(token) < 2:
                continue
            if token in haystack:
                score += 0.6
    page_range = document.get("page_range") or []
    if isinstance(page_range, list) and page_range:
        score += min(len(page_range), 6) * 0.05
    return round(score, 4)


def build_protocol_doc_index(
    project_id: str,
    blocks: Iterable[Any],
    dataset_id: str = "",
    protocol_type: str = "",
    message_codes: Optional[List[str]] = None,
    file_names: Optional[List[str]] = None,
    document_paths: Optional[List[str]] = None,
    document_fingerprints: Optional[Dict[str, str]] = None,
    source_block_ids: Optional[List[int]] = None,
    doc_set_id: str = "",
    index_ref: str = "",
    tags: Optional[List[str]] = None,
    rebuild: bool = False,
    file_store: Optional[FileStore] = None,
    client_factory: Optional[Callable[[Path], Any]] = None,
) -> Dict[str, Any]:
    """Build and persist a reusable PageIndex registry from project blocks."""
    resolved_project_id = str(project_id or "").strip()
    if not resolved_project_id:
        raise ValueError("project_id不能为空")

    store = file_store or FileStore()
    filtered_blocks = _filter_blocks(blocks, file_names=file_names, source_block_ids=source_block_ids)
    if not filtered_blocks:
        raise ValueError("未找到可用于建立协议文档索引的数据块")

    resolved_doc_set_id = str(doc_set_id or "").strip() or f"docset_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    resolved_index_ref = str(index_ref or "").strip() or f"idx_{int(time.time())}"
    resolved_protocol_type = str(protocol_type or "").strip()
    resolved_message_codes = _normalize_message_codes(message_codes)
    resolved_tags = _normalize_tags(tags)
    resolved_document_paths = _normalize_document_paths(document_paths)
    resolved_document_fingerprints = {
        str(name or "").strip(): str(fingerprint or "").strip()
        for name, fingerprint in (document_fingerprints or {}).items()
        if str(name or "").strip() and str(fingerprint or "").strip()
    }
    document_path_lookup = _build_document_path_lookup(resolved_document_paths)

    existing_registry = store.load_pageindex_registry(resolved_project_id, resolved_doc_set_id)
    if existing_registry and not rebuild:
        return existing_registry

    workspace_dir = PAGEINDEX_WORKSPACE_ROOT / resolved_project_id / resolved_doc_set_id
    docs_dir = PAGEINDEX_DOC_ROOT / resolved_project_id / resolved_doc_set_id
    workspace_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    client = (client_factory or _default_pageindex_client_factory)(workspace_dir)
    grouped_blocks = _group_blocks_by_file(filtered_blocks)
    documents: List[Dict[str, Any]] = []
    logical_document_count = len(grouped_blocks)

    for file_name, file_blocks in grouped_blocks.items():
        shards = _build_block_shards(file_blocks)
        source_document_path = document_path_lookup.get(file_name)
        logical_document_id = _stable_hash(f"{file_name}::{source_document_path or ''}")[:24]
        for shard_index, shard_blocks in enumerate(shards, start=1):
            markdown = _render_document_markdown(
                file_name=file_name,
                blocks=shard_blocks,
                protocol_type=resolved_protocol_type,
                message_codes=resolved_message_codes,
                tags=resolved_tags,
                shard_index=shard_index,
                shard_count=len(shards),
            )
            file_hash = _stable_hash(markdown)
            normalized_name = f"{_slugify(file_name, 'document')}_part_{shard_index:03d}_{file_hash[:12]}.md"
            normalized_path = docs_dir / normalized_name
            normalized_path.write_text(markdown, encoding="utf-8")
            doc_id = client.index(str(normalized_path), mode="md")
            documents.append(
                {
                    "doc_id": str(doc_id),
                    "file_name": file_name,
                    "logical_document_id": logical_document_id,
                    "normalized_path": str(normalized_path),
                    "file_hash": file_hash,
                    "source_document_path": source_document_path,
                    "protocol_type": resolved_protocol_type or None,
                    "message_codes": list(resolved_message_codes),
                    "tags": list(resolved_tags),
                    "source_block_ids": [int(getattr(block, "block_id", 0) or 0) for block in shard_blocks],
                    "page_range": sorted({int(getattr(block, "page_num", 0) or 0) for block in shard_blocks}),
                    "field_terms": _extract_field_terms_from_blocks(shard_blocks),
                    "sample_text": _build_sample_text(shard_blocks),
                    "shard_index": shard_index,
                    "shard_count": len(shards),
                    "shard_block_count": len(shard_blocks),
                    "shard_char_count": sum(len(_block_content(block)) for block in shard_blocks),
                    "status": "indexed",
                }
            )

    doc_set_payload = {
        "project_id": resolved_project_id,
        "dataset_id": str(dataset_id or "").strip() or None,
        "doc_set_id": resolved_doc_set_id,
        "index_ref": resolved_index_ref,
        "protocol_type": resolved_protocol_type or None,
        "message_codes": list(resolved_message_codes),
        "tags": list(resolved_tags),
        "document_count": logical_document_count,
        "indexed_shard_count": len(documents),
        "documents": [
            {
                "doc_id": item["doc_id"],
                "file_name": item["file_name"],
                "logical_document_id": item["logical_document_id"],
                "file_hash": item["file_hash"],
                "source_document_path": item.get("source_document_path"),
                "source_block_ids": item["source_block_ids"],
                "page_range": item.get("page_range") or [],
                "shard_index": item.get("shard_index"),
                "shard_count": item.get("shard_count"),
            }
            for item in documents
        ],
        "source_documents": list(resolved_document_paths),
        "source_document_fingerprints": dict(resolved_document_fingerprints),
        "created_at": datetime_now_iso(),
    }
    store.save_project_doc_set(resolved_project_id, resolved_doc_set_id, doc_set_payload)

    registry = {
        "project_id": resolved_project_id,
        "dataset_id": str(dataset_id or "").strip() or None,
        "doc_set_id": resolved_doc_set_id,
        "index_ref": resolved_index_ref,
        "status": "ready",
        "protocol_type": resolved_protocol_type or None,
        "message_codes": list(resolved_message_codes),
        "tags": list(resolved_tags),
        "workspace_dir": str(workspace_dir),
        "docs_dir": str(docs_dir),
        "document_count": logical_document_count,
        "indexed_shard_count": len(documents),
        "shard_config": {
            "max_blocks": DEFAULT_SHARD_MAX_BLOCKS,
            "max_pages": DEFAULT_SHARD_MAX_PAGES,
            "max_chars": DEFAULT_SHARD_MAX_CHARS,
        },
        "documents": documents,
        "source_documents": list(resolved_document_paths),
        "source_document_fingerprints": dict(resolved_document_fingerprints),
        "created_at": datetime_now_iso(),
        "updated_at": datetime_now_iso(),
    }
    store.save_pageindex_registry(resolved_project_id, resolved_doc_set_id, registry)

    if dataset_id:
        store.update_dataset_meta(
            str(dataset_id).strip(),
            {
                "doc_set_id": resolved_doc_set_id,
                "index_ref": resolved_index_ref,
                "protocol_type": resolved_protocol_type or None,
                "message_codes": list(resolved_message_codes),
            },
        )
    return registry


def datetime_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class TrainedDocEvidenceProvider(PageIndexEvidenceProvider):
    """Reuse training-stage PageIndex registries during rule generation."""

    def __init__(
        self,
        project_id: str = "",
        dataset_id: str = "",
        doc_set_id: str = "",
        index_ref: str = "",
        index_registry_path: Any = None,
        file_store: Optional[FileStore] = None,
        client_factory: Optional[Callable[[Path], Any]] = None,
    ):
        self.file_store = file_store or FileStore()
        has_index_registry_path = False
        if isinstance(index_registry_path, (list, tuple)):
            has_index_registry_path = any(str(item or "").strip() for item in index_registry_path)
        else:
            has_index_registry_path = bool(str(index_registry_path or "").strip())
        if has_index_registry_path:
            registries = _load_pageindex_registries_from_path(index_registry_path)
        else:
            resolved_registry = self.file_store.resolve_pageindex_registry(
                project_id=project_id,
                dataset_id=dataset_id,
                doc_set_id=doc_set_id,
                index_ref=index_ref,
            )
            registries = [resolved_registry] if resolved_registry else []
        self.registry = _merge_pageindex_registries(registries)
        workspace_dir = self.registry.get("workspace_dir") if isinstance(self.registry, dict) else None
        docs_dir = self.registry.get("docs_dir") if isinstance(self.registry, dict) else None
        self._registry_clients: Dict[str, Any] = {}
        super().__init__(
            workspace_dir=Path(workspace_dir) if workspace_dir else None,
            docs_dir=Path(docs_dir) if docs_dir else None,
            client_factory=client_factory,
        )

    def collect_evidence(
        self,
        source_protocol: Dict[str, Any],
        target_protocol: Dict[str, Any],
        source_message: Optional[Any] = None,
        max_snippets_per_role: int = 3,
    ) -> Dict[str, Any]:
        if not self.registry:
            return {
                "status": "unavailable",
                "reason": "trained_doc_registry_not_found",
                "evidence_snippets": [],
                "evidence_snippet_count": 0,
            }
        source_queries = self._extract_source_queries(source_protocol, source_message)
        target_queries = self._extract_target_queries(target_protocol)
        snippets = self._collect_source_registry_snippets(
            source_protocol=source_protocol,
            queries=source_queries,
            top_k=max_snippets_per_role,
        )

        if target_queries:
            try:
                client = self._get_client()
                target_doc_id = self._get_or_create_document(client, "target", target_protocol)
                if target_doc_id:
                    snippets.extend(
                        self._collect_role_snippets(
                            client=client,
                            doc_id=target_doc_id,
                            role="target",
                            protocol=target_protocol,
                            queries=target_queries,
                            top_k=max_snippets_per_role,
                        )
                    )
            except Exception:
                pass

        snippets.sort(key=lambda item: (-float(item.get("score") or 0), str(item.get("title") or "")))
        limited_snippets = snippets[: max(1, max_snippets_per_role * 2)]
        return {
            "status": "used" if limited_snippets else "fallback",
            "reason": None,
            "evidence_snippets": limited_snippets,
            "evidence_snippet_count": len(limited_snippets),
            "candidate_doc_count": len(self._filter_registry_documents(source_protocol)),
            "matched_doc_ids": sorted({str(item.get("doc_id") or "") for item in limited_snippets if item.get("doc_id")}),
            "doc_set_id": self.registry.get("doc_set_id"),
            "index_ref": self.registry.get("index_ref"),
            "registry_count": int(self.registry.get("registry_count") or 0),
            "registry_paths": list(self.registry.get("registry_paths") or []),
        }

    def _get_registry_client(self, workspace_dir: Any):
        resolved_workspace = Path(str(workspace_dir or self.workspace_dir)).resolve()
        cache_key = str(resolved_workspace)
        client = self._registry_clients.get(cache_key)
        if client is None:
            client = self.client_factory(resolved_workspace)
            self._registry_clients[cache_key] = client
        return client

    def _get_document_client(self, document: Dict[str, Any]):
        workspace_dir = str(document.get("_workspace_dir") or self.registry.get("workspace_dir") or "").strip()
        if not workspace_dir:
            return self._get_client()
        return self._get_registry_client(workspace_dir)

    def _filter_registry_documents(self, source_protocol: Dict[str, Any]) -> List[Dict[str, Any]]:
        documents = list((self.registry.get("documents") or [])) if isinstance(self.registry, dict) else []
        protocol_type = str(source_protocol.get("protocol_type") or "").strip()
        message_code = str(source_protocol.get("message_code") or "").strip()
        filtered = documents
        if protocol_type:
            protocol_filtered = [
                doc for doc in filtered if str(doc.get("protocol_type") or "").strip() in {"", protocol_type}
            ]
            if protocol_filtered:
                filtered = protocol_filtered
        if message_code:
            message_filtered = []
            for doc in filtered:
                codes = _normalize_message_codes(doc.get("message_codes"))
                if not codes or message_code in codes:
                    message_filtered.append(doc)
            if message_filtered:
                filtered = message_filtered
        return filtered or documents

    def _rank_registry_documents(self, documents: List[Dict[str, Any]], queries: List[str]) -> List[Dict[str, Any]]:
        if not documents:
            return []
        if not queries:
            return documents[:DEFAULT_SCAN_SHARD_LIMIT]

        ranked: List[Dict[str, Any]] = []
        for document in documents:
            scored = dict(document)
            scored["_registry_score"] = _score_document_shard(document, queries)
            ranked.append(scored)

        ranked.sort(
            key=lambda item: (
                -float(item.get("_registry_score") or 0.0),
                str(item.get("file_name") or ""),
                int(item.get("shard_index") or 0),
            )
        )

        selected: List[Dict[str, Any]] = []
        per_file_counts: Dict[str, int] = {}
        for item in ranked:
            file_name = str(item.get("file_name") or "")
            if per_file_counts.get(file_name, 0) >= DEFAULT_SCAN_SHARDS_PER_FILE:
                continue
            selected.append(item)
            per_file_counts[file_name] = per_file_counts.get(file_name, 0) + 1
            if len(selected) >= DEFAULT_SCAN_SHARD_LIMIT:
                break
        return selected or ranked[:DEFAULT_SCAN_SHARD_LIMIT]

    def _collect_source_registry_snippets(
        self,
        source_protocol: Dict[str, Any],
        queries: List[str],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        ranked: List[Dict[str, Any]] = []
        candidate_documents = self._rank_registry_documents(self._filter_registry_documents(source_protocol), queries)
        for document in candidate_documents:
            doc_id = str(document.get("doc_id") or "").strip()
            if not doc_id:
                continue
            try:
                client = self._get_document_client(document)
            except Exception:
                continue
            role_snippets = self._collect_role_snippets(
                client=client,
                doc_id=doc_id,
                role="source",
                protocol=source_protocol,
                queries=queries,
                top_k=top_k,
            )
            for item in role_snippets:
                item["doc_id"] = doc_id
                item["file_name"] = document.get("file_name")
            ranked.extend(role_snippets)
        ranked.sort(key=lambda item: (-float(item.get("score") or 0), str(item.get("title") or "")))
        return ranked[:top_k]


def get_trained_doc_evidence_provider(
    project_id: str = "",
    dataset_id: str = "",
    doc_set_id: str = "",
    index_ref: str = "",
    index_registry_path: Any = None,
    file_store: Optional[FileStore] = None,
    client_factory: Optional[Callable[[Path], Any]] = None,
) -> TrainedDocEvidenceProvider:
    return TrainedDocEvidenceProvider(
        project_id=project_id,
        dataset_id=dataset_id,
        doc_set_id=doc_set_id,
        index_ref=index_ref,
        index_registry_path=index_registry_path,
        file_store=file_store,
        client_factory=client_factory,
    )
