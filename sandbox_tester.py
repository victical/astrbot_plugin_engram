import asyncio
import datetime
import os
import re
import sqlite3
import sys
from collections import defaultdict
from typing import List, Dict, Any, Optional

import chromadb
from openai import AsyncOpenAI


DB_FILENAME = "engram_memories.db"
EXPORT_TXT_FILENAME = "engram_export_1622251059_20260218_235306.txt"
TARGET_USER_ID = "1622251059"  # 留空则自动嗅探日总结最多的用户
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")
API_KEY = os.getenv("OPENAI_API_KEY", "sk-auacjevxhuowhhcjmvfooalvbzwenfnijqoeswdfseisxbcb")
MODEL_NAME = os.getenv("OPENAI_MODEL", "deepseek-ai/DeepSeek-V3.2")
LIMIT = int(os.getenv("ENGRAM_LIMIT", "7"))
OFFSET_DAYS = int(os.getenv("ENGRAM_OFFSET_DAYS", "7"))
DEFAULT_QUERY = os.getenv("ENGRAM_QUERY", "我上周干了什么？")
EXPORT_TXT_PATH = os.getenv("ENGRAM_EXPORT_TXT", "")
USE_EXPORT_ONLY = os.getenv("ENGRAM_USE_EXPORT_ONLY", "1").lower() in {"1", "true", "yes"}
RUN_FOLDING = os.getenv("ENGRAM_RUN_FOLDING", "1").lower() in {"1", "true", "yes"}
RUN_RETRIEVAL = os.getenv("ENGRAM_RUN_RETRIEVAL", "1").lower() in {"1", "true", "yes"}

PROMPT_TEMPLATE = (
    "请从以下【daily_summary 列表】提炼内容。需要提炼：\n\n"
    "核心事件：影响未来的重要节点，简短描述。\n\n"
    "关系演进：描述互动深度的变化。\n\n"
    "记忆胶囊：一句话概括本周期最重要的信息。\n"
    "【daily_summary 列表】：\n{memory_texts}"
)


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _ensure_utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="ignore")
    except Exception:
        pass


def _resolve_db_path() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, DB_FILENAME)


def _resolve_export_path() -> str:
    if EXPORT_TXT_PATH:
        return EXPORT_TXT_PATH
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, EXPORT_TXT_FILENAME)


async def get_top_user(db_path: str) -> Optional[str]:
    _section("USER SNIFFING")
    if not os.path.exists(db_path):
        print("[ERROR] 数据库文件不存在。")
        return None

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id, COUNT(*) as c "
            "FROM memoryindex "
            "WHERE source_type = 'daily_summary' "
            "GROUP BY user_id "
            "ORDER BY c DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if not row:
            print("[WARN] 未找到任何 daily_summary 用户。")
            return None
        user_id, count = row
        print(f"Top User     : {user_id}")
        print(f"Daily Count  : {count}")
        return user_id
    finally:
        conn.close()


async def extract_data(
    db_path: str,
    user_id: str,
    source_type: str = "daily_summary",
    limit: int = 7,
    offset_days: int = 7,
) -> List[Dict[str, Any]]:
    _section("DATA EXTRACTION")
    print(f"DB Path      : {db_path}")
    print(f"User ID      : {user_id}")
    print(f"Source Type  : {source_type}")
    print(f"Limit        : {limit}")
    print(f"Offset Days  : {offset_days}")

    rows: List[Dict[str, Any]] = []
    if not os.path.exists(db_path):
        print("[ERROR] 数据库文件不存在，无法抽取数据。")
        return rows

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT summary, created_at "
            "FROM memoryindex "
            "WHERE user_id = ? AND source_type = ? "
            "AND created_at < datetime('now', '-' || ? || ' days') "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, source_type, offset_days, limit),
        )
        result = cursor.fetchall()
        rows = [
            {"summary": r[0], "created_at": r[1]}
            for r in result
            if r and r[0]
        ]
    finally:
        conn.close()

    if not rows:
        print("[WARN] 未获取到任何 summary。")
        print("[HINT] 可调整 ENGRAM_OFFSET_DAYS 或 ENGRAM_LIMIT 重新尝试。")
        export_rows = _extract_from_export_txt(limit=limit, offset_days=offset_days)
        if export_rows:
            print("[INFO] 已从导出 TXT 构造 daily_summary 数据。")
            return export_rows
        return rows

    rows.reverse()
    print(f"Fetched      : {len(rows)} summaries (chronological)")
    return rows


