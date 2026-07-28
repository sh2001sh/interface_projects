from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    root_dir = Path(__file__).resolve().parents[1]
    # The semantic-chunk client owns the superset of the project's local
    # compatibility tables. Protobridge business tables remain externally owned.
    project_dir = root_dir / "04_semantic_chunk"
    if str(project_dir) not in sys.path:
        sys.path.insert(0, str(project_dir))

    from database.mysql_client import MySQLClient

    client = MySQLClient()
    # Runtime interfaces are output-only, but this explicit administrator
    # command must be allowed to create the empty compatibility schema.
    client.write_enabled = True
    client.init_tables()
    print("MySQL 表结构初始化完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
