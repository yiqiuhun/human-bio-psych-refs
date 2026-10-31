---
name: human-bio-psych-refs
description: 人体·生理·心理·医学的默认自动引用层：答话里只要出现关于身体/大脑/睡眠/营养/运动/情绪/认知/量表的事实断言，就自动挂文献并核验，不等用户开口。也管找文献、判证据、写 APA/Vancouver/GB-T 7714 引用、把结论翻成建议、量表用法。触发词：文献/引用/参考文献/证据/有科学依据吗/PMID/DOI/生理/心理/营养/睡眠/运动/可信吗。
---

# 人体证据管线（Human Evidence Pipeline）

**一条铁律**：没被脚本核过一次的引用，等于没有引用。做人体/健康/心理内容时，最贵的错误不是结论错，而是**捏造出来的 DOI、PMID 和期刊卷页**——它看起来最专业，杀伤力最大。

## 第零步：自动引用契约（默认开启，不靠用户开口）

**范围**：任何关于人的身体 / 大脑 / 激素 / 睡眠 / 营养 / 运动 / 疾病 / 情绪 / 行为 / 认知 / 成瘾 / 衰老 / 心理测量的事实断言，默认就带引用。包括：写学习计划时引的生理前提、聊情绪时的神经科学断言、做训练方案时的剂量依据、评价一条网络说法、写文章、甚至随口解释“为什么会累”。

**四个动作**：

1. 带数字的句子 → 行内引用 `（作者 年份, 期刊缩写; PMID/DOI）`。
2. 回答末尾 → 固定一个 `参考来源` 段。
3. 引用先过 `scripts/ref_check.py`：`VERIFIED` 才写成正常引用；`PARTIAL` 标“（待核）”；`NOT_FOUND` 删掉或标“（未核验）”，改成定性说法。
4. 证据不足就直说“证据有限 / 尚不成立”，不为了凑引用而堆文献。
5. **优先引用顶刊**：同一断言有多篇候选时，按 TIER_A 顶刊（NEJM/Lancet/JAMA/BMJ/Nature/Cell 系、Cochrane、Sleep、Am J Clin Nutr、Br J Sports Med 等）> 指南/meta > 专业期刊 > 一般期刊的顺序取用；排序脚本已内置 `top_journal_boost` 加权，相关性相近时顶刊自动排前。顶刊观点与后续更大样本证据冲突时，两边都给并注明。

**不适用**：纯数学、代码、工程、行政、礼节性寒暄，以及用户明确说“别引文献”的时候。

**完成判据**：回答里每个数字都能在「参考来源」里找到对应条目，且每条都过过脚本。

## 第一步：给问题定性（不写代码，先定型）

把提问翻译成一句话的可检索命题，并标注分类：

| 用户问的是 | 本质 | 走哪条路 |
|---|---|---|
| 「XX 有没有用 / 真的假的 / 有没有科学依据」 | 效果证据（efficacy） | 找 meta 分析 → RCT → 队列，读 `01` `04` |
| 「为什么 XX 会这样」 | 机制解释 | 机制研究只解释，不定效果，读 `06` 的机制陷阱 |
| 「多少量 / 多久 / 怎么吃」 | 剂量-反应 | 找剂量分层研究，读 `01` 剂量节 |
| 「给我找几篇文献 / 写段带引用的稿子」 | 检索+引用 | `02` → `03` → `08` |
| 「这个量表能不能用」 | 测量工具 | `07` |
| 「这条建议我要写进笔记」 | 落地 | `05` + 本文件末节回写规则 |

**完成判据**：能用一句 PICO 式命题复述需求（谁、暴露/干预、对照、结局、时间尺度）。用户没说人群就默认「一般成年人」，并在输出里写明这一默认。

## 主功能：找文献（这一技能的第一用途）

手上的活是「这个问题有没有文献」「帮我找几篇支持的」「有没有相反证据」时，**直接跑 `lit_find.py`，不要凭记忆报文献名**。

```bash
# 基本：一句英文命题 → 分级候选表
python scripts/lit_find.py "resistance training adolescent bone density"

# 只要某一档设计
python scripts/lit_find.py "omega-3 depression" --design meta        # meta/系统综述
python scripts/lit_find.py "caffeine sprint performance" --design rct
python scripts/lit_find.py "hepatitis B vaccination schedule" --design guideline

# 找经典（高被引）与找最新
python scripts/lit_find.py "sleep duration mortality" --min-citations 200
python scripts/lit_find.py "creatine loading protocol" --min-year 2021

# 导出文献包（Markdown，可直接贴进第二大脑）
python scripts/lit_find.py "blue light melatonin suppression" --abstract --md litpack.md

# 中文主题：给英文检索式 + CNKI/万方句式
python scripts/lit_find.py "高中生 睡眠不足 成绩" --cn
```

