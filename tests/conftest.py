import os
import sys
from pathlib import Path


# 允许在仓库根目录直接执行 `pytest -q` 导入 `astrbot_plugin_engram`
PROJECT_PARENT = Path(__file__).resolve().parents[2]   # .../shouban
PROJECT_ROOT = Path(__file__).resolve().parents[1]     # .../astrbot_plugin_engram

if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

# 兼容测试中使用的相对路径（例如 astrbot_plugin_engram/_conf_schema.json）
# 统一切到父目录执行，避免 cwd 差异导致找不到文件。
os.chdir(PROJECT_PARENT)
