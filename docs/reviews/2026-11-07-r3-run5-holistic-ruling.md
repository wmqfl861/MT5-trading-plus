# R3 主检验 run5 整体授权裁定（治理链第 4 步，A1 级独立整体裁定）——2026-11-07

主会话转录 A1 级独立整体裁定者结论。裁定者未参与 R3 执行链（run1-run4）、
事件卡能力模块四步链（范围调查/范围裁定/TDD 实现/聚焦评审）的任何实现或
评审；全程只读（主仓 HEAD 09852e5 直读、研究库台账 mode=ro 直查+临时副本
verify_chain、全部 sha256/逐 hunk diff/策略指纹/single_spec 分支干跑由裁定者
独立重算，不采信实现方留档断言）；除本裁定记录外零文件修改、未派子代理、
未填 approved_by、未跑 gate、未 commit/push。解释器=研究库 .venv（-I 隔离、
PYTHONUTF8=1）。

**总裁定：APPROVED（附程序追认、授权边界与撤销条件）。** 五事项：
(1) run5 授权执行；(2) F7（C6 字面程序性偏离）追认；(2b) .gitattributes
落地（c518136）程序缺口追认（本裁定独立发现的第二处偏离）；(3) F5 磁盘
环境事项裁定为非阻塞；(4) run5 再失败预立处置条款；(5) 撤销条件与授权边界。

---

## 0. 独立核验基础（逐项抽验结果，全部通过）

### 0.1 R3 执行链四轮拒绝与零污染（逐份直读）

- **run1**（主仓 `.superpowers/sdd/2026-11-07-r3-main-test/`，14 文件在场）：
  stdout 末行 `✗ 装配失败（未开跑，无 runs 记录）：ValueError: runtime
  coverage USTEC/D1: 4.3093771 years < 90% of precheck 24.386037`——
  coverage_segments 空（登记预筛载荷不含覆盖分段）导致 runtime coverage
  无法对上预筛申报域，装配段拒绝。
- **run2**（主仓 `.superpowers/sdd/2026-11-07-r3-main-test-fix1/`，20 文件
  在场）：同一拒绝文案，根因层=FRED 年份闸门（无逐年核验证据 → used=false
  → merge 只计 used 年份；fix1 修 coverage_segments 携带但未解此层）。
  S8 §0 已完整叙述并经 fix2（yearly_verification 版 manifest，41/41
  used=true）闭合。
- **run3**（研究库 `.superpowers/sdd/2026-11-07-r3-main-test-run3/`，8 文件
  在场）：stdout 末行 `✗ 轮次文件 R3.toml 与开启时入账的内容不一致
  （sha256 591133582c70… vs 台账 72dc64030458…）（H-08）`——工作树 CRLF
  物化 vs 原始字节钉。
- **run4**（研究库 `.superpowers/sdd/2026-11-07-r3-main-test-run4/`，7 文件
  +engine-extension-impl 13 文件在场）：stdout 第 7 行 `✗ 卡 H102：
  selection_rule 'single_spec' 不在支持枚举 ['max_net_profit',
  'max_sharpe'] 内`——历次最深（H-08 两道、runtime coverage、成本装配
  首次全过），死于参数选择枚举。
- **零污染**：四次均零 runs、零预约、零 gates/events 写入（今日台账复测
  runs=450、trial_reservations=0、gates max rowid=78、ledger_events max
  rowid=1，与 run4 后留档一致）。cards 表 H102 行 registered_utc=
  2026-11-06T22:41:11.921487Z=run4 幂等重登（范围裁定核验 3 已披露的
  唯一台账侧效应，幂等、不触预算与链）。

### 0.2 S8 / S8b 裁定（逐字直读）

- S8（docs/reviews/2026-11-07-r3-s8-rebind-ruling.md，e159014 记录）：
  APPROVED——E1 重绑有效（rowid27 为有效登记预筛）、E3 FRED 放行（限域）、
  预算机械口径 0/2、run3 记 1/2、撤销条件 5 项。run3 失败触发其条件 2。
- S8b（docs/reviews/2026-11-07-r3-s8b-lineending-ruling.md，8f7d0ec 记录）：
  APPROVED——形态复原恰两文件、run4 授权、"run4=本谱系最后一轮授权执行
  ……R3 的走向……交主会话整体治理裁量——不再从本裁定链发放第四次单点
  修复授权"（§5 原文核对属实）。run4 失败后授权链按约终止，本裁定即
  主会话整体治理裁量下四步治理链的第 4 步收口。

