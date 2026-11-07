# 范围裁定：bt gate 事件卡选参能力扩展（single_spec）——CONDITIONAL APPROVED

2026-11-07。裁定者=独立范围裁定者（未参与缺口调查、实现与 R3 执行链）。
裁定对象=研究库 `.superpowers/sdd/2026-11-07-r3-main-test-run4/event-card-param-gap-scope.md`
提交的最小扩展请求（形态 a）+run4 证据（同目录 RUN4-REPORT.md）。
本记录**不 commit**（占位符形态；签署栏留空，由主会话裁量填署）。

裁定过程只读：主仓 HEAD 8f7d0ec（工作树恰两预期 M 文件=形态复原产物，文本
diff 为空）；台账 mode=ro 直查+临时副本 verify_chain；解释器=研究库 .venv
（-I 隔离、PYTHONUTF8=1）；未派子代理；除本记录外零文件修改、零台账写入。

---

## 0. 总裁定

**CONDITIONAL APPROVED（有条件批准）**——批准形态（a）最小扩展，附下列
约束性条件（C1-C6）。核验未发现调查文档任何结论性错误；发现两处实现级
细节须按条件闭合（C2/C3），另有一处调查表述的不精确（见 §1 核验 2 注）
不影响结论。

**条件（全部为授权生效前提，聚焦评审逐项核对）：**

- **C1（diff 边界）**：改动严格限于 §2 授权清单；`_SELECTION_METRICS`
  dict 本体零改动；`_select_params_auto` 的两枚举算法路径（SQL 查询、
  过滤、取最大/并列取 run_id 最小、返回结构）行为逐字节不变。
- **C2（防御路径消息不退化）**：`_check_card_lineage` 合法集放宽后，
  `_select_params_auto`（bt.py:1193-1194）对 rule="single_spec" 的直调
  防御路径不得退化为 `ValueError(None)`——1193 行的判据仍用 dict 原体
  （single_spec 仍不在其中→仍 raise，fail-closed 语义不变），但 1194 行
  的消息构造须自持（兜底文案），不得依赖已放宽的 `_check_card_lineage`
  返回值。此为裁定者独立发现的实现缺口，调查文档未覆盖。
- **C3（报告第三分支）**：关卡报告参数选择段（bt.py:5106-5115）必须加
  single_spec 第三分支，防止误标 manual（调查设计已含，此处升为硬条件）。
- **C4（TDD 与回归）**：RED 先行留档证据 → 最小 diff → 新测试 GREEN；
  §5.3 回归清单既有测试**一字不改**全绿 + 全量测试套件绿。
- **C5（冻结面零触碰证明）**：聚焦评审时提交冻结源空 diff 证明（§4 步骤
  3 清单），含 gate.py 冻结注册表五文件哈希与注册值一致。
- **C6（交付形态）**：实现以工作树修改+测试运行证据交付；commit/push
  由主会话在聚焦评审通过后统一裁量（沿用本谱系裁定/评审文书不 commit 的
  惯例）。

**可选子项（`bt card check` 对三张已提交 single_spec 卡由拒转过的修复性
对齐）：纳入**（§3）。

---

## 1. 独立核验结果（六项清单逐项抽验，不采信调查断言）

### 核验 1：R1 先例——成立

- 台账 mode=ro 直查：run 450（H101，actor=bt-gate，R1，
  2026-10-28T23:22:46Z）gates 表 V3 行 details_json
  `params_selection = {"selection": "manual", "params": {"threshold_pct": 0.0}}`
  ——与调查 §3 逐字一致；run 449（bt-screen）/450 params_json 均为
  `{"threshold_pct": 0.0}`。全 gates 表 distinct selection 值仅
  {auto, manual}（single_spec 将是新值；JSON 自由 schema，消费方见核验 4）。
- 四卡 selection_rule 直读：H101.toml:20、H101-v2.toml:14、H102.toml:16、
  草案 n2-draft/H103.toml:20 均 `"single_spec"`；H101/H101-v2 带
  `[params] threshold_pct=[0.0]`（单点网格）、H102/H103 无 `[params]`
  （cards.py:141-146 param_grid 无参 → `[{}]`）。H102.toml stop 规则原文
  注释"（single_spec，无参数网格）"在场。
- 时序：d23f32d（2026-10-25，git log 实测）已含 `_SELECTION_METRICS`
  与两处校验（git show d23f32d:scripts/bt.py 直读）——枚举校验早于
  run 450（2026-10-28/29），调查"R1 时 auto 同样会拒、当时走 manual 通道"
  的论断成立。

### 核验 2：枚举与消费点——成立（含一处表述不精确注记）

