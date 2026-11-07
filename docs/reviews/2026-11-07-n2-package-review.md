# N2 候选登记包（宏观公告日溢价，C006/H103/R4 占位）A1 级包审核记录（2026-11-07）

> **状态：占位记录（未 commit）。** 审核者 = A1 级独立审核者，未参与 N2 登记包
> 撰写/装配，未参与 N1-R3 执行链（run1–run6）任何工作。全程只读：主仓
> demo-next-20261105（HEAD **20039f3**，worktree clean，直读）；研究库
> mt5-pline 台账 `data/research/ledger.sqlite` **mode=ro URI 直查**。除本记录外
> 零文件修改、零台账写入；不派子代理；不填 approved_by；不开轮（R3 尚 open，
> 开轮须待其关轮后另裁）。解释器 = 研究库 .venv（`-I`、`PYTHONUTF8=1`）。
> 日历确定性复现审核在临时目录隔离副本进行（`/tmp/n2repro`），主仓工件未触碰。
> 关键量（SHA、事件计数、FOMC 子集逐位比对、七元组去重 SQL、装载器三态、
> 功效/夏普/成本倍数）全部由审核者独立重算，不采信包内留档断言。

---

## 0. 总裁定

**NEEDS REVISION（2 项，均为重列级/文字级，非结构性）。** 两项修订完成后，
本登记包即符合"APPROVED for future round（登记/预筛用途）"条件——本记录
**预先授权且仅授权**下述 REV-1/REV-2 两处修订（§3），修订后提升不需要重审；
除此之外内容逐字冻结，任何其他变更即失效须重审重绑（N1 A1 先例 E1 同款）。
**本裁定不含开轮批准**：R4 开轮 = 预筛真实结论 + §6 前置清单完成 + R3 关轮后
另裁。按登记口径（口径 B）的如实预期，预筛将判 **waiting_data（铁律 18，
非 fail）**、R4 不开——见 §4/REV-2，此为诚实闸门行为，登记包本身无缺陷性
虚报。

裁定基准 = N1 A1 裁定五修订项标准（docs/reviews/2026-11-06-n1-a1-ruling.md）：
N2 包起草即吸收五教训（成本 ×1.5 重derive、覆盖年数契约行、日历异常预先
裁决、盲区披露机读化、D33 台账口径），逐项核验**全部落实**（§2）；新增两处
须修订项均为台账时间演进与功效表述精度问题，无结构性缺陷。

---

## 1. 审核对象与盘点（先完整盘点后逐件审）

主仓 `.superpowers/h-new-candidates/`（git-ignored 路径，草案形态，非
research/ 扫描路径——与占位编号语义一致）：

| 文件 | 字节 | 核验 |
| --- | ---: | --- |
| n2-draft/C006.toml | 14670 | 真实装载器实测 OK（§2.3） |
| n2-draft/H103.toml | 6283 | 真实装载器实测 OK（§2.3） |
| n2-draft/R4.toml | 4730 | load_round 按设计拒（approved_by 空） |
| n2-draft/N2-precheck-spec-2026-11-07.md | 15974 | 逐节核（§2/§4/§5） |
| n2-calendar/announcements.json | 202502 | SHA=**9b444ec6a913…6546**，1022 事件日（§2.1） |
| n2-calendar/PROVENANCE.md | 13908 | 出证链/异常/缺口逐项核（§2.2） |
| n2-calendar/raw/（9 文件） | 15041 | 9/9 SHA 与字节数逐位一致（下表） |

raw/ 九文件 SHA256/字节与 PROVENANCE 表 **10/10 全部一致**（含
announcements.json 本体）。包写作时间 = 文件 mtime 2026-11-07 00:11–00:17
（UTC+8）；此时间戳是 REV-1 的关键事实（§3）。

## 2. 逐项审核（比照 N1 A1 五修订项标准）

### 2.1 日历工件（announcements.json）——PASS

- **哈希/条目数**：SHA256 `9b444ec6a9131d00448764b89a232246eca8f47da790
  323df00aa722744e6546`（审核者重算一致）；`total_event_days=1022`
  （1994-02-04 → 2026-11-06）。
- **确定性重现**：审核者将 `_tools/parse_announcements.py` 与 raw/、N1 日历
  复制到临时目录重跑——输出 **SHA 逐位一致**（9b444ec6…）。解析器纯本地、
  无网络无行情；内嵌 N1 工件 SHA 前缀校验（a53577d9）fail-closed；周末日期、
  重复 token、时代窗外日期、逐年计数越界均硬校验报错。PROVENANCE 的
  "可由 raw/ 逐位重建"主张实测成立。
