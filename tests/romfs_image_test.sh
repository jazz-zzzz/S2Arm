#!/bin/bash
# 门禁只看：pack/romfs_image.py tests/romfs_image_test.py
# RomFS 单文件镜像生成：布局、覆盖层分层、索引对不上报红、未变沿用。见 tests/romfs_image_test.py
set -eu
R=$(cd "$(dirname "$0")/.." && pwd)
PY=${S2A_PYTHON:-${S2A_PY:-python3}}
cd "$R" && PYTHONUTF8=1 "$PY" tests/romfs_image_test.py
echo "✓ RomFS 单文件镜像单测全过"
