#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/env.sh}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

DTK_ENV_FILE="${DTK_ENV_FILE:-}"
if [[ -n "$DTK_ENV_FILE" && -f "$DTK_ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$DTK_ENV_FILE"
fi

echo "操作系统:"
sed -n '1,12p' /etc/os-release
echo "架构: $(uname -m)"

if ! command -v hy-smi >/dev/null 2>&1; then
  echo "检查失败: 未找到 hy-smi" >&2
  exit 1
fi
hy-smi

CONDA_BIN="${CONDA_BIN:-/opt/anaconda3/condabin/conda}"
CONDA_ENV_NAME="${CONDA_ENV_NAME:-interface_projects}"
if [[ ! -x "$CONDA_BIN" ]]; then
  echo "检查失败: 未找到 conda: $CONDA_BIN" >&2
  exit 1
fi

"$CONDA_BIN" run -n "$CONDA_ENV_NAME" python - <<'PY'
import sys
import torch

try:
    import torch_dcu  # type: ignore[import-not-found]  # noqa: F401
    print(f"torch_dcu={getattr(torch_dcu, '__version__', 'loaded')}")
except ModuleNotFoundError:
    print("torch_dcu=not-separate-package")

print(f"torch={torch.__version__}")
print(f"torch.version.hip={getattr(torch.version, 'hip', None)}")
print(f"accelerator_available={torch.cuda.is_available()}")
print(f"device_count={torch.cuda.device_count()}")
if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
    sys.exit("检查失败: 厂商 PyTorch 未识别到海光 DCU")
print(f"device_0={torch.cuda.get_device_name(0)}")
PY

echo "海光 DCU 基础环境检查通过"