- **主段过滤**：date < 2021-05-17 → **850** 事件日 = CPI 328 + NFP 328 +
  FOMC 213 − 窗内同日合并 19（审核者独立复算一致）；年均 31.1584；时代
  1994-02-04 → 2021-05-17 = 27.279945 年（复算逐位一致）。
- **FOMC 子集与 N1 逐位一致**：N2 工件 FOMC 257 日与 N1 fomc_meetings.json
  `in_default_event_set` 集合**列表恒等**（审核者比对 True）；主段窗 213 与
  C005.precheck.json `window_events=213` 一致。内嵌绑定
  `a53577d96c699a1a6e7bab2bfb5f80d43408380ea3e2f48c7e88c1e76ec971c5`
  与 N1 工件实测 SHA 一致。
- **结构事实复核**：同日合并 20 全为 CPI+FOMC、无 CPI+NFP 同日（结构性）；
  主段窗星期分布周二 147/周三 223/周四 82/周五 398、**周一 0**（无跨周末
  事件的结构基础成立）；逐年计数 33/33 年 CPI/NFP∈{10,11,12}、FOMC∈{7,8}，
  1994/2020/2025/2026 偏差年与 PROVENANCE 表一致。
- **token 算术闭合**：cpi 156+84+84+68=392；empsit 156+84+84+69=393；
  392+393+257−20=1022 ✓。
- **抽检**：2021-01-08 NFP（01082021）、2021-04-13 CPI（04132021）、
  2013-10-22 停摆顺延 NFP（10222013）、2020-05-12 CPI（05122020）、
  2024-06-12 CPI+FOMC 同日（06122024）——全部在 raw token 中命中。

### 2.2 出证链（WAF 降级分块提取）——PASS（附条件 C4，强度如实弱于 N1）

- 降级事实：直接 curl 被 BLS WAF 拒（403），原始 HTML 字节未留存——N1 是
  27 个年度页原件+逐文件哈希，N2 做不到同等强度，PROVENANCE 如实登记。
  **接受该降级**，依据：(a) token 内嵌于官方档案 URL 文件名（=发布日），
  任一期可用浏览器独立核验（自证结构）；(b) 确定性重建实测成立（§2.1）；
  (c) 交叉验证链完整——逐年计数带越界硬校验（无静默丢条会暴露）、FOMC
  子集与 N1（强出证工件）逐位一致、星期结构、已知历史发布日抽检 5/5 命中；
  (d) 已知异常（1995-96/2013/2025 停摆、2020 FOMC=7、2026 部分年边界）
  机读列于 `summary.known_anomalies` 并逐条预先裁决（N1 A1 教训 4 落实）。
- 1986-1993 覆盖缺口如实登记、零日期编造；替代来源提案（ALFRED
  vintagedates API→FRASER→学术集仅核对）按优先序在案。**裁定**：缺口处置
  合格；功效门取工件保守口径 1994-02-04（§2.4 契约行）正确。

### 2.3 候选 TOML / 卡 / 轮次（真实装载器实测）——PASS

审核者用研究库 .venv 直接装载主仓真实 loader（无 stub）：

- `load_candidate(C006.toml)` → OK：id=C006、mechanism_class=event（七类
  枚举内）、status=registered、declared 八字段全读出（32 / 0.0008 / 1 /
  16 / 16 / 1.0 / 0.23 / static_long）；observed_in_sample 非空（339 字符，
  机读披露——N1 A1 教训 5 落实）；continues_family 空（新族，lineage=0）；
  literature 1 原作 + 4 复证 + 2 反驳 + search_log 869 字符。
- `load_card(H103.toml)` → OK：v2 卡、version=1 新族 parent 空、
  **selection_rule=single_spec**、strategy=None（legacy 口径——运行须显式
  --strategy，与"策略类未实现、另立任务"的披露一致）。
- `load_round(R4.toml)` → **按设计拒绝**（approved_by 空）——"批准前不开
  轮"机器保证复现（N1 A1 同款三态验证）。
