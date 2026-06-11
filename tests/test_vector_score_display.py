#!/usr/bin/env python3
"""
测试方案B：向量检索显示纯vector_score而非混合分数

直接修改 memory_manager.py 的临时副本进行测试
"""
import sys
import json


def analyze_vector_scores():
    """分析向量检索返回的分数"""

    print("\n" + "="*60)
    print("方案B测试：显示纯vector_score vs 当前混合分数")
    print("="*60)

    # 模拟三条检索结果
    test_results = [
        {
            "index_id": "b329b602",
            "summary": "用户询问小糯是否记得之前看的电影...",
            "distance": 0.95,  # ChromaDB 距离
            "keyword_score": 2.8,
            "recency_score": 0.7,
            "activity_score": 0.5,
        },
        {
            "index_id": "16f00a48",
            "summary": "用户指出小糯最早记忆并非1月11日...",
            "distance": 1.02,
            "keyword_score": 2.5,
            "recency_score": 0.65,
            "activity_score": 0.4,
        },
        {
            "index_id": "d1517b60",
            "summary": "用户通过affinity插件查询好感度...",
            "distance": 1.15,
            "keyword_score": 1.8,
            "recency_score": 0.9,
            "activity_score": 0.3,
        }
    ]

    # 配置：preference_fact 自动调整后的权重
    similarity_threshold = 1.275
    weight_vector = 0.30
    weight_keyword = 0.65
    weight_recency = 0.05
    weight_activity = 0.06
    total_weight = weight_vector + weight_keyword + weight_recency + weight_activity

    print(f"\n配置权重（preference_fact自动调整）：")
    print(f"  向量: {weight_vector:.2f} (30%)")
    print(f"  关键词: {weight_keyword:.2f} (65%)")
    print(f"  时间: {weight_recency:.2f} (5%)")
    print(f"  活跃度: {weight_activity:.2f} (6%)")

    # 归一化关键词分数
    max_keyword = max(r['keyword_score'] for r in test_results)
    for r in test_results:
        r['keyword_score_norm'] = r['keyword_score'] / max_keyword

        # 计算 vector_score
        r['vector_score'] = max(0.0, min(1.0, 1 - r['distance'] / similarity_threshold))

        # 当前的混合分数
        r['rank_score'] = (
            weight_vector * r['vector_score'] +
            weight_keyword * r['keyword_score_norm'] +
            weight_recency * r['recency_score'] +
            weight_activity * r['activity_score']
        ) / total_weight

        # RRF 模式的额外调整
        quality_factor = max(0.0, 1.5 - r['distance']) / 1.5
        raw_percent = r['rank_score'] / test_results[0]['rank_score'] * 100
        r['current_display'] = int(raw_percent * quality_factor)

        # 方案B：纯vector_score
        r['plan_b_display'] = int(r['vector_score'] * 100)

    print("\n" + "-"*60)
    for i, r in enumerate(test_results, 1):
        print(f"\n记忆 #{i} ({r['index_id'][:8]})")
        print(f"  距离: {r['distance']:.3f}")
        print(f"  向量分: {r['vector_score']:.3f}")
        print(f"  关键词分(归一化): {r['keyword_score_norm']:.3f}")
        print(f"  混合分: {r['rank_score']:.3f}")
        print(f"  ──────────────────────────")
        print(f"  当前显示: {r['current_display']}%  ← 混合+quality_factor")
        print(f"  方案B显示: {r['plan_b_display']}%  ← 纯向量相似度")
        print(f"  差异: {r['plan_b_display'] - r['current_display']:+d}%")

    print("\n" + "="*60)
    print("结论：")
    print("  当前显示：受关键词权重(65%)主导，第一条只有36%")
    print("  方案B显示：纯向量相似度，第一条有25%")
    print("  问题：为什么向量相似度本身就这么低？")
    print("="*60 + "\n")


if __name__ == "__main__":
    analyze_vector_scores()
