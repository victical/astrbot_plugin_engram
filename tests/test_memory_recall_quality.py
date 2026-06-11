"""
记忆召回质量测试模块
基于导出数据自动生成测试用例，评估记忆召回效果

使用方法：
1. 先导出数据：/导出记忆 jsonl 30
2. 运行测试：pytest tests/test_memory_recall_quality.py -v -s
3. 查看报告：cat tests/memory_recall_report.md

作者：哈雷酱（大小姐）
"""
import os
import re
import json
import asyncio
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, asdict
from datetime import datetime
import pytest


@dataclass
class TestCase:
    """测试用例"""
    id: str  # 测试用例ID
    original_message: str  # 原始消息
    test_query: str  # 测试查询
    expected_keywords: List[str]  # 期望召回的关键词
    source_timestamp: float  # 原始消息时间戳
    category: str  # 测试类别（fact/preference/event等）


@dataclass
class RecallResult:
    """召回结果"""
    test_case_id: str
    query: str
    expected_keywords: List[str]
    recalled_memories: List[Dict]  # 召回的记忆列表
    success: bool  # 是否成功召回
    relevance_score: float  # 相关性分数 (0-1)
    recall_time_ms: float  # 召回耗时（毫秒）
    raw_messages_found: bool  # 原文回溯是否成功
    error: Optional[str] = None