- 成本基准（N1 A1 教训 2）：×1.5 冻结基准 `external-spread-x1.5-v1` 组成
  按引用沿用 N1/C005 实测——C005.precheck.json 实测
  spread_round_trip=1.01081e-04 + slippage 6.73876e-06×2 + commission 0 +
  overnight 0 = **1.14559e-04**，与包内 1.146bp 组成逐位一致；倍数口径
  A 11.4/1.146≈9.95×、口径 B 8.0/1.146≈6.98×（复算一致，门槛 ≥3 过）；
  旧 ≈14×（0.81bp 未加成口径）已废弃并注明出处。
- σ/年数基准钉死（N1 A1 教训 3）：σ 参考 = N1/C005 预筛实测
  **1.6661507476%**（T84/K4、window_bars=1，precheck JSON 逐位核对）；
  覆盖年数契约行两口径（名义 1986≈35.37y / 工件保守 1994≈27.28y），功效门
  取保守口径、数据根全量年数明文禁用于门（N1 A1 原句同款）——合规。
- [v3.event] 否决适用性：gates.toml 实测 `delay_bars_veto=false`、
  `buyhold_excess_veto=false`——R4 预写"唯一适用否决=成本 ×1.5 后净
  均值>0"与冻结 gates 一致；随机化次数下限 max(1999,⌈20N/α⌉) 同源。

### 2.4 功效预筛两口径——口径选择合法；一处表述须更正（REV-2）

审核者用项目 `mt5quant.research.power.power_one_sided`（装载器同款公式，
SE=√((1+SR²/2)/T)）独立重算：

| 口径 | SR | 功效@27.28y | 功效@35.37y | 备注 |
| --- | --- | --- | --- | --- |
| A（SW 原作 11.4bp，32/年） | 0.3482 | **54.8%** | 64.3% | 包写 ≈57% 系简化式 Φ(√T·SR−1.645)；两式同侧 ≥50% |
| B（Ghaderi 折减 8.0bp，**登记值**） | 0.2327 | **32.8%** | 39.0% | 包写 ≈33% ✓ |
| B 登记声明值 | 0.23 | **32.3%** | 38.4% | 门功效由 min(声明,隐含) 钉死在声明值 |

- **33%<50% 属于哪个口径**：登记口径 B（发表后折减、`expected_sharpe=0.23`
  登记值）。原作口径 A 为 54.8%（≥50）但**不是登记值**。
- **口径选择合法性**：登记取更不利发表后口径 = C001/C005 先例（"登记数值
  取更不利者"）；预写门在装载器冻结规则 `sharpe_used=min(声明,隐含)`（铁律
  18/蓝图 §6.2 条件 4）+ R4 advance_rule "waiting_data 则不开"。选择合法且
  诚实——与 C002 式真实数据负结果不同，本候选未跑预筛，登记即预警。
- **但（REV-2 依据）**：在 min() 规则下，口径 B 的门功效被声明值 0.23 钉死：
  27.28y→32.3%、35.37y→38.4%，单次 50% 需 **T≈51–52 年**。故包内 §4/§5 的
  "补强路径 = 1986-1993 日历扩展（SR_req 0.315→0.277）或 σ 实测显著低于
  参考"对**口径 B 过门无效**——σ 更低只抬隐含夏普、被 min() 截断；时代扩到
  35.37y 仍 38.4%<50%。该两句是 SR_req 框架下的真命题，但作为"补强路径"
  陈述会误导（投入 1986-1993 扩展并不能开 R4）。须按 §3 REV-2 更正表述。
  参考：α/N=3 口径下 A 35.9%、B 17.3%（advance_rule 已预写"单次功效门与
  α/N 功效分别报告，不得混称"，合规）。

### 2.5 引擎就绪——selection_rule 已落地；event kind 有缺口（包已如实自限）

- **selection_rule=single_spec：已支持**。落地提交 **065abf7**（bt gate 选参
  层 single_spec，聚焦评审 docs/reviews/2026-11-07-engine-single-spec-review.md
  Spec/Quality 双 PASS），审核者验证其为 HEAD 20039f3 祖先；bt.py 合法集 =
  两枚举 ∪ single_spec，恒等采用登记规格、参数网格≠1 即拒。H103 用值正确。
  （注：任务链提及的 ca626ae 是 v3_event_diagnostics window_shift 降级提交，
  亦在 HEAD；single_spec 本体是 065abf7。）
