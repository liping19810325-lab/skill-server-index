#!/bin/bash
# 漫流 AI 剪辑台 · 一键启动全部本机服务（Mac）
# 一个窗口同时跑：8788 主服务（导出/链接/AI剪辑/转发转写） + 9000 faster-whisper（AI字幕本机转写）
# 关闭本窗口或按 Ctrl+C，两个服务会一起停止。只下载本文件也能用：会自动补齐两个核心脚本。
cd "$(dirname "$0")" || exit 1

echo "=========================================================="
echo "   漫流 AI 剪辑台 · 一键启动全部本机服务"
echo "   8788 主服务 + 9000 转写服务  （只监听本机，不对外开放）"
echo "   ★ 请保持此窗口开着；关闭窗口或按 Ctrl+C 即同时停止两个服务 ★"
echo "=========================================================="
echo ""

# 找 Python3
PY=""
if command -v python3 >/dev/null 2>&1; then PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then PY="$(command -v python)"; fi
if [ -z "$PY" ]; then
  echo "【错误】没有找到 Python 3，请先安装：https://www.python.org/downloads/"
  echo "按回车键关闭..."; read -r _; exit 1
fi

# 代理（本机开了 7890 如 iKuuuVPN 则自动带上）
PROXY_HOST="127.0.0.1:7890"
if curl -s --max-time 2 -x "http://$PROXY_HOST" -o /dev/null https://pypi.org/simple/ 2>/dev/null; then
  export HTTPS_PROXY="http://$PROXY_HOST" HTTP_PROXY="http://$PROXY_HOST"
  echo "检测到本机代理 $PROXY_HOST，下载时自动使用。"
fi

# 通用补下载核心脚本（主服务零依赖；转写脚本首次可能需几分钟）
fetch_core(){
  local name="$1"
  local ok=0
  [ -s "$name" ] && return 0
  local raw="https://raw.githubusercontent.com/liping19810325-lab/skill-server-index/main/${name}"
  local cdn="https://cdn.jsdelivr.net/gh/liping19810325-lab/skill-server-index@main/${name}"
  echo "首次运行：正在下载核心脚本 $name ..."
  curl -fsSL --retry 2 --max-time 90 "$raw" -o "$name" && ok=1
  if [ "$ok" = 0 ]; then curl -fsSL --retry 2 --max-time 90 -x "http://$PROXY_HOST" "$raw" -o "$name" && ok=1; fi
  if [ "$ok" = 0 ]; then curl -fsSL --retry 2 --max-time 90 "$cdn" -o "$name" && ok=1; fi
  if [ "$ok" = 0 ] || [ ! -s "$name" ]; then rm -f "$name"; echo "【错误】下载 $name 失败，请检查网络后重试。"; return 1; fi
  echo "下载完成：$name"
}

MAIN="manflows-local-service.py"
FW="faster-whisper-server.py"
fetch_core "$MAIN" || { echo "按回车键关闭..."; read -r _; exit 1; }
fetch_core "$FW"   || { echo "按回车键关闭..."; read -r _; exit 1; }

# 探测服务是否已运行（8788 用 /api/ping；9000 OpenAI 兼容服务用 /v1/models）
ping_ok(){ curl -fsS --max-time 2 "http://127.0.0.1:$1/api/ping" 2>/dev/null | grep -q '"ok"'; }
fw_ok(){   curl -fsS --max-time 2 "http://127.0.0.1:9000/v1/models" 2>/dev/null | grep -q '"data"'; }

MAIN_PID=""
FW_PID=""

if ping_ok 8788; then
  echo "✅ 8788 主服务已在运行，跳过（复用现有窗口）。"
else
  echo "▶ 启动 8788 主服务..."
  "$PY" "$MAIN" &
  MAIN_PID=$!
fi

if fw_ok; then
  echo "✅ 9000 转写服务已在运行，跳过（复用现有窗口）。"
else
  VENV="$HOME/.manflows/fw-venv"
  if [ ! -x "$VENV/bin/python" ]; then
    echo "▶ 创建转写虚拟环境 $VENV ..."
    "$PY" -m venv "$VENV" || { echo "创建虚拟环境失败"; echo "按回车键关闭..."; read -r _; exit 1; }
  fi
  if ! "$VENV/bin/python" -c "import faster_whisper" >/dev/null 2>&1; then
    echo "▶ 安装 faster-whisper（首次约几分钟）..."
    "$VENV/bin/python" -m pip install -q --upgrade pip setuptools wheel
    "$VENV/bin/python" -m pip install "faster-whisper==1.2.0" "av==14.2.0" requests || { echo "安装失败，请检查网络/代理"; echo "按回车键关闭..."; read -r _; exit 1; }
  fi
  if [ -z "$HF_ENDPOINT" ]; then
    if ! curl -s --max-time 3 -o /dev/null https://huggingface.co 2>/dev/null; then
      export HF_ENDPOINT="https://hf-mirror.com"
      echo "huggingface 直连不通，自动改用镜像 $HF_ENDPOINT 下载模型。"
    fi
  fi
  echo "▶ 启动 9000 转写服务（首次会自动下载 small 模型约 460MB，请耐心等待）..."
  "$VENV/bin/python" "$FW" &
  FW_PID=$!
fi

if [ -z "$MAIN_PID" ] && [ -z "$FW_PID" ]; then
  echo ""
  echo "=========================================================="
  echo "  ✅ 两个服务都已在运行，无需重复启动！"
  echo "  8788 主服务：http://127.0.0.1:8788"
  echo "  9000 转写服务：http://127.0.0.1:9000"
  echo "  请直接回到剪辑台使用，功能一切正常。"
  echo "=========================================================="
  echo "按回车键关闭此窗口..."; read -r _; exit 0
fi

echo ""
echo "=========================================================="
echo "  两个服务已就绪，回到剪辑台即可使用。"
echo "  关闭本窗口或按 Ctrl+C 会同时停止本次启动的服务。"
echo "=========================================================="

cleanup(){
  echo ""
  echo "正在停止本次启动的本机服务..."
  [ -n "$MAIN_PID" ] && kill "$MAIN_PID" 2>/dev/null
  [ -n "$FW_PID" ]   && kill "$FW_PID" 2>/dev/null
  exit 0
}
trap cleanup EXIT INT TERM
wait