### 0.3 事件卡能力模块四步链（每步记录在案与内容相符）

1. **范围调查**（研究库 run4 目录 event-card-param-gap-scope.md）：提交
   形态（a）最小扩展请求（auto 遇 single_spec=恒等选择、≠1 组 fail-closed），
   含缺口表征/R1 先例（run 450 manual 通道实证值）/冻结面论证/非代码路径
   否决/TDD 设计——在场且与后继文书自洽。
2. **范围裁定**（主仓 docs/reviews/2026-11-07-event-card-selection-scope-
   ruling.md，入库 d6888da=253 行纯文档提交）：CONDITIONAL APPROVED
   C1-C6，§2 授权 diff 边界（bt.py 选参层 4 段+测试新增），§4 预立本步
   （治理链第 4 步）的 run5 授权输入边界。
3. **TDD 实现**（065abf7，4 文件 217 insertions/7 deletions）：抽查两 hunk
   ——`_check_card_lineage`（@@ -491,10 +493,15）`legal =
   set(_SELECTION_METRICS) | {"single_spec"}`、dict 本体不动==裁定 §2.2
   逐字；`_gate_choose_params`（@@ -4406,6 +4421,24）single_spec 分支位于
   auto 开头、≠1 组 ValueError 消息含"网格"、selection 四键形态==裁定
   §2.1 逐字。8f7d0ec..HEAD 全变更集恰 7 文件（.gitattributes+两份
   docs/reviews 文书+bt.py+三个 tests），无越界文件。
4. **聚焦评审**（主仓 docs/reviews/2026-11-07-engine-single-spec-review.md，
   入库 09852e5=234 行纯文档提交）：Spec PASS / Quality PASS，C1-C6 逐项
   闭合（C4 含 12 失败环境性归因），F1-F7 无阻断；实现证据 13 文件实物
   在场（engine-extension-impl/，含 RED×6/GREEN 7/回归 9/全量/冻结源
   空 diff/C2 hazard 证明/机制零变化证明）。
5. **S8b(d) .gitattributes 落地**（c518136，15 行）：内容与 S8b §4 建议
   四条规则逐字一致+效果边界注释；落地取证 11 文件在场（.superpowers/
   sdd/2026-11-07-secret-redaction/gitattributes-rollout/），含双文件
   delete+checkout **重物化实测==钉**（20/21 号文件：R3.toml 72dc6403…/
   C005.toml d00adad5…，0 CR 字节、w/lf、status clean）与全仓无 mass
   dirtying 证明。时序 08:25-08:30，满足 S8b §4"run4 之后"。

### 0.4 数据/登记链现状（mode=ro 现场直查）

- 台账：runs count=max=450、trial_reservations=0、gates max rowid=78、
  ledger_events max rowid=1、lockbox_uses=0；R1 closed、**R3 open/
  trial_budget=2/approved_by 已填**、rounds.toml_sha256==盘上 R3.toml
  原始字节（72dc6403…）；C005 恰 rowid25/26/27 三行全 pass、**rowid27
  仍最新**；rowid27 载荷 precheck_source 四钉抽验在场（calendar_sha256
  a53577d9…、candidate_sha256_bound d00adad5…）；coverage_segments 在场；
  runs H102=0；gates distinct selection={None, auto, manual}。
- **五链校验**：台账复制至临时目录后 `Ledger.verify_chain()` 复跑 →
  **problems=[]**。
- **六钉+两钉定文件原始字节现场重算（全部命中）**：fred manifest
  `6f068426…`、fred parquet `c6645256…`、日历工件 `a53577d9…`（64 字符）、
  R3.toml `72dc6403…`（3805 字节）、C005.toml `d00adad5…`（11853 字节）。
  两钉定文件在 .gitattributes 提交+重物化之后**保持==钉**（F4 事项现场
  复核通过），主仓工作树 clean。
- **冻结源**：bed580c..HEAD 全程冻结源清单空 diff，**唯 bt.py 有 diff 且
  其量（47+/7-）恰为 065abf7 授权变更**；无任何越界触碰。

