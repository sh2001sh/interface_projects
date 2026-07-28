from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
VENDORED_DOCLING_PATH = PROJECT_ROOT / "vendor" / "docling"
DEFAULT_ARTIFACTS_PATH = PROJECT_ROOT / "runtime" / "docling-artifacts"


def _ensure_sys_path() -> None:
    if str(VENDORED_DOCLING_PATH) not in sys.path:
        sys.path.insert(0, str(VENDORED_DOCLING_PATH))


def prepare_minimal_pdf_artifacts(output_dir: Path, *, force: bool = False, progress: bool = True) -> list[Path]:
    _ensure_sys_path()
    from docling.datamodel.layout_model_specs import DOCLING_LAYOUT_V2
    from docling.models.stages.layout.layout_model import LayoutModel
    from docling.models.stages.table_structure.table_structure_model import TableStructureModel

    output_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    downloaded.append(
        LayoutModel.download_models(
            local_dir=output_dir / DOCLING_LAYOUT_V2.model_repo_folder,
            force=force,
            progress=progress,
            layout_model_config=DOCLING_LAYOUT_V2,
        )
    )
    downloaded.append(
        TableStructureModel.download_models(
            local_dir=output_dir / TableStructureModel._model_repo_folder,
            force=force,
            progress=progress,
        )
    )
    return downloaded


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare local Docling artifacts for the PDF pipeline.")
    parser.add_argument("--output-dir", default=str(DEFAULT_ARTIFACTS_PATH))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    os.environ["DOCLING_ARTIFACTS_PATH"] = str(output_dir)

    try:
        downloaded = prepare_minimal_pdf_artifacts(output_dir, force=args.force, progress=not args.quiet)
    except Exception as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"DOCLING_ARTIFACTS_PATH={output_dir}")
    for path in downloaded:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
