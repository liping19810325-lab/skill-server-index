@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 漫流 MANFLOWS - 本机启停桥接服务

where python >nul 2>nul
if not errorlevel 1 goto RUN_PY
where py >nul 2>nul
if not errorlevel 1 goto RUN_PYLAUNCHER
echo [错误] 没找到 Python，请安装 Python 3.8 以上版本并勾选 Add Python to PATH
goto END

:RUN_PY
python manflows-bridge.py
goto END

:RUN_PYLAUNCHER
py manflows-bridge.py
goto END

:END
echo.
echo 桥接服务已退出。按任意键关闭窗口。
pause >nul
