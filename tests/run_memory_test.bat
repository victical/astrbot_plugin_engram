@echo off
chcp 65001 > nul
REM Engram 记忆召回质量测试 - 快速启动脚本
REM 作者：哈雷酱（大小姐）

echo ========================================
echo Engram 记忆召回质量测试
echo ========================================
echo.

REM 检查是否在项目根目录
if not exist "tests\test_memory_recall_quality.py" (
    echo ❌ 错误：请在项目根目录运行此脚本！
    echo    当前目录：%CD%
    pause
    exit /b 1
)

REM 检查导出目录
set EXPORT_DIR=data\plugins_data\astrbot_plugin_engram\exports
if not exist "%EXPORT_DIR%" (
    echo ❌ 导出目录不存在：%EXPORT_DIR%
    echo.
    echo 💡 请先在 AstrBot 中执行：
    echo    /导出记忆 jsonl 30
    echo.
    pause
    exit /b 1
)

REM 检查是否有导出文件
dir /b "%EXPORT_DIR%\engram_export_*.jsonl" > nul 2>&1
if errorlevel 1 (
    echo ❌ 没有找到导出文件
    echo.
    echo 💡 请先在 AstrBot 中执行：
    echo    /导出记忆 jsonl 30
    echo.
    pause
    exit /b 1
)

echo ✅ 发现导出文件
echo.

REM 询问测试方式
echo 请选择测试方式：
echo   [1] 使用 pytest（推荐）
echo   [2] 直接运行 Python 脚本
echo.
set /p choice="请输入选择 (1 或 2): "

if "%choice%"=="1" (
    echo.
    echo 🧪 使用 pytest 运行测试...
    echo.
    pytest tests\test_memory_recall_quality.py -v -s
) else if "%choice%"=="2" (
    echo.
    echo 🧪 直接运行 Python 脚本...
    echo.
    python tests\test_memory_recall_quality.py
) else (
    echo.
    echo ❌ 无效的选择！
    pause
    exit /b 1
)

echo.
echo ========================================
echo 测试完成！
echo ========================================
echo.

REM 检查报告是否生成
if exist "tests\memory_recall_report.md" (
    echo ✅ 测试报告已生成：tests\memory_recall_report.md
    echo.
    set /p open_report="是否立即查看报告? (y/n): "
    if /i "%open_report%"=="y" (
        start notepad tests\memory_recall_report.md
    )
) else (
    echo ⚠️ 未找到测试报告
)

echo.
echo 💡 提示：
echo    - 详细使用说明：tests\README_memory_testing.md
echo    - 生成的测试用例：tests\test_cases_generated.jsonl
echo.
pause
