# V3 诊断层 window_shift 异常降级修复——范围裁定（路径 A 第 1 步，2026-11-07）

裁定者身份：独立范围裁定者，未参与 R3 处置裁定、run5 执行与离线复现。
全程只读：主仓 <workspace>/demo-next-20261105
（HEAD **6c32286**，worktree 对本裁定前状态 clean，直读；72d1014→6c32286
仅新增处置裁定文书一个 DOC 提交，events.py/bt.py/gates.toml 与 run5
运行时代码逐字相同——git diff --stat 直证）；研究库台账
data/research/ledger.sqlite **mode=ro 直查**；生产装配复现用临时副本。
除本裁定记录外零文件修改（复现脚本只写入系统临时目录，用后即删）；
未派子代理；未填 approved_by；未 commit/push。解释器=研究库 .venv
（-I、PYTHONUTF8=1）。

上游文书（逐份直读）：docs/reviews/2026-11-07-r3-disposition-ruling.md
（(b) 引擎缺陷五重证据 + (c) 修复合法性四步链 + §3.4 最小 diff 指定）；
研究库 .superpowers/sdd/2026-11-07-r3-main-test-run5/RUN5-REPORT.md
（§4.2/§4.3 反事实与死因）；research/rounds/R3.toml（trial_budget=2 注释
"1 次主规格 + 1 次代码修复后的重跑"，第 12-14 行原文）。

---

## 总裁定

**有条件批准（approved with conditions）**：路径 A 第 1 步——
events.py `v3_event_diagnostics` window_shift 段的 try/except 异常降级
修复——**在本文书 §2 授权边界内合法且充分**，处置裁定 (b) 项五重证据
**全部独立复核成立**（§1），降级语义不掩盖真实诊断信息（§3），R1 先例
零影响成立（§3.3）。修复须按 §4 TDD 要求实施，进入处置裁定 §3.3 四步
治理链的第 2 步；**第 2 次运行（run6）的授权与执行形态不在本文书权限内**，
本文书仅输出防终止原则约束（§5）供 A1 整体裁量采信。撤销条件见 §7。

---

## 0. 独立核验基础（全部直读/独立复算，全部通过）

### 0.1 台账 mode=ro 直查

- run#451：R3/H102/USTEC/D1/research/bt-gate/engine A-1.7/ok=true；
  data_sha256=h-input-v1:f233c9bb…、gates_hash=d0a8b2f3…、
  strategy_sha=8742cd4f…（后两项见 §0.2 独立重算）。
- gates 行 79-83 = G0 ✓/G1 ✗/G2 ✗/G3 ✓/G4 ✗；**run#451 无 V3 行**。
- 全台账**唯一 V3 行 = rowid 77 / run_id=450**（R1/H101/london_fix）：
  diagnostics 四项齐全，window_shift_1_bar = {passed: True, net_mean:
  -0.0002107…, n_events: 46, **dropped: 7**}——R1 形状下该诊断项完整
  落账且 dropped 计数语义被真实行使（先例证明设计预期=计数报告）。
- rounds：R1 closed / R3 open，R3 toml_sha256==盘上 R3.toml 原始字节
  sha256（72dc6403…，3805 B，独立重算 MATCH）；trial_reservations 恰
  1 条（R3/completed）。两钉定文件 R3.toml 72dc6403…/C005.toml
  d00adad5…（11853 B）哈希独立重算均 MATCH。

### 0.2 身份链独立重算（修复不得改变的三项指纹）

- **strategy_fingerprint**（研究库 .venv 独立调用生产函数）=
  8742cd4f53ab5dc8…==台账 run#451.strategy_sha 逐位一致；闭包=3 个
  策略文件（strategies/base.py、library/buy_hold.py、library/
  fomc_preannouncement.py），**events.py 与 bt.py 均不在闭包内**（T37/
  H3/D23/H-12/X-05 口径：指纹=策略类模块+传递 import 的项目内模块）。
  → 修复 events.py 不改 strategy_sha，七元组不变，N 仍=2（α/N=0.025）。
- **gates_sha256(config/gates.toml)** 独立重算 = d0a8b2f3105366e4…==
  台账 run#451.gates_hash 逐位一致。→ 修复不得触碰 gates.toml。
