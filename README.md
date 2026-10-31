# human-bio-psych-refs 人体证据管线

一个让 AI 回答人体/生理/心理/医学话题时**默认自动带真实文献引用**的 Skill（Claude Code / DSH / 任何支持 SKILL.md 的 agent 均可安装）。

## 它解决什么问题

- AI 最贵的错误不是结论错，而是**捏造 DOI/PMID/卷期页**——看起来最专业、杀伤力最大
- 本技能把「检索 → 分级 → 核验 → 引用」做成管线：**没被脚本核过的引用等于没有引用**

## 三件核心工具（纯标准库，无 API key）

| 脚本 | 干什么 |
|---|---|
| `scripts/lit_find.py` | 一句英文命题直出候选文献：Europe PMC+OpenAlex 双源、设计分级排序、顶刊加权、剔撤稿/预印本/会议摘要、已知被推翻主题拦截、A/B/C 等级+人群样本量 |
| `scripts/ref_check.py` | DOI/PMID/标题三路核验，VERIFIED 才许引用；Vancouver/APA/GB-T 7714 三格式直出；24h 缓存 |
| `scripts/vault_scan.py` | 扫笔记库：找「带数字无出处」行，生成证据补强清单 |

排序质量经标注集评估：15 查询 / 3666 候选 / 193 条人工标注，nDCG@10 0.762→0.806（权重在 `references/ranking-weights.json`）。

## 安装（Claude Code / 通用）

```bash
git clone https://github.com/<YOUR>/human-bio-psych-refs.git
# 放进技能目录：
cp -r human-bio-psych-refs ~/.claude/skills/
```

DSH 用户：拷到 `.dsh/skills/` 即可。

## 快速上手

```bash
# 找文献（meta 分析优先，顶刊排前）
python scripts/lit_find.py "sleep duration memory consolidation" --design meta

# 核验引用（不 VERIFIED 不许进正文）
python scripts/ref_check.py --doi 10.1093/sleep/33.5.585

# 中文主题加 CNKI 句式
python scripts/lit_find.py "高中生 睡眠不足 成绩" --cn
```

## 自检

```bash
bash tests/../scripts/selftest.sh   # 或直接 python tests/test_scripts.py
```

## License

MIT
