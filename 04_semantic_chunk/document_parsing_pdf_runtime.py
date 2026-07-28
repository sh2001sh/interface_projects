from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)

DOCLING_ARTIFACTS_ROOT = Path(__file__).resolve().parent / "runtime" / "docling_artifacts"
DOCLING_LAYOUT_REPO_FOLDER = "docling-project--docling-layout-heron"
DOCLING_TABLE_REPO_FOLDER = "docling-project--docling-models"
DOCLING_MIRROR_ENDPOINT = os.environ.get("HF_ENDPOINT") or os.environ.get("HUGGINGFACE_ENDPOINT") or "https://hf-mirror.com"
DOCLING_DEVICE = str(os.environ.get("DOCLING_DEVICE") or "cpu").strip().lower() or "cpu"

_PDF_CONVERTER: Any = None


def _layout_model_ready(root: Path) -> bool:
    layout_dir = root / DOCLING_LAYOUT_REPO_FOLDER
    required_files = [
        layout_dir / "config.json",
        layout_dir / "preprocessor_config.json",
        layout_dir / "model.safetensors",
    ]
    return all(path.exists() for path in required_files)


def _table_model_ready(root: Path) -> bool:
    accurate_dir = root / DOCLING_TABLE_REPO_FOLDER / "model_artifacts" / "tableformer" / "accurate"
    required_files = [
        accurate_dir / "tm_config.json",
        accurate_dir / "tableformer_accurate.safetensors",
    ]
    return all(path.exists() for path in required_files)


def _download_docling_repo(repo_id: str, target_dir: Path, revision: Optional[str] = None) -> None:
    from huggingface_hub import snapshot_download

    target_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "downloading docling artifacts: repo_id=%s target_dir=%s endpoint=%s",
        repo_id,
        target_dir,
        DOCLING_MIRROR_ENDPOINT,
    )
    snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=str(target_dir),
        endpoint=DOCLING_MIRROR_ENDPOINT,
        max_workers=1,
    )


def ensure_docling_artifacts_root() -> Path:
    root = DOCLING_ARTIFACTS_ROOT
    root.mkdir(parents=True, exist_ok=True)

    if not _layout_model_ready(root):
        _download_docling_repo(
            repo_id="docling-project/docling-layout-heron",
            target_dir=root / DOCLING_LAYOUT_REPO_FOLDER,
            revision="main",
        )
    if not _table_model_ready(root):
        _download_docling_repo(
            repo_id="docling-project/docling-models",
            target_dir=root / DOCLING_TABLE_REPO_FOLDER,
            revision="v2.3.0",
        )

    if not _layout_model_ready(root):
        raise RuntimeError(f"docling 布局模型未准备完成: {root / DOCLING_LAYOUT_REPO_FOLDER}")
    if not _table_model_ready(root):
        raise RuntimeError(f"docling 表格模型未准备完成: {root / DOCLING_TABLE_REPO_FOLDER}")
    return root


def get_pdf_converter() -> Any:
    global _PDF_CONVERTER
    if _PDF_CONVERTER is None:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import AcceleratorOptions, PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        pipeline_options = PdfPipelineOptions(
            do_ocr=False,
            do_table_structure=True,
            artifacts_path=ensure_docling_artifacts_root(),
            accelerator_options=AcceleratorOptions(device=DOCLING_DEVICE),
        )
        _PDF_CONVERTER = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            }
        )
    return _PDF_CONVERTER


def convert_pdf_document(file_path: str) -> Any:
    converter = get_pdf_converter()
    result = converter.convert(Path(file_path))
    return result.document
