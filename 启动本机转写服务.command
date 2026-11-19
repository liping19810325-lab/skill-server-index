#!/bin/bash
# 漫流 AI 剪辑台 · 本机 faster-whisper 转写服务（Mac 双击启动，一键安装依赖）
# 监听 127.0.0.1:9000，只对本机开放。首次运行会创建虚拟环境、安装 faster-whisper 并下载 small 模型。

cd "$(dirname "$0")" || exit 1
echo "=========================================================="
echo "   漫流 AI 剪辑台 · 本机转写服务 (faster-whisper)"
echo "   地址：http://127.0.0.1:9000  （只监听本机）"
echo "   ★ 请保持此窗口开着，关闭即停止转写服务 ★"
echo "=========================================================="
echo ""

# 找 Python3
PY=""
if command -v python3 >/dev/null 2>&1; then PY="$(command -v python3)"; fi

if [ -z "$PY" ]; then
  echo "【错误】没有找到 Python 3，请先安装：https://www.python.org/downloads/"
  echo "按回车键关闭..."; read -r _; exit 1
fi

# 代理（如本机用了 7890 代理则自动带上）
PROXY_HOST="127.0.0.1:7890"
if curl -s --max-time 2 -x "http://$PROXY_HOST" -o /dev/null https://pypi.org/simple/ 2>/dev/null; then
  export HTTPS_PROXY="http://$PROXY_HOST" HTTP_PROXY="http://$PROXY_HOST"
  echo "检测到本机代理 $PROXY_HOST，安装时自动使用。"
fi

# 自动补齐转写服务脚本（用户往往只下载了这个启动器）
CORE="faster-whisper-server.py"
if [ ! -f "$CORE" ]; then
  echo "① 首次运行：正在自动下载核心脚本 $CORE ..."
  RAW="https://raw.githubusercontent.com/liping19810325-lab/skill-server-index/main/$CORE"
  ok=0
  curl -fsSL --retry 2 --max-time 60 "$RAW" -o "$CORE" && ok=1
  if [ "$ok" = 0 ]; then curl -fsSL --retry 2 --max-time 60 "https://cdn.jsdelivr.net/gh/liping19810325-lab/skill-server-index@main/$CORE" -o "$CORE" && ok=1; fi
  if [ "$ok" = 0 ] || [ ! -s "$CORE" ]; then
    rm -f "$CORE"
    echo "【错误】自动下载失败，请检查网络后重试；或到仓库手动下载 $CORE 放到本文件同一目录。"
    echo "仓库：https://github.com/liping19810325-lab/skill-server-index"
    echo "按回车键关闭..."; read -r _; exit 1
  fi
  echo "下载完成：$CORE"
fi

VENV="$HOME/.manflows/fw-venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "② 创建独立虚拟环境 $VENV ..."
  "$PY" -m venv "$VENV" || { echo "创建虚拟环境失败"; read -r _; exit 1; }
fi

if ! "$VENV/bin/python" -c "import faster_whisper" >/dev/null 2>&1; then
  echo "③ 安装 faster-whisper（首次约几分钟）..."
  "$VENV/bin/python" -m pip install -q --upgrade pip setuptools wheel
  "$VENV/bin/python" -m pip install "faster-whisper==1.2.0" "av==14.2.0" requests || { echo "安装失败，请检查网络/代理"; read -r _; exit 1; }
fi

echo "④ 启动转写服务（首次会自动下载 small 模型，约 460MB，请耐心等待）..."
# 国内直连 huggingface 通常不通，优先用镜像（只在没设 HF_ENDPOINT 且直连不通时启用）
if [ -z "$HF_ENDPOINT" ]; then
  if ! curl -s --max-time 3 -o /dev/null https://huggingface.co 2>/dev/null; then
    export HF_ENDPOINT="https://hf-mirror.com"
    echo "检测到 huggingface 直连不通，自动改用镜像 $HF_ENDPOINT 下载模型。"
  fi
fi
exec "$VENV/bin/python" faster-whisper-server.py