def _extract_from_export_txt(limit: int, offset_days: int) -> List[Dict[str, Any]]:
    export_path = _resolve_export_path()
    _section("EXPORT FALLBACK")
    print(f"Export Path  : {export_path}")

    if not os.path.exists(export_path):
        print("[WARN] 导出 TXT 不存在，无法构造 summary。")
        return []

    line_pattern = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\] (.*?): (.*)$")
    day_bucket: Dict[str, List[str]] = defaultdict(list)

    with open(export_path, "r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            match = line_pattern.match(line)
            if not match:
                continue
            date_str, time_str, sender, content = match.groups()
            if not content:
                continue
            day_bucket[date_str].append(f"{sender}: {content}")

    if not day_bucket:
        print("[WARN] 导出 TXT 未解析到有效对话行。")
        return []

    cutoff = datetime.datetime.now() - datetime.timedelta(days=offset_days)
    selected_days = [
        day for day in sorted(day_bucket.keys())
        if datetime.datetime.strptime(day, "%Y-%m-%d") < cutoff
    ]

    if not selected_days:
        print("[WARN] 导出 TXT 中没有符合 offset_days 的日期，改用最新日期段。")
        selected_days = sorted(day_bucket.keys())

    selected_days = selected_days[-limit:]
    summaries: List[Dict[str, Any]] = []
    for day in selected_days:
        messages = day_bucket[day]
        full_text = " | ".join(messages)
        summary = full_text if full_text else "当日对话记录为空"
        summaries.append({"summary": summary, "created_at": f"{day} 00:00:00"})

    print(f"Fallback     : {len(summaries)} summaries from export TXT")
    return summaries


async def simulate_folding(
    summaries: List[Dict[str, Any]],
) -> str:
    _section("LLM PROMPT")

    if not summaries:
        print("[WARN] summaries 为空，跳过 LLM 调用。")
        _section("LLM RESULT")
        print("<EMPTY>")
        return ""

    memory_texts = "\n".join(
        [f"- [{s['created_at']}] {s['summary']}" for s in summaries]
    )
    prompt = PROMPT_TEMPLATE.format(memory_texts=memory_texts)
    print(prompt)

    _section("LLM RESULT")

    if not API_KEY:
        print("[ERROR] API_KEY 为空，无法调用 LLM。")
        print("<EMPTY>")
        return ""

    result_text = ""
    try:
        client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        if response and response.choices:
            result_text = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        err_msg = str(exc)
        print(f"[ERROR] LLM 调用失败：{err_msg}")
        print(f"[DEBUG] BASE_URL={BASE_URL} MODEL_NAME={MODEL_NAME}")
        if "blocked" in err_msg.lower():
            print("[HINT] 服务端内容安全或账号策略拦截了该请求。请检查服务端控制台/限额/模型权限。")
        result_text = ""

    if result_text:
        print(result_text)
    else:
        print("<EMPTY>")

    return result_text


def _bm25_score(query_keywords: List[str], document: str) -> float:
    k1 = 1.2
    b = 0.75
    avg_doc_len = 80

    doc_lower = document.lower()
    doc_len = max(1, len(doc_lower))
    score = 0.0

    for keyword in query_keywords:
        if keyword in doc_lower:
            tf = doc_lower.count(keyword)
            norm_tf = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / avg_doc_len))
            keyword_weight = max(1.0, min(3.0, len(keyword) / 2.0))
            score += norm_tf * keyword_weight

    return score


