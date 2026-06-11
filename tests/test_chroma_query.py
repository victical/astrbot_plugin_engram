#!/usr/bin/env python3
"""直接测试ChromaDB检索"""
import chromadb
from openai import OpenAI

# ChromaDB 配置
chroma_client = chromadb.PersistentClient(path="E:/AI/shouban/astrbot_plugin_engram")
collection = chroma_client.get_collection(name="long_term_memories")

# OpenAI 配置
client = OpenAI(
    base_url="https://router.tumuer.me/v1",
    api_key="sk-v2d522PocckMsK8RDljtGCjdvwa1r93cKL18I0tmEAS61QAU"
)

# 生成查询向量
query = "小糯，我喜欢什么水果"
print(f"查询: {query}\n")

response = client.embeddings.create(
    input=query,
    model="text-embedding-3-large"
)
query_embedding = response.data[0].embedding

# 执行检索（返回top 10）
results = collection.query(
    query_embeddings=[query_embedding],
    n_results=10
)

print(f"召回 {len(results['ids'][0])} 条记忆:\n")

# 检查水果相关记忆是否在结果中
fruit_ids = [
    '5ad89a15-15f5-485e-ae44-90e75588968e',
    '8a3f32da-66a3-4870-b888-c421c43fedd7',
    'b3e64d13-e76a-4f6d-a772-d5e27b6a65e2'
]

for i, (idx, distance, doc) in enumerate(zip(results['ids'][0], results['distances'][0], results['documents'][0]), 1):
    is_fruit = "[水果]" if idx in fruit_ids else ""
    score = (1 - distance / 1.275) * 100  # 转换为百分比
    print(f"{i}. {is_fruit} 距离={distance:.3f} 相似度={score:.1f}%")
    print(f"   ID: {idx[:8]}...")
    print(f"   摘要: {doc[:60]}...")
    print()

# 检查水果记忆是否存在于collection
print("\n检查水果记忆是否在ChromaDB中:")
for fruit_id in fruit_ids:
    try:
        result = collection.get(ids=[fruit_id])
        if result['ids']:
            print(f"[OK] {fruit_id[:8]}... 存在")
        else:
            print(f"[FAIL] {fruit_id[:8]}... 不存在")
    except Exception as e:
        print(f"[ERROR] {fruit_id[:8]}... 查询失败: {e}")