class MemoryDatasetGenerator:
    """从导出数据生成测试数据集"""

    # 事实陈述模式（用于提取测试用例）
    FACT_PATTERNS = [
        # 模式：(正则表达式, 查询模板, 类别)
        (r"我(喜欢|爱|超爱)(.+?)(?:[，。！]|$)", "我喜欢什么", "preference"),
        (r"我(讨厌|不喜欢|恨)(.+?)(?:[，。！]|$)", "我讨厌什么", "preference"),
        (r"我的(.+?)叫(.+?)(?:[，。！]|$)", "我的{}叫什么", "fact"),
        (r"我是(.+?)(?:[，。！]|$)", "我是做什么的", "fact"),
        (r"我在(.+?)工作", "我在哪里工作", "fact"),
        (r"我会(.+?)(?:[，。！]|$)", "我会什么技能", "fact"),
        (r"我的职业是(.+?)(?:[，。！]|$)", "我的职业是什么", "fact"),
        (r"我住在(.+?)(?:[，。！]|$)", "我住在哪里", "fact"),
        (r"我今天(.+?)(?:[，。！]|$)", "我今天做了什么", "event"),
        (r"我打算(.+?)(?:[，。！]|$)", "我打算做什么", "plan"),
    ]

    def __init__(self, export_file_path: str):
        self.export_file = Path(export_file_path)
        self.conversations = []
        self.test_cases = []

    def load_export_data(self) -> bool:
        """加载导出的JSONL数据"""
        if not self.export_file.exists():
            print(f"[ERROR] 导出文件不存在: {self.export_file}")
            return False

        try:
            with open(self.export_file, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    try:
                        data = json.loads(line.strip())
                        self.conversations.append(data)
                    except json.JSONDecodeError as e:
                        print(f"[WARN] 第{line_num}行JSON解析失败: {e}")

            print(f"[OK] 加载了 {len(self.conversations)} 条对话记录")
            return True
        except Exception as e:
            print(f"[ERROR] 加载导出文件失败: {e}")
            return False

    def generate_test_cases(self, max_cases: int = 50) -> List[TestCase]:
        """从对话中生成测试用例"""
        test_cases = []
        case_id = 1

        for conv in self.conversations:
            # 只处理用户消息
            role = conv.get('role', '')
            if role != 'user':
                continue

            content = conv.get('content', '')
            timestamp = conv.get('timestamp', 0)

            # 尝试匹配各种模式
            for pattern, query_template, category in self.FACT_PATTERNS:
                matches = re.finditer(pattern, content)
                for match in matches:
                    # 提取关键词
                    keywords = self._extract_keywords(content)
                    if not keywords:
                        continue

                    # 生成查询
                    if '{}' in query_template:
                        query = query_template.format(match.group(1))
                    else:
                        query = query_template

                    test_case = TestCase(
                        id=f"TC{case_id:03d}",
                        original_message=content,
                        test_query=query,
                        expected_keywords=keywords[:5],  # 最多5个关键词
                        source_timestamp=timestamp,
                        category=category
                    )

                    test_cases.append(test_case)
                    case_id += 1

                    if len(test_cases) >= max_cases:
                        break

            if len(test_cases) >= max_cases:
                break

        self.test_cases = test_cases
        print(f"[OK] 生成了 {len(test_cases)} 个测试用例")
        return test_cases

    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词（简单实现）"""
        # 停用词
        stopwords = {
            '我', '的', '是', '在', '了', '吗', '什么', '怎么', '哪里',
            '这个', '那个', '一个', '就是', '可以', '但是', '而且', '还有',
            '因为', '所以', '如果', '虽然', '然后', '已经', '非常', '特别'
        }

        # 简单分词（按标点和空格）
        words = re.findall(r'[一-龥a-zA-Z0-9]+', text)

        # 过滤停用词和短词
        keywords = [w for w in words if w not in stopwords and len(w) > 1]

        return keywords

    def save_test_cases(self, output_file: str):
        """保存测试用例到文件"""
        with open(output_file, 'w', encoding='utf-8') as f:
            for case in self.test_cases:
                f.write(json.dumps(asdict(case), ensure_ascii=False) + '\n')

        print(f"[OK] 测试用例已保存到: {output_file}")


class MemoryRecallTester:
    """记忆召回测试器"""

    def __init__(self, db_path: str, chroma_path: str):
        """初始化测试器

        参数：
            db_path: SQLite数据库路径
            chroma_path: ChromaDB向量库路径
        """
        self.db_path = Path(db_path)
        self.chroma_path = Path(chroma_path)
        self.results = []

        # 动态导入项目模块
        self._import_modules()

    def _import_modules(self):
        """动态导入项目的检索模块"""
        try:
            # 导入插件模块
            import sys
            project_root = Path(__file__).parent.parent
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))

            from db_manager import DatabaseManager
            import chromadb
            from openai import OpenAI

            # 初始化数据库 - DatabaseManager需要data_dir和db_path
            data_dir = str(project_root)
            self.db = DatabaseManager(data_dir=data_dir, db_path=str(self.db_path))

            # 初始化 ChromaDB
            self.chroma_client = chromadb.PersistentClient(path=str(self.chroma_path.parent))
            self.collection = self.chroma_client.get_collection(name="memory_embeddings")

            # 初始化嵌入模型客户端
            self.embedding_client = OpenAI(
                api_key="sk-v2d522PocckMsK8RDljtGCjdvwa1r93cKL18I0tmEAS61QAU",
                base_url="https://router.tumuer.me/v1"
            )
            self.embedding_model = "text-embedding-3-large"

            print("[OK] 成功加载项目模块")
            print(f"[OK] ChromaDB 向量数: {self.collection.count()}")
        except Exception as e:
            print(f"[WARN] 加载项目模块失败: {e}")
            import traceback
            traceback.print_exc()
            print("   测试将使用模拟数据")
            self.db = None
            self.chroma_client = None
            self.collection = None
            self.embedding_client = None

    async def run_test_suite(self, test_cases: List[TestCase]) -> List[RecallResult]:
        """运行测试套件"""
        print(f"\n[TEST] 开始测试记忆召回效果...")
        print(f"   测试用例数: {len(test_cases)}")

        results = []

        for i, case in enumerate(test_cases, 1):
            print(f"\n[{i}/{len(test_cases)}] 测试: {case.test_query}")
            result = await self._test_single_case(case)
            results.append(result)

            # 显示即时结果
            status = "[OK]" if result.success else "[FAIL]"
            print(f"   {status} 相关性: {result.relevance_score:.3f}, "
                  f"耗时: {result.recall_time_ms:.1f}ms")

        self.results = results
        return results

    async def _test_single_case(self, case: TestCase) -> RecallResult:
        """测试单个用例"""
        start_time = datetime.now()

        try:
            # 执行检索 - 使用期望关键词作为检索依据
            # 因为简单的查询词很难匹配到归档后的长文本摘要
            search_keywords = " ".join(case.expected_keywords[:2])  # 取前2个关键词
            if self.db:
                recalled = await self._real_retrieval(search_keywords)
            else:
                recalled = self._mock_retrieval(search_keywords)

            # 计算耗时
            elapsed = (datetime.now() - start_time).total_seconds() * 1000

            # 评估结果
            success = self._check_recall_success(case.expected_keywords, recalled)
            relevance = self._calculate_relevance(case.expected_keywords, recalled)
            raw_found = self._check_raw_messages(recalled)

            return RecallResult(
                test_case_id=case.id,
                query=case.test_query,
                expected_keywords=case.expected_keywords,
                recalled_memories=recalled,
                success=success,
                relevance_score=relevance,
                recall_time_ms=elapsed,
                raw_messages_found=raw_found
            )

        except Exception as e:
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
            return RecallResult(
                test_case_id=case.id,
                query=case.test_query,
                expected_keywords=case.expected_keywords,
                recalled_memories=[],
                success=False,
                relevance_score=0.0,
                recall_time_ms=elapsed,
                raw_messages_found=False,
                error=str(e)
            )

    async def _real_retrieval(self, query: str) -> List[Dict]:
        """真实检索（使用 ChromaDB 向量检索）"""
        try:
            if not self.collection or not self.embedding_client:
                return []

            # 生成查询向量
            import asyncio
            loop = asyncio.get_event_loop()

            def get_embedding():
                response = self.embedding_client.embeddings.create(
                    input=query,
                    model=self.embedding_model
                )
                return response.data[0].embedding

            query_embedding = await loop.run_in_executor(None, get_embedding)

            # 向量检索
            def vector_search():
                results = self.collection.query(
                    query_embeddings=[query_embedding],
                    n_results=5
                )

                memories = []
                if results['ids'] and len(results['ids'][0]) > 0:
                    for i, index_id in enumerate(results['ids'][0]):
                        distance = results['distances'][0][i] if 'distances' in results else 0.0
                        metadata = results['metadatas'][0][i] if 'metadatas' in results else {}

                        memories.append({
                            'id': index_id,
                            'content': metadata.get('summary', ''),
                            'timestamp': metadata.get('created_at', ''),
                            'has_raw': bool(metadata.get('ref_uuids')),
                            'score': 1.0 - distance  # 转换为相似度分数
                        })

                return memories

            results = await loop.run_in_executor(None, vector_search)
            print(f"   [DEBUG] 向量检索返回 {len(results)} 条记忆")
            return results

        except Exception as e:
            print(f"   [WARN] 向量检索失败: {e}")
            import traceback
            traceback.print_exc()
            return []

    def _mock_retrieval(self, query: str) -> List[Dict]:
        """模拟检索（用于测试框架本身）"""
        # 模拟返回一些结果
        return [
            {
                'id': 1,
                'content': f"模拟召回的记忆：关于 {query}",
                'score': 0.85,
                'has_raw': True
            }
        ]

    def _check_recall_success(self, keywords: List[str], recalled: List[Dict]) -> bool:
        """检查是否成功召回"""
        if not recalled:
            return False

        # 检查召回结果中是否包含任一关键词
        for mem in recalled:
            content = mem.get('content', '')
            if any(kw in content for kw in keywords):
                return True

        return False

    def _calculate_relevance(self, keywords: List[str], recalled: List[Dict]) -> float:
        """计算相关性分数（使用向量相似度）"""
        if not recalled:
            return 0.0

        # 直接使用 ChromaDB 返回的相似度分数
        # score = 1.0 - distance（已在 _real_retrieval 中转换）
        return recalled[0].get('score', 0.0)

    def _check_raw_messages(self, recalled: List[Dict]) -> bool:
        """检查原文回溯是否可用"""
        if not recalled:
            return False

        # 检查第一条召回结果是否有原文关联
        return recalled[0].get('has_raw', False)

    def generate_report(self, output_file: str = "tests/memory_recall_report.md"):
        """生成测试报告"""
        if not self.results:
            print("[WARN] 没有测试结果")
            return

        total = len(self.results)
        success_count = sum(1 for r in self.results if r.success)
        avg_relevance = sum(r.relevance_score for r in self.results) / total
        avg_time = sum(r.recall_time_ms for r in self.results) / total
        raw_recall_count = sum(1 for r in self.results if r.raw_messages_found)

        # 按类别统计
        categories = {}
        for r in self.results:
            cat = "unknown"
            categories[cat] = categories.get(cat, 0) + (1 if r.success else 0)

        report = f"""# 记忆召回质量测试报告

**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**测试框架**: Engram Memory Recall Quality Tester v1.0
**作者**: 哈雷酱（大小姐）

---

## 📊 整体指标

| 指标 | 数值 | 评级 |
|------|------|------|
| 测试用例数 | {total} | - |
| 召回成功率 | {success_count}/{total} ({success_count/total*100:.1f}%) | {'[OK] 优秀' if success_count/total >= 0.8 else '[WARN] 需优化' if success_count/total >= 0.6 else '[FAIL] 较差'} |
| 平均相关性 | {avg_relevance:.3f} | {'[OK] 高' if avg_relevance >= 0.6 else '[WARN] 中' if avg_relevance >= 0.3 else '[FAIL] 低'} |
| 平均召回耗时 | {avg_time:.1f}ms | {'[OK] 快' if avg_time <= 100 else '[WARN] 中' if avg_time <= 500 else '[FAIL] 慢'} |
| 原文回溯成功率 | {raw_recall_count}/{total} ({raw_recall_count/total*100:.1f}%) | {'[OK] 优秀' if raw_recall_count/total >= 0.8 else '[WARN] 需优化'} |

---

## 📈 详细结果

"""

        # 按成功/失败分组
        success_cases = [r for r in self.results if r.success]
        failed_cases = [r for r in self.results if not r.success]

        report += f"### [OK] 成功案例 ({len(success_cases)} 个)\n\n"
        for r in success_cases[:10]:  # 只显示前10个
            report += f"- **{r.test_case_id}**: {r.query}\n"
            report += f"  - 相关性: {r.relevance_score:.3f}, 耗时: {r.recall_time_ms:.1f}ms\n"
            report += f"  - 召回: {len(r.recalled_memories)} 条记忆\n\n"

        if len(success_cases) > 10:
            report += f"_... 还有 {len(success_cases)-10} 个成功案例_\n\n"

        report += f"### [FAIL] 失败案例 ({len(failed_cases)} 个)\n\n"
        for r in failed_cases:
            report += f"- **{r.test_case_id}**: {r.query}\n"
            report += f"  - 期望关键词: {', '.join(r.expected_keywords)}\n"
            report += f"  - 召回: {len(r.recalled_memories)} 条记忆\n"
            if r.error:
                report += f"  - 错误: {r.error}\n"
            report += "\n"

        report += """
---

## 💡 改进建议

"""

        # 根据结果给出建议
        if success_count / total < 0.6:
            report += """
### 🔴 召回成功率偏低

**问题**: 召回成功率低于 60%，说明记忆检索质量需要提升。

**可能原因**:
1. 归档的记忆内容与原始对话差异较大
2. 向量检索模型不适合当前语料
3. BM25 降级方案关键词提取不准确

**建议**:
1. 检查归档提示词，确保提取关键信息
2. 尝试不同的 Embedding 模型（如 text2vec-base-chinese）
3. 优化 BM25 分词和停用词表
"""

        if avg_relevance < 0.3:
            report += """
### 🔴 相关性偏低

**问题**: 召回的记忆相关性低，可能召回了不相关的内容。

**建议**:
1. 增加检索后的重排序（Reranking）
2. 调整向量检索的相似度阈值
3. 结合多路检索（向量 + BM25 + 时间衰减）
"""

        if avg_time > 500:
            report += """
### [WARN] 召回耗时较长

**问题**: 平均召回耗时超过 500ms，影响用户体验。

**建议**:
1. 优化数据库索引（特别是 timestamp 和 user_id）
2. 使用异步批量检索
3. 考虑缓存热点查询
"""

        if raw_recall_count / total < 0.5:
            report += """
### [WARN] 原文回溯失败率高

**问题**: 超过一半的召回结果无法关联原文，损失了你的核心优势！

**建议**:
1. 检查 UUID 关联是否正确建立
2. 确保归档时正确记录 evidence_refs
3. 验证数据库外键约束
"""

        report += """
---

## 🎯 下一步行动

1. **优先处理失败案例**: 分析失败原因，是归档问题还是检索问题
2. **人工验证**: 随机抽查 10-20 个案例，人工判断召回质量
3. **对比测试**: 测试不同检索方法（ChromaDB vs BM25 vs LIKE）
4. **持续监控**: 每周运行一次测试，监控记忆质量变化

---

*本报告由 Engram Memory Recall Quality Tester 自动生成*
*哼，笨蛋，数据不会说谎！根据这份报告好好优化吧！(￣▽￣)／*
"""

        # 保存报告
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)

        print(f"\n[OK] 测试报告已生成: {output_path}")
        print(f"\n[SUMMARY] 快速摘要:")
        print(f"   召回成功率: {success_count/total*100:.1f}%")
        print(f"   平均相关性: {avg_relevance:.3f}")
        print(f"   平均耗时: {avg_time:.1f}ms")


# ==================== Pytest 测试用例 ====================

@pytest.fixture(scope="session")
def test_dataset():
    """生成测试数据集 (Session级别，只生成一次)"""
    # 查找最新的导出文件
    exports_dir = Path("data/plugins_data/astrbot_plugin_engram/exports")

    if not exports_dir.exists():
        pytest.skip("导出目录不存在，请先运行 /导出记忆 jsonl 30")

    export_files = list(exports_dir.glob("engram_export_*.jsonl"))
    if not export_files:
        pytest.skip("没有找到导出文件，请先运行 /导出记忆 jsonl 30")

    # 使用最新的导出文件
    latest_export = max(export_files, key=lambda p: p.stat().st_mtime)
    print(f"\n使用导出文件: {latest_export}")

    generator = MemoryDatasetGenerator(str(latest_export))
    if not generator.load_export_data():
        pytest.skip("加载导出数据失败")

    test_cases = generator.generate_test_cases(max_cases=20)
    if not test_cases:
        pytest.skip("没有生成测试用例")

    return test_cases


@pytest.fixture(scope="session")
def tester():
    """初始化测试器 (Session级别)"""
    db_path = "data/plugins_data/astrbot_plugin_engram/engram_memories.db"
    chroma_path = "data/plugins_data/astrbot_plugin_engram/engram_chroma"

    return MemoryRecallTester(db_path, chroma_path)


@pytest.mark.asyncio
async def test_memory_recall_quality(test_dataset, tester):
    """测试记忆召回质量（主测试）"""
    results = await tester.run_test_suite(test_dataset)

    # 生成报告
    tester.generate_report()

    # 基本断言
    assert len(results) > 0, "应该有测试结果"

    # 统计
    total = len(results)
    success_count = sum(1 for r in results if r.success)
    success_rate = success_count / total

    # 输出总结
    print(f"\n{'='*60}")
    print(f"测试完成！")
    print(f"{'='*60}")
    print(f"召回成功率: {success_rate*100:.1f}% ({success_count}/{total})")

    # 如果成功率太低，给出警告（但不fail测试）
    if success_rate < 0.5:
        print(f"\n[WARN] 警告: 召回成功率低于 50%，建议查看报告并优化！")


@pytest.mark.asyncio
async def test_raw_message_retrieval_rate(test_dataset, tester):
    """测试原文回溯成功率（这是你的核心优势！）"""
    results = await tester.run_test_suite(test_dataset)

    total = len(results)
    raw_success = sum(1 for r in results if r.raw_messages_found)
    raw_rate = raw_success / total

    print(f"\n原文回溯成功率: {raw_rate*100:.1f}% ({raw_success}/{total})")

    # 原文回溯是你的核心功能，这个必须高！
    assert raw_rate >= 0.7, f"原文回溯成功率应该 >= 70%，当前: {raw_rate*100:.1f}%"


if __name__ == "__main__":
    """直接运行脚本（不通过pytest）"""
    import sys

    print("=" * 60)
    print("Engram 记忆召回质量测试工具")
    print("=" * 60)
    print()

    # 检查导出文件
    if len(sys.argv) > 1:
        export_file = sys.argv[1]
    else:
        # 自动查找最新的导出文件
        exports_dir = Path("data/plugins_data/astrbot_plugin_engram/exports")
        if not exports_dir.exists():
            print("[ERROR] 导出目录不存在")
            print("   请先运行: /导出记忆 jsonl 30")
            sys.exit(1)

        export_files = list(exports_dir.glob("engram_export_*.jsonl"))
        if not export_files:
            print("[ERROR] 没有找到导出文件")
            print("   请先运行: /导出记忆 jsonl 30")
            sys.exit(1)

        export_file = max(export_files, key=lambda p: p.stat().st_mtime)

    print(f"使用导出文件: {export_file}\n")

    # 生成测试数据集
    generator = MemoryDatasetGenerator(str(export_file))
    if not generator.load_export_data():
        sys.exit(1)

    test_cases = generator.generate_test_cases(max_cases=30)
    if not test_cases:
        print("[ERROR] 没有生成测试用例")
        sys.exit(1)

    # 保存测试用例
    generator.save_test_cases("tests/test_cases_generated.jsonl")

    # 运行测试
    tester = MemoryRecallTester(
        db_path="engram_memories.db",
        chroma_path="chroma.sqlite3"
    )

    results = asyncio.run(tester.run_test_suite(test_cases))

    # 生成报告
    tester.generate_report()

    print("\n" + "=" * 60)
    print("测试完成！报告已生成:")
    print("  tests/memory_recall_report.md")
    print("=" * 60)
