@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo    漫流 AI 剪辑台 - 本机转写服务 (faster-whisper)
echo    地址: http://127.0.0.1:9000  (只监听本机)
echo    请保持此窗口开着, 关闭即停止转写服务
echo ==========================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到 Python, 请先安装: https://www.python.org/downloads/  (安装时勾选 Add to PATH)
  pause
  exit /b 1
)

set "VENV=%USERPROFILE%\.manflows\fw-venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo [1/3] 创建独立虚拟环境...
  python -m venv "%VENV%"
)

"%VENV%\Scripts\python.exe" -c "import faster_whisper" >nul 2>nul
if errorlevel 1 (
  echo [2/3] 安装 faster-whisper (首次约几分钟)...
  "%VENV%\Scripts\python.exe" -m pip install -q --upgrade pip setuptools wheel
  "%VENV%\Scripts\python.exe" -m pip install faster-whisper==1.2.0 av==14.2.0 requests
)

echo [3/3] 启动转写服务 (首次会自动下载 small 模型, 约 460MB)...
if not defined HF_ENDPOINT set "HF_ENDPOINT=https://hf-mirror.com"
"%VENV%\Scripts\python.exe" faster-whisper-server.py
pause