**它做对的四件事**

1. **两个源合并**（`--source both` 默认）：Europe PMC 偏生物医学、有 PMID；OpenAlex 覆盖更广、自带被引数，两边按 DOI/标题去重。
2. **按「回答了这个问题」排序**，不是按被引或按新旧：标题词命中 > 研究设计层级 > 被引数。查肌酸时 800 被引的蛋白质综述不会抢过 6/6 词命中的肌酸 meta。
3. **默认剔脏数据**：撤稿/更正通告、预印本（`--include-preprints` 放开）、**增刊会议摘要**（`Med Sci Sports Exerc 54(9S)` 这种，`--include-supplements` 放开）、邻题噪声（摘要词重合 <50% 或标题词重合 <1/3）、**已知被推翻主题**（`--refuted flag` 改为标注降权、`--refuted keep` 不管）。
4. **每条都给三样判读信息**：
   - **等级** `层级/字母`——1a 系统综述·Meta / 1b RCT / 2b 队列 / 3b 病例对照 / 4 横断面 / 5 叙述综述 / 6 机制·动物；字母 A 可直接下结论、B 必须带人群限定、C 只能当机制。
   - **人群 · 样本量**——从摘要自述抽（年龄区间、人群词、N），抽不到写「未标明」，**不猜**。
   - **⚠️ 推翻提示**——落进失败重复列表的主题直接标出原因。

**出口**：表格（人看）/ `--md` 文献包 / `--json` / `--index-row <桶> --claim "…"` 直接生成入库行。
**边界**：`lit_find` 只负责「找到、清洗、分级、排序」；能不能引，仍要过第四步 `ref_check.py`。脚本退出码 2 = 网络没验成，不是没文献。
判读细则（等级表、人群口径、已推翻清单）见 `references/10-证据等级-人群-已推翻清单.md`。

### 排序别靠手感：评估与调参（改动排序前必跑）

权重放在 `references/ranking-weights.json`，不是写死在代码里。改排序前后各跑一次：

```bash
python scripts/rank_eval.py --build-pool        # 15 条查询抓候选池（约 10 分钟，有缓存）
python scripts/rank_eval.py --dump-candidates "<query>" --limit 14   # 标注用：下标即 labels 里的数字
python scripts/rank_eval.py --eval              # 当前配置的 nDCG@10
python scripts/rank_eval.py --tune              # 坐标上升调参，写回 weights.json
python scripts/rank_eval.py --loo               # 逐条对比，看是不是靠个别查询刷分
```

现状（2026-10-31，15 条查询 / 3666 条候选 / 人工标注 193 个判断）：**nDCG@10 0.762 → 0.806**，改善 6 条、变差 1 条、持平 8 条。
（无约束调参能到 0.848，代价是被引权重被压成 0——榜上再无经典文献。宁要 0.806 的可信版。）

调参有两条硬约束，别去掉：

1. **被引权重有地板**（0.0033）：压到 0 等于不认经典文献，排行榜会被最新小样本屠榜；
2. **反证加权与被推翻罚分冻结**：标注集里没有被推翻主题的样本，调它们就是用噪声改参数。
3. **顶刊加权（top_journal_boost）有地板（8.0）**：压到 0 等于回到“顶刊不优先”的老行为；用户明确要求优先引用顶刊。

改标注集要遵守同一条规矩：**只标「这条论文是否回答了该查询」**，不标「我喜不喜欢它」。

## 第二步：检索（先锚点，再数据库）

顺序固定：**本地锚点库 → 二手权威（指南/meta）→ 一手文献**。

