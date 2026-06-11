#!/bin/bash
# Engram 记忆召回质量测试 - 快速启动脚本
# 作者：哈雷酱（大小姐）

set -e

echo "========================================"
echo "Engram 记忆召回质量测试"
echo "========================================"
echo ""

# 检查是否在项目根目录
if [ ! -f "tests/test_memory_recall_quality.py" ]; then
    echo "❌ 错误：请在项目根目录运行此脚本！"
    echo "   当前目录：$(pwd)"
    exit 1
fi

# 检查导出目录
EXPORT_DIR="data/plugins_data/astrbot_plugin_engram/exports"
if [ ! -d "$EXPORT_DIR" ]; then
    echo "❌ 导出目录不存在：$EXPORT_DIR"
    echo ""
    echo "💡 请先在 AstrBot 中执行："
    echo "   /导出记忆 jsonl 30"
    echo ""
    exit 1
fi

# 检查是否有导出文件
if ! ls $EXPORT_DIR/engram_export_*.jsonl 1> /dev/null 2>&1; then
    echo "❌ 没有找到导出文件"
    echo ""
    echo "💡 请先在 AstrBot 中执行："
    echo "   /导出记忆 jsonl 30"
    echo ""
    exit 1
fi

echo "✅ 发现导出文件"
echo ""

# 询问测试方式
echo "请选择测试方式："
echo "  [1] 使用 pytest（推荐）"
echo "  [2] 直接运行 Python 脚本"
echo ""
read -p "请输入选择 (1 或 2): " choice

if [ "$choice" == "1" ]; then
    echo ""
    echo "🧪 使用 pytest 运行测试..."
    echo ""
    pytest tests/test_memory_recall_quality.py -v -s
elif [ "$choice" == "2" ]; then
    echo ""
    echo "🧪 直接运行 Python 脚本..."
    echo ""
    python tests/test_memory_recall_quality.py
else
    echo ""
    echo "❌ 无效的选择！"
    exit 1
fi

echo ""
echo "========================================"
echo "测试完成！"
echo "========================================"
echo ""

# 检查报告是否生成
if [ -f "tests/memory_recall_report.md" ]; then
    echo "✅ 测试报告已生成：tests/memory_recall_report.md"
    echo ""

    # Linux/Mac 自动打开报告
    if command -v xdg-open > /dev/null; then
        read -p "是否立即查看报告? (y/n): " open_report
        if [ "$open_report" == "y" ]; then
            xdg-open tests/memory_recall_report.md
        fi
    elif command -v open > /dev/null; then
        read -p "是否立即查看报告? (y/n): " open_report
        if [ "$open_report" == "y" ]; then
            open tests/memory_recall_report.md
        fi
    fi
else
    echo "⚠️ 未找到测试报告"
fi

echo ""
echo "💡 提示："
echo "   - 详细使用说明：tests/README_memory_testing.md"
echo "   - 生成的测试用例：tests/test_cases_generated.jsonl"
echo ""