- **event_kind 现状**：bt.py `--event-kind` choices = **month_end |
  london_fix | fomc_calendar**（仅此三种）。events.py `fomc_calendar_events`
  只认 fomc_meetings.json schema（meetings/announcement_date/
  in_default_event_set/era_start）——**装不了 announcements.json**
  （events[]/date/types/sources schema）。N2 需要的新 kind
  **macro_calendar 不存在**。包 §7.8 已如实声明"引擎适配在预筛通过后另立
  任务、不在本登记包范围"——登记包层面合格，缺口进 §6 前置清单。
- **随机化对照**：现有 `randomize_event_times` 以"与原事件窗不重叠"排除对照
  （候选锚 c∈[a−hold,b] 全掩码）+ 同星期/月内三分位分层。对 D1 hold=1 窗，
  该规则**蕴含且严于** R4 预写的污染排除规则（对照窗 [t−1,t] 不得含公告日）；
  但排除集来自**已解析事件窗**——若某公告日因缺锚被 fail-closed 丢弃，该日
  不在排除集、可能落进对照窗。实现时排除集必须从**冻结日历全部公告日**派生
  （§6 P3）。

### 2.6 盲区/月末重叠披露（比照 T66/N1 的 15/213 处理）——PASS

- 主段窗 850 事件日中 **15 个**为日历月末最后工作日（`is_last_weekday_of_
  month` 逐事件机读标注），全部 FOMC 日，与 N1/C005 预筛 JSON 双口径
  （fred_last_trading_day / calendar_last_weekday）15 日期**逐一相同**
  （审核者三列表比对恒等）；**CPI/NFP 月末重叠=0**（结构性处月中，复核
  成立）。占比 15/850≈1.8%（N1 为 15/213≈7%）。
- observed_in_sample 机读披露齐备（T66 §3.2 月末窗暴露 + 15/850 + C002
  月度统计披露 + 非全盲声明 + 与 C005 共享 FOMC 子集的月末重叠继承声明）。
- 处置沿 N1 先例：不做事后剔除、锁箱/前向确认为确认路径、FRED 最后交易日
  口径实测计数入预筛产物——**接受**，并入 §5 C2/C3 义务。

### 2.7 与 N1/R3 独立性（七元组去重、非重复计数）——PASS（附裁定）

- **七元组**（strategy, params, symbol, timeframe, data_sha256,
  cost_model_version, strategy_sha）：runs 449/450（R1/H101）键恒等去重=1；
  run 451（R3/H102，fomc_preannouncement 策略）独立=1；C006 未来主规格用
  新策略类（macro 公告日策略）→ 键必然不同，无去重碰撞。D33 计数独立正确。
- **候选级独立性裁定（包 §6 交审核裁量，本记录裁定如下）**：
  与 C005/H102 ——**不同源、独立计数成立**。机制假设不同（央行信息溢价/
  公告前漂移 vs 无条件公告日风险溢价）、理论基础不同（LM 信息摩擦 vs
  SW/Ai-Bansal 风险再定价）、事件集主体不同（非 FOMC 占 637/850=75%）、
  文献链仅部分共享（Lucca-Moench 角色：N1 原作 vs 本候选 FOMC 子集复证）。
  四维声明中 signal 维同为 static_long、品种/D1/1 夜结构同款——均如实披露。
  **已知重叠**：FOMC 子集 213/850≈25% 与 N1 事件为同一根 D1 bar（同入场日
  同出场日）→ 统计检验独立（随机化对照各自独立、非 FOMC 事件零共享），但
  组合层面同日同向敞口——接受登记，缓解 = R4 附带诊断义务（FOMC vs
  CPI/NFP 子集分列均值，只报告不否决——对应 N1 月末子集义务先例）+ C3。
  与 C002/H101 ——四维全不同、文献链无共享（Melvin-Prins/Ito-Yamada/KMW
  vs SW/Ai-Bansal/Ghaderi）——不同源成立。
- **机制空缺条件 5（拥挤）**：event 类现已有活跃谱系 H102——装载器将出
  "已有活跃谱系——排后"排序类注记（blocking=False，代码实测）。**裁定**：
  H103 为 event 类内新亚族（公告日聚合风险溢价 vs 央行日程信息溢价），
  非同类别重复；排后注记照单登记，不构成否决。

### 2.8 D33 N 组成独立核验（mode=ro 台账直查 + SQL 独立重算）——见 §3 REV-1

