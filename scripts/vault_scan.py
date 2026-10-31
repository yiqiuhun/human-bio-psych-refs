#!/usr/bin/env python3
r"""扫描第二大脑，产出「证据补强清单」并做索引体检。

  1. 统计 `知识/证据引用索引.md` 各桶条目数
  2. 扫 `知识/` 全文（按段落/表格行切块，带最近标题）找「带数字但无出处」的断言
  3. 按文件名/标题猜建议桶（A营养 B脑科学 C康复 D健身 E感官学习 F生存医学）
  4. 读「已处理登记」跳过前几轮已结的条目（闭环）
  5. `--dup` 体检索引：论断近似重复 + DOI 重复

用法：
    python vault_scan.py                       # 扫库 → 写清单
    python vault_scan.py --top 40 --dry
    python vault_scan.py --dup                 # 只做索引体检
    python vault_scan.py --mark "01-生存医学.md|低钠盐"   # 登记已处理
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import re
import sys
import tempfile
from pathlib import Path

BUCKET_RE = re.compile(r"^##\s*([A-Z])\s*桶")
ROW_RE = re.compile(r"^\|\s*([A-Z]\d+(?:-[\u4e00-\u9fff]+)?)\s*\|")
STAT_RE = re.compile(
    r"(\d[\d.,]*\s*%|\bRR\b|\bHR\b|\bOR\b|95%\s*CI|≥?\s*\d+(\.\d+)?\s*(g|mg|μg|mcg|IU)\b|"
    r"\d+(\.\d+)?\s*(小时|分钟|天|周|岁|次)\b|p\s*[<>=]|\bd\s*=\s*\d)"
)
CITE_RE = re.compile(
    r"(PMID|DOI|doi\.org|10\.\d{4,9}/|[A-Z][a-z]+\s+(19|20)\d\d|"
    r"Cochrane|JAMA|BMJ|NEJM|Lancet|PNAS|WHO|IOM|NASEM|ACSM|ACOG|ISSN|中国营养学会|EFSA)"
)
CODE_FENCE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{2,4})\s+(.*)$")

# 这些文件不是「知识正文」，而是索引/目录/调研台账；扫了只会报假缺口
SKIP_FILES = ("域索引.md", "证据引用索引.md", "证据补强清单", "证据补强-已处理.md",
              "证据补强-清账", "抖音调研分布.md", "资产负债表.md", "跨域碰撞.md")
SKIP_BLOCK_MARKERS = ("原书章节", "核心问题", "→ [[知识/", "[[知识/", "[[_AI/",
                      "|---", "下一节", "返回总纲",
                      # 抖音精读台账：元数据与互动数不是生理/心理断言
                      "元数据：", "digg=", "转写约",
                      # 思维模型库的定义句：模型是思维工具，不是实验结论
                      "口诀：", "**本质：")

BUCKET_HINTS = [
    ("A", ("营养", "补剂", "饮食", "蛋白", "纤维", "钠", "维生", "热量", "碳水", "咖啡", "酒精")),
    ("B", ("脑", "认知", "学习", "记忆", "注意", "睡眠科学", "压力", "情绪")),
    ("C", ("康复", "体态", "颈", "腰", "踝", "膝", "拉伸", "伤病")),
    ("D", ("健身", "训练", "力量", "有氧", "肌肉", "增肌", "减脂", "跑步")),
    ("E", ("阅读", "笔记", "听", "音乐", "屏幕", "感官")),
    ("F", ("生存", "急救", "筛查", "疫苗", "用药", "外因死亡", "戒烟", "戒酒", "常备药")),
]


# ------------------------------------------------------------------ helpers
def strip_code(text: str) -> list[tuple[int, str]]:
    out, fence = [], False
    for i, line in enumerate(text.splitlines(), 1):
        if CODE_FENCE.match(line):
            fence = not fence
            continue
        if not fence:
            out.append((i, line))
    return out


def blocks_with_context(text: str) -> list[tuple[str, int, str]]:
    """按空行切块，带最近标题；返回 (heading, 起始行号, 块文本)。"""
    out: list[tuple[str, int, str]] = []
    heading = ""
    buf: list[str] = []
    start = 0

    def flush() -> None:
        nonlocal buf
        if buf:
            out.append((heading, start, "\n".join(buf)))
            buf = []

    for ln, line in strip_code(text):
        m = HEADING_RE.match(line)
        if m:
            flush()
            heading = m.group(2).strip()
            continue
        if not line.strip():
            flush()
            continue
        if not buf:
            start = ln
        buf.append(line)
    flush()
    return out


def guess_bucket(name: str, heading: str = "") -> str:
    """先看文件名（更可靠），再看标题。"""
    for hay in (name, heading):
        for bucket, keys in BUCKET_HINTS:
            if any(k in hay for k in keys):
                return bucket
    return "?"


def bucket_stats(index_path: Path) -> tuple[dict[str, int], int]:
    if not index_path.exists():
        return {}, 0
    counts: dict[str, int] = {}
    cur = None
    for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = BUCKET_RE.match(line)
        if m:
            cur = m.group(1)
            counts.setdefault(cur, 0)
            continue
        if line.startswith("## "):
            cur = None
            continue
        if cur and ROW_RE.match(line):
            counts[cur] += 1
    return counts, sum(counts.values())


def load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            done.add(line)
    return done


def done_key(rel: str, text: str) -> str:
    return f"{rel}|{re.sub(r'\\s+', ' ', text)[:40]}"


def scan_gaps(files: list[Path], brain: Path, done: set[str], limit: int) -> list[tuple[str, int, str, str]]:
    gaps = []
    for p in files:
        if any(s in p.name for s in SKIP_FILES):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(p.relative_to(brain))
        for heading, ln, block in blocks_with_context(text):
            flat = re.sub(r"\s+", " ", block).strip()
            if len(flat) < 12 or CITE_RE.search(block) or not STAT_RE.search(block):
                continue
            if any(mk in block for mk in SKIP_BLOCK_MARKERS):
                continue  # 链接行/目录行/表格头：不是知识断言
            if done_key(rel, flat) in done:
                continue
            bucket = guess_bucket(rel, heading)
            gaps.append((rel, ln, flat[:200], bucket))
    gaps.sort(key=lambda g: ("高性价比人生指南" not in g[0], g[0], g[1]))
    return gaps[:limit]


def dup_check(index_path: Path, threshold: float = 0.82) -> list[str]:
    """索引体检：论断近似重复 + DOI 重复。"""
    if not index_path.exists():
        return ["索引文件不存在"]
    rows = []
    for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ROW_RE.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        rows.append({"id": cells[0], "claim": cells[1], "cite": cells[2]})

    findings = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            r = difflib.SequenceMatcher(None, rows[i]["claim"], rows[j]["claim"]).ratio()
            if r >= threshold:
                findings.append(f"近似重复论断 {r:.2f}：{rows[i]['id']} 与 {rows[j]['id']} —— {rows[i]['claim'][:30]} / {rows[j]['claim'][:30]}")

    seen: dict[str, str] = {}
    for r in rows:
        for doi in re.findall(r"10\.\d{4,9}/[^\s;)]+", r["cite"], flags=re.I):
            key = doi.lower().rstrip(".")
            if key in seen:
                findings.append(f"DOI 重复：{seen[key]} 与 {r['id']} 同为 {key}")
            else:
                seen[key] = r["id"]

    return findings or [f"体检通过：{len(rows)} 行，无论断近似重复、无 DOI 重复"]


# --------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="第二大脑证据补强扫描 + 索引体检")
    ap.add_argument("--brain", default=r"D:\SecondBrain_DSH\知识")
    ap.add_argument("--out", default="")
    ap.add_argument("--out-name", default="", dest="out_name",
                    help="输出文件名（仅 ASCII，落在 <brain>/知识/ 下；中文路径走命令行参数会乱码）")
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--dup", action="store_true", help="只做索引体检")
    ap.add_argument("--mark", default="", help='登记已处理，格式 "相对路径|文本片段"')
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    brain = Path(args.brain)
    if not brain.is_dir():
        print(f"找不到知识目录：{brain}", file=sys.stderr)
        return 1
    index = brain / "证据引用索引.md"
    done_file = brain / "证据补强-已处理.md"

    if args.mark:
        try:
            with open(done_file, "a", encoding="utf-8") as fh:
                fh.write(args.mark.strip() + "\n")
            print(f"已登记：{args.mark}")
        except PermissionError:
            # 本机 vault 对 python 只读：把该追加的行原样吐出，由外部写回。
            print(f"[!] 登记表写不进（vault 对 python 只读），请追加此行到 {done_file.name}：")
            print(args.mark.strip())
        return 0

    if args.dup:
        for f in dup_check(index):
            print(f)
        return 0

    counts, total = bucket_stats(index)
    done = load_done(done_file)
    mds = sorted(brain.rglob("*.md"))
    gaps = scan_gaps(mds, brain, done, args.top)
    today = dt.date.today().isoformat()

    lines = [
        f"# 证据补强清单 · {today}",
        "",
        "> 由 `skills/human-bio-psych-refs/scripts/vault_scan.py` 自动生成。",
        "> 处理完一条，在该行末尾打 ✅ 并在 `知识/证据补强-已处理.md` 加一行 `<路径>|<文本片段>`；下次扫描会自动跳过。",
        "> 发现能用 Europe PMC 查到的 → `ref_check.py --index-row <桶>` → 粘贴进「证据引用索引」；查不到 → 降成定性表述或标【无充分文献】。",
        "",
        "## 一、引用层现状",
        "",
        "| 桶 | 条目数 |",
        "|---|---|",
    ]
    for k in sorted(counts):
        lines.append(f"| {k} 桶 | {counts[k]} |")
    lines += [f"| **合计** | **{total}** |", ""]

    dup_findings = dup_check(index)
    lines += ["## 二、索引体检", ""]
    lines += [f"- {f}" for f in dup_findings]
    lines += [""]

    lines += [
        "## 三、待补证候选（带数字但无出处）",
        "",
        f"共扫 {len(mds)} 份笔记；已跳过历史已处理 {len(done)} 条；列出前 {len(gaps)} 条。",
        "",
        "| # | 建议桶 | 文件 | 行 | 文本片段 | 状态 |",
        "|---|---|---|---|---|---|",
    ]
    for i, (rel, ln, s, bucket) in enumerate(gaps, 1):
        safe = s.replace("|", "｜")
        lines.append(f"| {i} | {bucket} | {rel} | {ln} | {safe} | 待处理 |")

    report = "\n".join(lines) + "\n"
    # --out 走的是命令行参数：在 Git Bash / cmd 下中文路径传进来必乱码（GBK 解码），
    # 写入会落到一个不存在的乱码路径上。--out-name 只传 ASCII 文件名，目录由脚本拼。
    if args.out_name:
        base = brain if brain.name == "知识" else brain / "知识"
        out = base / args.out_name
    elif args.out:
        out = Path(args.out)
    else:
        out = brain / f"证据补强清单-{today}.md"

    print(f"桶统计：{counts} 合计 {total}；已处理登记 {len(done)}；待补证 {len(gaps)}")
    for f in dup_findings:
        print(f"  {f}")
    for i, (rel, ln, s, bucket) in enumerate(gaps[:10], 1):
        print(f"  {i}. [{bucket}] {Path(rel).name}:{ln}  {s[:70]}")

    if args.dry:
        print("（dry run，未写文件）")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.write_text(report, encoding="utf-8")
        print(f"已写入 {out}")
    except PermissionError:
        # 本机把整个 vault 对 python 进程设为只读（文件工具与 PowerShell 可写）。
        # 不在这里硬顶：落临时目录，并给出接力命令。
        fallback = Path(tempfile.gettempdir()) / out.name
        fallback.write_text(report, encoding="utf-8")
        print(f"[!] vault 对 python 进程只读，报告暂存 {fallback}")
        print(f"    搬运：Move-Item -LiteralPath '{fallback}' -Destination '{out}' -Force")
    return 0


if __name__ == "__main__":
    sys.exit(main())
