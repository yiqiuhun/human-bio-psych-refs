#!/usr/bin/env python3
"""rank_eval.py —— 排序质量评估与调参（让权重不再靠手感）。

三步闭环：
    1) python rank_eval.py --build-pool     # 抓候选池 → references/eval-pool.json
    2) 人工标注：在 references/eval-labels.json 里为每个查询列出相关条目的标题
    3) python rank_eval.py --eval           # 算 nDCG@10
       python rank_eval.py --tune           # 坐标上升调权重 → references/ranking-weights.json

为什么必须这样做：`lit_find.py` 的排序权重原本是三次试调的结果，
换个主题就可能错，而整个工具的可信度都挂在这个公式上。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lit_find as L  # noqa: E402
import ref_check as rc  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
POOL = ROOT / "references" / "eval-pool.json"
LABELS = ROOT / "references" / "eval-labels.json"
WEIGHTS = ROOT / "references" / "ranking-weights.json"

# 覆盖不同证据形态的查询：营养补剂 / 运动 / 睡眠 / 心理 / 临床 / 已被推翻的主题
QUERIES = [
    "creatine supplementation muscle strength resistance training",
    "caffeine ergogenic effect endurance performance",
    "protein intake older adults muscle mass",
    "intermittent fasting weight loss adults",
    "omega-3 fatty acids depression",
    "resistance training bone density adolescents",
    "sleep restriction cognitive performance",
    "blue light melatonin suppression evening",
    "sleep duration all-cause mortality",
    "exercise depression adults meta-analysis",
    "mediterranean diet cardiovascular events",
    "vitamin d supplementation respiratory infection",
    "mindfulness meditation anxiety students",
    "aerobic exercise hippocampal volume memory",
    "power posing hormones behavior",
]


class Args:
    """最小参数对象：lit_find 的取数与打分都读这些字段。"""

    def __init__(self, query: str, max_results: int = 20):
        self.query = query
        self.design = "all"
        self.min_year = ""
        self.human = False
        self.include_preprints = False
        self.include_supplements = False
        self.sort = ""
        self.source = "both"
        self.max_results = max_results
        self.min_overlap = 0.0
        self.min_title_overlap = 0.0
        self.min_citations = 0
        self.min_design_weight = 0.0
        self.refuted = "keep"


def build_pool() -> int:
    pools: dict[str, list[dict]] = {}
    for q in QUERIES:
        args = Args(q)
        state, recs = L.fetch_records(args)
        kept, seen = [], set()
        for rec in recs:
            if not rec.get("title"):
                continue
            key = rc.norm_title(rec["title"])
            if key in seen:
                continue
            seen.add(key)
            weight, label = L.design_of(rec.get("pubtypes", []), rec["title"])
            kept.append({
                "title": rec["title"],
                "year": rec.get("year", ""),
                "journal": rec.get("journal", ""),
                "cited": rec.get("cited", 0),
                "pubtypes": rec.get("pubtypes", []),
                "abstract": (rec.get("abstract") or "")[:1200],
                "design": label,
                "design_weight": weight,
                "pmid": rec.get("pmid", ""),
                "doi": rec.get("doi", ""),
            })
        pools[q] = kept
        print(f"  {q[:60]:<60} {state} {len(kept)} 条")
    POOL.write_text(json.dumps(pools, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"候选池已写入 {POOL}（共 {sum(len(v) for v in pools.values())} 条）")
    return 0


def load_pool() -> dict[str, list[dict]]:
    return json.loads(POOL.read_text(encoding="utf-8"))


def candidates_for(query: str, pools: dict[str, list[dict]], min_overlap: float = 0.6) -> list[dict]:
    """与人工标注时看到的列表完全一致：标题词重合 ≥0.6，按设计权重降序。

    标注文件里的下标就是这个列表的位置，所以这一步必须确定性可复现。
    """
    import re as _re
    terms = [t for t in _re.split(r"\W+", query.lower()) if len(t) > 3]
    rows = [
        r for r in pools.get(query, [])
        if terms and sum(1 for t in terms if t in r["title"].lower()) / len(terms) >= min_overlap
    ]
    rows.sort(key=lambda r: -r.get("design_weight", 0))
    for i, r in enumerate(rows):
        r["_pos"] = i
    return rows


def load_labels() -> dict[str, list[int]]:
    try:
        return json.loads(LABELS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def relevance(rec: dict, gold: list[int]) -> int:
    return 1 if rec.get("_pos") in gold else 0


def dcg(gains: list[int], k: int) -> float:
    import math
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains[:k]))


def ndcg_at_k(ranked: list[dict], gold: list[str], k: int = 10) -> float:
    gains = [relevance(r, gold) for r in ranked]
    ideal = sorted(gains, reverse=True)
    ideal_dcg = dcg(ideal, k)
    return 1.0 if ideal_dcg == 0 else dcg(gains, k) / ideal_dcg


def rank_with(pool: list[dict], query: str, w: dict) -> list[dict]:
    scored = []
    for rec in pool:
        weight = rec.get("design_weight", 2.5)
        s = L.score(rec, weight, query, w)
        if L.refuted_flag(rec):
            s -= w.get("refuted_penalty", 15.0)
        if L.is_counter_evidence(rec):
            s += w.get("counter_boost", 12.0)
        scored.append((s, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored]


def evaluate(w: dict | None = None, verbose: bool = True) -> tuple[float, dict[str, float]]:
    pools = load_pool()
    labels = load_labels()
    w = w or L.load_weights()
    scores: dict[str, float] = {}
    for q in pools:
        gold = labels.get(q, [])
        if not gold:
            continue
        cands = candidates_for(q, pools)
        ranked = rank_with(cands, q, w)
        scores[q] = ndcg_at_k(ranked, gold)
        if verbose:
            print(f"  nDCG@10 {scores[q]:.3f}  {q[:64]}")
    mean = sum(scores.values()) / len(scores) if scores else 0.0
    return mean, scores


def dump_candidates(query: str, limit: int) -> int:
    pools = load_pool()
    rows = candidates_for(query, pools)
    print(f"##### {query}  ({len(rows)} 条候选，显示前 {min(limit, len(rows))})")
    for r in rows[:limit]:
        print(f"{r['_pos']:2d}|{r['design']}|{r['year']}|{r['title'][:100]}")
    return 0


def tune() -> int:
    # 只调有标注信号支撑的维度。counter_boost / refuted_penalty 冻结：
    # 标注集里没有被推翻主题的样本，调它们等于用噪声改参数。
    grid = {
        "title": [20.0, 30.0, 40.0, 55.0, 70.0],
        "abstract": [0.0, 3.0, 5.0, 10.0],
        "design": [2.5, 5.0, 8.0, 12.0, 16.0],
        "cited": [0.0033, 0.0067, 0.0133],   # 留地板：被引不能被压成 0，否则经典文献出不来
        "year_fresh": [0.0, 1.0, 2.0, 3.0],
    }
    frozen = {k: L.load_weights()[k] for k in ("counter_boost", "refuted_penalty", "cited_cap", "year_recent", "year_mid")}
    best = dict(L.load_weights())
    # 起点必须是「有地板」的配置：历史上一次无约束调参把 cited 与 counter_boost 压成 0，
    # 那等于「不认经典文献」+「不给反证加权」，被这里强制拉回。
    best["cited"] = max(best.get("cited", 0.0), 0.0033)
    best["counter_boost"] = max(best.get("counter_boost", 0.0), 12.0)
    frozen["counter_boost"] = best["counter_boost"]
    best_mean, _ = evaluate(best, verbose=False)
    print(f"起始 nDCG@10 = {best_mean:.4f}")
    improved = True
    rounds = 0
    while improved and rounds < 8:
        improved = False
        rounds += 1
        for key, values in grid.items():
            current = best.get(key, 0.0)
            for v in values:
                if abs(v - current) < 1e-9:
                    continue
                cand = dict(best)
                cand[key] = v
                mean, _ = evaluate(cand, verbose=False)
                if mean > best_mean + 1e-4:
                    best, best_mean = cand, mean
                    improved = True
            if best.get(key, 0.0) != current:
                print(f"  round {rounds}: {key} {current} → {best[key]}  (nDCG {best_mean:.4f})")
    best.update(frozen)  # 冻结项回填，防止被顺手改掉
    WEIGHTS.write_text(json.dumps(best, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"最终 nDCG@10 = {best_mean:.4f}；权重已写入 {WEIGHTS}")
    return 0


def leave_one_out() -> int:
    """换权重是否真的变好：逐条对比默认与当前配置，数改善/变差条数。"""
    pools = load_pool()
    labels = load_labels()
    cur = L.load_weights()
    base = dict(cur)
    base.update({"title": 40.0, "abstract": 5.0, "design": 5.0, "cited": 0.0067,
                 "year_fresh": 2.0, "year_recent": 1.5, "year_mid": 1.0})
    better = worse = same = 0
    for q, gold in labels.items():
        if not gold:
            continue
        cands = candidates_for(q, pools)
        a = ndcg_at_k(rank_with(cands, q, base), gold)
        b = ndcg_at_k(rank_with(cands, q, cur), gold)
        mark = "+" if b > a + 1e-6 else ("-" if b < a - 1e-6 else "=")
        if mark == "+":
            better += 1
        elif mark == "-":
            worse += 1
        else:
            same += 1
        print(f"  {mark} {a:.3f} → {b:.3f}  {q[:60]}")
    print(f"\n改善 {better} / 变差 {worse} / 持平 {same}")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="排序质量评估与调参")
    ap.add_argument("--build-pool", action="store_true", dest="build_pool")
    ap.add_argument("--dump-candidates", default="", metavar="QUERY",
                    help="打印某条查询的候选列表（标注用，下标即 labels 里要写的数字）")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--tune", action="store_true")
    ap.add_argument("--loo", action="store_true", help="逐条对比默认与当前权重的 nDCG")
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()

    if args.build_pool:
        return build_pool()
    if args.dump_candidates:
        return dump_candidates(args.dump_candidates, args.limit)
    if args.eval:
        mean, scores = evaluate()
        if not scores:
            print(f"[EMPTY] 还没有标注。先在 {LABELS} 里为每个查询列出相关标题。")
            return 1
        print(f"\n平均 nDCG@{args.k} = {mean:.4f}（{len(scores)} 个查询）")
        return 0
    if args.tune:
        return tune()
    if args.loo:
        return leave_one_out()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