- 台账现状（审核时点 2026-11-07）：runs 共 451 行；run_class=research 且
  round_id 非空 = {449,450,451}；**七元组去重 = 2**（R1:1 + R3:1）。
  R1 closed；**R3 仍 open**；run#451（H102 主规格）created_utc
  **2026-11-07T00:47:20Z**（本地 08:47）；run6 已授权（bd45348，命令与
  run1 逐字恒等）**未落账**——落账后同七元组去重不增 N（run6 授权裁定
  §0.4/三指纹背书，审核者复核命令恒等主张与七元组字段一致）。
- **N2 加入后 N = 1(R1) + 1(R3) + 1(C006) = 3，α/N = 0.05/3 ≈ 0.016667**
  ——审核者独立重算与任务口径一致；随机化次数下限 max(1999,⌈20×3/0.05⌉)
  = 1999。
- 包内声明比对：R4.toml advance_rule 与 H103.toml falsified_if 预写
  **N=2、α/N=0.025**，附让步句"若 R3 主规格先行完成则 N=3……以台账
  ledger.primary_test_n 实际取数为准重列，不硬编码"。**时间线**：包写于
  00:11–00:17（本地），run#451 落账于 08:47（本地）——包内"R3 已开轮零
  运行（台账直查 2026-11-07）"在起草时点为真，审核时点已被台账演进超越；
  让步句精确预写了该情形（与 R3 自身 advance_rule 的让步句结构同款，N1 A1
  修订项 1 先例）。**处理 = REV-1 重列**（§3），非诚实性缺陷。

## 3. 须修订项（2 项；完成后即 APPROVED for future round，仅此两处免重审）

- **REV-1（D33 重列，N1 A1 修订项 1 同款）**：R4.toml `advance_rule` 与
  H103.toml `falsified_if` 中预写 "N=2、α/N=0.025" 更新为让步句成为唯一
  口径的表述：**N = R1 计 1（runs 449/450 七元组去重）+ R3 计 1（run#451
  已落账 2026-11-07；run6 同键去重不增）+ 本候选主规格 = N=3、
  α/N=0.05/3≈0.01667**；保留"开轮时以台账 ledger.primary_test_n 实际取数
  为准重列、不硬编码"句。N2-precheck-spec §7.7 同步（文档级）。
- **REV-2（补强路径表述更正，§2.4 依据）**：N2-precheck-spec §4 末句与
  §5.4 补强路径、R4.toml advance_rule "waiting_data 的可能性实质存在"句，
  更正为如实表述：**按装载器 min(声明 0.23, 隐含) 规则，登记口径 B 的门
  功效由声明值钉死——27.28y→32.3%、35.37y→38.4%（power.py 口径），50%
  需 T≈51–52 年；1986-1993 日历扩展与 σ 实测更低均不能使口径 B 过门
  （σ 更低只抬隐含、被 min() 截断）。预筛判 waiting_data 为登记口径下的
  确定性预期（非"可能性"）；waiting_data 不是 fail，R4 不开；补强路径仅
  对未来依新发表证据的重新登记有意义。** 不得改动登记数值本身
  （expected_sharpe=0.23、gross 8.0bp 冻结——"不回改声明"预写继续有效）。

## 4. 功效门结论（如实预期，非包缺陷）

登记口径 B 下预筛 **必然 waiting_data**（单次功效 32.3%<50%）；口径 A
（54.8%）非登记值。R4 advance_rule 预写"waiting_data 则不开"——闸门行为
诚实。**本登记包的价值 = 机制/文献/日历/成本/独立性要件的完整登记与
waiting_data 状态的如实入账**（铁律 18：检验力不足不开卡），不构成 C002
式真实数据负结果，也无"诚实负结果无出路"问题——出路即等待新证据重新
登记或永久搁置，均合法。

## 5. 条件（C 列表；登记/预筛/开轮全程有效）

- **C1 逐字冻结**：除 REV-1/REV-2 两处授权修订外，提升到 research/
  正式路径的内容与本审核草案逐字一致；任何其他变更即失效须重审重绑
  （announcements.json 9b444ec6… 在规格 §2/H103/R4 三处绑定，一并冻结）。
- **C2 预筛义务**（包 §7 清单逐项有效）：σ 工件 T84/K4 实测重建；覆盖
  年数实测（门取事件时代保守口径 1994-02-04）；成本组成对 calibrated.json/
  specs 快照核对（变则实测改写留痕）；charged 口径逐事件夜数机械复检
  （82 个周四公告事件入场夜=周三 triple 息；NFP 恒周五无跨周末）；FRED
  最后交易日口径月末重叠计数入 precheck JSON（N1 先例）；manifest 无逐年
  声明按 no_yearly_verification_declared 登记。
