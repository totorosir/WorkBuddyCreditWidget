@echo off
REM =====================================================================
REM  WorkBuddyCreditWidget - Windows 打包脚本（PyInstaller onefile）
REM  用法：在 crossplatform\ 目录下执行 build_win.bat
REM  产物：dist\WorkBuddyCreditWidget.exe
REM  说明：与既有 output\WorkBuddyCreditWidget.exe 同源码基线；此脚本使用
REM         platform_adapter 跨平台版主程序（Windows 后端行为与 v2.1.0 一致）。
REM  建议：先安装依赖（可选）：python -m pip install pyinstaller
REM =====================================================================
chcp 65001 >nul
cd /d "%~dp0"

where pyinstaller >nul 2>nul
if errorlevel 1 (
    echo [提示] 未检测到 pyinstaller，尝试通过模块启动...
    python -m PyInstaller --version >nul 2>nul
    if errorlevel 1 (
        echo [错误] 未安装 PyInstaller。请先执行：
        echo        python -m pip install pyinstaller
        pause
        exit /b 1
    )
)

echo [1/2] 清理旧产物...
if exist build rmdir /s /q build
if exist dist\WorkBuddyCreditWidget.exe del /q dist\WorkBuddyCreditWidget.exe

echo [2/2] 执行 PyInstaller 打包（onefile，无控制台）...
pyinstaller --clean --noconfirm --windowed --icon=widget_icon.ico WorkBuddyCreditWidget_cross.spec

if exist dist\WorkBuddyCreditWidget.exe (
    echo.
    echo [完成] 产物：%CD%\dist\WorkBuddyCreditWidget.exe
) else (
    echo [失败] 打包未产出可执行文件，请查看上方日志。
)
pause
