#!/usr/bin/env bash

set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"

if [ ! -x .venv/bin/uvicorn ]; then
  echo "依赖尚未安装。请先运行 ./scripts/bootstrap-macos.sh。" >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo "缺少 .env。请先运行 ./scripts/bootstrap-macos.sh 并完成配置。" >&2
  exit 1
fi

echo "Workless 已启动： http://127.0.0.1:8787/quick-capture"
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8787