### 0.5 run5 关键前置的裁定者独立验证

- **run1 command.txt 原文**：sha256 现场实测
  `d0d4d081f6526c95deed78b7bd3650da11bf7597cd62908a1ac950b475b9240b`；
  run3/run4 目录副本逐字节相同（sha 同值）；原文不含 `--params`；日历
  SHA=64 字符全文。
- **bt.py@HEAD single_spec 分支行为预检（干跑，零台账零 gate）**：以
  研究库 .venv（-I）从主仓绝对路径内存加载 bt.py，对**真卡 H102**
  （cards.load_card 实装载）实测——selection_rule='single_spec'、
  param_grid()=[{}]（恰 1 组）；`_gate_choose_params`（ledger=None、
  args.params=None）返回 `({}, {"selection":"single_spec",
  "rule":"single_spec","grid_points":1,"params":{}})`，与范围裁定 §2.1
  逐字一致并打印一行选择记录；`_check_card_lineage` 对 single_spec 卡
  返回 None（合法）；`_select_params_auto` 直调防御 raise ValueError
  且消息含卡 id 与 single_spec（C2 闭合，无 ValueError(None) 退化）；
  多点网格 stub raise 消息含"网格"（RED-2 fail-closed 语义）；
  `_SELECTION_METRICS` 本体 keys 仍恰 {max_net_profit, max_sharpe}。
  **即：run5 命令（不传 --params）在参数选择阶段将落入恒等选择分支，
  run4 死点已消除。**
- **策略指纹**：strategy_fingerprint(主仓 HEAD 工作树) 复算=
  `8742cd4f53ab…`==run1-run4 四次横幅——连续性成立（改 bt.py 不入指纹，
  范围裁定核验 4 论证+本次实测双重确认）。

---

## 1. 事项一：run5 授权——**APPROVED**

1. **命令**：主仓 `.superpowers/sdd/2026-11-07-r3-main-test/command.txt`
   **原文逐字重跑**（sha256 `d0d4d081…`，即：bt.py gate --card H102
   --strategy mt5quant.strategies.library.fomc_preannouncement:
   PreFomcAnnouncementDrift --gates G0,G1,G2,G3,G4,V3 --event-kind
   fomc_calendar --fomc-calendar <主仓日历工件> --fomc-calendar-sha256
   <64 字符全文 a53577d9…> --event-days-start 1986-01-02
   --event-days-end 2021-05-17；**不含 --params**——single_spec 分支使
   auto 通道直接可跑）。日历 SHA 参数必须为 64 字符全文（S8 F-A 防线
   沿用）。
2. **执行形态**：cwd=研究库工作区、PYTHONUTF8=1、解释器=研究库 .venv
   （-B）、bt.py=主仓 HEAD 09852e5 工作树——同 run4。
3. **预算**：runs 落账后计 **1/2**（机械口径，沿 S8 §2：单位=append-only
   台账的去重研究试验；run1-run4 零 runs 零预约不构成扣减）。运行期
   trial_reservations 照常预约/释放；本裁定不豁免任何运行期机械检查
   （随机化入口缺锚 fail-closed、≥1999 次抽取同冻结日历、static_long、
   seed 确定性等按 R3.toml advance_rule 原样执行）。
4. **运行前核验（run4 §2 全套 + 三项新增，全部通过后才启动）**：
   - run4 §2 全套：主仓 HEAD/工作树状态（预期=09852e5+clean，两钉定
     文件不再显示 M）、冻结源自 bed580c 空 diff（bt.py 除外=065abf7
     授权变更）、六钉+绑定链实物重算（11 钉口径）、台账直查+临时副本
     verify_chain、rowid27 最新性、策略指纹 8742cd4f…、命令传代
     cmp（run1 原文↔run5 留档副本）；
   - **F4（新增）**：两钉定文件（research/rounds/R3.toml、
     research/candidates/C005.toml）**原始字节 sha256==钉**复核留档
     （.gitattributes 重物化已过，运行前再核一次留档——不得以 git
     status clean 或文本 diff 空为据）；
   - **single_spec 分支行为预检（新增）**：干跑验证或等价证据（本裁定
     §0.5 已做一次并留档于本记录；执行者可复用或重做，不真跑 gate）；
   - **.gitattributes 在场确认（新增）**：主仓 .gitattributes==c518136
     入库内容（防 checkout 物化复发层在位）。
