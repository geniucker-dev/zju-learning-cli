#!/bin/bash
# 只重设认证；保留资料库和追踪课程配置。
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "ZJU LearnPilot requires uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 127
fi
uv run --quiet --script zju.py login --reset "$@"
echo
read -r -n 1 -s -p "按任意键关闭视窗…"
echo
