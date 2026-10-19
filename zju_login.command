#!/bin/bash
# 只重设认证；保留资料库和追踪课程配置。
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
uv run --quiet --script zju.py login --reset "$@"
echo
read -r -n 1 -s -p "按任意键关闭视窗…"
echo