- **ENGINE_VERSION**="A-1.7" 为 src/mt5quant/backtest/engine_a.py 硬编码
  常量（第 115 行），与 events.py 无派生关系。

### 0.3 生产装配独立复现（研究库 .venv，临时台账副本，只读冻结输入）

与 run5 worker 同一生产链（bt._h_declared → _fomc_event_binding →
_load_h_research_inputs → _h_event_setup）装配后：

- 合并序列（USTEC/D1，10,292 bars）1996-06-28..07-09 邻域 bar 日期 =
  {06-28, 07-01, **07-02, 07-03**, 07-05, 07-08, 07-09}——**1996-07-04
  无 bar**（独立日假日，独立证实）；
- 事件 fomc_calendar:1996-07-03（entry=07-02, exit=07-03）**原始窗
  strict 解析成功**（n=1, dropped=0）；
- `shift_events_by_bars(+1)` 单事件 → exit=1996-07-04 →
  `event_returns` 抛 **SignalUnavailable: "event unavailable: exact
  window missing for fomc_calendar:1996-07-03"**；全 213 事件平移集
  同样抛出（首个失败标签同一事件）；
- **新事实（处置裁定未载）**：平移集 strict 不可解析事件 **2/213**——
  fomc_calendar:**1996-07-03** 与 fomc_calendar:**2002-01-30**（后者
  =E7 已证实的无 2002-01-29 bar 事件，平移后入场锚 01-29 精确匹配
  失败）。降级后 reason 只会引用迭代序首个失败标签（1996-07-03），
  读 run6 台账行时不得误读为"仅一事件受影响"；真实计数 2/213 由
  报告层并列（离线可复算，不要求引擎记录）。
- `v3_event_diagnostics`（sims=50）现状**原样抛出 SignalUnavailable**
  ——缺陷经生产函数确定性复现（year_block 段先完成，异常发生在
  window_shift 段）。

### 0.4 调用面与测试覆盖盘点

- `v3_event_diagnostics` 生产调用点**恰一处**：scripts/bt.py:1612
  （`_v3_gate_evaluate` 事件分支，无 try/except）。tests/ 内两处引用：
  test_t112_research_inputs.py:385 为 monkeypatch 桩（不受影响）、
  test_validation_events.py:779 为非 strict 回归基线（§4 保留）。
- **tests/ 全目录现无任何 strict_windows / SignalUnavailable 直接测试**
  （grep 零命中）——strict 主检验域的 fail-closed 语义目前只由生产代码
  与评审承担，无测试钉定；本文书 §4(c) 因此新增主检验域钉定测试。

---

## 1. 缺陷定性五重证据复核（逐项直读，全部成立）

1. **冻结规范三处一致排除**（三处原文均直读）：
   (i) config/gates.toml [v3.event].diagnostics 数组注释"D35 修改 3：
   诊断四项（**只报告，不否决**）"，window_shift_1_bar 明列第 2 项；
   (ii) research/rounds/R3.toml advance_rule："诊断四项（年块自助对 0、
   事件窗前后各移一根 bar、分段均值、已知缺口注记）**只报告不否决**"；
   (iii) research/rounds/R1.toml advance_rule 同句。**成立**。
2. **函数 docstring 明文覆盖异常情形**：events.py:1024-1035——"全部
   只报告，不参与否决"、"passed 恒 True（报告层语义：**诊断失败不改变
   关卡结论**）"。配套：validation/gates.py `evaluate_v3`（768-789 行）
   判定 passed=all(veto.values())，diagnostics 仅进报告字典
   （`{name: bool(check.get("passed", False))}`），**结构上不可能翻转
   关卡结论**。**成立**。
3. **同函数不对称保护=疏漏内证**：events.py year_block_vs_zero 段
   （1041-1050）`try/except ValueError` → `{"passed": True, "reason":
   str(e), "note": …}`；window_shift_1_bar 段（**1051-1059**：
   shift_events_by_bars + event_returns + 输出构造）**无同等保护**；
   且输出结构含 "dropped" 计数字段（1058）——非 strict 路径的计数
   语义（events.py:508），R1 rowid 77 dropped=7 为其真实行使记录。
   **成立**。
