# 记忆召回质量测试指南

> 作者：哈雷酱（大小姐）  
> 版本：v1.0  
> 更新时间：2026-06-07

## 📖 概述

这是一套基于**真实导出数据**的记忆召回质量测试工具。不同于单纯的功能测试，它专注于评估**记忆效果**：能否准确召回用户信息、相关性如何、原文回溯是否可用。

### 核心价值

- ✅ **真实数据驱动**：使用你自己的对话数据，不是构造的假数据
- ✅ **自动化评估**：自动生成测试用例、执行检索、计算指标
- ✅ **可视化报告**：生成 Markdown 报告，清晰展示问题
- ✅ **持续监控**：定期运行，追踪记忆质量变化趋势

---

## 🚀 快速开始

### Step 1: 导出对话数据

```bash
# 在 AstrBot 中执行指令（导出最近30天的数据）
/导出记忆 jsonl 30
```

导出文件位置：`data/plugins_data/astrbot_plugin_engram/exports/engram_export_*.jsonl`

### Step 2: 运行测试

有两种方式：

#### 方式 A：使用 pytest（推荐）

```bash
# 进入项目目录
cd E:\AI\shouban\astrbot_plugin_engram

# 运行测试（-v 显示详细信息，-s 显示打印输出）
pytest tests/test_memory_recall_quality.py -v -s
```

#### 方式 B：直接运行脚本

```bash
# 自动使用最新的导出文件
python tests/test_memory_recall_quality.py

# 或指定导出文件
python tests/test_memory_recall_quality.py data/plugins_data/astrbot_plugin_engram/exports/engram_export_xxx.jsonl
```

### Step 3: 查看报告

测试完成后，查看生成的报告：

```bash
# Windows
notepad tests/memory_recall_report.md

# Linux/Mac
cat tests/memory_recall_report.md
```

---

## 📊 报告解读

### 整体指标说明

| 指标 | 含义 | 优秀标准 |
|------|------|---------|
| **召回成功率** | 测试查询能否找到相关记忆 | ≥ 80% |
| **平均相关性** | 召回内容与期望的相似度（Jaccard） | ≥ 0.6 |
| **平均召回耗时** | 单次检索耗时 | ≤ 100ms |
| **原文回溯成功率** | 能否关联到原始对话 | ≥ 80% |

### 评级标准

#### 召回成功率
- ✅ **优秀**（≥80%）：记忆质量很好
- ⚠️ **需优化**（60%-80%）：有改进空间
- ❌ **较差**（<60%）：需要重点优化

#### 平均相关性
- ✅ **高**（≥0.6）：召回内容高度相关
- ⚠️ **中**（0.3-0.6）：召回内容部分相关
- ❌ **低**（<0.3）：召回内容不相关

#### 召回耗时
- ✅ **快**（≤100ms）：用户无感知
- ⚠️ **中**（100-500ms）：可接受
- ❌ **慢**（>500ms）：影响体验

---

## 🔧 测试原理

### 测试用例生成

工具会自动从导出的对话中提取**事实陈述**，生成测试用例。例如：

**原始对话**：
```
用户: 我最喜欢吃火锅，特别是麻辣锅底
```

**生成测试用例**：
- 查询：`我喜欢什么`
- 期望关键词：`火锅`, `麻辣`, `锅底`
- 类别：`preference`（偏好）

### 支持的模式

工具预设了多种模式来提取测试用例：

1. **偏好类**
   - `我喜欢xxx` → "我喜欢什么"
   - `我讨厌xxx` → "我讨厌什么"

2. **事实类**
   - `我的猫叫xxx` → "我的猫叫什么"
   - `我是xxx` → "我是做什么的"
   - `我在xxx工作` → "我在哪里工作"

3. **事件类**
   - `我今天xxx` → "我今天做了什么"

4. **计划类**
   - `我打算xxx` → "我打算做什么"

### 评估方法

1. **召回成功判定**：召回的记忆中包含任一期望关键词
2. **相关性计算**：使用 Jaccard 相似度（关键词交集/并集）
3. **原文回溯检查**：验证召回结果是否关联了原始消息

---

## 🎯 根据报告优化

