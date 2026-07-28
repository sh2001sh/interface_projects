from __future__ import annotations

import json
import os
from pathlib import Path

from modelscope import snapshot_download


def _ensure_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        try:
            if link.resolve() == target.resolve():
                return
        except Exception:
            pass
        if link.is_symlink():
            link.unlink()
        else:
            backup = link.with_name(f"{link.name}.backup")
            if backup.exists() or backup.is_symlink():
                if backup.is_symlink():
                    backup.unlink()
            link.rename(backup)
    link.symlink_to(target, target_is_directory=True)


def main() -> int:
    model_cache_dir = Path(os.path.expanduser(os.getenv("MODEL_CACHE_DIR", "~/model_cache")))
    modelscope_cache_dir = Path(os.path.expanduser(os.getenv("MODELSCOPE_CACHE_DIR", str(model_cache_dir / "modelscope"))))
    llm_repo = os.getenv("LLM_MODEL_REPO", "Qwen/Qwen3-4B")
    embed_repo = os.getenv("EMBED_MODEL_REPO", "Qwen/Qwen3-Embedding-0.6B")
    rerank_repo = os.getenv("RERANK_MODEL_REPO", "Qwen/Qwen3-Reranker-0.6B")

    llm_dir = Path(os.path.expanduser(os.getenv("LLM_MODEL_DIR", str(model_cache_dir / "Qwen" / "Qwen3-4B"))))
    embed_dir = Path(os.path.expanduser(os.getenv("EMBED_MODEL_DIR", str(model_cache_dir / "Qwen" / "Qwen3-Embedding-0.6B"))))
    rerank_dir = Path(os.path.expanduser(os.getenv("RERANK_MODEL_DIR", str(model_cache_dir / "Qwen" / "Qwen3-Reranker-0.6B"))))

    modelscope_cache_dir.mkdir(parents=True, exist_ok=True)

    downloads = {
      "llm": {"repo": llm_repo, "target": llm_dir},
      "embedding": {"repo": embed_repo, "target": embed_dir},
      "reranker": {"repo": rerank_repo, "target": rerank_dir},
    }
    manifest = {}

    for key, meta in downloads.items():
        local_path = Path(snapshot_download(meta["repo"], cache_dir=str(modelscope_cache_dir)))
        _ensure_symlink(local_path, meta["target"])
        manifest[key] = {"repo": meta["repo"], "downloaded_to": str(local_path), "link_path": str(meta["target"])}
        print(f"{key}: {meta['repo']} -> {local_path}")

    manifest_path = model_cache_dir / "model_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
