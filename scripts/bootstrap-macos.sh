#!/usr/bin/env bash

set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3。请先安装 Python 3，再重新运行此脚本。" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已创建 .env。请填写飞书与 DeepSeek 配置后再启动服务。"
else
  echo ".env 已存在，保留当前配置。"
fi

if ! command -v lark-cli >/dev/null 2>&1; then
  echo "提示：未找到 lark-cli。浏览器快速收集可启动，但飞书同步与消息提醒需要安装并登录 lark-cli。"
fi

cat <<'EOF'

下一步：
1. 编辑 .env，填写飞书多维表、收件人和 DeepSeek API Key。
2. 确认本机 lark-cli 已登录并有目标多维表权限。
3. 运行 ./scripts/run-local.sh。
4. 打开 http://127.0.0.1:8787/quick-capture，开始收集待办。
EOF
