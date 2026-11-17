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

VENV="$HOME/.manflows/fw-venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "① 创建独立虚拟环境 $VENV ..."
  "$PY" -m venv "$VENV" || { echo "创建虚拟环境失败"; read -r _; exit 1; }
fi

if ! "$VENV/bin/python" -c "import faster_whisper" >/dev/null 2>&1; then
  echo "② 安装 faster-whisper（首次约几分钟）..."
  "$VENV/bin/python" -m pip install -q --upgrade pip setuptools wheel
  "$VENV/bin/python" -m pip install "faster-whisper==1.2.0" "av==14.2.0" requests || { echo "安装失败，请检查网络/代理"; read -r _; exit 1; }
fi

echo "③ 启动转写服务（首次会自动下载 small 模型，约 460MB，请耐心等待）..."
# 国内直连 huggingface 通常不通，优先用镜像（只在没设 HF_ENDPOINT 且直连不通时启用）
if [ -z "$HF_ENDPOINT" ]; then
  if ! curl -s --max-time 3 -o /dev/null https://huggingface.co 2>/dev/null; then
    export HF_ENDPOINT="https://hf-mirror.com"
    echo "检测到 huggingface 直连不通，自动改用镜像 $HF_ENDPOINT 下载模型。"
  fi
fi
exec "$VENV/bin/python" faster-whisper-server.py
