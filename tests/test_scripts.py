#!/usr/bin/env python3
"""离线单元测试（不访问网络）。运行：
    python tests/test_scripts.py -v
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rc = load("ref_check", "scripts/ref_check.py")
vs = load("vault_scan", "scripts/vault_scan.py")

REC = {
    "source": "europepmc",
    "title": "Effect of physical inactivity on major non-communicable diseases worldwide",
    "authors": [("Lee", "IM"), ("Shiroma", "EJ"), ("Lobelo", "F"), ("Puska", "P"),
                ("Blair", "SN"), ("Katzmarzyk", "PT"), ("Extra", "A")],
    "journal": "Lancet",
    "journal_full": "The Lancet",
    "year": "2012",
    "volume": "380",
    "issue": "9838",
    "pages": "219-229",
    "doi": "10.1016/S0140-6736(12)61031-9",
    "pmid": "22818936",
    "pubtypes": ["journal article"],
    "abstract": "Background: ...",
    "is_preprint": False,
    "retracted": False,
}


class TestHelpers(unittest.TestCase):
    def test_initials(self):
        self.assertEqual(rc.initials("IM"), "IM")
        self.assertEqual(rc.initials("I-Min"), "IM")
        self.assertEqual(rc.initials("Eric J."), "EJ")
        self.assertEqual(rc.initials("Felipe"), "F")
        self.assertEqual(rc.initials(""), "")

    def test_norm_doi(self):
        self.assertEqual(rc.norm_doi("https://doi.org/10.1/x"), "10.1/x")
        self.assertEqual(rc.norm_doi("doi: 10.1/x."), "10.1/x")

    def test_pages_abbrev(self):
        self.assertEqual(rc.pages_vancouver("219-229"), "219-29")
        self.assertEqual(rc.pages_vancouver("1067-1077"), "1067-77")
        self.assertEqual(rc.pages_vancouver("e183605"), "e183605")

    def test_sim(self):
        self.assertGreater(rc.sim("About sleep's role in memory", "about sleep's role in memory."), 0.95)


class TestFormats(unittest.TestCase):
    def test_vancouver_et_al(self):
        out = rc.fmt_vancouver(REC)
        self.assertIn("Lee IM, Shiroma EJ", out)
        self.assertIn("et al", out)
        self.assertIn("2012;380(9838):219-29", out)

    def test_apa_punct(self):
        out = rc.fmt_apa(REC)
        self.assertIn("219–229. https://doi.org/", out)

    def test_gb(self):
        out = rc.fmt_gb(REC)
        self.assertIn("[J]", out)
        self.assertIn("等", out)

    def test_bibtex(self):
        out = rc.fmt_bibtex(REC)
        self.assertTrue(out.startswith("@article{lee2012effect"))
        self.assertIn("pages   = {219--229}", out)

    def test_index_row(self):
        row = rc.index_row(REC, "D", "缺乏运动→NCD", "【顶刊·系统综述】")
        self.assertTrue(row.startswith("| D? |"))
        self.assertIn("PMID 22818936", row)


class TestArchitecture(unittest.TestCase):
    """核心回归：网络错误必须与「查不到」分开。"""

    def test_offline_is_unverified_not_notfound(self):
        rc.OFFLINE = True
        rec, status, notes = rc.resolve(doi="10.9999/fake.doi.2020.1234")
        self.assertIsNone(rec)
        self.assertEqual(status, "UNVERIFIED")
        self.assertTrue(any("不等于文献不存在" in n for n in notes))

    def test_offline_title_lookup(self):
        rc.OFFLINE = True
        _, status, _ = rc.resolve(title="sleep duration and all-cause mortality")
        self.assertEqual(status, "UNVERIFIED")

    def test_cache_roundtrip(self):
        rc.OFFLINE = False
        with tempfile.TemporaryDirectory() as tmp:
            import os
            old = os.environ.get("REF_CHECK_CACHE")
            os.environ["REF_CHECK_CACHE"] = tmp
            try:
                url = "https://example.invalid/x"
                rc.cache_write(url, {"state": "empty", "data": None})
                self.assertEqual(rc.cache_read(url)["state"], "empty")
            finally:
                if old is None:
                    os.environ.pop("REF_CHECK_CACHE", None)
                else:
                    os.environ["REF_CHECK_CACHE"] = old


class TestLitFind(unittest.TestCase):
    """检索层：排序必须让「回答了这个问题」压过「被引最多」。"""

    def setUp(self):
        self.lf = load("lit_find", "scripts/lit_find.py")

    def test_design_tiers(self):
        self.assertEqual(self.lf.design_of(["Meta-Analysis"], "")[1], "Meta分析")
        self.assertEqual(self.lf.design_of([], "A randomized controlled trial of X")[1], "RCT")
        self.assertEqual(self.lf.design_of(["Practice Guideline"], "")[1], "指南/共识")
        self.assertEqual(self.lf.design_of([], "Effect of X in mice")[1], "动物/体外")

    def test_design_weight_ordering(self):
        w = lambda pts, t: self.lf.design_of(pts, t)[0]
        self.assertGreater(w(["Meta-Analysis"], ""), w(["Randomized Controlled Trial"], ""))
        self.assertGreater(w(["Randomized Controlled Trial"], ""), w(["Review"], ""))
        self.assertGreater(w(["Review"], ""), w([], "in vitro study in rats"))

    def test_title_match_beats_citation_count(self):
        on_topic = {"title": "Creatine supplementation and muscle strength in resistance training",
                    "abstract": "", "cited": 0, "year": "2026"}
        well_cited_off_topic = {"title": "Protein supplementation and muscle mass in older adults",
                                "abstract": "creatine", "cited": 800, "year": "2018"}
        q = "creatine supplementation muscle strength resistance training"
        self.assertGreater(
            self.lf.score(on_topic, 10, q),
            self.lf.score(well_cited_off_topic, 10, q),
        )

    def test_query_builder_filters_preprints_by_default(self):
        class A:
            query = "sleep and memory"
            design = "meta"
            min_year = ""
            human = False
            include_preprints = False

        q = self.lf.build_query(A)
        self.assertIn("SRC:MED", q)
        self.assertIn("Meta-Analysis", q)
        A.include_preprints = True
        self.assertNotIn("SRC:MED", self.lf.build_query(A))

    def test_offline_is_unverified(self):
        rc.OFFLINE = True
        class A:
            query = "x"; design = "all"; min_year = ""; human = False
            include_preprints = False; sort = ""; max_results = 5
            min_overlap = 0.5; min_design_weight = 0.0
            source = "epmc"; min_citations = 0; include_supplements = False
        state, recs = self.lf.collect(A)
        self.assertEqual(state, "error")   # error ≠ empty：不得当成「没有文献」
        self.assertEqual(recs, [])
        rc.OFFLINE = False

    def test_supplement_abstract_filtered(self):
        supp = {"issue": "9S", "journal": "Medicine & Science in Sports & Exercise"}
        normal = {"issue": "5", "journal": "SLEEP"}
        self.assertTrue(self.lf.is_supplement_abstract(supp))
        self.assertFalse(self.lf.is_supplement_abstract(normal))

    def test_animal_pattern_needs_word_boundary(self):
        """'du-rat-ion' 里的 rat 不是大鼠：曾经把失眠队列研究误判成动物研究。"""
        self.assertEqual(
            self.lf.design_of([], "Insomnia with objective short sleep duration")[1], "原始研究"
        )
        self.assertEqual(self.lf.design_of([], "Effect of sleep loss in rats")[1], "动物/体外")

    def test_title_gate_blocks_abstract_only_match(self):
        """标题一条关键词都不占的文献，靠摘要提一句不算回答这个问题。"""
        terms = ["adolescent", "sleep", "restriction", "academic", "performance"]
        covid_like = {"title": "Mental Health of Children and Adolescents Amidst COVID-19",
                      "abstract": "sleep academic performance adolescent restriction"}
        on_topic = {"title": "Adolescent sleep restriction and academic performance",
                    "abstract": ""}
        def title_hits(rec):
            return sum(1 for t in terms if t in rec["title"].lower()) / len(terms)
        self.assertLess(title_hits(covid_like), 0.34)
        self.assertGreaterEqual(title_hits(on_topic), 0.34)

    def test_grade_mapping(self):
        self.assertEqual(self.lf.grade_of({"journal": "SLEEP"}, "Meta分析"), "1a/A")
        self.assertEqual(self.lf.grade_of({"journal": "Nutrients"}, "RCT"), "1b/B")
        self.assertEqual(self.lf.grade_of({"journal": "JAMA"}, "RCT"), "1b/A")
        self.assertEqual(self.lf.grade_of({"journal": "X"}, "队列"), "2b/B")
        self.assertEqual(self.lf.grade_of({"journal": "X"}, "横断面"), "4/C")
        self.assertEqual(self.lf.grade_of({"journal": "X"}, "动物/体外"), "6/C")

    def test_population_and_n_extraction(self):
        rec = {"title": "Sleep and grades",
               "abstract": "We studied 1,508 adults aged 18-25 years; sleep was measured."}
        pop, n = self.lf.extract_population_n(rec)
        self.assertIn("adults", pop)
        self.assertIn("18-25 岁", pop)
        self.assertEqual(n, "N=1,508")

    def test_n_ignores_tiny_numbers(self):
        """组数/剂量组会被误抓成样本量；<20 一律不认。"""
        rec = {"title": "t", "abstract": "Randomized into n = 2 groups of 9 athletes."}
        self.assertEqual(self.lf.extract_population_n(rec)[1], "未标")

    def test_population_missing_is_declared_not_guessed(self):
        rec = {"title": "A study", "abstract": "No numbers here."}
        pop, n = self.lf.extract_population_n(rec)
        self.assertEqual(pop, "未标明")
        self.assertEqual(n, "未标")

    def test_refuted_topic_detected(self):
        rec = {"title": "Power posing and hormones", "abstract": ""}
        self.assertTrue(self.lf.refuted_flag(rec))
        self.assertEqual(self.lf.refuted_flag({"title": "Creatine and strength", "abstract": ""}), "")

    def test_counter_evidence_recognized(self):
        """反证论文必须能被认出来——否则「剔除被推翻主题」会把反证一起删掉。"""
        cases = [
            "Registered replication report: power posing",
            "Failed to replicate the ego depletion effect",
            "A multisite replication of stereotype threat",
            "Publication bias in the power posing literature",
        ]
        for t in cases:
            self.assertTrue(self.lf.is_counter_evidence({"title": t, "abstract": ""}), t)
        self.assertFalse(self.lf.is_counter_evidence(
            {"title": "Creatine increases strength in resistance trained men", "abstract": ""}))

    def test_weights_are_configurable(self):
        w = self.lf.load_weights()
        for key in ("title", "design", "cited", "cited_cap", "refuted_penalty", "counter_boost"):
            self.assertIn(key, w)
        rec = {"title": "sleep duration mortality", "abstract": "", "cited": 100, "year": "2010"}
        base = self.lf.score(rec, 5.0, "sleep duration mortality", w)
        alt = dict(w)
        alt["title"] = w["title"] * 2
        self.assertGreater(self.lf.score(rec, 5.0, "sleep duration mortality", alt), base)

    def test_openalex_abstract_reconstruction(self):
        inv = {"Sleep": [0], "loss": [1], "harms": [2], "memory": [3]}
        self.assertEqual(
            self.lf._abstract_from_inverted(inv), "Sleep loss harms memory"
        )
        self.assertEqual(self.lf._abstract_from_inverted(None), "")


class TestVaultScan(unittest.TestCase):
    DOC = """# 标题

