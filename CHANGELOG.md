# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.1] - 2026-02-09

### Changed
- **统一计算逻辑**：`profile_renderer.py` 使用 `BondCalculator` 替代本地重复方法
  - 删除 `_calculate_profile_depth()`, `_calculate_bond_level()`, `_get_next_level_hints()` 冗余代码
  - 减少约 220 行重复代码

- **重构 main.py 为纯路由层**：
  - 使用 `MemoryScheduler` 替代 main.py 中的调度方法
  - 删除 `_daily_persona_scheduler()`, `background_worker()`, `_calculate_next_check_time()` 冗余代码
  - 初始化 `MemoryCommandHandler`, `ProfileCommandHandler`, `OneBotSyncHandler`, `LLMContextInjector`
  - main.py 现在仅负责装饰器绑定和参数解析，业务逻辑委托给 handlers/
  - 减少约 100 行重复代码

## [1.3.0] - 2026-02-09

### Changed
- **架构重构**：使用 `MemoryFacade` 门面模式统一接口
  - 新增 `core/memory_facade.py` 作为统一入口
  - `MemoryManager` 和 `ProfileManager` 职责分离更清晰
- 版本号从 1.2.5 升级到 1.3.0

### Fixed
- 修复 `core/memory_manager.py` 中的导入路径错误（`from .db_manager` → `from ..db_manager`）

### Internal
- 代码结构优化，遵循单一职责原则
- 新架构与旧版 `MemoryLogic` 接口完全兼容，无需修改任何调用代码

## [1.2.5] - Previous Release

- 仿生双轨记忆系统初始架构
- 双轨记忆（L2叙事归档 + 原文指针回溯）
- 深度用户画像（7级羁绊系统）
- ChromaDB 向量检索
- 多格式数据导出（JSONL/Alpaca/ShareGPT）
