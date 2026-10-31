#!/usr/bin/env python3
"""lit_find.py —— 按主题自动检索文献（human-bio-psych-refs 第二步的自动化版）。

用途：手上只有一句命题（"间歇性禁食对青少年体脂的影响"），要立刻拿到
真实存在、可引用的候选文献，按研究设计分级排序，而不是凭记忆编。

用法：
    python lit_find.py "intermittent fasting adolescent body fat"
    python lit_find.py "creatine strength" --design rct --min-year 2015
    python lit_find.py "sleep deprivation memory consolidation" --design meta
    python lit_find.py "维生素D 抑郁" --cn          # 中文主题：给英文检索式 + CNKI 句式
    python lit_find.py "omega-3 depression" --json out.json
    python lit_find.py "omega-3 depression" --index-row A --claim "n-3 对抑郁症状有小效应"

退出码：0 = 有候选；1 = 无候选；2 = 网络未验成（不等于没有文献）
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ref_check as rc  # noqa: E402  复用缓存/超时/记录解析，不重复造轮子

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
OPENALEX = "https://api.openalex.org/works"
DROPPED: list[str] = []   # 本轮因「已知被推翻」剔掉的条数说明，供 main 打印

# 设计层级 → (权重, 中文标签)。数字只用于排序，不是证据质量评分。
DESIGN = [
    (re.compile(r"meta-?analysis", re.I), 10, "Meta分析"),
    (re.compile(r"systematic review", re.I), 9, "系统综述"),
    (re.compile(r"practice guideline|guideline|consensus|position paper|recommendation", re.I), 8.5, "指南/共识"),
    (re.compile(r"randomized controlled trial|randomised controlled trial|randomized|clinical trial, phase (ii|iii)", re.I), 8, "RCT"),
    (re.compile(r"cohort|prospective|longitudinal|follow-up study", re.I), 6, "队列"),
    (re.compile(r"case-control", re.I), 5, "病例对照"),
    (re.compile(r"cross-sectional|survey|questionnaire", re.I), 4, "横断面"),
    (re.compile(r"review", re.I), 3, "综述"),
    (re.compile(r"\b(in vitro|cell line|cell culture|mice|mouse|rats?|animals?)\b", re.I), 1.5, "动物/体外"),
]

# 设计过滤用的 Europe PMC 检索片段
DESIGN_FILTER = {
    "meta": '(PUB_TYPE:"Meta-Analysis" OR PUB_TYPE:"Systematic Review")',
    "sysrev": 'PUB_TYPE:"Systematic Review"',
    "rct": '(PUB_TYPE:"Randomized Controlled Trial" OR PUB_TYPE:"Clinical Trial")',
    "cohort": '(PUB_TYPE:"Observational Study" OR TITLE:"cohort")',
    "guideline": '(PUB_TYPE:"Practice Guideline" OR PUB_TYPE:"Guideline" OR TITLE:"guideline")',
}

# A 级期刊白名单（顶刊/顶刊级/权威指南来源）。只用于给读者提示，不替代判读。
TIER_A = {
    "nature", "science", "cell", "the lancet", "lancet", "new england journal of medicine",
    "n engl j med", "jama", "bmj", "annals of internal medicine", "ann intern med",
    "circulation", "journal of the american college of cardiology", "j am coll cardiol",
    "nature medicine", "nat med", "nature neuroscience", "nat neurosci", "gut",
    "diabetes care", "sleep", "the bmj", "jama internal medicine", "jama psychiatry",
    "lancet neurology", "nature human behaviour", "psychological bulletin",
    "american journal of clinical nutrition", "am j clin nutr", "british journal of sports medicine",
    "br j sports med", "sports medicine", "journal of applied physiology",
    "cochrane database of systematic reviews",
}


def design_of(pubtypes: list[str], title: str) -> tuple[float, str]:
    """按出版类型优先、标题兜底判断研究设计。"""
    blob = " ".join(pubtypes) + " " + (title or "")
    for pattern, weight, label in DESIGN:
        if pattern.search(blob):
            return weight, label
    if any("clinical trial" in p for p in pubtypes):
        return 8, "RCT"
    return 2.5, "原始研究"


def tier_letter(rec: dict, design_weight: float) -> str:
    """证据等级提示：A 顶刊/指南，B 专业期刊，C 机制/弱设计。"""
    journal = (rec.get("journal") or "").lower().strip()
    if design_weight >= 8.5:
        return "A"
    if journal in TIER_A:
        return "A"
    if design_weight <= 1.5:
        return "C"
    return "B"


# ---------------------------------------------------------------- 证据分级
# 设计 → 层级（CEBM 风格）。数字答「设计有多硬」，字母答「能不能当结论用」。
LEVEL_OF_DESIGN = {
    "Meta分析": "1a",
    "系统综述": "1a",
    "指南/共识": "1a",
    "RCT": "1b",
    "队列": "2b",
    "病例对照": "3b",
    "横断面": "4",
    "综述": "5",
    "动物/体外": "6",
    "原始研究": "2b",
}

POPULATION_WORDS = (
    "children", "adolescents", "adolescent", "youth", "students", "schoolchildren",
    "adults", "older adults", "elderly", "young adults", "men", "women", "males", "females",
    "athletes", "players", "runners", "patients", "survivors", "pregnant women",
    "infants", "neonates", "toddlers", "workers", "nurses", "physicians", "doctors",
    "participants", "subjects", "volunteers", "mice", "rats",
)

_N_PATTERNS = [
    re.compile(r"\b[nN]\s*=\s*([\d][\d,]{1,})"),
    re.compile(
        r"\b(\d{1,3}(?:,\d{3})+|\d{2,6})\s+(?:[A-Za-z\-]+\s+){0,3}(?:"
        + "|".join(POPULATION_WORDS)
        + r")\b",
        re.I,
    ),
]
_AGE_RANGE_RE = re.compile(r"aged?\s*(\d{1,2})\s*(?:-|–|to|~)\s*(\d{1,2})", re.I)
_AGE_MEAN_RE = re.compile(r"mean age\s*(?:of|:)?\s*(\d{1,2}(?:\.\d)?)", re.I)


def extract_population_n(rec: dict) -> tuple[str, str]:
    """从标题+摘要抽「人群」与「样本量」。抽不到就写未标明——不猜。"""
    text = f"{rec.get('title', '')}. {rec.get('abstract', '')}"
    low = text.lower()

    pops = [w for w in POPULATION_WORDS if re.search(rf"\b{re.escape(w)}\b", low)]
    pops = [p for p in pops if not any(p != q and p in q for q in pops)]
    pop = ", ".join(dict.fromkeys(pops[:2])) if pops else "未标明"

    age = ""
    m = _AGE_RANGE_RE.search(text)
    if m:
        age = f"{m.group(1)}-{m.group(2)} 岁"
    else:
        m = _AGE_MEAN_RE.search(text)
        if m:
            age = f"均值 {m.group(1)} 岁"

    n = ""
    cands: list[int] = []
    for rx in _N_PATTERNS:
        for h in rx.findall(text):
            if not h:
                continue
            try:
                cands.append(int(h.replace(",", "")))
            except ValueError:
                pass
    # 人体研究的样本量下限：<20 的多是组数、剂量组、页码误抓
    big = [c for c in cands if c >= 20]
    if big:
        n = f"N={max(big):,}"

    bits = [pop]
    if age:
        bits.append(age)
    if n:
        bits.append(n)
    return " · ".join(bits), (n or "未标")


def grade_of(rec: dict, design: str) -> str:
    """等级 = 设计层级 + 确定性字母（A 可直接用 / B 需限定 / C 只能当机制）。"""
    level = LEVEL_OF_DESIGN.get(design, "2b")
    top = (rec.get("journal") or "").lower().strip() in TIER_A
    if level == "1a":
        return "1a/A"
    if level == "1b":
        return "1b/A" if top else "1b/B"
    if level in ("2b", "3b"):
        return f"{level}/B"
    return f"{level}/C"


# 已知被推翻或大幅缩水的主张：只做提示，命中即标 ⚠️ 并降权
REFUTED_TOPICS = (
    ("power posing", "强势姿势改变激素/风险偏好：重复失败，效应近零"),
    ("ego depletion", "自我损耗（意志力像电池）：多实验室重复未获支持"),
    ("facial feedback", "面部反馈假说：元分析后效应极小"),
    ("growth mindset", "成长型思维干预：学业效应很小、异质性高"),
    ("stereotype threat", "刻板印象威胁：重复研究效应大幅缩水"),
    ("implicit bias", "内隐偏见训练：改分数不改行为"),
    ("sugar hyperactivity", "吃糖致儿童多动：多项 RCT 否定"),
    ("msg headache", "味精致头痛：双盲试验未能重复"),
    ("night light myopia", "夜间灯光致近视：后续队列削弱"),
    ("vitamin d depression", "维生素 D 改善抑郁：RCT 多为阴性"),
    ("multivitamin cognition", "复合维生素改善认知：证据不一致、效应极小"),
    ("detox", "排毒/清体：无生理机制与临床证据"),
)


def refuted_flag(rec: dict) -> str:
    blob = f"{rec.get('title', '')} {rec.get('abstract', '')}".lower()
    for kw, note in REFUTED_TOPICS:
        if kw in blob:
            return note
    return ""


def build_query(args) -> str:
    q = args.query.strip()
    parts = [f"({q})"]
    if not args.include_preprints:
        parts.append("(SRC:MED OR SRC:PMC)")
    if args.design and args.design != "all":
        frag = DESIGN_FILTER.get(args.design)
        if frag:
            parts.append(frag)
    if args.min_year:
        parts.append(f"(FIRST_PDATE:[{args.min_year}-01-01 TO 2100-12-31])")
    if args.human:
        parts.append('(("humans"[MeSH Terms]) OR PUB_TYPE:"Review")')
    return " AND ".join(parts)


SUPPLEMENT_RE = re.compile(r"(^|\D)\d{1,2}\s*S\b|suppl|supplement|abstracts?\s+of|conference", re.I)


def is_supplement_abstract(rec: dict) -> bool:
    """增刊/会议摘要：卷期带 9S / Suppl，或刊物名含 Supplement。

    这类条目看起来和正式论文一样有卷期页，最容易混进参考文献表；
    它们没有完整方法学，不能当原始研究引。
    """
    blob = f"{rec.get('issue') or ''} {rec.get('journal') or ''}"
    return bool(SUPPLEMENT_RE.search(blob))


def openalex_search(text: str, args, sort: str) -> tuple[str, list[dict]]:
    """OpenAlex：覆盖面比 Europe PMC 广（含非 MEDLINE 期刊），自带被引数与摘要倒排表。"""
    params = {
        "search": text,
        "per-page": str(min(args.max_results * 4, 100)),
        "sort": sort or "relevance_score:desc",
    }
    filters = ["type:article|review|preprint"]
    if args.min_year:
        filters.append(f"from_publication_date:{args.min_year}-01-01")
    params["filter"] = ",".join(filters)
    params["mailto"] = "lit_find@example.com"
    url = OPENALEX + "?" + urllib.parse.urlencode(params)
    state, data = rc.fetch(url)
    if state != "ok" or not data:
        return state, []
    return "ok", data.get("results") or []


def _abstract_from_inverted(inv: dict | None) -> str:
    """OpenAlex 用 {词: [位置]} 表示摘要，按位置还原成文本。"""
    if not inv:
        return ""
    slots: dict[int, str] = {}
    for word, positions in inv.items():
        for p in positions or []:
            slots[p] = word
    return " ".join(slots[i] for i in sorted(slots))


def openalex_to_rec(w: dict) -> dict:
    src = ((w.get("primary_location") or {}).get("source") or {})
    biblio = w.get("biblio") or {}
    ids = w.get("ids") or {}
    pmid = ""
    m = re.search(r"(\d{5,9})", ids.get("pmid") or "")
    if m:
        pmid = m.group(1)
    authors = []
    for au in (w.get("authorships") or []):
        name = ((au.get("author") or {}).get("display_name") or "").strip()
        if not name:
            continue
        parts = name.split()
        authors.append((parts[-1], "".join(p[0] for p in parts[:-1])[:4]))
    wtype = (w.get("type") or "").lower()
    return {
        "source": "openalex",
        "title": html.unescape(re.sub(r"<[^>]+>", "", w.get("title") or "")),
        "authors": authors,
        "journal": src.get("display_name") or "",
        "journal_full": src.get("display_name") or "",
        "year": str(w.get("publication_year") or ""),
        "volume": str(biblio.get("volume") or ""),
        "issue": str(biblio.get("issue") or ""),
        "pages": (f"{biblio.get('first_page')}-{biblio.get('last_page')}"
                  if biblio.get("first_page") and biblio.get("last_page")
                  else str(biblio.get("first_page") or "")),
        "doi": rc.norm_doi(w.get("doi") or ""),
        "pmid": pmid,
        "pubtypes": [wtype] if wtype else [],
        "abstract": _abstract_from_inverted(w.get("abstract_inverted_index")),
        "is_preprint": wtype == "preprint",
        "retracted": bool(w.get("is_retracted")),
        "cited": int(w.get("cited_by_count") or 0),
    }


def search(query: str, page_size: int, sort: str) -> tuple[str, list[dict]]:
    params = {
        "query": query,
        "format": "json",
        "pageSize": str(page_size),
        "resultType": "core",
    }
    if sort:
        params["sort"] = sort
    url = EPMC + "?" + urllib.parse.urlencode(params)
    state, data = rc.fetch(url)
    if state != "ok" or not data:
        return state, []
    results = (data.get("resultList") or {}).get("result") or []
    return "ok", results


def load_weights() -> dict:
    """排序权重从配置文件读；读不到就用内置默认值（默认值来自标注集调参，见 references/ranking-weights.json）。"""
    defaults = {
        "design": 5.0,
        "title": 40.0,
        "abstract": 5.0,
        "cited": 0.0067,   # 被引上限按 400 折算
        "cited_cap": 400,
        "year_fresh": 2.0,
        "year_recent": 1.5,
        "year_mid": 1.0,
        "refuted_penalty": 15.0,
        "counter_boost": 12.0,   # 反证论文（重复失败/Meta 否定）加权
    }
    path = Path(__file__).resolve().parents[1] / "references" / "ranking-weights.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        defaults.update({k: float(v) for k, v in loaded.items() if k in defaults})
    except (OSError, ValueError):
        pass
    return defaults


WEIGHTS = load_weights()


# 反证论文的信号：它们不是「该主题成立」的证据，而是「该主题被否掉」的证据
COUNTER_RE = re.compile(
    r"fail(ed|ure)?\s+to\s+replicat|replicat(ion|ed)\s+(fail|attempt)|"
    r"could\s+not\s+replicat|no\s+evidence\s+(for|that)|multisite\s+replicat|"
    r"same[- ]lab|registered\s+replication|meta-analy(sis|tic).*(null|no effect)|"
    r"overestimat|publication bias|reanalysis",
    re.I,
)


def is_counter_evidence(rec: dict) -> bool:
    return bool(COUNTER_RE.search(f"{rec.get('title','')} {rec.get('abstract','')}"))


def score(rec: dict, weight: float, query: str, w: dict | None = None) -> float:
    """标题命中词为主，设计权重次之，被引与年份只做微调。

    为什么标题压过被引：查 "creatine ... muscle strength" 时，一篇 800 被引的
    蛋白质综述在词语重合上看着很近，但它回答的不是这个问题。
    """
    w = w or WEIGHTS
    s = weight * w["design"]
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 3]
    title = (rec.get("title") or "").lower()
    abstract = (rec.get("abstract") or "").lower()
    if terms:
        s += w["title"] * sum(1 for t in terms if t in title) / len(terms)
        s += w["abstract"] * sum(1 for t in terms if t in abstract) / len(terms)
    s += min(float(rec.get("cited", 0) or 0), w["cited_cap"]) * w["cited"]
    if (rec.get("journal") or "").lower().strip() in TIER_A:
        s += w.get("top_journal_boost", 8.0)  # 顶刊加权：同等相关性时优先引顶刊
    try:
        year = int(rec.get("year") or 0)
    except ValueError:
        year = 0
    if year >= 2020:
        s += w["year_fresh"]
    elif year >= 2015:
        s += w["year_recent"]
    elif year >= 2010:
        s += w["year_mid"]
    return s


def fetch_records(args) -> tuple[str, list[dict]]:
    """只负责取原始记录（双源、双排序），不做过滤与排序——标注池与在线检索共用这一段。"""
    query = build_query(args)
    states: list[str] = []
    recs: list[dict] = []

    if args.source in ("epmc", "both"):
        for sort in ([args.sort] if args.sort else ["", "CITED desc"]):
            state, chunk = search(query, args.max_results * 3, sort)
            states.append(state)
            for r in chunk:
                rec = rc.epmc_to_rec(r)
                rec["title"] = html.unescape(re.sub(r"<[^>]+>", "", r.get("title") or ""))
                rec["cited"] = int(r.get("citedByCount") or 0)
                recs.append(rec)

    if args.source in ("openalex", "both"):
        for sort in ([args.sort] if args.sort else ["", "cited_by_count:desc"]):
            state, chunk = openalex_search(args.query, args, sort)
            states.append(state)
            recs.extend(openalex_to_rec(w) for w in chunk)

    if "ok" not in states:
        return next(s for s in states if s != "ok"), []
    return "ok", recs


def collect(args) -> tuple[str, list[dict]]:
    query = build_query(args)
    state, recs = fetch_records(args)
    if state != "ok":
        return state, []

    terms = [t for t in re.split(r"\W+", args.query.lower()) if len(t) > 3]
    seen_doi: set[str] = set()
    seen_title: set[str] = set()
    out: list[dict] = []
    dropped: list[str] = []
    for rec in recs:
        if not rec["title"] or rc.is_correction(rec["title"]):
            continue
        # 撤稿/更正通告：标题带前缀，或出版类型标了撤回
        if re.match(r"^\s*(retracted|retraction|withdrawn|expression of concern)", rec["title"], re.I):
            continue
        if rec["is_preprint"] and not args.include_preprints:
            continue
        if rec["retracted"]:
            continue
        if is_supplement_abstract(rec) and not args.include_supplements:
            continue
        doi = (rec.get("doi") or "").lower()
        key = rc.norm_title(rec["title"])
        # 两站同一篇：DOI 或规范化标题命中即视为重复，保留先到的那条
        if (doi and doi in seen_doi) or (not doi and key in seen_title):
            continue
        if doi:
            seen_doi.add(doi)
        seen_title.add(key)
        # 相关性闸：查过的词至少半数出现在标题/摘要，否则是碰巧同刊的邻题文献
        haystack = (rec["title"] + " " + rec["abstract"]).lower()
        if terms:
            if sum(1 for t in terms if t in haystack) / len(terms) < args.min_overlap:
                continue
            # 标题闸更严：摘要里提一句不算回答这个问题（COVID 心理健康综述曾靠摘要擦边混进来）
            title_hits = sum(1 for t in terms if t in rec["title"].lower()) / len(terms)
            if title_hits < args.min_title_overlap:
                continue
        if rec.get("cited", 0) < args.min_citations:
            continue
        weight, label = design_of(rec["pubtypes"], rec["title"])
        if weight < args.min_design_weight:
            continue
        rec["design"] = label
        rec["design_weight"] = weight
        rec["tier"] = tier_letter(rec, weight)
        rec["grade"] = grade_of(rec, label)
        rec["population"], rec["n"] = extract_population_n(rec)
        rec["refuted"] = refuted_flag(rec)
        # 反证论文（重复失败/否定性 Meta）必须留：删掉它等于把「这主题被推翻」的证据一起删了
        rec["counter"] = is_counter_evidence(rec)
        if rec["refuted"] and args.refuted == "drop" and not rec["counter"]:
            dropped.append(rec["refuted"])
            continue
        rec["score"] = round(score(rec, weight, args.query), 2)
        if rec["refuted"] and not rec["counter"]:
            rec["score"] -= WEIGHTS["refuted_penalty"]  # 有失败重复的主题：排到后面但不消失
        if rec["counter"]:
            rec["score"] += WEIGHTS["counter_boost"]
        out.append(rec)

    out.sort(key=lambda x: x["score"], reverse=True)
    DROPPED.clear()
    DROPPED.extend(dropped)
    return "ok", out[: args.max_results]


def show(query: str, recs: list[dict], show_abstract: bool) -> None:
    print(f"检索式: {query}")
    print(f"命中: {len(recs)} 条（已排除更正/勘误/撤稿/预印本/增刊摘要）\n")
    print(f"{'#':>2} {'年':>4} {'等级':<6} {'设计':<8} {'人群·N':<28} {'被引':>5}  标题")
    for i, r in enumerate(recs, 1):
        head = r["title"][:64]
        flag = "⚠️" if r.get("refuted") else "  "
        pop = (r.get("population") or "未标明")
        if len(pop) > 30:
            pop = pop[:30].rsplit(" ", 1)[0] + "…"
        print(f"{i:>2} {r['year']:>4} {r['grade']:<6} {r['design']:<8} {pop:<31} {r['cited']:>5}  {flag}{head}")
        ids = []
        if r["pmid"]:
            ids.append(f"PMID {r['pmid']}")
        if r["doi"]:
            ids.append(f"DOI {r['doi']}")
        print(f"   {' · '.join(ids)}  | {r['journal']} {r['year']};{r['volume']}({r['issue']}):{r['pages']}")
        if r.get("refuted"):
            print(f"   ⚠️ 该主题有失败重复/被推翻：{r['refuted']}")
        if show_abstract and r["abstract"]:
            print(f"   {r['abstract'][:300]}...")
        print()


def cn_hint(query: str) -> None:
    print("中文库（ref_check 不覆盖，需人工搜）：")
    print(f"  CNKI 高级检索：SU=('{query}') AND SU=('随机对照' OR 'Meta分析')")
    print(f"  万方：主题:('{query}') * 期刊论文 * 近 10 年")
    print("  英文式同上，用 lit_find 的结果优先——中文库同一主题多为一手重复，优先级低于 SCI。")


def write_md(path: str, query: str, args, recs: list[dict]) -> None:
    """导出「文献包」：可直接贴进第二大脑或交给别人核对。"""
    lines = [
        "# 文献包" , "",
        f"- 检索命题：`{args.query}`",
        f"- 检索式：`{query}`",
        f"- 数据源：{args.source}；设计过滤：{args.design}；年份下限：{args.min_year or '不限'}",
        f"- 结果：{len(recs)} 条（已排除更正/勘误/撤稿/预印本/增刊会议摘要）",
        "",
        "> 读法：等级 `层级/字母`——1a 系统综述·Meta，1b RCT，2b 队列，3b 病例对照，4 横断面，5 叙述综述，6 机制/动物。",
        "> 字母 A 可直接用于结论，B 必须带人群限定，C 只能当机制解释。人群·N 来自摘要自述，缺失即写「未标明」。",
        "",
        "| # | 年 | 等级 | 设计 | 人群 · 样本量 | 被引 | 标题 | 期刊 | PMID | DOI |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(recs, 1):
        t = r["title"].replace("|", "｜")
        flag = "⚠️ " if r.get("refuted") else ""
        lines.append(
            f"| {i} | {r['year']} | {r['grade']} | {r['design']} | {r.get('population','未标明')} | "
            f"{r['cited']} | {flag}{t} | {r['journal']} | {r['pmid'] or '—'} | {r['doi'] or '—'} |"
        )
    lines += ["", "## 逐条", ""]
    for i, r in enumerate(recs, 1):
        cites = rc.fmt_vancouver(r)
        lines.append(f"### {i}. {r['title']}")
        lines.append("")
        lines.append(f"- 引用：{cites}")
        lines.append(f"- 判读：{r['grade']} · {r['design']} · {r['year']} · {r['journal']}")
        lines.append(f"- 人群与样本量：{r.get('population','未标明')}（摘要自述；未标明不等于小样本）")
        if r.get("refuted"):
            lines.append(f"- ⚠️ 该主题有失败重复/被推翻：{r['refuted']}")
        if r["abstract"]:
            lines.append(f"- 摘要：{r['abstract'][:600]}")
        lines.append("")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[MD] 文献包已写出 {path}")


def main() -> int:
    # Windows 控制台按 GBK 解码时，中文与 ·/— 会乱码；统一走 UTF-8，写不了就降级替换。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="按主题检索真实文献并分级排序")
    ap.add_argument("query", help="英文命题/关键词（中文请配 --cn）")
    ap.add_argument("--design", default="all",
                    choices=["all", "meta", "sysrev", "rct", "cohort", "guideline"])
    ap.add_argument("--min-design-weight", type=float, default=0.0,
                    help="低于该设计权重丢弃（meta=10 / rct=8 / 队列=6 / 横断面=4 / 综述=3 / 动物=1.5）")
    ap.add_argument("--min-overlap", type=float, default=0.5,
                    help="检索词在标题/摘要中的最小重合率（默认 0.5，滤掉邻题噪声）")
    ap.add_argument("--min-title-overlap", type=float, default=0.34, dest="min_title_overlap",
                    help="检索词在标题中的最小重合率（默认 1/3；放宽到 0 只靠摘要判断）")
    ap.add_argument("--refuted", default="flag", choices=["flag", "drop", "keep"],
                    help="对已知被推翻/缩水的主题：flag=标注并降权（默认）/ drop=剔除（反证论文仍保留）/ keep=不管")
    ap.add_argument("--min-year", default="", help="只看该年之后的文献，如 2015")
    ap.add_argument("--max-results", type=int, default=10)
    ap.add_argument("--sort", default="",
                    help="Europe PMC 排序：留空=相关性；可选 'CITED desc' / 'P_PDATE_D desc'")
    ap.add_argument("--human", action="store_true", help="限定人类研究（MeSH humans）")
    ap.add_argument("--include-preprints", action="store_true")
    ap.add_argument("--include-supplements", action="store_true",
                    help="保留增刊/会议摘要（默认剔除：它们没有完整方法学）")
    ap.add_argument("--abstract", action="store_true")
    ap.add_argument("--cn", action="store_true", help="附中文库检索式")
    ap.add_argument("--source", default="both", choices=["both", "epmc", "openalex"],
                    help="数据源：epmc（生物医学）/ openalex（覆盖最广，含被引数）/ both（默认）")
    ap.add_argument("--min-citations", type=int, default=0,
                    help="最低被引数；查经典用 50+，查最新用 0")
    ap.add_argument("--md", default="", help="导出 Markdown 文献包到该路径（只传 ASCII 文件名）")
    ap.add_argument("--json", default="")
    ap.add_argument("--index-row", default="", metavar="BUCKET",
                    help="按 A/B/C/D/E/F 桶生成入库行（需 --claim）")
    ap.add_argument("--claim", default="", help="该文献支持的那句断言")
    ap.add_argument("--layer", default="", help="证据等级行，缺省按设计/期刊自动填")
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    rc.OFFLINE = args.offline
    query = build_query(args)
    state, recs = collect(args)

    if state != "ok":
        print(f"[UNVERIFIED] 网络未连通（离线或检索失败），不代表没有文献。稍后重跑。\n检索式: {query}")
        return 2
    if not recs:
        print(f"[EMPTY] 无候选。放宽 --min-year / --design，或换英文同义词。\n检索式: {query}")
        return 1

    show(query, recs, args.abstract)
    if DROPPED:
        uniq = sorted(set(DROPPED))
        print(f"[已剔除] {len(DROPPED)} 条落在已知被推翻/缩水主题上：")
        for note in uniq[:4]:
            print(f"  - {note}")
        print("  想看正反两方用 --refuted flag（标注并降权，不删除）。")
    if args.cn:
        print()
        cn_hint(args.query)

    if args.md:
        try:
            write_md(args.md, query, args, recs)
        except PermissionError:
            import tempfile
            alt = Path(tempfile.gettempdir()) / Path(args.md).name
            write_md(str(alt), query, args, recs)
            print(f"[!] 目标目录对 python 只读，已暂存 {alt}（用文件工具搬运）")

    if args.json:
        Path(args.json).write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[JSON] 已写入 {args.json}")

    if args.index_row:
        claim = args.claim or "[待填：这句话要支持的断言]"
        rows = []
        for r in recs[:3]:
            layer = args.layer or f"【{r['tier']}级·{r['design']}】"
            rows.append(rc.index_row(r, args.index_row, claim, layer))
        print("\n可选入库行（挑 1 条，核过再贴进 证据引用索引.md）：")
        for row in rows:
            print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
