@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo    漫流 AI 剪辑台 - 本机服务
echo    地址：http://127.0.0.1:8788  （只监听本机，不对外网开放）
echo    请保持此窗口开着，不要关闭；关闭即停止服务
echo ==========================================================
echo.

set "CORE=manflows-local-service.py"

rem ---- 自动补齐核心脚本（用户往往只下载了这个启动器）----
if not exist "%CORE%" (
    echo 首次运行：正在自动下载核心脚本 %CORE% ...
    powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $u='https://raw.githubusercontent.com/liping19810325-lab/skill-server-index/main/%CORE%'; try { Invoke-WebRequest -Uri $u -OutFile '%CORE%' -TimeoutSec 60 } catch { try { Invoke-WebRequest -Uri 'https://cdn.jsdelivr.net/gh/liping19810325-lab/skill-server-index@main/%CORE%' -OutFile '%CORE%' -TimeoutSec 60 } catch { exit 1 } }"
    if not exist "%CORE%" (
        echo 【错误】自动下载失败，请检查网络后重试；或到仓库手动下载 %CORE% 放到本文件同一目录。
        echo 仓库：https://github.com/liping19810325-lab/skill-server-index
        echo.
        pause
        exit /b 1
    )
    echo 下载完成：%CORE%（以后无需再下载）
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 %CORE%
    goto end
)
where python >nul 2>nul
if %errorlevel%==0 (
    python %CORE%
    goto end
)
echo 【错误】没有找到 Python 3，请先安装：https://www.python.org/downloads/ （安装时勾选 Add Python to PATH）
:end
echo.
pause
