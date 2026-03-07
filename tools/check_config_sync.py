#!/usr/bin/env python3
"""
配置项一致性自动核对脚本

用途：对比 `_conf_schema.json` 定义项与代码实际读取项，输出一致性报告。

输出：
- 终端摘要
- reports/config_sync_report.md
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "_conf_schema.json"
REPORT_PATH = ROOT / "reports" / "config_sync_report.md"

INCLUDE_DIRS = [
    ROOT / "core",
    ROOT / "handlers",
    ROOT / "services",
]
INCLUDE_FILES = [
    ROOT / "main.py",
    ROOT / "profile_renderer.py",
    ROOT / "export_handler.py",
    ROOT / "db_manager.py",
    ROOT / "utils.py",
]

EXCLUDE_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "tests",
    ".pi",
    "data",
}

# 读取模式：尽量覆盖项目里常见写法
CONFIG_PATTERNS = [
    re.compile(r"\bself\.config\.get\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\bconfig\.get\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\bself\._config\.get\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\b_config\.get\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\bself\.config\[\s*['\"]([^'\"]+)['\"]\s*\]"),
    re.compile(r"\bconfig\[\s*['\"]([^'\"]+)['\"]\s*\]"),
]


@dataclass
class Usage:
    key: str
    file: str
    line: int
    code: str


def load_schema_keys(path: Path) -> Dict[str, str]:
    """从 grouped schema 提取配置项，返回 {key: group_name}。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    result: Dict[str, str] = {}

    for group_name, group_value in data.items():
        if not isinstance(group_value, dict):
            continue

        items = group_value.get("items")
        if not isinstance(items, dict):
            continue

        for key in items.keys():
            result[str(key)] = str(group_name)

    return result


def iter_code_files() -> Iterable[Path]:
    files: Set[Path] = set()

    for p in INCLUDE_FILES:
        if p.exists() and p.is_file():
            files.add(p)

    for d in INCLUDE_DIRS:
        if not d.exists() or not d.is_dir():
            continue
        for p in d.rglob("*.py"):
            if any(part in EXCLUDE_PARTS for part in p.parts):
                continue
            files.add(p)

    return sorted(files)


def collect_usages(files: Iterable[Path]) -> Dict[str, List[Usage]]:
    key_usages: Dict[str, List[Usage]] = defaultdict(list)

    for file_path in files:
        rel = file_path.relative_to(ROOT).as_posix()
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()

        for i, line in enumerate(lines, start=1):
            line_keys: Set[str] = set()
            for pattern in CONFIG_PATTERNS:
                for m in pattern.finditer(line):
                    key = m.group(1).strip()
                    if key:
                        line_keys.add(key)

            for key in sorted(line_keys):
                key_usages[key].append(Usage(key=key, file=rel, line=i, code=line.strip()))

    return key_usages


def format_top_usages(usages: List[Usage], max_items: int = 3) -> str:
    if not usages:
        return "-"
    parts = [f"`{u.file}:{u.line}`" for u in usages[:max_items]]
    if len(usages) > max_items:
        parts.append(f"...(+{len(usages) - max_items})")
    return ", ".join(parts)


def build_markdown_report(
    schema_keys: Dict[str, str],
    code_usages: Dict[str, List[Usage]],
) -> str:
    schema_set = set(schema_keys.keys())
    code_set = set(code_usages.keys())

    both = sorted(schema_set & code_set)
    schema_only = sorted(schema_set - code_set)
    code_only = sorted(code_set - schema_set)

    lines: List[str] = []
    lines.append("# 配置项一致性自动核对报告")
    lines.append("")
    lines.append(f"- schema 配置项数：**{len(schema_set)}**")
    lines.append(f"- 代码读取配置项数：**{len(code_set)}**")
    lines.append(f"- 一致（定义且使用）：**{len(both)}**")
    lines.append(f"- 仅 schema（定义未使用）：**{len(schema_only)}**")
    lines.append(f"- 仅代码（使用未定义）：**{len(code_only)}**")
    lines.append("")

    lines.append("## 1) 定义且使用")
    lines.append("")
    lines.append("| 配置项 | 所属分组 | 代码位置示例 |")
    lines.append("|---|---|---|")
    for key in both:
        group = schema_keys.get(key, "-")
        where = format_top_usages(code_usages.get(key, []))
        lines.append(f"| `{key}` | `{group}` | {where} |")
    lines.append("")

    lines.append("## 2) 仅 schema（定义未使用）")
    lines.append("")
    if schema_only:
        lines.append("| 配置项 | 所属分组 |")
        lines.append("|---|---|")
        for key in schema_only:
            lines.append(f"| `{key}` | `{schema_keys.get(key, '-')}` |")
    else:
        lines.append("✅ 无")
    lines.append("")

    lines.append("## 3) 仅代码（使用未定义）")
    lines.append("")
    if code_only:
        lines.append("| 配置项 | 代码位置示例 |")
        lines.append("|---|---|")
        for key in code_only:
            where = format_top_usages(code_usages.get(key, []), max_items=5)
            lines.append(f"| `{key}` | {where} |")
    else:
        lines.append("✅ 无")
    lines.append("")

    lines.append("## 建议")
    lines.append("")
    lines.append("- 对 `仅 schema` 项：确认是否遗留配置，决定补实现或从 schema 移除。")
    lines.append("- 对 `仅代码` 项：补入 schema（若需对外配置）或改为内部常量。")
    lines.append("- 建议在 CI 中加入该脚本，作为配置一致性守护。")

    return "\n".join(lines) + "\n"


def main() -> int:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"schema not found: {SCHEMA_PATH}")

    schema_keys = load_schema_keys(SCHEMA_PATH)
    files = list(iter_code_files())
    code_usages = collect_usages(files)

    report = build_markdown_report(schema_keys, code_usages)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")

    schema_set = set(schema_keys.keys())
    code_set = set(code_usages.keys())
    both = schema_set & code_set
    schema_only = schema_set - code_set
    code_only = code_set - schema_set

    print("[config-sync] report generated:", REPORT_PATH.relative_to(ROOT).as_posix())
    print(
        f"[config-sync] schema={len(schema_set)} code={len(code_set)} "
        f"both={len(both)} schema_only={len(schema_only)} code_only={len(code_only)}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