- bt.py:406-409 定义；全仓 grep 恰 3 处消费：495/497
  （`_check_card_lineage`，被 cmd_card_check bt.py:2321 与
  `_select_params_auto` 消息构造共用）、1193/1195（`_select_params_auto`）。
- `--params` 仅 gate 子命令（5468-5472，缺省 None→auto）；screen 子命令
  无 `--params`、不消费 selection_rule（argparse 与 cmd_screen 直读）。
- auto 算法（1183-1222）：候选=runs 表同 card/symbol/timeframe/
  data_sha256/cost_model_version 行，过滤 metrics.ok 且规则指标有值，取
  最大、并列取 run_id 最小；非法规则（1193）或无候选（1216-1219）→
  ValueError。两调用点（4713 fittable 拟合前占位、4776-4781 主路径）均经
  `_gate_choose_params` 单一漏斗。
- H102 runs 行数=0（台账直查）；H101-v2=0；H101=2（449/450）。
  **"即便枚举合法 auto 也拒"复核成立**：H102 零 runs → best 恒 None →
  必入 1216 无候选拒绝，与枚举无关。
- 注记（不影响结论）：调查 §1.1 称候选为"screen 运行"——SQL 实际不按
  run_class/actor 过滤（沿用 docstring 口径），对 H102（零 runs）与结论
  无影响，实现与评审按代码实义理解即可。

### 核验 3：raise 点次序与 run4 台账侧效应——成立（调查的细化如实）

- 次序（代码直读）：register_card（4515-4516，ledger.py:941-960
  INSERT OR REPLACE 幂等、cards 表不在哈希链）**先于**参数选择 raise 点
  （4776-4781/4717-4719）**先于** X-01 占位（4811-4828）与基准运行
  record_run（4793 起构造、其后落账）与 gates 写入（4957/4969）。
  调查"raise 先于一切**试验预算相关**写入、但不先于 cards 幂等重登"的
  收窄表述准确。
- run4 台账侧效应（台账直查）：cards 表 H102 行
  registered_utc=2026-11-06T22:41:11.921487Z（=06:41:11+08，在 run4 执行
  窗 06:41:09-14+08 内）——run4 本身幂等重登所致，**这是需要披露的唯一
  台账侧效应**；runs=450、trial_reservations=0、gates 最大 rowid=78、
  ledger_events 最大 rowid=1 全不变；rounds R1 closed/R3 open
  （trial_budget=2、lockbox_uses_max=1、toml_sha256=72dc6403…）；
  临时副本 verify_chain→problems=[]。预算机械口径 0/2 成立。
  （RUN4-REPORT 的"零写入"按四表枚举口径成立；cards 重登的披露由范围
  调查文档 §1.2 补齐，核验为真。run5 运行前核验文案应沿用该收窄表述。）

### 核验 4：冻结面——成立

- 机器冻结注册表 gate.py:49-61 恰五文件（level_decision.py、tables.py、
  decision.py、candidates.py、prob_mode.py）——授权 diff 零触碰；tests
  无 bt.py 哈希钉（grep 实测）；策略指纹=策略文件集的 git hash-object
  拼接（cards.py:867-887），**不含 bt.py**——改 bt.py 不变
  strategy_sha 8742cd4f…（run5 横幅连续性不受影响）。
- gates.toml LF 归一 sha256 实算=d0a8b2f3…（与调查所引冻结指纹一致）；
  选参层（`_gate_choose_params`/`_select_params_auto`）不读任何门槛键
  （代码直读）。`params_selection` 全仓消费方=bt.py:4957 写入+报告渲染
  （5106-5115）+测试断言——无引擎/关卡判定逻辑读它，追加
  `selection:"single_spec"` 值为向后兼容扩展。
- S8b §5 原文核对（主仓 docs/reviews/2026-11-07-r3-s8b-lineending-ruling.md
  §5）："run4=本谱系最后一轮授权执行……不得再有任何复原/修复尝试；R3 的
  走向……交主会话整体治理裁量——不再从本裁定链发放第四次单点修复授权"
  ——调查引用逐字属实。S8/S8b 的 bt.py/src 代码边界是**运行授权窗口内的
  时点性边界**，链已终止；本裁定即主会话整体治理裁量下的**新范围裁定
  文书**，不是该链的延续、不构成"第四次单点修复授权"。
- "选参层不属引擎成交语义"：成交语义=engine A（mt5quant.backtest）与
  V3 事件机制；选参层只产出 params 字典且在其之前。single_spec=登记
  规格上的恒等函数：产出的 params 与 R1 manual 通道 run 450 已实证执行
  的值逐字节相同（核验 1），不新增选择自由度、不改门槛/抽样/统计量。
  论证成立。

