from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pymysql
from pymysql.cursors import DictCursor


@dataclass
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


def load_db_config() -> DBConfig:
    return DBConfig(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "protocol_user"),
        password=os.getenv("MYSQL_PASSWORD", "change_me"),
        database=os.getenv("MYSQL_DATABASE", "protocol_db"),
    )


def connect(db_config: Optional[DBConfig] = None):
    cfg = db_config or load_db_config()
    return pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
    )


def _unique_preserve_order(values: Iterable[Any]) -> List[Any]:
    seen = set()
    ordered: List[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _normalize_qa_text(text: Any) -> str:
    return " ".join(str(text or "").strip().split()).lower()


def _dedup_generated_qas(qa_pairs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按问答正文做稳定去重，避免相同 QA 被重复写入 doc_qa_pairs。"""
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for qa in qa_pairs:
        if not isinstance(qa, dict):
            continue
        question = str(qa.get("question") or "").strip()
        answer = str(qa.get("answer") or "").strip()
        if not question or not answer:
            continue
        key = (_normalize_qa_text(question), _normalize_qa_text(answer))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(qa)
    return deduped


def _extract_document_ids(chunks: Sequence[Dict[str, Any]]) -> List[int]:
    document_ids: List[int] = []
    for chunk in chunks:
        source_blocks = chunk.get("source_blocks") or []
        for block in source_blocks:
            metadata = block.get("metadata") if isinstance(block, dict) else {}
            raw_value = None
            if isinstance(metadata, dict):
                raw_value = metadata.get("document_id")
            if raw_value in (None, ""):
                continue
            try:
                document_ids.append(int(raw_value))
            except (TypeError, ValueError):
                continue
    return _unique_preserve_order(document_ids)


def _extract_document_names(chunks: Sequence[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for chunk in chunks:
        source_blocks = chunk.get("source_blocks") or []
        for block in source_blocks:
            file_name = str((block or {}).get("file_name") or "").strip()
            if file_name:
                names.append(file_name)
    return _unique_preserve_order(names)


def create_rag_chunk_task(
    conn,
    *,
    chunks: Sequence[Dict[str, Any]],
    trace_id: str,
    created_by: str,
    split_config: Optional[Dict[str, Any]] = None,
) -> int:
    doc_ids = _extract_document_ids(chunks)
    doc_names = _extract_document_names(chunks)
    if not doc_ids:
        raise ValueError("语义块结果中缺少可写入 rag_chunk_task 的 document_id")

    payload = split_config or {}
    cursor = conn.cursor()
    cursor.execute(
        """
            INSERT INTO rag_chunk_task (
                doc_ids,
                doc_names,
                prompt_config_id,
                split_config,
                task_status,
                error_msg,
                trace_id,
                deleted,
                created_by,
                updated_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            _json_text(doc_ids),
            _json_text(doc_names),
            None,
            _json_text(payload),
            1,
            None,
            trace_id,
            0,
            created_by,
            created_by,
        ),
    )
    return int(cursor.lastrowid)


def write_semantic_chunks_to_db(
    semantic_response: Dict[str, Any],
    *,
    trace_id: str,
    created_by: str = "external_backend",
    task_id: Optional[int] = None,
    split_config: Optional[Dict[str, Any]] = None,
    db_config: Optional[DBConfig] = None,
) -> Dict[str, Any]:
    data = semantic_response.get("data") if isinstance(semantic_response, dict) else None
    if not isinstance(data, dict):
        raise ValueError("semantic_response 缺少 data 对象")
    chunks = data.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("semantic_response.data.chunks 不能为空")

    conn = connect(db_config)
    try:
        resolved_task_id = task_id or create_rag_chunk_task(
            conn,
            chunks=chunks,
            trace_id=trace_id,
            created_by=created_by,
            split_config=split_config or {
                "source": "external_backend",
                "project_id": data.get("project_id"),
                "dataset_id": data.get("dataset_id"),
                "chunk_count": len(chunks),
            },
        )

        inserted_chunk_ids: List[str] = []
        with conn.cursor() as cursor:
            for chunk in chunks:
                chunk_id = str(chunk.get("chunk_id") or "").strip()
                if not chunk_id:
                    continue
                source_block_ids = chunk.get("source_block_ids") or []
                semantic_type = str(chunk.get("semantic_type") or "").strip() or None
                content_snapshot = (
                    str(chunk.get("merged_content") or "").strip()
                    or str(chunk.get("content_snapshot") or "").strip()
                    or None
                )
                metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
                cursor.execute(
                    """
                        INSERT INTO rag_chunk_metadata (
                            task_id,
                            chunk_id,
                            source_block_ids,
                            semantic_type,
                            content_snapshot,
                            metadata,
                            milvus_pk,
                            trace_id,
                            deleted,
                            created_by,
                            updated_by
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        resolved_task_id,
                        chunk_id,
                        _json_text(source_block_ids),
                        semantic_type,
                        content_snapshot,
                        _json_text(metadata) if metadata else None,
                        None,
                        trace_id,
                        0,
                        created_by,
                        created_by,
                    ),
                )
                inserted_chunk_ids.append(chunk_id)
        conn.commit()
        return {
            "task_id": resolved_task_id,
            "doc_ids": _extract_document_ids(chunks),
            "doc_names": _extract_document_names(chunks),
            "chunk_ids": inserted_chunk_ids,
            "chunk_count": len(inserted_chunk_ids),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def write_generated_qas_to_db(
    qa_response: Dict[str, Any],
    *,
    task_id: int,
    trace_id: str,
    created_by: str = "external_backend",
    db_config: Optional[DBConfig] = None,
) -> Dict[str, Any]:
    data = qa_response.get("data") if isinstance(qa_response, dict) else None
    if not isinstance(data, dict):
        raise ValueError("qa_response 缺少 data 对象")
    qa_pairs = data.get("qa_pairs")
    if not isinstance(qa_pairs, list) or not qa_pairs:
        raise ValueError("qa_response.data.qa_pairs 不能为空")
    qa_pairs = _dedup_generated_qas(qa_pairs)
    if not qa_pairs:
        raise ValueError("qa_response.data.qa_pairs 去重后为空")

    selected_chunk_ids = data.get("selected_chunk_ids") or []
    if not isinstance(selected_chunk_ids, list):
        selected_chunk_ids = []
    normalized_chunk_ids = [str(item).strip() for item in selected_chunk_ids if str(item).strip()]

    conn = connect(db_config)
    try:
        inserted_rows: List[Dict[str, Any]] = []
        with conn.cursor() as cursor:
            for qa in qa_pairs:
                question = str(qa.get("question") or "").strip()
                answer = str(qa.get("answer") or "").strip()
                if not question or not answer:
                    continue
                cursor.execute(
                    """
                        INSERT INTO doc_qa_pairs (
                            task_id,
                            source_chunk_ids,
                            question,
                            answer,
                            review_status,
                            validation_result_status,
                            trace_id,
                            deleted,
                            created_by,
                            updated_by
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        task_id,
                        _json_text(normalized_chunk_ids) if normalized_chunk_ids else None,
                        question,
                        answer,
                        0,
                        None,
                        trace_id,
                        0,
                        created_by,
                        created_by,
                    ),
                )
                inserted_rows.append(
                    {
                        "db_qa_id": int(cursor.lastrowid),
                        "api_qa_id": str(qa.get("qa_id") or "").strip() or None,
                        "question": question,
                    }
                )
        conn.commit()
        return {
            "task_id": task_id,
            "selected_chunk_ids": normalized_chunk_ids,
            "inserted": inserted_rows,
            "qa_count": len(inserted_rows),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _load_json_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 文件不是对象: {path}")
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将算法接口产物写入 protobridge_dev，供后续接口继续读取。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    semantic_parser = subparsers.add_parser("semantic", help="把 04 semantic_chunk 结果写入 rag_chunk_task/rag_chunk_metadata")
    semantic_parser.add_argument("--input", required=True, help="04 semantic_chunk 的完整 JSON 响应文件")
    semantic_parser.add_argument("--trace-id", required=True, help="写入数据库使用的 trace_id")
    semantic_parser.add_argument("--created-by", default="external_backend")

    qa_parser = subparsers.add_parser("qa", help="把 05 generate_qa 结果写入 doc_qa_pairs")
    qa_parser.add_argument("--input", required=True, help="05 generate_qa 的完整 JSON 响应文件")
    qa_parser.add_argument("--task-id", required=True, type=int, help="对应 rag_chunk_task.id，也作为 doc_qa_pairs.task_id")
    qa_parser.add_argument("--trace-id", required=True, help="写入数据库使用的 trace_id")
    qa_parser.add_argument("--created-by", default="external_backend")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "semantic":
        payload = _load_json_file(args.input)
        result = write_semantic_chunks_to_db(
            payload,
            trace_id=args.trace_id,
            created_by=args.created_by,
        )
    elif args.command == "qa":
        payload = _load_json_file(args.input)
        result = write_generated_qas_to_db(
            payload,
            task_id=args.task_id,
            trace_id=args.trace_id,
            created_by=args.created_by,
        )
    else:
        raise ValueError(f"不支持的命令: {args.command}")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
