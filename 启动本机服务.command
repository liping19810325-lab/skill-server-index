#!/bin/bash
# 漫流 AI 剪辑台 · 本机服务（macOS / Linux 双击启动）
cd "$(dirname "$0")" || exit 1

echo "=========================================================="
echo "   漫流 AI 剪辑台 · 本机服务"
echo "   地址：http://127.0.0.1:8788  （只监听本机，不对外网开放）"
echo "   ★ 请保持此窗口开着，不要关闭；关闭即停止服务 ★"
echo "=========================================================="
echo ""

if command -v python3 >/dev/null 2>&1; then
    exec python3 manflows-local-service.py
elif command -v python >/dev/null 2>&1; then
    exec python manflows-local-service.py
else
    echo "【错误】没有找到 Python 3。"
    echo "请先安装：https://www.python.org/downloads/"
    echo "（Mac 也可先装 https://brew.sh/ 再执行 brew install python）"
    echo ""
    echo "按回车键关闭此窗口..."
    read -r _
fi