### 场景 1：召回成功率低（<60%）

**可能原因**：
- 归档的记忆内容与原始对话差异大
- 向量检索效果不佳
- BM25 降级方案关键词提取不准

**优化方向**：
```python
# 1. 检查归档提示词
# 位置: logic.py 中的归档提示词
# 确保提示词要求LLM提取关键信息

# 2. 测试不同的 Embedding 模型
# 修改 ChromaDB 初始化参数

# 3. 优化 BM25 分词
# 位置: database.py 中的 FTS5 配置
```

### 场景 2：相关性低（<0.3）

**可能原因**：
- 检索召回了不相关的记忆
- 向量相似度阈值设置不当
- 缺少重排序机制

**优化方向**：
```python
# 1. 增加相似度阈值过滤
recalled = [r for r in results if r.score > 0.5]

# 2. 添加重排序（Reranking）
# 可以用 LLM 对召回结果重新打分

# 3. 结合多路检索
# 向量检索 + BM25 + 时间衰减综合排序
```

### 场景 3：召回耗时长（>500ms）

**可能原因**：
- 数据库索引缺失或不优化
- 向量库检索慢
- 没有使用异步

**优化方向**：
```sql
-- 1. 优化数据库索引
CREATE INDEX IF NOT EXISTS idx_timestamp ON raw_messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_user_archived ON raw_messages(user_id, is_archived);
```

```python
# 2. 使用异步批量检索
results = await asyncio.gather(*[
    search_memory(query) for query in queries
])

# 3. 缓存热点查询
from functools import lru_cache
@lru_cache(maxsize=100)
def cached_search(query):
    return search_memory(query)
```

### 场景 4：原文回溯失败率高（<50%）

**这是你的核心优势功能，必须重视！**

**可能原因**：
- UUID 关联未正确建立
- 归档时未记录 evidence_refs
- 数据库外键约束问题

**检查清单**：
```python
# 1. 检查归档时是否记录了 evidence_refs
# 位置: logic.py 的归档逻辑

# 2. 验证 UUID 关联
import sqlite3
conn = sqlite3.connect('engram_memories.db')
cursor = conn.execute("""
    SELECT mi.id, mi.evidence_refs, COUNT(rm.uuid)
    FROM memory_index mi
    LEFT JOIN raw_messages rm ON rm.uuid IN (mi.evidence_refs)
    GROUP BY mi.id
    HAVING COUNT(rm.uuid) = 0
    LIMIT 10
""")
# 如果有结果，说明存在孤立的记忆索引

# 3. 重建关联
# 如果发现问题，可能需要重新归档
```

---

## 📈 持续监控

### 建立基线

第一次运行测试后，保存报告作为基线：

```bash
cp tests/memory_recall_report.md tests/baseline_report.md
```

### 定期测试

建议每周运行一次测试，监控记忆质量变化：

```bash
# 创建定时任务（Linux/Mac）
# crontab -e
# 添加：0 2 * * 0 cd /path/to/project && python tests/test_memory_recall_quality.py

# Windows任务计划程序
# 设置每周日凌晨2点运行
```

### 对比测试

在优化前后对比测试结果：

```bash
# 优化前测试
pytest tests/test_memory_recall_quality.py -v -s
cp tests/memory_recall_report.md tests/report_before_optimization.md

# 进行优化...

# 优化后测试
pytest tests/test_memory_recall_quality.py -v -s
cp tests/memory_recall_report.md tests/report_after_optimization.md

# 对比
diff tests/report_before_optimization.md tests/report_after_optimization.md
```

---

## 🛠️ 高级用法

### 自定义测试用例

如果自动生成的测试用例不够，可以手动添加：

```python
# 创建 tests/custom_test_cases.jsonl
# 每行一个 JSON 对象
{"id": "CUSTOM001", "original_message": "我的猫叫奥利奥", "test_query": "我的宠物叫什么", "expected_keywords": ["猫", "奥利奥"], "source_timestamp": 1234567890, "category": "fact"}
{"id": "CUSTOM002", "original_message": "我在准备考研", "test_query": "我在准备什么考试", "expected_keywords": ["考研"], "source_timestamp": 1234567891, "category": "event"}
```