5. **报告义务沿用**：A1（2026-11-06）D 节——**15/213 个月末重叠事件子集
   的窗贡献分列（子集均值 vs 全集均值），不作否决项**；E7 charged 口径
   并列参考复检（运行期部分需真实 runs 产物）；E10 诊断段边界；E4 锁箱
   0/3 机械确认、E5 D33 按 runs 实际取数重列、E9 provisional 解除落档
   等开轮程序义务仍为主会话义务，本裁定不代行、不豁免。run5 完整留档
   同 run4 形态（新开 run5 证据目录：命令副本/运行前核验/完整输出/
   台账前后状态/报告）。

## 2. 事项二：F7 追认（C6 字面程序性偏离）——**追认（附约束）**

事实（裁定者独立核验）：范围裁定 C6 要求"实现以工作树修改+测试运行证据
交付；commit/push 由主会话在聚焦评审通过后统一裁量"；实际时序=065abf7
（实现 commit）08:20:43 → d6888da（裁定文书 commit）08:22:05 → c518136
08:27:18 → 09852e5（聚焦评审记录 commit）08:33:35——**实现与范围裁定
文书在第 3 步评审通过前已 commit**；范围裁定原文"本记录不 commit"字面
亦被偏离。四提交均未 push（origin/main..HEAD 实测在列）。

**裁定：追认**。理由：
1. **评审输入完整性无损**：聚焦评审对象正是提交 065abf7（git worktree
   detach 隔离评审），commit 在先反而使评审输入钉死于不可变对象；
2. **实质闭合零返工**：评审 Spec PASS/Quality PASS、C1-C6 逐项闭合、
   F1-F7 无阻断——若按 C6 字面时序重放（评审后 commit），产物逐字节
   等价；
3. **可回退性完好**：未 push、单一提交、revert 路径干净；
4. 范围裁定 C6 的立法意图（评审通过前不固化、主会话保留裁量）在实质
   上未被破坏——主会话通过第 4 步（本裁定）行使了该裁量。

**附约束**：(a) 065abf7/d6888da/c518136/09852e5 的 push 仍属主会话后续
统一裁量，本追认不含 push 授权；(b) 本追认仅覆盖本次已发生的时序偏离，
不构成对"评审前 commit"形态的一般性许可——后续同类链仍应按各自裁定的
交付形态条款执行。

## 2b. 事项二b：.gitattributes 落地程序缺口（本裁定独立发现）——**追认（附约束）**

事实：S8b §4 对 .gitattributes 的处置为"原则批准……走聚焦评审（仓库
配置变更）……时序：run4 之后"。实际落地链（c518136，08:25-08:30）有
完整的自证取证（11 文件：前后状态对照、内容逐字副本、双文件 delete+
checkout 重物化实测==钉、全仓无 mass dirtying、提交快照）与聚焦评审
（engine-single-spec）F4 段落的顺带交叉确认，但**无一份针对 .gitattributes
的独立聚焦评审 PASS 文书**——与 F7 同类的程序时序偏离（实质审查在案、
名义文书缺位）。

**裁定：追认**。理由：内容与 S8b §4 建议逐字一致（裁定者比对）；行为面
经实测取证钉死（重物化==钉、git diff 空、35 个 w/crlf toml 无状态变化）；
效果边界注释如实（只防未来 checkout 物化，不检测不修复现存 CRLF）；
时序满足"run4 之后"。**附约束**：(a) 任何后续 .gitattributes 范围扩展
（新增路径/规则变更）须补聚焦评审后落地；(b) run5 运行前核验含其在场
确认（§1.4）。

## 3. 事项三：F5 处置（C 盘 4.7GB<5GB）——**非阻塞，如实记录；盘清理由用户裁量**

事实（裁定者现场复测+聚焦评审 §5 归因复核）：C: 盘 145G/150G used、
**4.7-4.8GB free（97%）**；全量套件 12 failed（4178 passed/3 skipped）
全部位于 tests/test_demo_run_ps1.py，根因=demo_run.ps1:321-326 预检
"磁盘剩余 ≥5GB"（实测 4.61GB）；该测试文件与 demo_run.ps1 均不在
065abf7 变更集、与 bt.py/selection/single_spec 零调用关系；CI 不受影响
（本机环境性）。