4. **strict_windows 语义域越界**：events.py:499-514，动机注释（500-501）
   原文 "Missing observations must not disappear from a **randomized
   null sample**"；合法域=主检验随机化抽样（randomization_test 789-824，
   观测+1999 null 抽取全走 strict）与装配段原始观测事件窗可成交性
   （scripts/bt.py:3827-3829 exact trade window 检查——本次核验中该
   检查对 london_fix 缺窗形状正确触发，fail-closed 在合法域内工作
   正常）。诊断平移副本既非 null 样本也非主检验观测；run5 主检验
   213 观测+1999 抽取 strict 下全部成功（p=0.0085 可复算）。**成立**。
5. **数据形状必然性（非偶发）**：§0.3 独立证实 1996-07-04 无 bar、
   单事件与全集合平移均确定性抛出；fomc_calendar 213 事件含
   1996-07-03 事件必撞独立日。结构性形状，非可回避边角。**成立**。

**定性边界（沿处置裁定 §2，本裁定加重申）**：不认定 strict_windows
机制缺陷（主检验域行为正确且必要）；不认定 SignalUnavailable 机制缺陷
（防静默丢观测语义正确）；缺陷恰一处=诊断层 window_shift 段把主检验域
的 fail-closed 异常未捕获地传播为整关卡失败（经 bt.py:4975-4980 `_run`
包装 → {"passed": False, "error": …} 错误行）。

**补充定性（本裁定新证，强化而非扩大定性）**：R1 两笔运行创建于
2026-10-28，早于 strict_windows 引入（2026-11-02，commit 6f33ad5）；
rowid 77 的 dropped=7 产于前 strict 时代。代码推导：strict_windows=True
时 events.py:508 的 dropped 计数分支不可达，任何含≥1 不可解析平移窗的
事件族（含 R1 自身的 london_fix 形状）在现行引擎下重演都会在同一位置
抛出——缺陷是 **strict×诊断平移副本的引擎级交互**，非 fomc_calendar
数据特有（与处置裁定 §4.5 对 N2 的前置修复义务互证）。

---

## 2. 授权 diff 边界（唯一授权的代码改动）

### 2.1 授权改动（唯一）

- **文件**：src/mt5quant/validation/events.py，**仅**函数
  `v3_event_diagnostics`（1020-1077）内；
- **行区**：**仅** window_shift 段 **1051-1059**（`shift_events_by_bars`
  调用 + `event_returns` 调用 + `out["window_shift_1_bar"]` 赋值），整段
  包入 try/except，与 year_block 段（1041-1050）**同构**；
- **异常捕获面**：`except ValueError as e`——**SignalUnavailable 是
  ValueError 子类（events.py:317 `class SignalUnavailable(ValueError)`，
  直读证实）**，故该捕获面天然同时覆盖 SignalUnavailable 与 ValueError，
  与 year_block 段捕获面完全一致。等价可接受形态：显式
  `except (SignalUnavailable, ValueError)`（冗余但自文档化）。**禁止更宽
  捕获**（bare except / Exception）——真实实现缺陷（KeyError/TypeError
  等）必须继续向上传播；
- **降级记录结构**（与 year_block 降级分支同构，键集一致）：
  `{"passed": True, "reason": str(e), "note": <只报告语义注>}`——
  (i) reason=**实际异常消息原文**（含失败事件标签，如 "event
  unavailable: exact window missing for fomc_calendar:1996-07-03"），
  不得以合成摘要（如泛化 "shifted window unavailable"）替代——原文
  才是真实诊断信息载体；(ii) **不得**在降级分支补 net_mean/n_events/
  dropped 键（strict 下 frame 未产出，值不可得；见 §3.1）；
  (iii) passed 恒 True（报告层语义）。
- **docstring**：允许在函数 docstring 补一句降级说明（可选，不改既有
  语义表述）。

### 2.2 不得触碰面（越界任一=撤销条件）

1. `event_returns` 全函数（含 strict 分支 499-514——主检验域 fail-closed
   语义一字不动）；
2. `randomization_test`（789-824）、`shift_events_by_bars`（965-983）、
   `segmented_means`（986-1017）、`SignalUnavailable` 类定义（317-318）、
   `DeclaredEventSide`（383-437，含 strict_windows=True 类属性）；
