@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo    漫流 AI 剪辑台 - 本机服务
echo    地址：http://127.0.0.1:8788  （只监听本机，不对外网开放）
echo    请保持此窗口开着，不要关闭；关闭即停止服务
echo ==========================================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 manflows-local-service.py
    goto end
)
where python >nul 2>nul
if %errorlevel%==0 (
    python manflows-local-service.py
    goto end
)
echo 【错误】没有找到 Python 3，请先安装：https://www.python.org/downloads/
:end
echo.
pause