- **C3 独立性附带义务**：R4 诊断分列 FOMC 子集 vs CPI/NFP 子集均值（只
  报告不否决）+ 15 月末日子集窗贡献（N1 先例）；若与 N1 双双过门，组合
  层面 FOMC 子集同日同向敞口在轮次报告披露。
- **C4 日历工件使用限制**：运行侧必须登记哈希 9b444ec6…；运行期重抓另记
  哈希并说明差异（fail-closed）；建议预筛时抽验若干 token（任一期档案
  URL 可浏览器独立核验——WAF 降级的最小独立补强）。
- **C5 编号与批准**：C006/H103/R4 均占位，提升时由登记环节确认/改号并
  同步；approved_by 保持空直至批准方填写（实现方不得自填）。

## 6. R4 开轮前置清单（引擎/程序义务；开轮另裁）

引擎（现状缺口，包 §7.8 已如实自限）：
- **P1** 新 event kind **macro_calendar**：announcements.json schema
  （events[]/date/types/sources + SHA 绑定 fail-closed + 逐类时代起点 +
  主段窗界），bt.py `--event-kind` choices 扩展 + 成对绑定旗标（类比
  --fomc-calendar*/--event-days-start/end）。现状 choices 仅
  month_end|london_fix|fomc_calendar；fomc_calendar_events 只认
  fomc_meetings.json schema。
- **P2** 策略类实现（H103 strategy 空=legacy）：static_long、前一交易日
  纽约 16:00 入/公告日 16:00 出、1 根 D1、缺锚 fail-closed 丢事件登记。
- **P3** 随机化对照排除集从**冻结日历全部公告日**派生（含被 fail-closed
  丢弃事件之日）；现有 randomize_event_times 的原窗不重叠规则对 hold=1
  蕴含且严于 R4 预写规则，但排除集覆盖面须按本条实现规格写明。
- **P4** swap_free 隔夜申报接线（overnight_nights=1.0/事件）+ charged
  复检（周三 triple 息 82 事件逐夜机械核对）。

程序：
- **P5** FRED USTEC/D1 production input release（现为 real_precheck_only，
  N1 A1 E3 先例）。
- **P6** bt candidate check main-only 隔离证据先审核再放行（t114/N1 清单
  第 2 条已知限制：无 H 线区间/只读参数）。
- **P7** 台账 candidate_checks 最新 decision=pass（main-only）方可开轮；
  按 §4 如实预期，登记口径 B 下不会出现 pass——开轮与否以真实预筛为准。
- **P8** D33 开轮时定格 **N=3、α/N≈0.01667**（按当时台账重列；run6 落账
  不改变 N）；随机化次数 ≥ max(1999,⌈20N/α⌉)=1999。
- **P9** D16 锁箱预算机械确认（台账 lockbox_uses 0 行/0-3，R4 申报 1 在
  余量内；G5 使用另批）。
- **P10** R3 关轮后另裁 R4 开轮（本记录不含开轮批准）。
- **P11** 正式编号分配/复制提升 + approved_by 由批准方按本记录引用填写。

## 7. 审核 independence 复核清单（全部一致）

10 文件 SHA/字节；确定性重建 SHA 恒等；FOMC 子集 257/213 逐位=N1；主段
850/328/328/213−19、星期分布、无周一；月末 15 日三列表恒等；token 算术
392+393+257−20=1022；抽检 5/5；装载器三态（真实 loader、研究库 .venv）；
成本组成 1.14559e-04 与倍数 9.95×/6.98×；σ 参考 1.66615% 逐位；SR_req
0.3150/0.2766；功效 A 54.8%/B 32.3%（power.py 口径）与 T≈51–52y 门槛；
gates [v3.event] 两 veto=false；single_spec=065abf7 在 HEAD；event kind
清单三缺一；七元组去重 SQL=2、R3 open、run6 未落账、锁箱 0/3；时间线
（包 00:11–00:17 vs run#451 08:47 本地）——全部与包内声明吻合，除 §3
两修订项。

---

*审核者注：本记录为占位形态，未 commit；正式定稿/提升处置由批准方与主会话
决定。审核过程零台账写入、零主仓修改（本记录除外）、未派子代理。*
