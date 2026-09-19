#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
python_bin="${KERUI_PYTHON:-$root/.venv/bin/python}"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "macOS sidecar must be built natively on Apple Silicon" >&2
  exit 1
fi
if [[ ! -x "$python_bin" ]]; then
  echo "Python interpreter not found: $python_bin" >&2
  exit 1
fi

cd "$root"
"$python_bin" -m PyInstaller backend/packaging/kerui_recruit.spec --noconfirm

sidecar="$root/dist/kerui-recruit-sidecar"
if [[ ! -x "$sidecar" ]]; then
  echo "PyInstaller sidecar was not created" >&2
  exit 1
fi

bundle_dir="$root/desktop/src-tauri/binaries"
mkdir -p "$bundle_dir"
cp "$sidecar" "$bundle_dir/kerui-recruit-sidecar"
chmod 755 "$bundle_dir/kerui-recruit-sidecar"
file "$bundle_dir/kerui-recruit-sidecar"
