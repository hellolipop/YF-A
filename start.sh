#!/usr/bin/env bash
# AlphaDesk · A股/美股实时分析终端 —— 一键启动脚本
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8848}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未检测到 python3，请先安装 Python 3.8+（无需任何第三方依赖）"
  exit 1
fi

echo "启动 AlphaDesk 行情服务（端口 ${PORT}）…"
cd "$DIR"
python3 server.py --port "$PORT" &
SERVER_PID=$!

sleep 2
URL="http://127.0.0.1:${PORT}"
echo "服务地址: ${URL}"
echo "按 Ctrl+C 结束服务"

if command -v open >/dev/null 2>&1; then
  open "$URL" || true
fi

trap 'kill $SERVER_PID 2>/dev/null || true' INT TERM
wait $SERVER_PID
