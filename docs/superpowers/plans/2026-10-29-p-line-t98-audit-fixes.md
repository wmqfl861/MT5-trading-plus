# T98 计划 v2：P 线全项目审核修正（codex 审核 P-01–P-13）

来源：`docs/reviews/2026-10-29-global-code-audit.md` §P。v2 按计划审核子代理 8 条必改修订。
硬约束不变。**P-10 不做**（H 线未落地）；**fittable G3/V3 拒绝守卫为不可回归项**。
**P-13** 归模块丙（只补校准/边界测试，不改方法参数）。

## 波次 1（四子代理并行；bt.py 单主归甲、candidates.py 单主归丙）

### 模块甲：P-01 合并取数边界 — `merge.py` + `bt.py` 的 `_prob_load_bars`（bt.py 唯一波次 1 改动）
- merged_research_series 加可选 `end=None`；返回前对合并后全序列统一校验有效上界（全局
  lockbox cutoff 若存在、ext manifest research_cutoff、导出 source_cutoff、end、已收盘水位
  取最严）；**缺/冲突的来源 meta** 保守拒绝（非"无 lockbox.json 即拒"）。
- `_prob_load_bars(use_merged=True)` 传 end，越界拒绝。
- 测试：主库/导出库/有无 ext/cutoff 两侧合成 bar/来源 meta 缺失冲突/end 生效与越界拒绝/
  max_gap_days 与 cost_scenario 分支/计数与 prob test 入口一致性。

### 模块乙：P-07 获准动作与 exit_rules — `level_decision.py`（仅此文件；bt 侧核对挪波次 2）
- ALLOWED_HYPOTHESES 类常量（空=全部 A1–A4 兼容）；fit/generate_signals 双重过滤。
- exit_rules 改 `__init_subclass__` 或 property 重建（子类 SL/TP 变化跟随）。
- 多品种 SYMBOLS 拒绝或单品种绑定；**with_model 核对模型 dict 的 symbols/timeframe/bracket
  与类常量一致**（不一致拒绝）。
- 测试：仅 A3 获准但 A1 EV 更高不交易；SL/TP 子类 exit_rules/标签/回测一致；模型/类配置
  冲突拒绝。

### 模块丙：P-09 + P-11 + P-13 — `gaps.py`、`p1.py`、`counting.py`、`prob_mode.py`、
`candidates.py`（含丁的 P-03 docstring 注记）
- **共同可用集合 helper** `eligibility_mask(triggers, bars, gap_spans, situation, approach, K)`：
  S1/S3 回看窗 + **所有局面的 A2/A4 动作从 anchor 根起算**（triggers 含 anchor 列）+ 特征
  预热 2000 根 + 持有窗 K 根不跨 >max_gap_days 洞；counting 与 p1 共用。
- `randomize_triggers` 加可选 `eligible_bars=None` 参数（缺省全域行为不变——护住探针 03）；
  run_p1_card 显式传入合格 bar 集。
- **era 边界消费 merge 实测起点**：`gaps.mt5_first_time` 改为可用 merge 返回的 source_tags
  实测（或等价接口）；卡声明 cost_scenario_mult 而边界不可得时 **ValueError**（不静默 1×）。
- 报告口径（P-11）：real/null winrate 排除 skipped（与 delta_p_test 同口径）；n_triggers 改
  n_total/n_usable/n_skipped 三报；`plant_delta` 修复 .loc 位置索引 bug；prob_mode 的
  brier_skill/holm 补 sims/有限数/形状/概率区间/有限成本校验。
- P-13 测试：tables.py 父格 n=1/2/99/100、全赢全输、缺失特征、同日重叠、K 跨换日三倍日
  （只补测试不改参数）。
- candidates.py P-03 docstring 注记：锚点含义="名义触发日数"，旧标定数值因抽样单位偏差
  需重审；不改数值/插值逻辑。

### 模块丁：P-02 + P-03 + P-12 标定修正 — `scripts/prob_calibration.py`
- **P-02 成簇负对照**：real 与 null **均取平移后时刻**（null = 同种子下另一次独立平移+独立
  置换，**非** real 平移时刻的子集——删嵌套选项）；偏移改为**整周粒度宽范围**（wk + 0..51
  周，NY 时区往返 DateOffset 保本地小时，处理 DST 不存在/歧义）；越界丢弃（不做 nearest
  末端堆积）。捕获式标签器测试：两组均无原始触发时刻。
- **P-03 样本单位**：保留有放回按日抽（名义 n 不再声称=实际日数）；以 `delta_p_test` 的
  n_days_real/n_days_null 为**记录与锚点解释口径**；pool 源指纹入 fingerprint；
  **标定 build_costs 传 swap_ref_price（与正式 P1 同口径：全序列最后收盘）**。
- **P-12 merge manifest**：run 时生成 job manifest（phase×条件×seed×shard 完备集），merge
  校验精确匹配与无重复；**停止删除 pool.meta.json**（保留来源指纹；标签池本体仍删）。
- 测试：缺片/重复拒绝；名义 n≠实际日数时记录一致；cluster 两组均无原始时刻。

## 波次 2（波次 1 合入后，单子代理）：bt.py 集中修正 — P-04 + P-05 + P-06 + P-08 + P-07 bt 侧

- **P-04**：round open 逐条核对预筛 per_hypothesis passed（台账取）→ 冻结族入 rounds 行
  （新字段，旧轮兼容=空→磁盘文件+sha 校验等价）；_round_power_recheck 对 probability 不
  再跳过——用**冻结族条数**复核一次，不足者机械移除、家族不缩小；round-report：比对开轮
  toml_sha256、**取消磁盘 precheck 回退**、**拒绝家族外/额外运行**。
- **P-05**：策略指纹绑定提前到 run_p1_card 前；逐假设 started→success/failed 入台账（异常
  记 error 行）；cmd_fit 补完整准入链（committed/clean/候选预筛/轮次开启+批准+sha/预算
  ——预算非原子为 P-10 残留，注明）+传 __fit__.sizes/policies + 接受 standard+validation
  P2 卡；cards-dir 相对路径 absolutize（防御 H-09）；**测试含真实内置策略 fit 合成数据
  完整跑通**。
- **P-06**：成本循环逐品种保存 bars_full/参考价/成本版本/政策（消灭尾变量复用）；
  params.prob 加全品种 data_sha 映射、全品种 cost_version 映射、sims（prob_version 已在）。
- **P-08**：_slice_validation_eval 改 searchsorted+iloc 按实际 bar 数取预热；
  **_ZeroedBeforeSplit 改为 run 级归零**——识别 inner 信号连续非零段（目标仓位 run），凡
  run 起点在 split 前的整段置 0（含 split 后尾巴）；split 后新触发不受影响；交易/绩效从
  split 起算（引擎或 summary 后过滤——只改 P 适配层）。
- **P-07 bt 侧**：_check_card_strategy_constants 扩展核对 symbols/timeframe/exit_rules。
- 测试：各自行为级回归 + codex 探针中 P 相关行为翻转验证。

## 验收
- 全量回归 + 三探针全 PASS；codex 复现脚本中 P 相关行为的修复验证。
- 标定重跑属下一批批准点，本批先修代码与测试。

## 明确不做
- 引擎成交语义/实盘链路/gates.toml；三套受保护探针；H 线所有权文件；P-10；P-13 方法参数。
