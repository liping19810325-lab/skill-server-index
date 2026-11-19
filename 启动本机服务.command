#!/bin/bash
# 漫流 AI 剪辑台 · 本机服务（macOS / Linux 双击启动）
# 只下载了本文件也能用：若缺少 core 脚本会从仓库自动补齐。
cd "$(dirname "$0")" || exit 1

echo "=========================================================="
echo "   漫流 AI 剪辑台 · 本机服务"
echo "   地址：http://127.0.0.1:8788  （只监听本机，不对外网开放）"
echo "   ★ 请保持此窗口开着，不要关闭；关闭即停止服务 ★"
echo "=========================================================="
echo ""

PY=""
if command -v python3 >/dev/null 2>&1; then PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then PY="$(command -v python)"; fi
if [ -z "$PY" ]; then
    echo "【错误】没有找到 Python 3。"
    echo "请先安装：https://www.python.org/downloads/"
    echo "（Mac 也可先装 https://brew.sh/ 再执行 brew install python）"
    echo ""
    echo "按回车键关闭此窗口..."; read -r _; exit 1
fi

# ---- 自动补齐核心脚本（用户往往只下载了这个启动器）----
CORE="manflows-local-service.py"
if [ ! -f "$CORE" ]; then
    echo "① 首次运行：正在自动下载核心脚本 $CORE ..."
    RAW="https://raw.githubusercontent.com/liping19810325-lab/skill-server-index/main/$CORE"
    ok=0
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 2 --max-time 60 "$RAW" -o "$CORE" && ok=1
        if [ "$ok" = 0 ]; then
            # 用户本机若开了 7890 代理（如 iKuuuVPN），自动带上
            curl -fsSL --retry 2 --max-time 60 -x http://127.0.0.1:7890 "$RAW" -o "$CORE" && ok=1
        fi
        if [ "$ok" = 0 ]; then
            # raw 直连不通时试 jsDelivr CDN
            curl -fsSL --retry 2 --max-time 60 "https://cdn.jsdelivr.net/gh/liping19810325-lab/skill-server-index@main/$CORE" -o "$CORE" && ok=1
        fi
    fi
    if [ "$ok" = 0 ] || [ ! -s "$CORE" ]; then
        rm -f "$CORE"
        echo "【错误】自动下载失败，请检查网络后重试；或到仓库手动下载 $CORE 放到本文件同一目录。"
        echo "仓库：https://github.com/liping19810325-lab/skill-server-index"
        echo "按回车键关闭此窗口..."; read -r _; exit 1
    fi
    echo "下载完成：$CORE（以后无需再下载）"
fi

exec "$PY" "$CORE"