**裁定**：
1. **F5 不是 run5 前置**：run5=bt.py gate（Python 单进程、写台账与
   gates 行、无 PowerShell 链路、无 ≥5GB 预检），与 demo_run.ps1 无
   任何依赖；4.7GB 对 run5 的写入需求（KB-MB 级台账行+文本产物）富余
   充足。
2. C4"全量套件绿"以聚焦评审的**归因口径**成立（12 失败全环境性、与
   diff 零调用关系、隔离重跑同失败集）——run5 授权不要求字面全绿。
3. **磁盘清理属用户裁量事项**，本裁定不代行、不设为前置；建议（非
   义务）：盘清至 ≥5GB 后重跑 tests/test_demo_run_ps1.py 确认 12→0，
   使后续全量口径收敛为字面绿；run5 报告中如实记录当前磁盘状态即可。

## 4. 事项四：run5 再失败处置（预立条款）

- **若 run5 于 runs 落账前再次被拒（装配段或更早，任何原因）**：执行侧
  一律**停在原地完整取证报告**（同 run4 形态），**不得再有任何复原/
  修复/单点变更尝试**（不改命令、不改卡、不改代码、不改绑定、不动台账、
  不动工作树）；R3 的走向（继续/以从未执行关闭/其他）交主会话整体治理
  裁量并回 A1 级新裁定。**本裁定不发放第六轮修复授权；不存在隐含的
  下一次修复额度。**预算口径：仍 0/2（机械口径）。
- **若 run5 于 runs 落账后计算失败（统计/关卡段运行时错误）**：那是
  **真实结局**，照常记录（失败 runs 行本身就是 append-only 试验记录），
  预算已计 1/2；按 R3.toml advance_rule 与报告义务处置，不需新授权
  即可完成报告与关轮判定；是否构成 G5/锁箱事件按台账机制自判。
- **若 run5 完整跑通**：照常出完整报告+执行 §1.5 义务（A1-D 分列、
  E7 运行期复检等），预算 1/2。

## 5. 撤销条件（任一成立即失效，须回 A1 级重审）

1. 六个绑定工件（manifest `6f068426…`、parquet `c6645256…`、costs
   `c35550b9…`、specs `144bb224…`、日历 `a53577d9…`、候选 `d00adad5…`）
   或两钉定文件（R3.toml `72dc6403…`、C005.toml `d00adad5…`）在 run5
   前任何时点再变；或台账 C005 最新行不再是 rowid27；
2. run5 之前主仓出现冻结源/卡/登记/钉定文件的任何新变更（HEAD 09852e5
   之后 bt.py/src/config/research 的任何再改动——docs 与 .superpowers
   留档除外）；或工作树在运行前核验时不再满足 §1.4 全套；
3. 台账任何链校验出问题，或本裁定所依赖的任一 sha/证据被证伪（含
   single_spec 分支干跑预检在执行时点复验失败）；
4. 绑定域内发现任何 FRED 修订证据（同源重下载与绑定原件不再逐字节
   一致）——沿 S8 条件 3 全文；
5. 任何 used=true 声明被发现非核验程序产出（伪声明）——沿 S8 条件 5
   全文；
6. F7/2b 追认前提被证伪（如发现 065abf7 已 push、或评审 PASS 结论被
   推翻需返工）。

## 6. 授权边界汇总

- 本裁定授权**恰一次 run5 执行**，命令/形态如 §1；不授权任何代码、绑定、
  登记、卡片、命令原文变更；不授权 push；不授权 .gitattributes 扩展；
  不代行 E4/E5/E9/E10 等开轮程序义务；不豁免任何运行期机械检查。
- 四步治理链（范围调查→范围裁定→TDD 实现→聚焦评审→本整体裁定）经
  本裁定收口；后续任何使 run5 结果不可直接采信的新变更，均属撤销条件
  或须新裁定。

## 7. 签署（占位）

- 裁定者：A1 级独立整体裁定者（2026-11-07；只读核验留档于本记录 §0）
- approved_by：____________（占位，主会话裁量；本裁定记录不代填）
- 下一步入口：主会话按 §1 执行 run5（或按其裁量先行处置 F5 磁盘事项，
  非前置）。

（裁定只写本文件；不 commit，主会话集成。）
