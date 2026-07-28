# shared/database/mysql_client.py
# MySQL数据库客户端（支持SQLite本地回退）

import os
import json
import sqlite3
from typing import Optional, List, Dict, Any, Sequence
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from .models import Block, Chunk, QAPair, FinetuneJob, JobStatus, CleaningIssue


class MySQLClient:
    """MySQL数据库客户端（无服务时可自动回退SQLite）"""

    def __init__(
        self,
        host: str = None,
        port: int = None,
        user: str = None,
        password: str = None,
        database: str = None,
    ):
        self.host = host or os.getenv("MYSQL_HOST", "localhost")
        self.port = port or int(os.getenv("MYSQL_PORT", "3306"))
        self.user = user or os.getenv("MYSQL_USER", "root")
        self.password = password or os.getenv("MYSQL_PASSWORD", "password")
        self.database = database or os.getenv("MYSQL_DATABASE", "protocol_db")

        default_sqlite = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data",
            f"{self.database}.sqlite3",
        )
        self.sqlite_path = os.path.expanduser(os.getenv("SQLITE_DB_PATH", default_sqlite))
        self.backend = "sqlite" if os.getenv("MYSQL_USE_SQLITE", "false").lower() == "true" else "mysql"
        self.auto_fallback_sqlite = os.getenv("MYSQL_AUTO_FALLBACK_SQLITE", "true").lower() == "true"
        self._pool = []
        self.write_enabled = False
        self._schema_mode: Optional[str] = None

    @property
    def is_sqlite(self) -> bool:
        return self.backend == "sqlite"

    def _mysql_connection(self):
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset="utf8mb4",
            cursorclass=DictCursor,
        )

    def _sqlite_connection(self):
        os.makedirs(os.path.dirname(self.sqlite_path), exist_ok=True)
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_connection(self):
        """获取数据库连接。MySQL不可用时按配置自动回退SQLite。"""
        if self.is_sqlite:
            return self._sqlite_connection()
        try:
            return self._mysql_connection()
        except Exception:
            if not self.auto_fallback_sqlite:
                raise
            self.backend = "sqlite"
            return self._sqlite_connection()

    @contextmanager
    def connection(self):
        """连接上下文管理器"""
        conn = self._get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _normalize_sqlite_query(self, query: str) -> str:
        q = query.replace("%s", "?")
        q = q.replace("NOW()", "CURRENT_TIMESTAMP")
        q = q.replace("BOOLEAN", "INTEGER")
        q = q.replace("TRUE", "1").replace("FALSE", "0")
        return q

    def _execute(self, cursor, query: str, params: Optional[Sequence[Any]] = None):
        if self.is_sqlite:
            query = self._normalize_sqlite_query(query)
        if params is None:
            return cursor.execute(query)
        return cursor.execute(query, params)

    def _fetchall_dict(self, cursor) -> List[Dict[str, Any]]:
        rows = cursor.fetchall()
        if not rows:
            return []
        if isinstance(rows[0], dict):
            return rows
        return [dict(row) for row in rows]

    def _fetchone_dict(self, cursor) -> Optional[Dict[str, Any]]:
        row = cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return row
        return dict(row)

    def _column_exists(self, cursor, table_name: str, column_name: str) -> bool:
        """检查列是否存在（兼容MySQL/SQLite）"""
        if self.is_sqlite:
            self._execute(cursor, f"PRAGMA table_info({table_name})")
            columns = self._fetchall_dict(cursor)
            return any(col.get("name") == column_name for col in columns)

        self._execute(
            cursor,
            """
                SELECT 1
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME = %s
                  AND COLUMN_NAME = %s
                LIMIT 1
            """,
            (self.database, table_name, column_name),
        )
        return self._fetchone_dict(cursor) is not None

    def _placeholders(self, count: int) -> str:
        mark = "?" if self.is_sqlite else "%s"
        return ",".join([mark] * count)

    def _table_exists(self, cursor, table_name: str) -> bool:
        if self.is_sqlite:
            self._execute(
                cursor,
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = %s LIMIT 1",
                (table_name,),
            )
            return self._fetchone_dict(cursor) is not None

        self._execute(
            cursor,
            """
                SELECT 1
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME = %s
                LIMIT 1
            """,
            (self.database, table_name),
        )
        return self._fetchone_dict(cursor) is not None

    def _get_schema_mode(self) -> str:
        if self.is_sqlite:
            return "sqlite"
        if self._schema_mode:
            return self._schema_mode

        conn = None
        try:
            conn = self._mysql_connection()
            cursor = conn.cursor()
            has_protobridge = all(
                self._table_exists(cursor, table_name)
                for table_name in ("dataset", "document_split", "document_split_block", "document_clean")
            )
            has_native = all(
                self._table_exists(cursor, table_name)
                for table_name in ("blocks", "chunks", "qa_pairs", "finetune_jobs", "pipeline_payloads")
            )
            if has_protobridge:
                self._schema_mode = "protobridge_dev"
            else:
                self._schema_mode = "native" if has_native else "native"
        except Exception:
            self._schema_mode = "native"
        finally:
            if conn is not None:
                conn.close()

        return self._schema_mode

    def get_schema_mode(self) -> str:
        return self._get_schema_mode()

    def resolve_dataset_document_ids(self, dataset_ref: str) -> List[int]:
        """Resolve a public dataset/document reference to business document IDs."""
        if self._get_schema_mode() != "protobridge_dev":
            return []
        with self.connection() as conn:
            cursor = conn.cursor()
            return self._get_dataset_doc_ids(cursor, dataset_ref)

    def _json_load(self, raw_value: Any, default: Any):
        if raw_value in (None, ""):
            return default
        if isinstance(raw_value, (dict, list)):
            return raw_value
        try:
            return json.loads(raw_value)
        except (TypeError, json.JSONDecodeError):
            return default

    def _normalize_page_range(self, raw_value: Any, fallback_page_num: int = 0) -> List[int]:
        values = self._json_load(raw_value, [])
        if isinstance(values, list):
            normalized: List[int] = []
            for item in values:
                try:
                    page_num = int(item)
                except (TypeError, ValueError):
                    continue
                if page_num > 0 and page_num not in normalized:
                    normalized.append(page_num)
            if normalized:
                return normalized
        if fallback_page_num > 0:
            return [fallback_page_num]
        return []

    def _normalize_int_list(self, raw_value: Any) -> List[int]:
        values = self._json_load(raw_value, [])
        if not isinstance(values, list):
            return []
        normalized: List[int] = []
        for item in values:
            try:
                normalized.append(int(item))
            except (TypeError, ValueError):
                continue
        return normalized

    def _get_dataset_doc_ids(self, cursor, dataset_ref: str) -> List[int]:
        resolved_dataset_ref = str(dataset_ref or "").strip()
        if not resolved_dataset_ref:
            return []

        doc_ids: List[int] = []
        self._execute(
            cursor,
            """
                SELECT doc_ids
                FROM dataset
                WHERE id = %s
                  AND COALESCE(deleted, 0) = 0
                LIMIT 1
            """,
            (resolved_dataset_ref,),
        )
        row = self._fetchone_dict(cursor)
        doc_ids.extend(self._normalize_int_list(row.get("doc_ids") if row else None))

        self._execute(
            cursor,
            """
                SELECT document_id
                FROM document_clean
                WHERE dataset_id = %s
                  AND COALESCE(deleted, 0) = 0
                ORDER BY id DESC
            """,
            (resolved_dataset_ref,),
        )
        for row in self._fetchall_dict(cursor):
            try:
                doc_ids.append(int(row.get("document_id")))
            except (TypeError, ValueError):
                continue

        try:
            doc_ids.append(int(resolved_dataset_ref))
        except (TypeError, ValueError):
            pass

        if resolved_dataset_ref.startswith("ds_"):
            try:
                doc_ids.append(int(resolved_dataset_ref[3:]))
            except (TypeError, ValueError):
                pass

        unique_doc_ids: List[int] = []
        seen = set()
        for doc_id in doc_ids:
            if doc_id in seen:
                continue
            seen.add(doc_id)
            unique_doc_ids.append(doc_id)
        return unique_doc_ids

    def _get_document_clean_by_id(self, cursor, clean_id: int) -> Optional[Dict[str, Any]]:
        self._execute(
            cursor,
            """
                SELECT *
                FROM document_clean
                WHERE id = %s
                  AND COALESCE(deleted, 0) = 0
                LIMIT 1
            """,
            (clean_id,),
        )
        return self._fetchone_dict(cursor)

    def _get_latest_document_clean_by_dataset(self, cursor, dataset_ref: str) -> Optional[Dict[str, Any]]:
        self._execute(
            cursor,
            """
                SELECT *
                FROM document_clean
                WHERE dataset_id = %s
                  AND COALESCE(deleted, 0) = 0
                ORDER BY id DESC
                LIMIT 1
            """,
            (dataset_ref,),
        )
        return self._fetchone_dict(cursor)

    def _get_latest_document_clean_by_document_id(self, cursor, document_id: int) -> Optional[Dict[str, Any]]:
        self._execute(
            cursor,
            """
                SELECT *
                FROM document_clean
                WHERE document_id = %s
                  AND COALESCE(deleted, 0) = 0
                ORDER BY id DESC
                LIMIT 1
            """,
            (document_id,),
        )
        return self._fetchone_dict(cursor)

    def _get_protobridge_blocks_for_documents(
        self,
        cursor,
        document_ids: List[int],
        block_ids: Optional[List[int]] = None,
        block_id_column: str = "id",
    ) -> List[Block]:
        normalized_document_ids = [int(item) for item in document_ids if str(item).strip()]
        if not normalized_document_ids:
            return []

        doc_placeholders = self._placeholders(len(normalized_document_ids))
        params: List[Any] = list(normalized_document_ids)
        query = f"""
            SELECT b.*, s.project_id, s.file_name, b.document_id
            FROM document_split_block b
            JOIN (
                SELECT document_id, MAX(id) AS latest_split_id
                FROM document_split
                WHERE document_id IN ({doc_placeholders})
                  AND COALESCE(deleted, 0) = 0
                GROUP BY document_id
            ) latest ON latest.document_id = b.document_id
            LEFT JOIN document_split s ON s.id = latest.latest_split_id
            WHERE COALESCE(b.deleted, 0) = 0
              AND b.document_id IN ({doc_placeholders})
        """
        params.extend(normalized_document_ids)
        if block_ids:
            normalized_block_ids = [int(item) for item in block_ids if str(item).strip()]
            if normalized_block_ids:
                column_name = "block_id" if block_id_column == "block_id" else "id"
                query += f" AND b.{column_name} IN ({self._placeholders(len(normalized_block_ids))})"
                params.extend(normalized_block_ids)
        query += " ORDER BY s.document_id, b.page_num, b.id"
        self._execute(cursor, query, params)
        rows = self._fetchall_dict(cursor)
        return [self._row_to_block(row) for row in rows]

    def get_document_blocks_by_logical_ids(self, dataset_ref: str, block_ids: List[int]) -> List[Block]:
        """Read latest split blocks by public logical block_id under a dataset/document ref."""
        normalized_ids = [int(item) for item in block_ids if str(item).strip()]
        if not normalized_ids or self._get_schema_mode() != "protobridge_dev":
            return []
        with self.connection() as conn:
            cursor = conn.cursor()
            document_ids = self._get_dataset_doc_ids(cursor, dataset_ref)
            if not document_ids:
                return []
            blocks = self._get_protobridge_blocks_for_documents(
                cursor,
                document_ids,
                block_ids=normalized_ids,
                block_id_column="block_id",
            )
        block_map: Dict[int, Block] = {}
        for block in blocks:
            metadata = block.metadata if isinstance(block.metadata, dict) else {}
            logical_id = metadata.get("legacy_block_id")
            try:
                block_map.setdefault(int(logical_id), block)
            except (TypeError, ValueError):
                continue
        return [block_map[block_id] for block_id in normalized_ids if block_id in block_map]

    def _build_protobridge_payload(
        self,
        *,
        content_id: str,
        dataset_id: str,
        payload_type: str,
        blocks: List[Block],
    ) -> Dict[str, Any]:
        if not blocks:
            return {}
        project_id = str(blocks[0].project_id or "")
        payload_blocks = [block.to_dict() for block in blocks]
        return {
            "content_id": content_id,
            "project_id": project_id,
            "dataset_id": dataset_id or None,
            "payload_type": payload_type,
            "payload": {
                "project_id": project_id,
                "dataset_id": dataset_id or None,
                "blocks": payload_blocks,
            },
            "file_path": None,
            "file_name": blocks[0].file_name or None,
            "created_at": blocks[0].created_at,
            "updated_at": blocks[-1].updated_at,
        }

    def _build_protobridge_document_payload(self, cursor, document_id: int) -> Dict[str, Any]:
        blocks = self._get_protobridge_blocks_for_documents(cursor, [document_id])
        if not blocks:
            return {}
        clean_row = self._get_latest_document_clean_by_document_id(cursor, document_id)
        dataset_id = str(clean_row.get("dataset_id") or f"ds_{document_id}") if clean_row else f"ds_{document_id}"
        return self._build_protobridge_payload(
            content_id=f"protobridge_document:{document_id}",
            dataset_id=dataset_id,
            payload_type="blocks",
            blocks=blocks,
        )

    def _build_protobridge_clean_payload(self, cursor, clean_row: Dict[str, Any]) -> Dict[str, Any]:
        if not clean_row:
            return {}
        try:
            document_id = int(clean_row.get("document_id"))
        except (TypeError, ValueError):
            return {}
        block_ids = self._normalize_int_list(clean_row.get("block_ids"))
        blocks = self._get_protobridge_blocks_for_documents(cursor, [document_id], block_ids=block_ids or None)
        if not blocks:
            blocks = self._get_protobridge_blocks_for_documents(cursor, [document_id])
        return self._build_protobridge_payload(
            content_id=f"protobridge_clean:{clean_row['id']}",
            dataset_id=str(clean_row.get("dataset_id") or f"ds_{document_id}"),
            payload_type="cleaned_blocks",
            blocks=blocks,
        )

    def _build_protobridge_dataset_payload(self, cursor, dataset_ref: str, prefer_cleaned: bool) -> Dict[str, Any]:
        resolved_dataset_ref = str(dataset_ref or "").strip()
        if not resolved_dataset_ref:
            return {}

        doc_ids = self._get_dataset_doc_ids(cursor, resolved_dataset_ref)
        if not doc_ids:
            return {}

        if prefer_cleaned:
            clean_row = self._get_latest_document_clean_by_dataset(cursor, resolved_dataset_ref)
            if clean_row:
                payload = self._build_protobridge_clean_payload(cursor, clean_row)
                if payload:
                    return payload

        blocks = self._get_protobridge_blocks_for_documents(cursor, doc_ids)
        return self._build_protobridge_payload(
            content_id=f"protobridge_dataset:{resolved_dataset_ref}",
            dataset_id=resolved_dataset_ref,
            payload_type="blocks",
            blocks=blocks,
        )

    def get_dataset_document_blocks_payload(self, dataset_ref: str) -> Dict[str, Any]:
        """按 dataset_id 解析 document_id 列表，再按 document_id 读取块内容。"""
        resolved_dataset_ref = str(dataset_ref or "").strip()
        if not resolved_dataset_ref:
            return {}
        if self._get_schema_mode() != "protobridge_dev":
            return self.get_latest_pipeline_payload_by_dataset(
                resolved_dataset_ref,
                payload_types=["cleaned_blocks", "blocks", "upload_split_blocks", "upload_split"],
            )

        with self.connection() as conn:
            cursor = conn.cursor()
            document_ids: List[int] = []

            self._execute(
                cursor,
                """
                    SELECT document_id
                    FROM document_clean
                    WHERE dataset_id = %s
                      AND COALESCE(deleted, 0) = 0
                    ORDER BY id DESC
                """,
                (resolved_dataset_ref,),
            )
            for row in self._fetchall_dict(cursor):
                try:
                    document_ids.append(int(row.get("document_id")))
                except (TypeError, ValueError):
                    continue

            if not document_ids:
                self._execute(
                    cursor,
                    """
                        SELECT doc_ids
                        FROM dataset
                        WHERE id = %s
                          AND COALESCE(deleted, 0) = 0
                        LIMIT 1
                    """,
                    (resolved_dataset_ref,),
                )
                row = self._fetchone_dict(cursor)
                document_ids.extend(self._normalize_int_list(row.get("doc_ids") if row else None))

            if not document_ids and resolved_dataset_ref.startswith("ds_"):
                try:
                    document_ids.append(int(resolved_dataset_ref[3:]))
                except (TypeError, ValueError):
                    pass

            dedup_document_ids: List[int] = []
            seen = set()
            for document_id in document_ids:
                if document_id in seen:
                    continue
                seen.add(document_id)
                dedup_document_ids.append(document_id)

            if not dedup_document_ids:
                return {}

            blocks = self._get_protobridge_blocks_for_documents(cursor, dedup_document_ids)
            if not blocks:
                return {}

            payload = self._build_protobridge_payload(
                content_id=f"protobridge_dataset:{resolved_dataset_ref}",
                dataset_id=resolved_dataset_ref,
                payload_type="blocks",
                blocks=blocks,
            )
            payload["document_ids"] = dedup_document_ids
            return payload

    def get_dataset_document_blocks_payloads(self, dataset_refs: List[Any]) -> Dict[str, Any]:
        """按多个 dataset_id 解析 document_id 列表，再按 document_id 读取块内容。"""
        normalized_refs: List[str] = []
        seen_refs = set()
        for dataset_ref in dataset_refs or []:
            normalized = str(dataset_ref or "").strip()
            if not normalized or normalized in seen_refs:
                continue
            seen_refs.add(normalized)
            normalized_refs.append(normalized)
        if not normalized_refs:
            return {}

        if self._get_schema_mode() != "protobridge_dev":
            for dataset_ref in normalized_refs:
                payload = self.get_latest_pipeline_payload_by_dataset(
                    dataset_ref,
                    payload_types=["cleaned_blocks", "blocks", "upload_split_blocks", "upload_split"],
                )
                if payload:
                    return payload
            return {}

        with self.connection() as conn:
            cursor = conn.cursor()
            document_ids: List[int] = []
            for dataset_ref in normalized_refs:
                document_ids.extend(self._get_dataset_doc_ids(cursor, dataset_ref))

            dedup_document_ids: List[int] = []
            seen_document_ids = set()
            for document_id in document_ids:
                if document_id in seen_document_ids:
                    continue
                seen_document_ids.add(document_id)
                dedup_document_ids.append(document_id)

            if not dedup_document_ids:
                return {}

            blocks = self._get_protobridge_blocks_for_documents(cursor, dedup_document_ids)
            if not blocks:
                return {}

            payload = self._build_protobridge_payload(
                content_id=f"protobridge_dataset:{normalized_refs[0]}",
                dataset_id=normalized_refs[0],
                payload_type="blocks",
                blocks=blocks,
            )
            payload["document_ids"] = dedup_document_ids
            return payload

    def get_document_blocks_payloads(self, document_ids: List[Any]) -> Dict[str, Any]:
        """按 document_split_block.document_id 直接读取多个文档的块内容。"""
        normalized_document_ids: List[int] = []
        seen_ids = set()
        for document_id in document_ids or []:
            try:
                normalized = int(str(document_id).strip())
            except (TypeError, ValueError):
                continue
            if normalized in seen_ids:
                continue
            seen_ids.add(normalized)
            normalized_document_ids.append(normalized)
        if not normalized_document_ids:
            return {}
        if self._get_schema_mode() != "protobridge_dev":
            return {}

        with self.connection() as conn:
            cursor = conn.cursor()
            blocks = self._get_protobridge_blocks_for_documents(cursor, normalized_document_ids)
            if not blocks:
                return {}
            payload = self._build_protobridge_payload(
                content_id=f"protobridge_document:{normalized_document_ids[0]}",
                dataset_id=str(normalized_document_ids[0]),
                payload_type="blocks",
                blocks=blocks,
            )
            payload["document_ids"] = normalized_document_ids
            return payload

    def _init_tables_mysql(self, cursor):
        self._execute(
            cursor,
            """
                CREATE TABLE IF NOT EXISTS blocks (
                    block_id INT PRIMARY KEY AUTO_INCREMENT,
                    project_id VARCHAR(64) NOT NULL,
                    file_name VARCHAR(255) NOT NULL,
                    page_num INT DEFAULT 1,
                    content TEXT,
                    block_type VARCHAR(32) DEFAULT 'text',
                    cleaned_content TEXT,
                    metadata JSON,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_project (project_id),
                    INDEX idx_file (file_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        )

        self._execute(
            cursor,
            """
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id VARCHAR(64) PRIMARY KEY,
                    project_id VARCHAR(64) NOT NULL,
                    dataset_id VARCHAR(64) NOT NULL,
                    source_block_ids JSON,
                    semantic_type VARCHAR(64),
                    content_snapshot TEXT,
                    metadata JSON,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_project (project_id),
                    INDEX idx_dataset (dataset_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        )

        self._execute(
            cursor,
            """
                CREATE TABLE IF NOT EXISTS qa_pairs (
                    qa_id VARCHAR(64) PRIMARY KEY,
                    source_block_ids JSON,
                    question TEXT,
                    answer TEXT,
                    qa_task_type VARCHAR(64) DEFAULT 'protocol_understanding',
                    conversion_mode VARCHAR(32),
                    conversion_formula TEXT,
                    source_field VARCHAR(128),
                    source_fields JSON,
                    target_field VARCHAR(128),
                    concept_name VARCHAR(128),
                    formula_kind VARCHAR(32),
                    target_protocol_type VARCHAR(64),
                    target_message_code VARCHAR(64),
                    instruction TEXT,
                    is_low_quality BOOLEAN DEFAULT FALSE,
                    quality_reason VARCHAR(255),
                    extracted_info JSON,
                    validation_result JSON,
                    protocol_type VARCHAR(32) DEFAULT 'Link16',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_protocol (protocol_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        )

        self._execute(
            cursor,
            """
                CREATE TABLE IF NOT EXISTS finetune_jobs (
                    job_id VARCHAR(64) PRIMARY KEY,
                    status VARCHAR(32) DEFAULT 'pending',
                    base_model VARCHAR(128),
                    dataset_id VARCHAR(64),
                    config JSON,
                    progress JSON,
                    last_checkpoint JSON,
                    model_path VARCHAR(512),
                    metrics JSON,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP NULL,
                    completed_at TIMESTAMP NULL,
                    INDEX idx_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        )

        self._execute(
            cursor,
            """
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset_id VARCHAR(64) PRIMARY KEY,
                    project_id VARCHAR(64) NOT NULL,
                    name VARCHAR(128),
                    description TEXT,
                    block_count INT DEFAULT 0,
                    chunk_count INT DEFAULT 0,
                    qa_count INT DEFAULT 0,
                    file_path VARCHAR(512),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_project (project_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        )

        self._execute(
            cursor,
            """
                CREATE TABLE IF NOT EXISTS pipeline_payloads (
                    content_id VARCHAR(128) PRIMARY KEY,
                    project_id VARCHAR(64),
                    dataset_id VARCHAR(64),
                    payload_type VARCHAR(64) NOT NULL,
                    payload_json LONGTEXT,
                    file_path VARCHAR(512),
                    file_name VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_pipeline_project (project_id),
                    INDEX idx_pipeline_dataset (dataset_id),
                    INDEX idx_pipeline_type (payload_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        )

    def _init_tables_sqlite(self, cursor):
        self._execute(
            cursor,
            """
                CREATE TABLE IF NOT EXISTS blocks (
                    block_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    page_num INTEGER DEFAULT 1,
                    content TEXT,
                    block_type TEXT DEFAULT 'text',
                    cleaned_content TEXT,
                    metadata TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """,
        )
        self._execute(cursor, "CREATE INDEX IF NOT EXISTS idx_blocks_project ON blocks(project_id)")
        self._execute(cursor, "CREATE INDEX IF NOT EXISTS idx_blocks_file ON blocks(file_name)")

        self._execute(
            cursor,
            """
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    source_block_ids TEXT,
                    semantic_type TEXT,
                    content_snapshot TEXT,
                    metadata TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """,
        )
        self._execute(cursor, "CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project_id)")
        self._execute(cursor, "CREATE INDEX IF NOT EXISTS idx_chunks_dataset ON chunks(dataset_id)")

        self._execute(
            cursor,
            """
                CREATE TABLE IF NOT EXISTS qa_pairs (
                    qa_id TEXT PRIMARY KEY,
                    source_block_ids TEXT,
                    question TEXT,
                    answer TEXT,
                    qa_task_type TEXT DEFAULT 'protocol_understanding',
                    conversion_mode TEXT,
                    conversion_formula TEXT,
                    source_field TEXT,
                    source_fields TEXT,
                    target_field TEXT,
                    concept_name TEXT,
                    formula_kind TEXT,
                    target_protocol_type TEXT,
                    target_message_code TEXT,
                    instruction TEXT,
                    is_low_quality INTEGER DEFAULT 0,
                    quality_reason TEXT,
                    extracted_info TEXT,
                    validation_result TEXT,
                    protocol_type TEXT DEFAULT 'Link16',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """,
        )
        self._execute(cursor, "CREATE INDEX IF NOT EXISTS idx_qa_protocol ON qa_pairs(protocol_type)")

        self._execute(
            cursor,
            """
                CREATE TABLE IF NOT EXISTS finetune_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT DEFAULT 'pending',
                    base_model TEXT,
                    dataset_id TEXT,
                    config TEXT,
                    progress TEXT,
                    last_checkpoint TEXT,
                    model_path TEXT,
                    metrics TEXT,
                    error_message TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT,
                    completed_at TEXT
                )
            """,
        )
        self._execute(cursor, "CREATE INDEX IF NOT EXISTS idx_finetune_status ON finetune_jobs(status)")

        self._execute(
            cursor,
            """
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    name TEXT,
                    description TEXT,
                    block_count INTEGER DEFAULT 0,
                    chunk_count INTEGER DEFAULT 0,
                    qa_count INTEGER DEFAULT 0,
                    file_path TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """,
        )
        self._execute(cursor, "CREATE INDEX IF NOT EXISTS idx_datasets_project ON datasets(project_id)")

        self._execute(
            cursor,
            """
                CREATE TABLE IF NOT EXISTS pipeline_payloads (
                    content_id TEXT PRIMARY KEY,
                    project_id TEXT,
                    dataset_id TEXT,
                    payload_type TEXT NOT NULL,
                    payload_json TEXT,
                    file_path TEXT,
                    file_name TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """,
        )
        self._execute(cursor, "CREATE INDEX IF NOT EXISTS idx_pipeline_payloads_project ON pipeline_payloads(project_id)")
        self._execute(cursor, "CREATE INDEX IF NOT EXISTS idx_pipeline_payloads_dataset ON pipeline_payloads(dataset_id)")
        self._execute(cursor, "CREATE INDEX IF NOT EXISTS idx_pipeline_payloads_type ON pipeline_payloads(payload_type)")

    def init_tables(self):
        """初始化数据表"""
        if not self.write_enabled:
            print("数据库写入已禁用，跳过表初始化")
            return
        if self._get_schema_mode() == "protobridge_dev":
            print("检测到 protobridge_dev 表结构，跳过内置表初始化")
            return
        with self.connection() as conn:
            cursor = conn.cursor()
            if self.is_sqlite:
                self._init_tables_sqlite(cursor)
            else:
                self._init_tables_mysql(cursor)

            qa_column_migrations = [
                ("qa_task_type", "qa_task_type TEXT DEFAULT 'protocol_understanding'" if self.is_sqlite else "qa_task_type VARCHAR(64) DEFAULT 'protocol_understanding'"),
                ("conversion_mode", "conversion_mode TEXT" if self.is_sqlite else "conversion_mode VARCHAR(32)"),
                ("conversion_formula", "conversion_formula TEXT"),
                ("source_field", "source_field TEXT" if self.is_sqlite else "source_field VARCHAR(128)"),
                ("source_fields", "source_fields TEXT" if self.is_sqlite else "source_fields JSON"),
                ("target_field", "target_field TEXT" if self.is_sqlite else "target_field VARCHAR(128)"),
                ("concept_name", "concept_name TEXT" if self.is_sqlite else "concept_name VARCHAR(128)"),
                ("formula_kind", "formula_kind TEXT" if self.is_sqlite else "formula_kind VARCHAR(32)"),
                ("target_protocol_type", "target_protocol_type TEXT" if self.is_sqlite else "target_protocol_type VARCHAR(64)"),
                ("target_message_code", "target_message_code TEXT" if self.is_sqlite else "target_message_code VARCHAR(64)"),
            ]
            for column_name, column_ddl in qa_column_migrations:
                try:
                    if self._column_exists(cursor, "qa_pairs", column_name):
                        continue
                    self._execute(cursor, f"ALTER TABLE qa_pairs ADD COLUMN {column_ddl}")
                except Exception:
                    pass

            print(f"数据库表初始化完成 (backend={self.backend})")

    # ==================== Block 操作 ====================

    def insert_block(self, block: Block) -> int:
        """插入文档块"""
        if not self.write_enabled:
            return 0
        with self.connection() as conn:
            cursor = conn.cursor()
            self._execute(
                cursor,
                """
                    INSERT INTO blocks (project_id, file_name, page_num, content, block_type, cleaned_content, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    block.project_id,
                    block.file_name,
                    block.page_num,
                    block.content,
                    block.block_type,
                    block.cleaned_content,
                    json.dumps(block.metadata, ensure_ascii=False) if block.metadata else None,
                ),
            )
            return cursor.lastrowid

    def insert_blocks(self, blocks: List[Block]) -> List[int]:
        """批量插入文档块"""
        if not self.write_enabled:
            return []
        ids = []
        for block in blocks:
            ids.append(self.insert_block(block))
        return ids

    def get_blocks_by_ids(self, block_ids: List[int]) -> List[Block]:
        """根据ID列表获取文档块"""
        if not block_ids:
            return []
        if self._get_schema_mode() == "protobridge_dev":
            with self.connection() as conn:
                cursor = conn.cursor()
                normalized_ids = [int(block_id) for block_id in block_ids]
                placeholders = self._placeholders(len(normalized_ids))
                self._execute(
                    cursor,
                    f"""
                        SELECT b.*, s.project_id, s.file_name, s.document_id
                        FROM document_split_block b
                        JOIN document_split s ON s.id = b.document_split_id
                        WHERE b.id IN ({placeholders})
                          AND COALESCE(b.deleted, 0) = 0
                          AND COALESCE(s.deleted, 0) = 0
                        ORDER BY s.id DESC, b.id DESC
                    """,
                    normalized_ids,
                )
                rows = self._fetchall_dict(cursor)
                block_map: Dict[int, Block] = {}
                for row in rows:
                    block_key = int(row["id"])
                    if block_key not in block_map:
                        block_map[block_key] = self._row_to_block(row)
                return [block_map[block_id] for block_id in normalized_ids if block_id in block_map]
        with self.connection() as conn:
            cursor = conn.cursor()
            placeholders = self._placeholders(len(block_ids))
            self._execute(
                cursor,
                f"SELECT * FROM blocks WHERE block_id IN ({placeholders})",
                block_ids,
            )
            rows = self._fetchall_dict(cursor)
            return [self._row_to_block(row) for row in rows]

    def get_blocks_by_project(self, project_id: str) -> List[Block]:
        """根据项目ID获取所有文档块"""
        if self._get_schema_mode() == "protobridge_dev":
            with self.connection() as conn:
                cursor = conn.cursor()
                self._execute(
                    cursor,
                    """
                        SELECT b.*, s.project_id, s.file_name, s.document_id
                        FROM document_split_block b
                        JOIN document_split s ON s.id = b.document_split_id
                        WHERE s.project_id = %s
                          AND COALESCE(b.deleted, 0) = 0
                          AND COALESCE(s.deleted, 0) = 0
                        ORDER BY s.id, b.id
                    """,
                    (project_id,),
                )
                rows = self._fetchall_dict(cursor)
                return [self._row_to_block(row) for row in rows]
        with self.connection() as conn:
            cursor = conn.cursor()
            self._execute(
                cursor,
                "SELECT * FROM blocks WHERE project_id = %s ORDER BY block_id",
                (project_id,),
            )
            rows = self._fetchall_dict(cursor)
            return [self._row_to_block(row) for row in rows]

    def get_blocks_by_file_names(self, file_names: List[str], project_id: str = "") -> List[Block]:
        """根据文件名列表读取文档块，兼容 protobridge_dev。"""
        normalized_file_names = [str(item).strip() for item in file_names if str(item).strip()]
        if not normalized_file_names:
            return []

        if self._get_schema_mode() == "protobridge_dev":
            with self.connection() as conn:
                cursor = conn.cursor()
                file_placeholders = self._placeholders(len(normalized_file_names))
                params: List[Any] = list(normalized_file_names)
                project_filter = ""
                if str(project_id or "").strip():
                    project_filter = " AND project_id = %s"
                    params.append(str(project_id).strip())
                self._execute(
                    cursor,
                    f"""
                        SELECT b.*, s.project_id, s.file_name, s.document_id
                        FROM document_split_block b
                        JOIN document_split s ON s.id = b.document_split_id
                        JOIN (
                            SELECT document_id, MAX(id) AS latest_split_id
                            FROM document_split
                            WHERE file_name IN ({file_placeholders})
                              AND COALESCE(deleted, 0) = 0
                              {project_filter}
                            GROUP BY document_id
                        ) latest ON latest.latest_split_id = s.id
                        WHERE COALESCE(b.deleted, 0) = 0
                        ORDER BY s.project_id, s.file_name, b.page_num, b.id
                    """,
                    params,
                )
                rows = self._fetchall_dict(cursor)
                return [self._row_to_block(row) for row in rows]

        with self.connection() as conn:
            cursor = conn.cursor()
            params = list(normalized_file_names)
            where_clauses = [f"file_name IN ({self._placeholders(len(normalized_file_names))})"]
            if str(project_id or "").strip():
                where_clauses.append("project_id = %s")
                params.append(str(project_id).strip())
            self._execute(
                cursor,
                f"""
                    SELECT *
                    FROM blocks
                    WHERE {" AND ".join(where_clauses)}
                    ORDER BY project_id, file_name, page_num, block_id
                """,
                params,
            )
            rows = self._fetchall_dict(cursor)
            return [self._row_to_block(row) for row in rows]

    def update_block_content(self, block_id: int, cleaned_content: str):
        """更新文档块清洗后的内容"""
        if not self.write_enabled:
            return
        with self.connection() as conn:
            cursor = conn.cursor()
            self._execute(
                cursor,
                """
                    UPDATE blocks SET cleaned_content = %s, updated_at = NOW()
                    WHERE block_id = %s
                """,
                (cleaned_content, block_id),
            )

    def _row_to_block(self, row: Dict[str, Any]) -> Block:
        """数据库行转Block对象"""
        if "document_split_id" in row and "block_type" not in row:
            metadata = self._json_load(row.get("protocol_fields"), {})
            if not isinstance(metadata, dict):
                metadata = {"protocol_fields": metadata}
            row_id = int(row["id"])
            page_num = int(row.get("page_num") or 0)
            page_range = self._normalize_page_range(metadata.get("merged_pages"), page_num)
            if not page_range:
                end_page = int(metadata.get("end_page") or 0)
                if page_num > 0 and end_page > 0:
                    start_page, stop_page = sorted((page_num, end_page))
                    page_range = list(range(start_page, stop_page + 1))
                elif page_num > 0:
                    page_range = [page_num]
            metadata.setdefault("document_id", row.get("document_id"))
            metadata.setdefault("document_split_id", row.get("document_split_id"))
            metadata.setdefault("row_id", row_id)
            metadata.setdefault("legacy_block_id", row.get("block_id"))
            return Block(
                block_id=row_id,
                project_id=str(row.get("project_id") or ""),
                file_name=str(row.get("file_name") or ""),
                page_num=page_num,
                content=row.get("content") or "",
                block_type=str(row.get("type") or "text"),
                cleaned_content=None,
                page_range=page_range or None,
                metadata=metadata,
                created_at=row.get("create_time"),
                updated_at=row.get("update_time"),
            )
        metadata = self._json_load(row.get("metadata"), {})
        if not isinstance(metadata, dict):
            metadata = {}
        page_num = int(row.get("page_num") or 0)
        page_range = self._normalize_page_range(metadata.get("merged_pages"), page_num)
        if not page_range:
            end_page = int(metadata.get("end_page") or 0)
            if page_num > 0 and end_page > 0:
                start_page, stop_page = sorted((page_num, end_page))
                page_range = list(range(start_page, stop_page + 1))
            elif page_num > 0:
                page_range = [page_num]
        return Block(
            block_id=row["block_id"],
            project_id=row["project_id"],
            file_name=row["file_name"],
            page_num=page_num,
            content=row["content"],
            block_type=row["block_type"],
            cleaned_content=row["cleaned_content"],
            page_range=page_range or None,
            metadata=metadata,
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    # ==================== Chunk 操作 ====================

    def insert_chunk(self, chunk: Chunk) -> str:
        """插入语义块"""
        if not self.write_enabled:
            return chunk.chunk_id
        with self.connection() as conn:
            cursor = conn.cursor()
            self._execute(
                cursor,
                """
                    INSERT INTO chunks (chunk_id, project_id, dataset_id, source_block_ids, semantic_type, content_snapshot, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    chunk.chunk_id,
                    chunk.project_id,
                    chunk.dataset_id,
                    json.dumps(chunk.source_block_ids, ensure_ascii=False),
                    chunk.semantic_type,
                    chunk.content_snapshot,
                    json.dumps(chunk.metadata, ensure_ascii=False) if chunk.metadata else None,
                ),
            )
            return chunk.chunk_id

    def get_chunks_by_dataset(self, dataset_id: str) -> List[Chunk]:
        """根据数据集ID获取语义块"""
        with self.connection() as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT * FROM chunks WHERE dataset_id = %s", (dataset_id,))
            rows = self._fetchall_dict(cursor)
            return [self._row_to_chunk(row) for row in rows]

    def _row_to_chunk(self, row: Dict[str, Any]) -> Chunk:
        return Chunk(
            chunk_id=row["chunk_id"],
            project_id=row["project_id"],
            dataset_id=row["dataset_id"],
            source_block_ids=json.loads(row["source_block_ids"]) if row.get("source_block_ids") else [],
            semantic_type=row["semantic_type"],
            content_snapshot=row["content_snapshot"],
            metadata=json.loads(row["metadata"]) if row.get("metadata") else {},
            created_at=row.get("created_at"),
        )

    # ==================== QA 操作 ====================

    def insert_qa(self, qa: QAPair) -> str:
        """插入QA对"""
        if not self.write_enabled:
            return qa.qa_id
        values = (
            qa.qa_id,
            json.dumps(qa.source_block_ids, ensure_ascii=False),
            qa.question,
            qa.answer,
            qa.qa_task_type,
            qa.conversion_mode,
            qa.conversion_formula,
            qa.source_field,
            json.dumps(qa.source_fields, ensure_ascii=False) if qa.source_fields else None,
            qa.target_field,
            qa.concept_name,
            qa.formula_kind,
            qa.target_protocol_type,
            qa.target_message_code,
            qa.instruction,
            int(bool(qa.is_low_quality)),
            qa.quality_reason,
            json.dumps(qa.extracted_info, ensure_ascii=False) if qa.extracted_info else None,
            json.dumps(qa.validation_result, ensure_ascii=False) if qa.validation_result else None,
            qa.protocol_type,
        )
        with self.connection() as conn:
            cursor = conn.cursor()
            self._execute(
                cursor,
                f"""
                    INSERT INTO qa_pairs (qa_id, source_block_ids, question, answer,
                        qa_task_type, conversion_mode, conversion_formula,
                        source_field, source_fields, target_field, concept_name,
                        formula_kind, target_protocol_type, target_message_code,
                        instruction,
                        is_low_quality, quality_reason, extracted_info, validation_result, protocol_type)
                    VALUES ({self._placeholders(len(values))})
                """,
                values,
            )
            return qa.qa_id

    def get_qa_by_id(self, qa_id: str) -> Optional[QAPair]:
        """根据ID获取QA对"""
        with self.connection() as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT * FROM qa_pairs WHERE qa_id = %s", (qa_id,))
            row = self._fetchone_dict(cursor)
            return self._row_to_qa(row) if row else None

    def _row_to_qa(self, row: Dict[str, Any]) -> QAPair:
        return QAPair(
            qa_id=row["qa_id"],
            source_block_ids=json.loads(row["source_block_ids"]) if row.get("source_block_ids") else [],
            question=row["question"],
            answer=row["answer"],
            qa_task_type=row.get("qa_task_type") or "protocol_understanding",
            conversion_mode=row.get("conversion_mode"),
            conversion_formula=row.get("conversion_formula"),
            source_field=row.get("source_field"),
            source_fields=json.loads(row["source_fields"]) if row.get("source_fields") else [],
            target_field=row.get("target_field"),
            concept_name=row.get("concept_name"),
            formula_kind=row.get("formula_kind"),
            target_protocol_type=row.get("target_protocol_type"),
            target_message_code=row.get("target_message_code"),
            instruction=row.get("instruction") or "",
            is_low_quality=bool(row.get("is_low_quality")),
            quality_reason=row.get("quality_reason"),
            extracted_info=json.loads(row["extracted_info"]) if row.get("extracted_info") else None,
            validation_result=json.loads(row["validation_result"]) if row.get("validation_result") else None,
            protocol_type=row.get("protocol_type") or "Link16",
            created_at=row.get("created_at"),
        )

    # ==================== FinetuneJob 操作 ====================

    def insert_job(self, job: FinetuneJob) -> str:
        """插入微调任务"""
        if not self.write_enabled:
            return job.job_id
        with self.connection() as conn:
            cursor = conn.cursor()
            self._execute(
                cursor,
                """
                    INSERT INTO finetune_jobs (job_id, status, base_model, dataset_id, config, progress, last_checkpoint, model_path, metrics, error_message, started_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job.job_id,
                    job.status.value if isinstance(job.status, JobStatus) else job.status,
                    job.base_model,
                    job.dataset_id,
                    json.dumps(job.config, ensure_ascii=False) if job.config else None,
                    json.dumps(job.progress, ensure_ascii=False) if job.progress else None,
                    json.dumps(job.last_checkpoint, ensure_ascii=False) if job.last_checkpoint else None,
                    job.model_path,
                    json.dumps(job.metrics, ensure_ascii=False) if job.metrics else None,
                    job.error_message,
                    job.started_at,
                ),
            )
            return job.job_id

    def get_job(self, job_id: str) -> Optional[FinetuneJob]:
        """获取微调任务"""
        with self.connection() as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT * FROM finetune_jobs WHERE job_id = %s", (job_id,))
            row = self._fetchone_dict(cursor)
            return self._row_to_job(row) if row else None

    def update_job(self, job_id: str, **kwargs):
        """更新微调任务"""
        if not self.write_enabled:
            return
        allowed_fields = ["status", "progress", "last_checkpoint", "model_path", "metrics", "error_message", "started_at", "completed_at"]
        updates = []
        values = []
        for key, value in kwargs.items():
            if key in allowed_fields:
                if key in ["progress", "last_checkpoint", "metrics"]:
                    value = json.dumps(value, ensure_ascii=False) if value else None
                updates.append(f"{key} = %s")
                values.append(value)
        if not updates:
            return
        values.append(job_id)
        with self.connection() as conn:
            cursor = conn.cursor()
            self._execute(
                cursor,
                f"UPDATE finetune_jobs SET {', '.join(updates)} WHERE job_id = %s",
                values,
            )

    def get_latest_checkpoint(self, job_id: str) -> Optional[Dict[str, Any]]:
        """获取最新检查点"""
        job = self.get_job(job_id)
        return job.last_checkpoint if job else None

    def _row_to_job(self, row: Dict[str, Any]) -> FinetuneJob:
        status_value = row.get("status") or JobStatus.PENDING.value
        status = JobStatus(status_value) if status_value in JobStatus._value2member_map_ else JobStatus.PENDING
        return FinetuneJob(
            job_id=row["job_id"],
            status=status,
            base_model=row.get("base_model") or "",
            dataset_id=row.get("dataset_id") or "",
            config=json.loads(row["config"]) if row.get("config") else {},
            progress=json.loads(row["progress"]) if row.get("progress") else {},
            last_checkpoint=json.loads(row["last_checkpoint"]) if row.get("last_checkpoint") else None,
            model_path=row.get("model_path"),
            metrics=json.loads(row["metrics"]) if row.get("metrics") else {},
            error_message=row.get("error_message"),
            created_at=row.get("created_at"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
        )

    # ==================== PipelinePayload 操作 ====================

    def get_pipeline_payload(self, content_id: str) -> Dict[str, Any]:
        """根据外部内容ID读取中间内容载荷。"""
        resolved_content_id = str(content_id or "").strip()
        if not resolved_content_id:
            return {}
        if self._get_schema_mode() == "protobridge_dev":
            with self.connection() as conn:
                cursor = conn.cursor()
                if resolved_content_id.startswith("protobridge_clean:"):
                    try:
                        clean_id = int(resolved_content_id.split(":", 1)[1])
                    except (IndexError, TypeError, ValueError):
                        return {}
                    clean_row = self._get_document_clean_by_id(cursor, clean_id)
                    return self._build_protobridge_clean_payload(cursor, clean_row) if clean_row else {}
                if resolved_content_id.startswith("protobridge_document:"):
                    try:
                        document_id = int(resolved_content_id.split(":", 1)[1])
                    except (IndexError, TypeError, ValueError):
                        return {}
                    return self._build_protobridge_document_payload(cursor, document_id)
                if resolved_content_id.startswith("protobridge_dataset:"):
                    dataset_ref = resolved_content_id.split(":", 1)[1]
                    return self._build_protobridge_dataset_payload(cursor, dataset_ref, prefer_cleaned=False)
                if resolved_content_id.startswith("ds_"):
                    return self._build_protobridge_dataset_payload(cursor, resolved_content_id, prefer_cleaned=True)
                if resolved_content_id.isdigit():
                    document_payload = self._build_protobridge_document_payload(cursor, int(resolved_content_id))
                    if document_payload:
                        return document_payload
                    clean_row = self._get_document_clean_by_id(cursor, int(resolved_content_id))
                    if clean_row:
                        return self._build_protobridge_clean_payload(cursor, clean_row)
                    return self._build_protobridge_dataset_payload(cursor, resolved_content_id, prefer_cleaned=False)
                return self._build_protobridge_dataset_payload(cursor, resolved_content_id, prefer_cleaned=True)
        with self.connection() as conn:
            cursor = conn.cursor()
            self._execute(cursor, "SELECT * FROM pipeline_payloads WHERE content_id = %s", (resolved_content_id,))
            row = self._fetchone_dict(cursor)
            return self._row_to_pipeline_payload(row) if row else {}

    def get_latest_pipeline_payload_by_dataset(
        self,
        dataset_id: str,
        payload_types: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """根据数据集ID读取最新一条中间内容载荷。"""
        resolved_dataset_id = str(dataset_id or "").strip()
        if not resolved_dataset_id:
            return {}
        if self._get_schema_mode() == "protobridge_dev":
            normalized_types = [str(item).strip() for item in (payload_types or []) if str(item).strip()]
            prefer_cleaned = not normalized_types or "cleaned_blocks" in normalized_types
            payload = self._build_protobridge_dataset_payload_from_ref(resolved_dataset_id, prefer_cleaned)
            if not payload:
                return {}
            if normalized_types and str(payload.get("payload_type") or "").strip() not in normalized_types:
                if prefer_cleaned:
                    fallback_payload = self._build_protobridge_dataset_payload_from_ref(resolved_dataset_id, prefer_cleaned=False)
                    if fallback_payload and str(fallback_payload.get("payload_type") or "").strip() in normalized_types:
                        return fallback_payload
                return {}
            return payload

        normalized_types = [str(item).strip() for item in (payload_types or []) if str(item).strip()]
        query = "SELECT * FROM pipeline_payloads WHERE dataset_id = %s"
        params: List[Any] = [resolved_dataset_id]
        if normalized_types:
            query += f" AND payload_type IN ({self._placeholders(len(normalized_types))})"
            params.extend(normalized_types)
        query += " ORDER BY created_at DESC"

        with self.connection() as conn:
            cursor = conn.cursor()
            self._execute(cursor, query, params)
            rows = self._fetchall_dict(cursor)
            for row in rows:
                payload = self._row_to_pipeline_payload(row)
                if payload:
                    return payload
        return {}

    def _build_protobridge_dataset_payload_from_ref(self, dataset_ref: str, prefer_cleaned: bool) -> Dict[str, Any]:
        with self.connection() as conn:
            cursor = conn.cursor()
            return self._build_protobridge_dataset_payload(cursor, dataset_ref, prefer_cleaned=prefer_cleaned)

    def _row_to_pipeline_payload(self, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not row:
            return {}
        payload_json = row.get("payload_json")
        try:
            payload = json.loads(payload_json) if payload_json else None
        except (TypeError, json.JSONDecodeError):
            payload = payload_json
        return {
            "content_id": row.get("content_id"),
            "project_id": row.get("project_id"),
            "dataset_id": row.get("dataset_id"),
            "payload_type": row.get("payload_type"),
            "payload": payload,
            "file_path": row.get("file_path"),
            "file_name": row.get("file_name"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
