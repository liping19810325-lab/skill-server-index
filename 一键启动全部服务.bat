@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo   漫流 AI 剪辑台 - 一键启动全部本机服务 (Windows)
echo   8788 主服务 + 9000 转写服务  （只监听本机，不对外开放）
echo   本窗口会自动拉起两个服务窗口；两个服务窗口都别关。
echo ==========================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到 Python, 请先安装: https://www.python.org/downloads/  (安装时勾选 Add to PATH)
  pause
  exit /b 1
)

rem 自动补齐核心脚本：raw 失败回退 jsDelivr
if not exist "manflows-local-service.py" (
  echo 首次运行：正在下载 manflows-local-service.py ...
  powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $u='https://raw.githubusercontent.com/liping19810325-lab/skill-server-index/main/manflows-local-service.py'; try { Invoke-WebRequest -Uri $u -OutFile 'manflows-local-service.py' -TimeoutSec 90 } catch { Invoke-WebRequest -Uri 'https://cdn.jsdelivr.net/gh/liping19810325-lab/skill-server-index@main/manflows-local-service.py' -OutFile 'manflows-local-service.py' -TimeoutSec 90 }"
)
if not exist "faster-whisper-server.py" (
  echo 首次运行：正在下载 faster-whisper-server.py ...
  powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $u='https://raw.githubusercontent.com/liping19810325-lab/skill-server-index/main/faster-whisper-server.py'; try { Invoke-WebRequest -Uri $u -OutFile 'faster-whisper-server.py' -TimeoutSec 90 } catch { Invoke-WebRequest -Uri 'https://cdn.jsdelivr.net/gh/liping19810325-lab/skill-server-index@main/faster-whisper-server.py' -OutFile 'faster-whisper-server.py' -TimeoutSec 90 }"
)
rem 自动补齐两个单独启动器(整合器用它们分别拉起服务，且方便用户日后单独控制)
powershell -NoProfile -Command "$ErrorActionPreference='SilentlyContinue'; $base='https://raw.githubusercontent.com/liping19810325-lab/skill-server-index/main/'; $cdn='https://cdn.jsdelivr.net/gh/liping19810325-lab/skill-server-index@main/'; foreach($f in @('启动本机服务.bat','启动本机转写服务.bat')){ if(-not (Test-Path $f)){ try{ Invoke-WebRequest -Uri ($base+[uri]::EscapeDataString($f)) -OutFile $f -TimeoutSec 60 }catch{ Invoke-WebRequest -Uri ($cdn+[uri]::EscapeDataString($f)) -OutFile $f -TimeoutSec 60 } } }"

rem 探测 8788 主服务
set "SRV="
for /f "delims=" %%i in ('powershell -NoProfile -Command "try{$r=Invoke-RestMethod -Uri 'http://127.0.0.1:8788/api/ping' -TimeoutSec 2; if($r.ok){'RUNNING'}}catch{}" 2^>nul') do set "SRV=%%i"
if "%SRV%"=="RUNNING" (
  echo [跳过] 8788 主服务已在运行。
) else (
  echo [启动] 8788 主服务 ^(新窗口^)...
  start "漫流-本机服务8788" cmd /c ""%~dp0启动本机服务.bat""
  echo   提示：若上面这行未能启动，请手动双击同目录下的「启动本机服务.bat」。
)

rem 探测 9000 转写服务
set "FW="
for /f "delims=" %%i in ('powershell -NoProfile -Command "try{$r=Invoke-RestMethod -Uri 'http://127.0.0.1:9000/v1/models' -TimeoutSec 2; if($r.data){'RUNNING'}}catch{}" 2^>nul') do set "FW=%%i"
if "%FW%"=="RUNNING" (
  echo [跳过] 9000 转写服务已在运行。
) else (
  echo [启动] 9000 转写服务 ^(新窗口，首次会装依赖并下模型^)...
  start "漫流-转写服务9000" cmd /c ""%~dp0启动本机转写服务.bat""
  echo   提示：若上面这行未能启动，请手动双击同目录下的「启动本机转写服务.bat」。
)

echo.
echo ==========================================================
echo   已处理完毕。两个服务窗口保持开着，即可回剪辑台使用。
echo   关闭某个服务窗口 = 停止对应服务；本窗口可关闭。
echo ==========================================================
echo.
pause