async def simulate_retrieval(
    weekly_summary: str,
    daily_summaries: List[Dict[str, Any]],
) -> None:
    _section("RETRIEVAL SANDBOX")
    if DEFAULT_QUERY:
        print(f"[INFO] 使用默认检索词：{DEFAULT_QUERY}")
        query = DEFAULT_QUERY
    else:
        try:
            query = input("请输入测试检索词: ").strip()
        except EOFError:
            print("[WARN] 未检测到交互输入，终止检索。")
            return
        if not query:
            print("[WARN] 检索词为空，终止检索。")
            return
    print(f"[INFO] 执行检索: query={query}")

    client = chromadb.EphemeralClient()
    collection = client.get_or_create_collection(name="engram_sandbox")

    documents: List[str] = []
    metadatas: List[Dict[str, Any]] = []
    ids: List[str] = []

    weekly_doc = weekly_summary.strip() if weekly_summary else "（周总结为空）"
    documents.append(weekly_doc)
    metadatas.append({"source_type": "weekly_summary"})
    ids.append("weekly_1")

    for idx, summary in enumerate(daily_summaries, start=1):
        documents.append(summary["summary"])
        metadatas.append({"source_type": "daily_summary", "created_at": summary["created_at"]})
        ids.append(f"daily_{idx}")

    if not documents:
        print("[WARN] 没有可供检索的文档。")
        return

    collection.add(ids=ids, documents=documents, metadatas=metadatas)

    results = collection.query(
        query_texts=[query],
        n_results=min(10, len(documents)),
        include=["documents", "distances", "metadatas"],
    )

    result_docs = results.get("documents", [[]])[0]
    result_distances = results.get("distances", [[]])[0]
    result_metas = results.get("metadatas", [[]])[0]

    if not result_docs:
        print("[WARN] ChromaDB 返回空结果。")
        return

    query_keywords = [k.lower() for k in re.split(r"[^\w]+", query) if k.strip()]

    items: List[Dict[str, Any]] = []
    for i, doc in enumerate(result_docs):
        distance = result_distances[i] if i < len(result_distances) else float("inf")
        metadata = result_metas[i] if i < len(result_metas) else {}
        keyword_score = _bm25_score(query_keywords, doc or "")
        items.append(
            {
                "id": ids[i] if i < len(ids) else f"doc_{i+1}",
                "document": doc or "",
                "distance": float(distance) if distance is not None else float("inf"),
                "keyword_score": float(keyword_score),
                "metadata": metadata,
            }
        )

    vector_rank = {
        item_index: rank + 1
        for rank, item_index in enumerate(
            sorted(range(len(items)), key=lambda idx: items[idx]["distance"])
        )
    }
    keyword_rank = {
        item_index: rank + 1
        for rank, item_index in enumerate(
            sorted(range(len(items)), key=lambda idx: items[idx]["keyword_score"], reverse=True)
        )
    }

    for i, item in enumerate(items):
        v_rank = vector_rank[i]
        k_rank = keyword_rank[i]
        rrf_score = (0.5 / (60 + v_rank)) + (0.5 / (60 + k_rank))
        item["rrf_score"] = rrf_score
        item["vector_rank"] = v_rank
        item["keyword_rank"] = k_rank

    items.sort(key=lambda x: x["rrf_score"], reverse=True)

    top_item = items[0] if items else None
    if top_item:
        top_source = top_item.get("metadata", {}).get("source_type", "unknown")
        print(
            f"[INFO] Top1 => id={top_item['id']} source={top_source} "
            f"distance={top_item['distance']:.4f} keyword={top_item['keyword_score']:.4f} "
            f"rrf={top_item['rrf_score']:.6f}"
        )

    _section("RRF RESULT PANEL")
    header = "ID | Distance | Keyword | RRF | Source"
    print(header)
    print("-" * len(header))
    for item in items:
        source = item.get("metadata", {}).get("source_type", "unknown")
        print(
            f"{item['id']:<8} | "
            f"{item['distance']:.4f} | "
            f"{item['keyword_score']:.4f} | "
            f"{item['rrf_score']:.6f} | "
            f"{source}"
        )
        print(f"  {item['document']}")


async def main() -> None:
    _ensure_utf8_stdout()
    db_path = _resolve_db_path()
    user_id = TARGET_USER_ID.strip() if TARGET_USER_ID else ""

    if not user_id:
        user_id = await get_top_user(db_path) or ""

    if not user_id:
        print("[ERROR] 无可用用户，终止测试。")
        return

    if USE_EXPORT_ONLY:
        _section("EXPORT ONLY MODE")
        summaries = _extract_from_export_txt(limit=LIMIT, offset_days=OFFSET_DAYS)
        if not summaries:
            summaries = _extract_from_export_txt(limit=LIMIT, offset_days=0)
    else:
        summaries = await extract_data(
            db_path=db_path,
            user_id=user_id,
            source_type="daily_summary",
            limit=LIMIT,
            offset_days=OFFSET_DAYS,
        )

    weekly_summary = ""
    if RUN_FOLDING:
        weekly_summary = await simulate_folding(summaries=summaries)
    else:
        _section("LLM RESULT")
        print("[SKIP] ENGRAM_RUN_FOLDING=0，已跳过周总结。")

    if RUN_RETRIEVAL:
        await simulate_retrieval(
            weekly_summary=weekly_summary,
            daily_summaries=summaries,
        )
    else:
        _section("RETRIEVAL SANDBOX")
        print("[SKIP] ENGRAM_RUN_RETRIEVAL=0，已跳过检索测试。")


if __name__ == "__main__":
    asyncio.run(main())