### 核验 5：形态对比与非代码路径否决——成立

- (b) dict 加成员 `single_spec: None`：会同时放宽 1193 判据，metric=None
  使一切候选指标取值为 None→误报"找不到候选"；且污染 rule→metric 映射
  与全部 sorted() 消息。否决理由成立。
- (c) 分支入 `_select_params_auto`：漏斗等效（两调用点同一分发器），仅
  稀释"从 screen 选参"语义；(a) 优先、(c) 可接受的排序合理。
- 非代码路径：(i) run5 加 `--params '{}'` 违反 run1 原文逐字基准（run1
  command.txt sha256 实算=d0d4d081…、grep 确认不含 --params）；(ii) 改
  H102 卡触发 A1 E1（docs/reviews/2026-11-06-n1-a1-ruling.md:31 原文
  核对："内容须与审核草案逐字一致……任何变更即失效须重审重绑"）；
  (iii) H102-v2 新卡=全新预筛/开轮链。三者否决理由均成立。

### 核验 6：TDD 设计充分性——基本充分，附两处补强（并入 C2/C4）

- 回归清单实物逐一到场且语义核实：test_validation_gates.py:833/881/906/
  928（auto 选参/manual/未知规则 max_moon_phase 拒/无候选拒——max_moon_phase
  在放宽后合法集之外，仍拒，回归有效）；test_research_bt_script.py:467
  （card check 拒 max_moon_phase）；test_research_cards.py 装载缺省三例
  （loader 零改动，恒绿）；test_t112_research_inputs.py:338-340（直调，
  用 max_net_profit，不涉 single_spec，保持绿）。
- "两枚举路径零变化"的机制证明成立：dict 恒等+`_select_params_auto`
  算法不触碰 → 全仓仅有的 3 个消费点对两枚举规则行为不变；无字节级
  快照测试，以 diff 审查为主证（聚焦评审执行）。
- 补强 1（→C2）：调查称"single_spec 直调 `_select_params_auto` 仍
  ValueError——防御性不变"只对了 raise 本身；放宽 `_check_card_lineage`
  后 1194 行 `ValueError(_check_card_lineage(...))` 对 single_spec 将得到
  `ValueError(None)`——消息退化。实现须按 C2 闭合；建议 RED 集补一例
  直调防御测试（raise 且消息非 None）。
- 补强 2（可选）：495-497 拒绝文案 `sorted(_SELECTION_METRICS)` 在放宽后
  不再列全合法集（纯文案，不强制）。

---

## 2. 授权的 diff 范围边界

**可改：`scripts/bt.py` 选参层 4 段（约 15-25 行含注释）+ 下列测试新增：**

1. `_gate_choose_params`（bt.py:4403-4424）：auto 分支开头新增
   single_spec 分支——`card.param_grid()` 恰 1 组则取该组（恒等选择，
   返回 `{"selection":"single_spec","rule":"single_spec","grid_points":1,
   "params":…}` 形态并打印一行选择记录）；≠1 组 → ValueError（fail-closed，
   消息含"网格"字样，RED-2 钉死）；其后原样调用 `_select_params_auto`。
2. `_check_card_lineage`（bt.py:493-499）：合法集改为
   `set(_SELECTION_METRICS) | {"single_spec"}`；dict 本体不动。
3. 关卡报告参数选择段（bt.py:5106-5115）：single_spec 第三分支（C3）。
4. `_select_params_auto`（bt.py:1183-1222）：**仅限** 1194 行错误消息
   构造的最小防御性修正（C2；兜底文案）；算法本体（SQL/过滤/取最大/
   并列规则/返回结构/1216 无候选文案）零改动。
5. （可选，属 bt.py 内文档同步）模块 docstring 64-67 行"附录 A 枚举"
   措辞补 single_spec。

**测试（新增，不改既有）：**

- tests/test_validation_gates.py：RED-1 两例（单点 `[params]` 卡与无
  `[params]` 卡；无预置 screen 运行、不传 --params、rc==0、runs 最新行
  params_json 断言、gates details `params_selection.selection==
  "single_spec"`）；RED-2（single_spec+两点网格 → rc==1、零 runs、输出
  含"网格"）。
- tests/test_research_bt_script.py：RED-3（single_spec 卡 `bt card check`
  rc==0）。
- （建议）tests/test_t112_research_inputs.py：直调防御测试（C2 对应）。