然后修改测试脚本加载自定义用例：
```python
# 在 test_memory_recall_quality.py 中添加
custom_cases = []
with open('tests/custom_test_cases.jsonl') as f:
    for line in f:
        custom_cases.append(TestCase(**json.loads(line)))

test_cases.extend(custom_cases)
```

### 对比不同检索方法

修改测试脚本，测试不同检索方法的效果：

```python
class ComparativeTester:
    """对比测试器"""
    
    async def compare_methods(self, test_cases):
        methods = {
            'ChromaDB向量': self.test_with_chromadb,
            'BM25关键词': self.test_with_bm25,
            'LIKE兜底': self.test_with_like
        }
        
        results = {}
        for name, method in methods.items():
            print(f"测试 {name}...")
            results[name] = await method(test_cases)
        
        # 生成对比报告
        self._generate_comparison_report(results)
```

### 人工验证辅助

对于不确定的结果，可以人工验证：

```python
# 运行测试后，人工检查失败案例
failed = [r for r in results if not r.success]

for r in failed:
    print(f"\n查询: {r.query}")
    print(f"期望: {r.expected_keywords}")
    print(f"召回: {[m['content'][:50] for m in r.recalled_memories]}")
    
    correct = input("召回结果是否相关? (y/n): ")
    # 记录人工标注结果
```

---

## ❓ 常见问题

### Q: 测试用例生成数量太少怎么办？

A: 可能是导出的对话数据不够，或者对话中缺少明确的事实陈述。建议：
1. 增加导出天数：`/导出记忆 jsonl 60`
2. 添加自定义测试用例（见高级用法）
3. 扩展 `FACT_PATTERNS` 正则模式

### Q: 如何跳过项目模块导入错误？

A: 测试工具设计为可以在模块导入失败时使用模拟数据运行（用于测试框架本身）。但要测试真实效果，需要：
1. 确保项目路径正确
2. 检查 `database.py` 和 `logic.py` 是否存在
3. 安装所有依赖：`pip install -r requirements.txt`

### Q: 原文回溯测试失败怎么办？

A: 这是你的核心功能，必须修复！检查：
1. 归档逻辑是否正确记录了 `evidence_refs`
2. 数据库中 `memory_index.evidence_refs` 字段是否为空
3. UUID 格式是否正确

### Q: 召回成功率始终很低怎么办？

A: 可能的原因和解决方案：
1. **归档质量差**：检查归档提示词，确保提取了关键信息
2. **检索方法不适合**：尝试不同的 Embedding 模型
3. **测试用例不合理**：人工检查几个失败的案例，看是否是测试本身的问题

---

## 🎓 测试最佳实践

### 1. 建立测试习惯

- 每次重大功能更新后运行测试
- 每周定期测试，监控质量趋势
- 保存每次测试报告，便于对比

### 2. 关注核心指标

- **召回成功率**：这是记忆质量的直接体现
- **原文回溯成功率**：这是你的核心竞争力
- 其他指标作为参考

### 3. 结合人工验证

- 自动化测试提供定量指标
- 人工验证提供定性判断
- 两者结合才能全面评估

### 4. 持续优化

- 根据测试报告针对性优化
- 优化后立即测试验证效果
- 记录优化前后的数据对比

---

## 📚 相关资源

- [项目 README](../README.md)
- [数据库设计文档](../docs/数据库设计文档.md)
- [检索系统文档](../docs/retrieval.md)（如果有）

---

## 🎉 总结

哼，笨蛋，记住这几点！(￣▽￣)／

1. **数据驱动**：用真实数据测试，不要靠猜测
2. **持续监控**：定期测试，追踪质量变化
3. **针对性优化**：根据报告发现的问题有的放矢
4. **保持优势**：原文回溯是你的核心竞争力，必须保证高成功率

**不要盲目追功能，先把现有的做到极致！**

本测试工具会告诉你真实的记忆质量，而不是功能清单上的勾勾！(｀∀´)ノ

---

*本文档由哈雷酱（大小姐）编写，如有问题欢迎提 Issue！(*/ω\*)*