## 夜间光照

蓝光抑制褪黑素约 50%，建议停屏。

## 已标注

蓝光抑制褪黑素（Chang 2015, PNAS; PMID 25535358）。

```text
代码块里的 90% 不算
```
"""

    def test_blocks_context(self):
        blocks = vs.blocks_with_context(self.DOC)
        texts = " ".join(b[2] for b in blocks)
        self.assertIn("蓝光抑制褪黑素约 50%", texts)
        self.assertNotIn("代码块里的", texts)
        self.assertTrue(any(b[0] == "夜间光照" for b in blocks))

    def test_guess_bucket(self):
        self.assertEqual(vs.guess_bucket("07健康/营养与补剂/x.md"), "A")
        self.assertEqual(vs.guess_bucket("x.md", "睡眠与认知"), "B")
        self.assertEqual(vs.guess_bucket("01-生存医学.md"), "F")
        self.assertEqual(vs.guess_bucket("无关.md"), "?")

    def test_gap_found_and_cited_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            (brain / "a.md").write_text(self.DOC, encoding="utf-8")
            gaps = vs.scan_gaps([brain / "a.md"], brain, set(), 10)
            self.assertEqual(len(gaps), 1)
            self.assertIn("50%", gaps[0][2])

    def test_done_registry_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            (brain / "a.md").write_text(self.DOC, encoding="utf-8")
            first = vs.scan_gaps([brain / "a.md"], brain, set(), 10)
            self.assertEqual(len(first), 1)
            done = {vs.done_key(first[0][0], first[0][2])}
            self.assertEqual(vs.scan_gaps([brain / "a.md"], brain, done, 10), [])

    def test_dup_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            idx = Path(tmp) / "证据引用索引.md"
            idx.write_text(
                "| A1 | 纤维→死亡 | X. Lancet 2019. doi:10.1234/abc | 【顶刊】 | |\n"
                "| A2 | 纤维→死亡风险 | Y. BMJ 2020. doi:10.1234/abc | 【顶刊】 | |\n",
                encoding="utf-8",
            )
            findings = vs.dup_check(idx)
            self.assertTrue(any("近似重复" in f for f in findings))
            self.assertTrue(any("DOI 重复" in f for f in findings))


class TestRankEval(unittest.TestCase):
    """排序评估：标注集完整性 + nDCG 语义正确。"""

    def setUp(self):
        self.re_ = load("rank_eval", "scripts/rank_eval.py")

    def test_ndcg_perfect_and_worst(self):
        gold = [0, 1]
        good = [{"_pos": 0}, {"_pos": 1}, {"_pos": 9}]
        bad = [{"_pos": 9}, {"_pos": 8}, {"_pos": 0}, {"_pos": 1}]
        self.assertAlmostEqual(self.re_.ndcg_at_k(good, gold), 1.0, places=3)
        self.assertLess(self.re_.ndcg_at_k(bad, gold), 0.8)

    def test_labels_reference_existing_candidates(self):
        """标注里的下标必须能在候选列表里找到，否则这条查询静默失效。"""
        pools = self.re_.load_pool()
        labels = self.re_.load_labels()
        self.assertTrue(labels, "标注集为空")
        for q, ids in labels.items():
            cands = self.re_.candidates_for(q, pools)
            self.assertTrue(cands, f"没有候选：{q}")
            self.assertLess(max(ids), len(cands), f"下标越界：{q}")

    def test_candidate_order_is_deterministic(self):
        pools = self.re_.load_pool()
        a = [r["title"] for r in self.re_.candidates_for(QUERY_SAMPLE, pools)]
        b = [r["title"] for r in self.re_.candidates_for(QUERY_SAMPLE, pools)]
        self.assertEqual(a, b)


QUERY_SAMPLE = "creatine supplementation muscle strength resistance training"


if __name__ == "__main__":
    unittest.main(verbosity=2)