3. `v3_event_diagnostics` 内其余段：obs 初次调用（1036-1039）、
   year_block 段（1041-1050）、segmented_means 段（1060-1062）、
   known_gaps 段（1063-1076）；
4. scripts/bt.py 全文件（含 `_v3_gate_evaluate` 1612-1615 调用、
   `_run` 4975-4980 包装语义、装配段 3827-3829）；
5. config/gates.toml（gates_sha256 必须==d0a8b2f3…不变）；
   research/rounds/R3.toml、research/candidates/C005.toml（台账钉定，
   原始字节 sha 72dc6403…/d00adad5…不变）；
6. src/mt5quant/strategies/**（strategy_sha 必须==8742cd4f…不变）、
   src/mt5quant/backtest/engine_a.py（ENGINE_VERSION）、研究库台账
   一切写路径；
7. 除上述 events.py 单函数单段外的一切文件（含 mql5/、config/、docs/
   除评审文书惯例新增外）。

### 2.3 身份不变性（已由 §0.2 独立重算背书）

修复后须复核：strategy_fingerprint 仍=8742cd4f…、gates_sha256 仍=
d0a8b2f3…、两钉定文件 sha 不变 → run6 与 run#451 七元组相同 →
primary_test_n 不增（去重口径先例 R1 runs 449/450=1）、α/N 仍 0.025、
确定性 seed 下主检验数值逐位可预期复现——**无选参自由度、无结果挑选
空间**（沿处置裁定 §3.6）。

---

## 3. 降级语义边界风险裁定

### 3.1 记什么 / 不记什么

- **记**：passed=True + reason（实际异常消息原文，含首个失败事件标签）
  + note（只报告语义）。**不记**：合成 dropped 计数、net_mean/n_events。
  理由：(i) strict 抛出即中止，frame 从未产出——任何 dropped 数字都
  需要额外一次非 strict 扫描=新计算路径+strict/非 strict 语义混排，
  超出最小缺陷修复，属设计变更，不在授权内；(ii) R1 先例的 dropped=7
  产于前 strict 时代的非 strict 路径——在 strict 时代为诊断副本重建
  非 strict 语义是重新设计，不是修缺陷。真实计数（本案 2/213，§0.3）
  可由报告层离线复算并列，不要求引擎记录。

### 3.2 会不会掩盖真实诊断信息——不会，三向对比

- vs 修复前反事实（引擎错误行）：错误行丢失全部四项诊断与关卡判定；
  降级后保留 reason+其余三项+完整判定——**信息严格增加，零抑制**。
- vs "设计理想"（非 strict 计数）：该理想随 strict 接线（6f33ad5）对
  主检验域的正当强化而在诊断段失效——恢复它=设计变更非缺陷修复；
  且 year_block 段同构降级先例（1048-1050）已是函数内既定设计语言。
- vs 主检验域：主检验/随机化抽样路径一字不动（§2.2-1），fail-closed
  在其合法域内语义完整保留（§1.4 装配段触发实证）。

### 3.3 R1 先例零影响论证（run#450/V3 rowid 77 完整完成的先例）

1. **台账不可变性**：append-only 台账无行改写路径；修复不触任何台账
   代码；rowid 77 是历史工件，不存在"被改写"的通道。
2. **R1 已关轮**：rounds R1 status=closed；close_rule"本轮不能再运行"
   ——不存在以 R1 名义经修复后代码重演的合法路径。
3. **非异常路径逐字节不变**：try/except 仅在异常路径生效；对平移集
   全部可解析的输入（含 R1 形状在非 strict 调用方下的等价物），输出
   dict 的构造代码、键序、数值完全不变——由 §4(b) 回归测试钉定。
4. **非 strict 调用方零影响**：无 strict_windows 属性的 side_fn（如
   现存 T86 测试 always_long、bt.py:1565 兼容 lambda）永不触发 except，
   dropped 计数行为原样。
5. **R1 形状在现行引擎下的真相反而受保护**（§1 补充定性）：若 R1
   事件族在现行 strict 引擎下重演，修复前将在同一位置崩溃，修复后
   降级——修复只会扩大"能完整落账的形状集合"，不会把任何原本能
   完成判定的形状变成降级（except 只在现行为=抛异常处生效）。

---

## 4. TDD 要求（处置裁定 §6.2a 的细化，实现步必须逐条满足）

### 4.1 RED（先写测试，修复前必须观察其失败）

新测试（tests/test_validation_events.py T86 区段）：
`test_v3_event_diagnostics_window_shift_degrades_on_strict_holiday_hit`
——合成 D1 bar 序列含假日缺口（形状=1996-07-03 事件平移 +1 撞
1996-07-04 无 bar，即本裁定 §0.3 复现形状的最小合成版），side_fn=
`ev.declared_event_side("static_long")`（DeclaredEventSide，
strict_windows=True 类属性；static_long 无需 signal bars）。断言：

1. `v3_event_diagnostics` **不抛异常**（修复前本断言失败=RED，现状
   SignalUnavailable 传播，§0.3 已证）；
2. `diags["window_shift_1_bar"]["passed"] is True` 且含 "reason" 键，
   reason 内含 "exact window missing" 与失败事件标签；
3. 降级分支**不含** net_mean/n_events/dropped 键（键集与 year_block
   降级分支同构）；
4. 其余三项诊断结构照常齐全；
5. **降级不改变关卡结论**：以该 diagnostics 调 `vgate.evaluate_v3`
   （G0/primary/cost_scale 构造通过、delay/buyhold applicable=False），
   断言 verdict["passed"] 与 diagnostics=None 时一致，且
   verdict["diagnostics"]["window_shift_1_bar"] 为 True。

### 4.2 GREEN 后回归（全部必须通过且修复前亦应通过）

- (a) 现存 `test_v3_event_diagnostics_reports_four_items`（非 strict
  路径，net_mean/n_events+dropped==len(evs) 守恒断言）**原样通过、
  零改动**；
- (b) 新增：strict side_fn + 平移集全部可解析的合成形状 →
  window_shift 含 net_mean/n_events/dropped 且 n_events+dropped==
  len(evs)（钉定正常路径逐字节不变）；
- (c) 新增（主检验域零触碰的测试钉定，现存零覆盖故必须新增）：
  strict side_fn 下 `event_returns` 对缺窗/不精确匹配**仍然抛
  SignalUnavailable**（两个分支 507/514 各一例）——把主检验域
  fail-closed 语义钉进测试，防修复副作用；
- (d) 冻结源比对（GREEN 后留档）：git diff 范围==§2.1 授权边界（仅
  events.py 该函数该段）；gates.toml/R3.toml/C005.toml/strategies/**
  /bt.py/engine_a.py 零字节变化；strategy_fingerprint 重算==8742cd4f…、
  gates_sha256 重算==d0a8b2f3…（§2.3）。

### 4.3 测试红线

测试不得为迁就实现而改断言语义；不得新增对 run6 台账行内容的预写
期望（处置裁定 §3.7：不预判 V3 行内容，以引擎真实行为为准）。

---

## 5. run6 防外部终止原则约束（裁定输入，机制选择权在 A1/执行模块）

**run5 死因确认**（kill-forensics.txt 直读）：引擎作为 harness 后台任务
于 08:47:13 启动，~09:36-09:47 被外部终止（exit 137，任务通知
status=killed，非执行方 TaskStop）——**存活窗 ~49-60 分钟**；而 V3
随机化 1999 抽取单进程实测需 **~5.7-5.9 小时**（~10.3-10.7 s/抽取×
1999）。死因=执行载体存活窗与引擎运行时的**结构性错配**，非偶发。

裁定约束（四条原则，具体机制——分离进程/Windows 计划任务+轮询/
独立会话/nohup 类脱离/其他——留给 A1 整体裁量或执行模块选择）：

- **P1 存活独立性**：run6 引擎进程必须由**其存活不依赖任何会话型
  harness/对话任务生命周期**的执行载体承载；授权文书（A1 整体裁量）
  必须在启动前指明执行形态并给出该形态存活属性的论证（预期运行时
  ≥6h 量级）。任何"后台任务 + 事后补看"形态若无存活论证即不满足。
- **P2 台账 V3 行必须引擎本体写入**：V3 gates 行只能由引擎进程经
  生产 bt gate 路径 append 写入；离线复现/补算产物只能作留档（沿
  处置裁定 §3.5 原文）；轮询观察者只读；任何一方不得合成、预写、
  回填该行（处置裁定 §1：A1 不得以复现代行引擎判定职能）。
- **P3 预立再终止条款**：授权文书必须预写"若 run6 再被外部终止"的
  处置条款（沿处置裁定 §3.5 要求），并按 run5 形态全量留档（命令
  副本/运行前核验/输出/台账前后状态/报告）。
- **P4 命令原文**：run6 命令=run1 command.txt 原文逐字（沿处置裁定
  §3.5/§6.2b）；零台账人工写入。

---

## 6. 治理链四步确认（沿处置裁定 §3.3/§6.2a；本文书=第 1 步输出）

| 步 | 文书/动作 | 输入 | 输出 | 状态 |
|---|---|---|---|---|
| 1 | **本范围裁定**（docs/reviews/2026-11-07-v3-diag-degrade-scope-ruling.md） | 处置裁定 §3.4 边界建议 + run5 报告 §4.3 + R3.toml + 本裁定 §0 独立核验 | 授权 diff 边界（§2）+TDD 要求（§4）+防终止原则（§5）+撤销条件（§7） | **本文书** |
| 2 | TDD 实现 | §2 边界 + §4 要求 | RED/GREEN 证据 + 回归 + 冻结源比对（§4.2d）留档 | 待执行 |
| 3 | 聚焦评审文书（docs/reviews/） | 第 2 步产物（diff+测试证据） | PASS/FAIL 与发现项 | 待执行 |
| 4 | A1 级整体裁量文书 | 第 1-3 步全部产物 + §5 原则 | 授权（或拒绝）第 2 次运行（含执行保活形态与 P3 条款） | 待执行 |

第 4 步通过后：按 run1 命令原文执行 run6（保活形态，~6h）→ 台账
run#452+V3 行（引擎本体写入）→ 按 advance_rule/close_rule 以真实 V3
行判定与关轮（G5/锁箱另批；沿处置裁定 §6.2）。

---

## 7. 撤销条件（任一成立本文书授权即失效，回 A1 级重审）

1. 处置裁定 §5 全部撤销条件（六项上游条件）任一被触发；
2. 本裁定 §0 独立核验被证伪（1996-07-04 无 bar 事实、strategy_sha/
   gates_hash 重算、台账 V3 行缺位事实、SignalUnavailable⊂ValueError
   类关系任一被更高权威推翻）；
3. 实施 diff 越出 §2.1 边界或触碰 §2.2 任一不得触碰面（含捕获面宽于
   ValueError、降级记录键集超出同构形态、合成 dropped 计数）；
4. TDD 证据不符 §4（RED 未先于修复观察失败、回归 (a)-(d) 任一缺失或
   被改写迁就、冻结源比对失败）；
5. 出现新的冻结规范文本反证（证明诊断异常升级为关卡失败系明文设计）
   ——同处置裁定 §5.3；
6. run6 未经第 4 步 A1 整体裁量授权而执行，或执行违反 §5 P1-P4、
   或台账 V3 行非引擎本体写入。

---

## 8. 执行清单（谁做什么）

- **实现方（第 2 步）**：按 §2.1 实现、按 §4 留 RED/GREEN/回归/冻结源
  比对证据；不 commit 由主会话集成（沿先例）；零台账写入。
- **聚焦评审（第 3 步）**：逐项核 §2 边界与 §4 证据链。
- **A1 整体裁量（第 4 步）**：采信/驳回；指定 run6 执行保活形态与
  P3 再终止条款。
- **主会话**：路径选择登记（沿处置裁定 §6.1）；文书集成与 push 裁量。
- **本裁定者**：不执行上述任何机械步骤；本记录为唯一输出。

## 9. 签署（占位）

- 裁定者：独立范围裁定者（2026-11-07；只读核验留档于本记录 §0）
- approved_by：____________（占位，主会话裁量；本裁定记录不代填）

（裁定只写本文件；不 commit，主会话集成。）
