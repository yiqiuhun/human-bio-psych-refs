#!/usr/bin/env python3
"""文献核验与引用生成器（人体 / 医学 / 生物 / 心理）。

用法:
    python ref_check.py --doi 10.1016/S0140-6736(12)61031-9 --style all
    python ref_check.py --pmid 22818936 --abstract
    python ref_check.py --title "About sleep's role in memory" --style apa
    python ref_check.py --file refs.txt --style gb --json out.json --threads 2
    python ref_check.py --title "..." --index-row A --claim "低钠盐→卒中"
    python ref_check.py --file refs.txt --append-index "D:/SecondBrain_DSH/知识/证据引用索引.md" --bucket F
    python ref_check.py --file cn_titles.txt --cn          # 中文标题半自动

数据源（均免费、无需 API key）: Europe PMC / Crossref / OpenAlex。
输出状态:
    VERIFIED    已解析且标题匹配（>=0.80）
    PARTIAL     命中但缺 DOI/PMID，或标题匹配 0.55-0.80
    NOT_FOUND   各数据源明确返回 0 命中（可以认定不存在）
    UNVERIFIED  网络/服务错误，本次没验成（不等于不存在，禁止当“查无此文”）

退出码: 0 = 全部 VERIFIED/PARTIAL；1 = 存在 NOT_FOUND；2 = 存在 UNVERIFIED。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = "ref_check/2.0 (citation verification)"
TIMEOUT = 25
RETRIES = 2
CACHE_TTL = 24 * 3600
OFFLINE = False
MAILTO = ""

STATES = {"VERIFIED", "PARTIAL", "NOT_FOUND", "UNVERIFIED"}


# ------------------------------------------------------------------ plumbing
def cache_dir() -> Path:
    """缓存目录；任何一个候选目录写不了就退到系统临时目录，绝不因此报错。"""
    env = os.environ.get("REF_CHECK_CACHE")
    candidates = [Path(env)] if env else []
    candidates += [Path.home() / ".cache" / "ref_check", Path(tempfile.gettempdir()) / "ref_check"]
    for d in candidates:
        if not d.is_absolute():
            continue  # 相对路径会在 cwd 下建目录，污染工作区
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return d
        except OSError:
            continue
    return Path(tempfile.gettempdir())


def _cache_key(url: str) -> Path:
    return cache_dir() / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".json")


def cache_read(url: str) -> dict | None:
    p = _cache_key(url)
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - blob.get("ts", 0) > CACHE_TTL:
        return None
    return blob.get("data")


def cache_write(url: str, data: dict) -> None:
    try:
        _cache_key(url).write_text(json.dumps({"ts": time.time(), "data": data}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def fetch(url: str) -> tuple[str, dict | None]:
    """返回 (state, data)。state: ok | empty | error。

    error 与 empty 必须分开：网络错误不能当成“文献不存在”。
    """
    hit = cache_read(url)
    if hit is not None:
        return hit.get("state", "ok"), hit.get("data")
    if OFFLINE:
        return "error", None

    last = "error"
    for attempt in range(RETRIES + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            cache_write(url, {"state": "ok", "data": body})
            return "ok", body
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                cache_write(url, {"state": "empty", "data": None})
                return "empty", None
            last = "error"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            last = "error"
        if attempt < RETRIES:
            time.sleep(0.8 * 2**attempt)
    return last, None


def with_mailto(url: str) -> str:
    if not MAILTO:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}mailto={urllib.parse.quote(MAILTO)}"


# ------------------------------------------------------------------ helpers
def norm_doi(doi: str) -> str:
    doi = (doi or "").strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.I)
    return doi.strip().strip(".")


def norm_title(t: str) -> str:
    t = (t or "").lower()
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()


def initials(given: str) -> str:
    """"IM" -> "IM"；"I-Min" -> "IM"；"Eric J." -> "EJ"；"Felipe" -> "F"。"""
    g = (given or "").strip()
    if not g:
        return ""
    if re.fullmatch(r"[A-Z]{1,4}", g):
        return g
    parts = re.split(r"[\s\-\.]+", g)
    return "".join(p[0].upper() for p in parts if p and p[0].isalpha())[:4]


# ---------------------------------------------------------------- Europe PMC
CORRECTION_RE = re.compile(r"^\s*(correction|erratum|corrigendum|retraction|addendum|publisher'?s note)", re.I)


def is_correction(title: str) -> bool:
    return bool(CORRECTION_RE.match(title or ""))


def epmc_search(query: str) -> tuple[str, dict | None]:
    params = {"query": query, "format": "json", "pageSize": "10", "resultType": "core"}
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode(params)
    state, data = fetch(url)
    if state != "ok" or not data:
        return state, None
    results = data.get("resultList", {}).get("result", [])
    if not results:
        return "empty", None
    # 更正/勘误/撤稿通告优先排除：它们与原文标题几乎一样，最容易误引
    results.sort(key=lambda r: is_correction(r.get("title", "")))
    return "ok", results[0]


def epmc_to_rec(r: dict) -> dict:
    ji = r.get("journalInfo", {}) or {}
    journal = ji.get("journal", {}) or {}
    authors = []
    for a in (r.get("authorList", {}) or {}).get("author", []) or []:
        last = a.get("lastName") or a.get("collectiveName") or ""
        ini = initials(a.get("initials", ""))
        if not last:
            full = a.get("fullName", "")
            parts = full.split()
            last = parts[0] if parts else full
            ini = initials("".join(p[0] for p in parts[1:]))
        authors.append((last.strip(), ini))
    pubtypes = [p.lower() for p in ((r.get("pubTypeList") or {}).get("pubType") or [])]
    return {
        "source": "europepmc",
        "title": (r.get("title") or "").rstrip("."),
        "authors": authors,
        "journal": journal.get("medlineAbbreviation") or journal.get("title") or r.get("journalTitle", ""),
        "journal_full": journal.get("title", ""),
        "year": str(ji.get("yearOfPublication") or r.get("pubYear") or ""),
        "volume": str(ji.get("volume") or ""),
        "issue": str(ji.get("issue") or ""),
        "pages": str(r.get("pageInfo") or ""),
        "doi": norm_doi(r.get("doi") or ""),
        "pmid": str(r.get("pmid") or ""),
        "pubtypes": pubtypes,
        "abstract": re.sub(r"<[^>]+>", "", r.get("abstractText") or "").strip(),
        "is_preprint": any("preprint" in p for p in pubtypes) or r.get("source") == "PPR",
        "retracted": any("retract" in p for p in pubtypes),
    }


# ------------------------------------------------------------------ Crossref
def crossref_doi(doi: str) -> tuple[str, dict | None]:
    url = with_mailto("https://api.crossref.org/works/" + urllib.parse.quote(norm_doi(doi)))
    state, data = fetch(url)
    if state != "ok" or not data or data.get("status") != "ok":
        return state, None
    return "ok", crossref_to_rec(data["message"])


def crossref_to_rec(m: dict) -> dict:
    authors = []
    for a in m.get("author", []) or []:
        last = a.get("family") or a.get("name") or ""
        if last:
            authors.append((last, initials(a.get("given", ""))))
    date = (m.get("published") or m.get("issued") or {}).get("date-parts", [[None]])[0]
    return {
        "source": "crossref",
        "title": (m.get("title") or [""])[0].rstrip("."),
        "authors": authors,
        "journal": (m.get("short-container-title") or m.get("container-title") or [""])[0],
        "journal_full": (m.get("container-title") or [""])[0],
        "year": str(date[0] or ""),
        "volume": str(m.get("volume") or ""),
        "issue": str(m.get("issue") or ""),
        "pages": str(m.get("page") or ""),
        "doi": norm_doi(m.get("DOI") or ""),
        "pmid": "",
        "pubtypes": [m.get("type", "")],
        "abstract": re.sub(r"<[^>]+>", "", m.get("abstract") or "").strip(),
        "is_preprint": m.get("type") == "posted-content",
        "retracted": False,
    }


def crossref_title(title: str) -> tuple[str, dict | None]:
    params = {
        "query.bibliographic": title,
        "rows": "3",
        "select": "DOI,title,author,container-title,volume,issue,page,published,issued,type,abstract",
    }
    url = with_mailto("https://api.crossref.org/works?" + urllib.parse.urlencode(params))
    state, data = fetch(url)
    if state != "ok" or not data or data.get("status") != "ok":
        return state, None
    items = data.get("message", {}).get("items", [])
    if not items:
        return "empty", None
    items.sort(key=lambda m: is_correction((m.get("title") or [""])[0]))
    best = max(items, key=lambda m: sim(title, (m.get("title") or [""])[0]))
    return "ok", crossref_to_rec(best)


# ------------------------------------------------------------------- OpenAlex
def openalex_retracted(doi: str) -> bool | None:
    if not doi:
        return None
    url = with_mailto("https://api.openalex.org/works/doi:" + urllib.parse.quote(norm_doi(doi)))
    state, data = fetch(url)
    if state != "ok" or not data:
        return None
    return bool(data.get("is_retracted"))


# --------------------------------------------------------------- resolution
def resolve(doi: str = "", pmid: str = "", title: str = "") -> tuple[dict | None, str, list[str]]:
    """返回 (记录, 状态, 注记)。状态只能用 STATES 里的值。"""
    notes: list[str] = []
    rec: dict | None = None
    seen_empty = 0
    seen_error = 0
    doi_ok = False

    def note_state(state: str, ok_msg: str, empty_msg: str, err_msg: str) -> None:
        nonlocal seen_empty, seen_error
        if state == "ok":
            notes.append(ok_msg)
        elif state == "empty":
            seen_empty += 1
            notes.append(empty_msg)
        else:
            seen_error += 1
            notes.append(err_msg)

    if pmid:
        st, r = epmc_search(f"EXT_ID:{pmid} AND SRC:MED")
        if st == "ok" and r:
            rec = epmc_to_rec(r)
        note_state(st, f"Europe PMC 命中 PMID {pmid}", f"Europe PMC 无此 PMID {pmid}",
                   f"Europe PMC 访问失败（PMID {pmid} 未验）")

    if doi:
        edoi = norm_doi(doi)
        st, r = epmc_search(f'DOI:"{edoi}"')
        if st == "ok" and r:
            rec = rec or epmc_to_rec(r)
            notes.append("Europe PMC 按 DOI 命中")
        elif st == "error":
            seen_error += 1
        cb_st, cr = crossref_doi(edoi)
        if cb_st == "ok" and cr:
            doi_ok = True
            if rec is None:
                rec = cr
            else:
                notes.append(f"Crossref 已注册，标题匹配度 {sim(rec['title'], cr['title']):.2f}")
        elif cb_st == "empty":
            seen_empty += 1
            notes.append("Crossref 明确无此 DOI")
        else:
            seen_error += 1
            notes.append("Crossref 访问失败（DOI 未验）")

    if title and rec is None:
        st, r = epmc_search(f'TITLE:"{title}"')
        if st == "ok" and r:
            rec = epmc_to_rec(r)
            notes.append(f"Europe PMC 标题命中（匹配度 {sim(title, rec['title']):.2f}）")
        elif st == "empty":
            seen_empty += 1
        else:
            seen_error += 1
        if rec is None:
            cb_st, cr = crossref_title(title)
            if cb_st == "ok" and cr:
                rec = cr
                notes.append(f"Crossref 书目检索命中（匹配度 {sim(title, cr['title']):.2f}）")
            elif cb_st == "empty":
                seen_empty += 1
            else:
                seen_error += 1

    if rec is None:
        if seen_error:
            notes.append("未能完成核验（网络/服务错误）——不等于文献不存在，稍后重跑")
            return None, "UNVERIFIED", notes
        return None, "NOT_FOUND", notes

    if doi and rec.get("doi"):
        rec["doi"] = norm_doi(doi)

    ratio = sim(title, rec["title"]) if title else 1.0
    if title and ratio < 0.55:
        notes.append(f"标题严重不符（{ratio:.2f}）")
        return rec, "NOT_FOUND", notes

    if rec.get("retracted"):
        notes.append("注意：Europe PMC 标记为 RETRACTED")
    if openalex_retracted(rec.get("doi", "")) is True:
        rec["retracted"] = True
        notes.append("注意：OpenAlex 标记 is_retracted=true")

    if title and ratio < 0.80:
        return rec, "PARTIAL", notes
    if not doi_ok and not rec.get("pmid") and not rec.get("doi"):
        notes.append("缺 DOI 与 PMID")
        return rec, "PARTIAL", notes
    return rec, "VERIFIED", notes


# ------------------------------------------------------------------ formats
def authors_vancouver(a: list[tuple[str, str]]) -> str:
    names = [f"{l} {i}".strip() for l, i in a]
    if not names:
        return ""
    if len(names) <= 6:
        return ", ".join(names)
    return ", ".join(names[:6]) + ", et al"


def authors_apa(a: list[tuple[str, str]]) -> str:
    if not a:
        return ""
    fmt = [f"{l}, {'. '.join(i)}." if i else l for l, i in a]
    if len(fmt) == 1:
        return fmt[0]
    if len(fmt) <= 20:
        return ", ".join(fmt[:-1]) + ", & " + fmt[-1]
    return ", ".join(fmt[:19]) + ", ... " + fmt[-1]


def authors_gb(a: list[tuple[str, str]]) -> str:
    names = [f"{l} {i}".strip() for l, i in a]
    if not names:
        return ""
    if len(names) <= 3:
        return ", ".join(names)
    return ", ".join(names[:3]) + ", 等"


def pages_vancouver(p: str) -> str:
    m = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", p or "")
    if m and len(m.group(1)) == len(m.group(2)) and m.group(1)[:-2] == m.group(2)[:-2]:
        return f"{m.group(1)}-{m.group(2)[-2:]}"
    return p


def fmt_vancouver(r: dict) -> str:
    au = authors_vancouver(r["authors"])
    vol_issue = r["volume"] + (f"({r['issue']})" if r["issue"] else "")
    tail = r["year"]
    if vol_issue:
        tail += f";{vol_issue}"
    if r["pages"]:
        tail += f":{pages_vancouver(r['pages'])}"
    doi = f" doi:{r['doi']}" if r["doi"] else ""
    return f"{au}. {r['title']}. {r['journal']}. {tail}.{doi}".replace("..", ".")


def fmt_apa(r: dict) -> str:
    au = authors_apa(r["authors"])
    vol = r["volume"] + (f"({r['issue']})" if r["issue"] else "")
    page = r["pages"].replace("-", "–") if r["pages"] else ""
    tail = ", ".join(x for x in [vol, page] if x)
    link = f"https://doi.org/{r['doi']}" if r["doi"] else ""
    out = f"{au} ({r['year']}). {r['title']}. {r['journal_full'] or r['journal']}"
    if tail:
        out += f", {tail}"
    out += "."
    if link:
        out += f" {link}"
    return out


def fmt_gb(r: dict) -> str:
    au = authors_gb(r["authors"])
    vol_issue = r["volume"] + (f"({r['issue']})" if r["issue"] else "")
    body = f"{r['journal']}, {r['year']}"
    if vol_issue:
        body += f", {vol_issue}"
    if r["pages"]:
        body += f": {r['pages']}"
    doi = f" DOI:{r['doi']}." if r["doi"] else ""
    return f"{au}. {r['title']}[J]. {body}.{doi}"


def fmt_bibtex(r: dict) -> str:
    first = r["authors"][0][0].split()[0].lower() if r["authors"] else "anon"
    first = re.sub(r"[^a-z]", "", first) or "anon"
    word = re.sub(r"[^a-z]", "", (r["title"].split() or ["x"])[0].lower()) or "ref"
    authors = " and ".join(f"{l}, {i}" if i else l for l, i in r["authors"]) or "Unknown"
    lines = [
        f"@article{{{first}{r['year']}{word},",
        f"  author  = {{{authors}}},",
        f"  title   = {{{r['title']}}},",
        f"  journal = {{{r['journal_full'] or r['journal']}}},",
        f"  year    = {{{r['year']}}},",
    ]
    if r["volume"]:
        lines.append(f"  volume  = {{{r['volume']}}},")
    if r["issue"]:
        lines.append(f"  number  = {{{r['issue']}}},")
    if r["pages"]:
        lines.append(f"  pages   = {{{r['pages'].replace('-', '--')}}},")
    if r["doi"]:
        lines.append(f"  doi     = {{{r['doi']}}},")
    lines.append("}")
    return "\n".join(lines)


FORMATTERS = {"vancouver": fmt_vancouver, "apa": fmt_apa, "gb": fmt_gb, "bibtex": fmt_bibtex}


def index_row(rec: dict, bucket: str, claim: str, layer: str) -> str:
    """生成「证据引用索引」的行；bucket 为空时不带编号。"""
    cite = f"{authors_vancouver(rec['authors'])}. {rec['journal']} {rec['year']}"
    if rec["volume"]:
        cite += f";{rec['volume']}"
    if rec["pages"]:
        cite += f":{pages_vancouver(rec['pages'])}"
    cite += "."
    if rec.get("pmid"):
        cite += f" PMID {rec['pmid']}"
    if rec.get("doi"):
        cite += f". doi:{rec['doi']}"
    tag = f"{bucket}?" if bucket else "·"
    return f"| {tag} | {claim} | {cite} | {layer} | 待填人群边界/因果判断 |"


# ------------------------------------------------------------------ reporting
def report(rec: dict | None, status: str, notes: list[str], styles: list[str], show_abstract: bool) -> None:
    print(f"[{status}] " + (rec["title"][:90] if rec else "(无记录)"))
    if rec:
        meta = []
        if rec.get("pmid"):
            meta.append(f"PMID {rec['pmid']}")
        if rec.get("doi"):
            meta.append(f"DOI {rec['doi']}")
        if rec.get("retracted"):
            meta.append("!! RETRACTED")
        if rec.get("is_preprint"):
            meta.append("[preprint]")
        if meta:
            print("    " + " | ".join(meta))
    for n in notes:
        print("    - " + n)
    if rec and status != "NOT_FOUND":
        if show_abstract and rec.get("abstract"):
            print(f"    摘要: {rec['abstract'][:600]}\n")
        for s in styles:
            print(f"    {s}: {FORMATTERS[s](rec)}\n")


def parse_refs_file(path: str) -> list[tuple[str, str, str]]:
    jobs = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if re.match(r"^10\.\d{4,9}/\S+$", line):
                jobs.append((line, "", ""))
            elif re.match(r"^\d{5,9}$", line):
                jobs.append(("", line, ""))
            else:
                jobs.append(("", "", line))
    return jobs


def log_negative(rec, status: str, query: dict, notes: list[str]) -> None:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": status, "query": query, "notes": notes}
    try:
        with open(cache_dir() / "negatives.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def append_index(path: str, bucket: str, rows: list[str]) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    header = f"\n\n## {bucket} 桶（ref_check 自动追加）\n\n| # | 论断 | 文献 | 分层 | 用法判断 |\n|---|---|---|---|---|\n"
    if f"## {bucket} 桶" not in text:
        text += header
    text = text.rstrip() + "\n" + "\n".join(rows) + "\n"
    p.write_text(text, encoding="utf-8")
    print(f"已追加 {len(rows)} 行到 {p}（{bucket} 桶）")


def main() -> int:
    global OFFLINE, MAILTO
    ap = argparse.ArgumentParser(description="人体/医学/心理文献核验与引用生成")
    ap.add_argument("--doi", default="")
    ap.add_argument("--pmid", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--file", default="")
    ap.add_argument("--style", default="vancouver", choices=["vancouver", "apa", "gb", "bibtex", "all"])
    ap.add_argument("--json", default="")
    ap.add_argument("--abstract", action="store_true", help="打印摘要前 600 字（用于“结论对位”）")
    ap.add_argument("--threads", type=int, default=2, help="--file 并发数，默认 2（对公共 API 友好）")
    ap.add_argument("--offline", action="store_true", help="只用本地缓存，不发网络请求")
    ap.add_argument("--mailto", default="", help="Crossref/OpenAlex 礼貌池邮箱")
    ap.add_argument("--index-row", dest="index_row", default="", metavar="BUCKET", help="输出索引行，BUCKET 如 A/F")
    ap.add_argument("--append-index", dest="append_index", default="", help="把索引行追加到该 md 文件")
    ap.add_argument("--bucket", default="", help="配合 --append-index，指定桶字母")
    ap.add_argument("--claim", default="", help="索引行里的论断短语")
    ap.add_argument("--layer", default="【待分层】", help="索引行里的证据分层")
    ap.add_argument("--cn", action="store_true", help="中文文献半自动：只生成待填模板，不走网络")
    args = ap.parse_args()

    OFFLINE = args.offline
    MAILTO = args.mailto
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    styles = ["vancouver", "apa", "gb"] if args.style == "all" else [args.style]

    if args.file:
        jobs = parse_refs_file(args.file)
    elif args.doi or args.pmid or args.title:
        jobs = [(args.doi, args.pmid, args.title)]
    else:
        ap.error("至少给 --doi / --pmid / --title / --file 之一")
        return 2

    if args.cn:
        for _, _, title in jobs:
            print(f"| · | {title} | 【待录入：作者. 题名[J]. 刊名, 年, 卷(期): 页. DOI】 | 【待分层】 | 中文库无 API，人工补全后删掉本行标记 |")
        print(f"== {len(jobs)} 条待人工录入 ==")
        return 0

    def work(job: tuple[str, str, str]):
        doi, pmid, title = job
        return job, resolve(norm_doi(doi), pmid, title)

    if len(jobs) > 1 and args.threads > 1:
        with ThreadPoolExecutor(max_workers=max(1, args.threads)) as pool:
            results = list(pool.map(work, jobs))
    else:
        results = [work(j) for j in jobs]

    out = []
    rows = []
    bad = 0
    unverified = 0
    for (doi, pmid, title), (rec, status, notes) in results:
        if status == "NOT_FOUND":
            bad += 1
        if status == "UNVERIFIED":
            unverified += 1
        report(rec, status, notes, styles, args.abstract)
        if status in {"NOT_FOUND", "UNVERIFIED"}:
            log_negative(rec, status, {"doi": doi, "pmid": pmid, "title": title}, notes)
        if rec and args.index_row:
            claim = args.claim or rec["title"][:40]
            rows.append(index_row(rec, args.index_row, claim, args.layer))
        out.append({"query": {"doi": doi, "pmid": pmid, "title": title}, "status": status, "record": rec, "notes": notes})

    if args.append_index and rows:
        append_index(args.append_index, args.bucket or args.index_row or "X", rows)
    elif rows:
        for r in rows:
            print(r)

    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 写入 {args.json}")

    print(f"== 共 {len(jobs)} 条；未找到 {bad}；未验成 {unverified} ==")
    return 0 if (bad == 0 and unverified == 0) else (1 if bad else 2)


if __name__ == "__main__":
    sys.exit(main())
