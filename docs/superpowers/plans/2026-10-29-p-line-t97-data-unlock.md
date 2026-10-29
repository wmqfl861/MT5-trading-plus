# T97 计划 v2：P 线数据解锁批（ext 合并取数 + 情景点差 + 新版本候选 C104–C106）

**审核链**：T77 §6 轮次决定已经 P 线审核子代理审核（维持"不提交 R2 草案"，批准 T97
方向 + 6 项先决）；本计划 v1 经 P 线计划审核子代理审核出 5 条必改，v2 全部并入。
**T97 不承诺开轮**——合并后触发日数乐观 ~15–150 日 vs 要求 >7,424，大概率仍
waiting_data；可能翻转的只有 EURUSD 的事前成本关（情景点差下实测）。§7 须把
"bid-only 与引擎 ask=bid+点差（Costs 接口）天然一致"写成显式口径行（先决 3 可审计）。

## 先决条件（硬约束）

1. 合并语义：重叠期 MT5 为准、ext 只前向延长（去重证明入测试）；裁 research_cutoff；
   bar 带来源标记（"mt5" / "ext:histdata:2009"）。
2. 情景点差**同时接 precheck 与标签**（v1 削减项，已改）：标签经引擎 Costs 接口用
   点差定成交与首达胜负——ext 段触发的标签必须用情景倍数包装的 Costs；precheck
   双档报告。倍数出处（v1 的 3.0 无据，已改）：**保守绑定档沿用 H 线
   `robustness.py EARLY_YEARS_SPREAD_FACTOR = 1.5`（同源先例）；双档 [1.0, 1.5]；
   3.0 作非绑定参考档并列报告**（未实测的保守上界，如实登记）。
3. bid/ask：HistData bid-only 与"bar=bid、ask=bid+点差"一致，§7 显式口径行。
4. 洞：合并后按 **月 × 来源段** 检查周日开盘小时（UTC）唯一性（含美国 DST 切换的
   月允许两个相邻小时；MT5 段豁免——实证其周日首 bar 多为 0 点属券商时段结构）；
   v1 的"周一 UTC + 原始时戳换算"方案废弃（无原始时戳、周一恒 0 点空转、循环论证
   ——审核实证）。触发抑制（包含式定义写死）：洞 G={(s,e)}（s=洞前末 bar、e=洞后
   首 bar、宽 >max_gap_days）；触发 bar i 的回看窗 W=[i−M, i]（S1: M=24 根；
   S3: M=480 根；S2: W=[前一会话首 bar, i]）；**抑制 ⇔ ∃(s,e)∈G 使 s、e 均严格
   落在 [t_{W首}, t_i] 之内**（洞止于窗首或始于触发 bar 均不抑制）。
5. D51：approach/bracket/Δp 与 C101–C103 逐字相同；增量只有数据覆盖与品种。
6. 全版本留痕：每次 precheck 入台账（既有机制自动）。

## 模块与波次

### M1（子代理甲，波次 1）：`src/mt5data/ext/merge.py`（新文件）+ `tests/test_ext_merge.py`
- `merged_research_series(data_root, symbol, timeframe) -> (bars, source_tags,
  gap_spans, bad_months)` 一体管线：MT5 bars + ext（仅 `yearly_verification` 
  used=true 年份、只取 MT5 first_time 之前）→ 排序去重（重复时间戳 MT5 为准）→
  裁 research_cutoff → 周日开盘小时月度自检（上述定义，MT5 段豁免）剔坏月 →
  gap_spans（>30 天；与既有 `_GAP_SPLIT` 边界一致性入测试）→ 返回四元组。
- 测试（合成数据）：去重/重叠期 MT5 为准；cutoff 裁剪；used=false 年份不进；
  周日小时自检（正常月/DST 切换月双值/错一小时月）；gap_spans 合成缺口与 30 天
  边界；source_tags 正确。
- 不改既有文件（mt5data 零 H 线影响）。

### M2（子代理乙，波次 1，与 M1 无文件交集）：登记与文档
- C101–C103 回填 `status = "screened_fail"`（行内注释引报告 §4）。
- 新登记 C104–C106（S1/S2/S3；symbols=["XAUUSD","EURUSD"]；逐品种 level_grid
  （EURUSD 0.0050）；approach/bracket/Δp 与 C101–C103 逐字同；[prob] 新增可选键
  `max_gap_days = 30`、`cost_scenario_mult = [1.0, 1.5]`、
  `cost_scenario_reference = 3.0`（非绑定参考档）——**同一波次**同步扩展
  `_validate_prob`（可选键、缺省不破坏旧候选——探针 `2026-10-28-pline-probes.py`
  的 prob_data 不带新键，行为须逐位不变）；notes 引用前驱。
- EURUSD 文献增量（Osler 原始市场即含主要货币对的 S/R 证据；检索记录一行）。
- 报告 §7 骨架：目的/口径（含 bid-only 显式行、情景倍数出处=H 线 1.5、3.0 参考）/
  预期管理（大概率 waiting_data）。
- 测试：`tests/test_prob_precheck.py` 增 C104 装载/新键校验（缺省兼容）/C101 状态
  回填守护。

### 波次 2（M1+M2 合入后，子代理丙）：接线与实运行（含行为级测试——v1 缺失项）
- **取数换源**：`scripts/bt.py` 两处装载点（`_candidate_check_prob` 的 L≈1411 与
  `bt prob test` 的 L≈2041）概率分支改用 `merged_research_series`（ext 触发判定
  机制择一：按"bar 时刻 < 该品种 MT5 first_time"定 era，写死）。
- **抑制 helper**：`mt5quant.prob` 新增共用函数（counting 与 p1 都调）：给定触发
  bar 下标、窗口类型（S1/S2/S3）、bars/gap_spans，按先决 4 包含式定义判定。
- **情景点差**：precheck（candidates.py）ext 段触发成本双档 [1.0, 1.5]，保守档
  绑定 `ex_ante_cost_prob.passed`，1x 与 3.0 参考档并列进 checks；标签路径
  （p1.run_p1_card / bt prob test）：ext 段触发单独用 `ScenarioCosts(costs,
  1.5)` 包装跑 label_triggers，MT5 段用原 costs，结果拼接（缺省场景 [1.0,1.0]
  时包装为恒等——旧卡/探针零变化）。
- **行为级测试**（v1 缺失，必补）：合成候选"1x 档过、保守档 fail → 整体 fail"；
  跨洞触发被抑制/洞后正常触发保留；ext/MT5 段拼接标签各用对的成本（用计数式
  Costs 记录调用价验证倍数生效）。
- **实运行**：`bt segment propose --symbols XAUUSD,EURUSD`（写进 §7）；`bt
  candidate check C104–C106` 照实填 §7（双档成本、逐假设触发日数 vs 7424、
  结论；EURUSD 成本关实测是本批关键产出）。

## 验收
- M1/M2/波次 2 测试 + 全量回归绿；探针三文件全 PASS（全名：
  `docs/reviews/probes/2026-10-28-pline-probes.py`、`-03.py`、`-04.py`——
  不带新键的卡行为逐位不变）；
- 三份新 precheck.json + 台账入账；报告 §7 定稿；CI 绿 → 勾选（主会话）。

## 明确不做
- approach/bracket/Δp 调整（D51）；开轮承诺；改 C101–C103 的
  symbols/hypotheses/预期；绕过锁箱 cutoff；改 H 线行为（mt5data 只增新文件）。