1. 查 `references/04-主题锚点文献.md` 里有没有现成锚点；有就拿来当起点，不要从零搜。
2. **主动检索用脚本，不靠记忆**：`scripts/lit_find.py` 一句话命题直出候选文献（Europe PMC，无需 key）：

   ```bash
   python scripts/lit_find.py "creatine supplementation muscle strength resistance training" --design meta
   python scripts/lit_find.py "sleep deprivation memory consolidation" --min-year 2015 --abstract
   python scripts/lit_find.py "间歇性禁食 青少年 体脂" --cn            # 中文主题：给英文检索式 + CNKI 句式
   ```

   输出按「标题词命中 × 研究设计」排序（Meta > 系统综述 > 指南 > RCT > 队列 > 横断面 > 综述 > 动物/体外），
   自动剔除更正/勘误/撤稿/预印本，每条带 PMID + DOI + 期刊卷期页 + 证据等级提示 A/B/C。
   `--index-row F --claim "…"` 直接吐可粘贴的入库行。退出码 2 = 网络未验成 ≠ 没有文献。
3. 需要新鲜证据时按 `references/02-数据库与检索.md` 的检索语法手写更复杂的检索式（Europe PMC REST 免费无 key，`curl` 直连）；`lit_find.py` 是常用式的快捷入口，复杂布尔式仍手写。
4. 检索词用英文；中文主题同时查 CNKI/万方（`02` 给句式，`--cn` 会打印现成格式）。

**完成判据**：拿到 ≥1 篇 meta/指南 + ≥1 篇原始研究，且记录下每篇的 DOI 或 PMID 原始字符串。
检索出来的每条都必须再过第四步 `ref_check.py`——`lit_find.py` 只负责「找到并排序」，不负责「判定可引」。

## 第三步：判读（这一步决定输出质量）

按 `references/01-证据分级与判读.md` 走三级筛：设计层级 → 效应量与绝对风险 → 偏倚与可重复性。四句话必须能填出来：

- 设计：RCT / 队列 / 病例对照 / 横断面 / 机制 / 动物 / 体外 / n-of-1
- 效应：原始单位 + 相对值 + 绝对值（例：睡眠 6h vs 7-8h 全因死亡 RR 1.12，绝对风险 +1.2/1000 人年）
- 人群：样本量、年龄、性别、基线健康状况、国家
- 裂缝：混杂、测量方式（自报 vs 客观）、随访长度、发表偏倚、是否被重复

命中 `references/06-误用识别.md` 任一条（机制当效果、小鼠剂量、观察性当因果、显著当重要、单研究定论）→ 输出里必须点出来。

**完成判据**：结论句带效应量、带人群限定、带一句不确定度。只写「研究发现 XX 有显著影响」= 未完成。

## 第四步：引用落地（唯一不可跳的一步）

```bash
# 按 DOI / PMID / 标题 三选一，校验并直接生成三种格式的条目
python scripts/ref_check.py --doi 10.1016/S0140-6736(12)61031-9 --style all
python scripts/ref_check.py --pmid 22818936
python scripts/ref_check.py --title "About sleep's role in memory" --style gb
python scripts/ref_check.py --file refs.txt        # 批量：每行一个 DOI/PMID/标题
```

脚本打 `VERIFIED` 才写进正文；打 `NOT_FOUND` 的一律降级成「作者+年份+期刊（未核验）」，或删掉。格式规范、作者数阈值、撤稿检查见 `references/03-引用格式与核查.md`。

标题检索的边界（实测踩过）：同名会议摘要、预印本、正式论文会同时命中。输入「Cappuccio FP 2010 sleep duration...」时脚本先命中的是 2009 年会议摘要款（Int J Cardiol 增刊），不是 SLEEP 2010 正刊。所以：

- 能拿到 DOI / PMID 就不用标题检索；用标题时必须人工核对期刊名、卷期页、文献类型。
- 多条候选时选「有 PMID + 正式刊物 + 卷期页齐」的那条。
- 刊物名里出现 Supplement / Abstract / Conference 字样 = 会议摘要，不能当原始研究引。

**完成判据**：正文里每一个方括号/上标都能在参考文献表里找到，且参考文献表每一条都过过脚本。

## 第五步：转成人和 AI 都能用的东西

- **先过三筛**：年龄（15 岁发育期）→ 发育期零缺口 → 地贫红线。过不去就直接写「不适用于你」（具体约束见 `references/09-第二大脑接口-我的身体约束.md`）。
- 给人：按 `references/05-文献到建议.md` 把效应量折成个体预期（含依从性折扣），按 `references/08-写作模板.md` 写句子。
- 给库：按 `09` 里的格式落地——索引行写进 `知识/证据引用索引.md` 对应桶（A营养/B脑科学/C康复/D健身/E感官学习），分层只从六档里选；新笔记同时更新该域 `域索引.md`、过一遍 `_AI/碰撞日志.md`。证据等级映射：顶刊/顶刊级/指南 = A；专业期刊 = B；机制/证据弱 = C。