**零触碰（违者本裁定失效）：**`_SELECTION_METRICS` dict 本体；
`_select_params_auto` 算法本体；src/ 整包（含 gate.py:49-61 冻结注册表
五文件、cards.py 装载器、ledger.py）；config/gates.toml；
research/cards/*.toml；research/candidates/C005.toml；
research/rounds/R3.toml；run1 command.txt 及全部 .superpowers 留档工件；
台账（data/research/ledger.sqlite）。

---

## 3. 可选子项（card check 对齐）：纳入

`_check_card_lineage` 是单一函数、两消费点（card check 与 auto 消息构造）
共享合法集；只放宽 gate 通道须引入两套合法集，diff 更大且延续 H101/
H101-v2/H102 定义时校验与已登记实物的既有不一致（核验 1/5 证实三卡已
提交、其中 H101 已经 R1 真实执行）。放宽=**修复性对齐**，行为面变化由
RED-3+max_moon_phase 回归测试（467 行）双向钉死。不采纳调查 §8.4 的
保守备选（分开两套合法集）。

## 4. run5 前置：四步治理链（本裁定为第 1 步）

| 步骤 | 输入 | 通过判据 |
|---|---|---|
| 1 范围裁定（本记录） | 范围调查+RUN4-REPORT+本核验 | CONDITIONAL APPROVED（C1-C6） |
| 2 TDD 实现 | 本裁定 §2 边界 | RED 证据先行留档 → 最小 diff → 新测试 GREEN+§回归清单原样全绿+全量套件绿；工作树交付，不 commit/push（C6） |
| 3 聚焦评审 | 完整 diff+测试证据+冻结源空 diff 证明 | 冻结清单逐项空 diff（src/mt5quant/research/ 整包、src/mt5data/ext/merge.py、h_readonly_precheck.py、prob/{tables,decision}.py、strategies/**、validation/**、config/gates.toml；bt.py 变更限 §2 清单）；gate.py 五文件哈希==注册值；C1-C5 逐项核对；PASS 文档入 docs/reviews/ |
| 4 A1 整体裁量（run5 授权） | 评审 PASS+run5 请求 | 命令=run1 command.txt 原文逐字（sha d0d4d081…，不含 --params——扩展后 auto 遇 single_spec 即取卡唯一规格）；预算沿 S8 §2（runs 落账计 1/2，机械口径）；运行前核验沿 run4 §2 全套（六钉/冻结源/复原两文件/台账链+cards 重登披露/rowid27 最新性/策略指纹 8742cd4f…）；A1-D 15/213 子集分列、E7 charged 复检、E4/E5/E9 义务沿用；撤销条件由该裁定自载 |

治理链确认：四步即治理链；S8→S8b 链已按 S8b §5 终止（原文核对无误），
本裁定是主会话整体治理裁量下的新文书，与"不再发放第四次单点修复授权"
不冲突——本裁定裁的是**登记语义的引擎实现补全（能力扩展）**，非运行
复原/修复。

## 5. 风险与撤销条件

**风险（沿调查 §8 并补裁定者新发现）：**

1. 治理风险：bt.py 处于刚终止授权链的冻结面内——由本裁定 §2 边界+C5
   空 diff 证明缓解。
2. 语义风险：single_spec 误配多点网格——RED-2 fail-closed 钉死。
3. 报告风险：漏第三分支误标 manual——C3 硬条件。
4. 行为面变化：card check 三卡由拒转过——修复性对齐（§3）。
5. 零变化证明依赖 diff 审查（无字节级快照）——聚焦评审第二道。
6. run5 仍可能死于后续阶段（X-01/基准运行/V3）——本扩展只保证越过
   run4 死点，不预支 run5 结果；失败运行如实留痕、预算按落账计。
7. （新）防御路径消息退化 ValueError(None)——C2 闭合。

**撤销条件（本裁定任一成立即失效，回主会话全量重审）：**

1. diff 超出 §2 边界（触碰 dict 本体/算法本体/冻结源/卡/登记/台账/
   命令原文任一）；
2. 两既有枚举路径行为出现任何变化（回归变红，或评审发现语义改动）；
3. C2/C3/C4/C5 任一未闭合即进入下一步；
4. 发现本裁定所依赖的核验事实与实物不符（台账漂移、卡片/工件变更、
   指纹不符）；
5. run5 授权裁定设定更严边界时，以该裁定为准。

---

## 6. 签署（占位）

- 裁定者：独立范围裁定者（2026-11-07，只读核验留档于本记录 §1）
- approved_by：____________（占位，主会话裁量；本裁定记录不代填）
- 下一步入口：治理链第 2 步（TDD 实现），边界=本记录 §2。
