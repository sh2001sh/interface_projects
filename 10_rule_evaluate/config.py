"""Shared config values derived from local config.yaml."""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime_config import get_shared_project_config


_CONFIG = get_shared_project_config(PROJECT_ROOT)

PROJECT_ROOT = _CONFIG["PROJECT_ROOT"]
PROJECT_NAME = _CONFIG["PROJECT_NAME"]
MODEL_CACHE_DIR = _CONFIG["MODEL_CACHE_DIR"]
EMBED_MODEL_NAME = _CONFIG["EMBED_MODEL_NAME"]
EMBED_MODEL_DIR = _CONFIG.get("EMBED_MODEL_DIR", "")
RERANK_MODEL_NAME = _CONFIG["RERANK_MODEL_NAME"]
RERANK_MODEL_DIR = _CONFIG.get("RERANK_MODEL_DIR", "")
LLM_MODEL_NAME = _CONFIG["LLM_MODEL_NAME"]
USE_VLLM = _CONFIG["USE_VLLM"]
VLLM_URL = _CONFIG["VLLM_URL"]
MYSQL_HOST = _CONFIG["MYSQL_HOST"]
MYSQL_PORT = _CONFIG["MYSQL_PORT"]
MYSQL_USER = _CONFIG["MYSQL_USER"]
MYSQL_PASSWORD = _CONFIG["MYSQL_PASSWORD"]
MYSQL_DATABASE = _CONFIG["MYSQL_DATABASE"]
MILVUS_HOST = _CONFIG["MILVUS_HOST"]
MILVUS_PORT = _CONFIG["MILVUS_PORT"]
MILVUS_DB = _CONFIG["MILVUS_DB"]
DATA_DIR = _CONFIG["DATA_DIR"]
SERVICE_PORTS = _CONFIG["SERVICE_PORTS"]
API_ENDPOINTS = _CONFIG["API_ENDPOINTS"]
TRAINING_CONFIG = _CONFIG["TRAINING_CONFIG"]

os.makedirs(DATA_DIR, exist_ok=True)
for key in ["checkpoint_dir", "output_dir"]:
    os.makedirs(TRAINING_CONFIG[key], exist_ok=True)