**完成判据**：读者能据此做出一个具体动作，且知道这个动作的证据有多硬。

## 硬规则（违反其一 = 返工）

1. **效应量 + 绝对值一起给**。只说 p<0.05 的句子，读者无法判断值不值得做。
2. **观察性研究一律写成「相关，不能推因果」**，包括看起来很硬的大队列。
3. **动物剂量不换算就不许类比人类**（HED 换算与物种差异见 `01`）。
4. **不确定度显式说出来**：证据强 / 有但有限 / 尚不成立，三档选一，不许含混。
5. **数字优先于形容词**：写「HR 0.80（95%CI 0.74–0.87）」而不是「显著降低」。
6. **冲突证据要并列**，不要只挑支持自己结论的那半。
7. **重要/成段输出必须 ≥15 条参考来源，且多方面覆盖**：至少横跨 4 类视角——① meta/系统综述（≥3 条）；② RCT/干预（≥3 条）；③ 队列/观察性（≥2 条）；④ 机制/综述/指南（≥2 条），其余补齐到 15+；主题子问题（如“睡眠→记忆→成绩”各环节）各有代表文献，不许多条重复证同一句话。凑不满 15 条时如实写“该主题全库仅 N 条可核文献”，禁止用低相关文献充数。
8. **自动引用不是可选项**：正文里关于人的事实断言没挂引用 = 未完成，不是风格选择。

## 参考文件地图

| 文件 | 里面是什么 | 什么时候读 |
|---|---|---|
| `references/01-证据分级与判读.md` | 设计层级、GRADE、效应量与 NNT、偏倚清单、动物外推、可重复性 | 每次判读 |
| `references/02-数据库与检索.md` | PubMed/MEDLINE 语法、Europe PMC / Crossref / OpenAlex API、中文库、指南库、预注册平台 | 检索时 |
| `references/03-引用格式与核查.md` | Vancouver/APA 7/GB-T 7714-2015/AMA 规则、作者阈值、BibTeX、反幻觉五步 | 写参考文献时 |
| `references/04-主题锚点文献.md` | 12 个主题的身体与心理锚点文献（睡眠/运动/营养/心理/学习/成瘾/疼痛/衰老/女性生理/免疫…） | 冷启动找起点 |
| `references/05-文献到建议.md` | 效应量→个体预期、NNT、依从性折扣、证据强度×效应量四象限 | 出建议时 |
| `references/06-误用识别.md` | 机制≠效果、小鼠剂量、因果误判、心理学可重复性危机、常见伪科学清单 | 判读与辟谣 |
| `references/07-量表工具.md` | PHQ-9/GAD-7/PSS-10/PSQI/ESS/Big Five 等：切点、用途边界、信效度 | 涉及测评 |
| `references/08-写作模板.md` | 中文/英文引用句模板：结论句、限定句、分歧句、证据不足句 | 落笔时 |
| `references/09-第二大脑接口-我的身体约束.md` | 我的三筛（15 岁/发育期/地贫）、库内索引与碰撞日志格式、自我革新四循环、写库权限坑 | 写库、出建议、迭代技能时 |
| `references/10-证据等级-人群-已推翻清单.md` | 等级表（1a→6 + A/B/C 含义）、人群口径为什么必须单列、14 条已推翻/缩水主张、四道清洗的顺序 | 判读检索结果、写文献包时 |
| `references/ranking-weights.json` | 排序权重（调参产出，别手改） | 想改排序时 |
| `references/eval-pool.json` / `eval-labels.json` | 标注池（15 查询 / 3666 条）与人工标注 | 评估排序质量时 |
| `scripts/rank_eval.py` | 建池、导出候选、nDCG 评估、坐标上升调参、逐条对比 | 改动排序前后 |
| `scripts/ref_check.py` | DOI/PMID/标题校验 + 三格式输出 + 撤稿标记 | 第四步强制 |
| `scripts/lit_find.py` | 按主题自动检索真实文献：设计分级排序、A/B/C 等级提示、入库行直出 | 第二步检索（手上没有现成文献时） |
| `scripts/vault_scan.py` | 扫库：索引桶计数 + 带数字无出处行 → 生成《证据补强清单》 | 缺口扫描（L2） |

脚本依赖：`urllib` 标准库（Europe PMC + Crossref + OpenAlex），无需安装、无需 API key；本机 python 路径见下。

```text
D:\deepseek-harness\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe
```
