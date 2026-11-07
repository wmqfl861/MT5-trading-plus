# R3 主检验 run6 最终授权裁定（A1 级独立裁定者，路径 A 第 4 步，2026-11-07）

裁定者身份：A1 级独立裁定者，未参与路径 A 前三步（范围裁定 27bbdfe /
TDD 实现 ca626ae / 聚焦评审 16c83e7）任何工作，未参与 run1-run5 执行与
离线复现。全程只读：主仓 demo-next-20261105（HEAD **16c83e7**，worktree
clean，直读）；研究库台账 data/research/ledger.sqlite **mode=ro 直查**
（verify_chain 用临时副本，用后即删）；上游五份文书逐份直读；关键量
（三指纹、11 钉定/绑定链、冻结源 diff、命令 sha、台账现状、计划任务
先例、磁盘余量、single_spec 干跑）**由裁定者本人独立重算/复跑**，不采信
任何执行方留档断言。除本裁定记录外零文件修改；未派子代理；未填
approved_by；未跑 gate；未 commit/push；零台账写入。解释器=研究库
.venv（-I、-B、-X utf8、PYTHONUTF8=1）。

上游链（逐份直读，编号沿用）：
1. 处置裁定 docs/reviews/2026-11-07-r3-disposition-ruling.md（(a) 路径 A
   合法性/(c) 四步链+七元组不膨胀/(e) 预算 1/2、第 2 次落账后 2/2 终态）；
2. 范围裁定 docs/reviews/2026-11-07-v3-diag-degrade-scope-ruling.md
   （§2 授权 diff 边界+§4 TDD+§5 P1-P4 防终止原则）；
3. 实现 commit **ca626ae**（events.py window_shift 段 try/except
   ValueError 降级 + TDD 测试）；
4. 聚焦评审 docs/reviews/2026-11-07-diag-degrade-review.md（Spec/Quality
   双 PASS，10 项主张核验，F1-F6 非阻断，§7 撤销条件零触发）；
5. run5 报告 研究库 .superpowers/sdd/2026-11-07-r3-main-test-run5/
   RUN5-REPORT.md（kill 取证 + 离线复现 + 死因结构性错配论证）。

---

## 总裁定

**APPROVED（附条件）——run6 授权执行。**逐项：

1. **四步治理链完整收口**：处置裁定（6c32286）已裁定路径 A 合法且为
   预注册预算结构明文预留场景（trial_budget=2 注释"1 次主规格 + 1 次
   代码修复后的重跑"）；四步链文书/提交全部在库且内容与本裁定 §0
   核验一致；本裁定=第 4 步（A1 级整体裁量）。路径 A 第 1-3 步的撤销
   条件经本裁定复核**零触发**。
2. **run6 授权恰一次**：命令=run1 command.txt 原文逐字（sha256
   d0d4d081f6526c95deed78b7bd3650da11bf7597cd62908a1ac950b475b9240b，
   本裁定独立复核 MATCH；不含 --params；argv 级恒等，见 §1.3）；预算
   落账后 R3 机械口径 **2/2**；七元组不变（§0.4 三指纹重算背书）→
   primary_test_n 去重口径不增（R1 runs 449/450 同七元组去重=1 先例）、
   α/N 仍 0.025；确定性 seed 使主检验数值逐位可预期复现——无选参
   自由度、无结果挑选空间（沿处置裁定 §3.6）。
3. **A1-D/E7 义务沿用**：run6 报告必须并列月末重叠子集分列（A1-D）与
   charged 口径夜数复检（E7）；同输入确定性下可直接引用 run5 复现值
   并注明等价依据，或重算，两者皆可，缺一即报告义务未完成。
4. **运行前核验=run5 §1 全套对 HEAD 16c83e7 适配**（§1.1 清单），由
   执行侧在启动时重跑并落档 run6 证据目录；本裁定 §0 已由裁定者本人
   独立执行同套核验一遍（全绿），执行侧重跑为程序义务而非对裁定者
   核验的不信任。
5. **防终止机制（P1-P4 落地）**：载体=**Windows 计划任务一次性任务**
   （§2 机制裁定，明确到可执行启动命令形态与监控方式）。run5 死因
   （harness 后台任务存活窗 ~49-60 分钟 vs 引擎 ~6.1-6.4 小时的结构性
   错配）经本裁定复核成立；计划任务载体在先例（t121-release-v2-once-
   run-r2，现存 Ready）与机制属性上均满足 P1 存活独立性。
6. **不预判 run6 的 V3 行内容**（沿处置裁定 §3.7、范围裁定 §4.3）：
   判定一律以台账真实行为为准；确定性预期值（p=0.0085 等）仅作异常
   检测对照参考，不构成对台账行的预写期望。
7. **本裁定不含的授权**：G5/锁箱使用（R3.toml 明文"另经批准"）；任何
   新的代码变更；任何台账人工写入；第三次及以后的 R3 运行。

---

## 0. 独立核验基础（全部直读/独立重算，全部通过）

### 0.1 四步链记录完整性（git 直查）

- 72d1014..16c83e7 恰 4 个提交，序与角色完全吻合：
  6c32286 [DOC] 处置裁定 → 27bbdfe [DOC] 范围裁定 → **ca626ae [S] 实现**
  → 16c83e7 [DOC] 聚焦评审（PASS）。
- name-only diff 72d1014..16c83e7 = **恰 5 个文件**：3 份 docs/reviews
  文书 + src/mt5quant/validation/events.py + tests/test_validation_events.py
  ——无第六文件，与聚焦评审 §1-1 主张一致。
- 实现证据目录 diag-degrade-impl/（EVIDENCE/RED/GREEN/REGRESSION/
  FULL-SUITE/diff-stat/frozen-comparison/patch 共 8 工件）在场，时间戳
  11:27→12:41 与 TDD 时序一致（聚焦评审 §1-10 复核）。

### 0.2 ca626ae diff 与范围裁定 §2 吻合（抽验，独立 diff 直读）

- events.py 恰 2 hunk：docstring 增一句（§2.1 明文可选授权）+
  window_shift 段整段包入 `try/except ValueError`；捕获面单一
  `except ValueError as e`（SignalUnavailable⊂ValueError，events.py:317
  类定义直读）；降级记录 `{"passed": True, "reason": str(e),
  "note": <只报告语义注+裁定引用>}` 与 year_block 降级分支键集同构；
  无合成 dropped/net_mean/n_events；正常路径仅 +4 缩进（聚焦评审已做
  去缩进逐字节比对 True，本裁定抽验 diff 文本吻合）。
- 冻结面：gate.py EXPECTED_ACCEPTANCE_SOURCE_SHA256 五文件不含
  events.py（聚焦评审 §2-D）；本裁定另验 bed580c..16c83e7 range 内
  research/strategies 两包零文件变更（见 §0.3）。

### 0.3 冻结源 diff 逐字节比对（HEAD 16c83e7 适配，run5 §1 等效）

- **bt.py**：`diff(bed580c..065abf7) == diff(bed580c..16c83e7)`
  （限定 scripts/bt.py）**逐字节相等**；`diff(065abf7..16c83e7)` 为空；
  worktree==HEAD。→ bt.py 现值==065abf7 授权量（single_spec 四段，
  事件卡范围裁定授权）。
- **events.py**：`diff(bed580c..27bbdfe)` 为空（run5 时代 events.py 未
  变）+ `diff(27bbdfe..ca626ae) == diff(bed580c..16c83e7)`（限定
  events.py）**逐字节相等**。→ events.py 现值恰=ca626ae 授权量，无
  第二处变更。
- 其余冻结文件（scripts/merge.py、h_readonly_precheck.py、
  prob/tables.py、prob/decision.py、config/gates.toml、
  src/mt5quant/research/、src/mt5quant/strategies/）bed580c..16c83e7
  **零行变更**；validation 包内变更文件恰 events.py 一个。
- **结论**：冻结源自 run5 基线以来的全部增量 = ca626ae 授权量
  （events.py）+ run5 已消费的 065abf7 授权量（bt.py），无未授权字节。

### 0.4 三指纹+两钉定+11 钉绑定链（生产函数/原始字节独立重算，全 MATCH）

- **strategy_fingerprint**（cards.strategy_fingerprint，主仓工作树
  16c83e7）= 8742cd4f53ab5dc8c2e5168fc4388ba1309a83b1b04b5905949bdf1
  da3f1d780 == 台账 run#451.strategy_sha 逐位；闭包恰 3 文件
  （strategies/base.py、library/buy_hold.py、library/
  fomc_preannouncement.py），events.py/bt.py 均不在闭包。
- **gates_sha256**（vgate.gates_sha256，LF 归一生产实现）=
  d0a8b2f3105366e4173adcf15fb6dc650a032e277425ca7277abdc92086bb17b ==
  台账 run#451.gates_hash 逐位。
- **ENGINE_VERSION** = "A-1.7"（engine_a.py 硬编码，未触碰）。
- 两钉定文件原始字节：R3.toml **3805B** sha256 **72dc6403**0458ad01…
  MATCH；C005.toml **11853B** sha256 **d00adad5**f6303b55… MATCH。
- **11 钉绑定链全 MATCH**（fred manifest 6f068426/fred parquet
  c6645256/costs c35550b9/specs 144bb224/日历工件 a53577d9/n1-draft
  C005.toml d00adad5/fix2 载荷 94af2dfb/装配器 67f2abc8/F1 日历导出
  b0dea4b2/R3.toml 72dc6403/C005.toml d00adad5）——原始字节逐项重算。
- .gitattributes 自 c518136 零变更；worktree==HEAD（全文件）。

### 0.5 台账现状（mode=ro 直查）

- runs max=451、count=451；H102 恰 1 条（run#451：R3/v1/USTEC/D1/
  research/bt-gate/A-1.7、ok=true、created 2026-11-07T00:47:20Z、
  data_sha=h-input-v1:f233c9bb…、gates_hash=d0a8b2f3…、strategy_sha=
  8742cd4f…、params_json="{}"）。
- **gates 行 79-83 = G0 ✓/G1 ✗/G2 ✗/G3 ✓/G4 ✗；run_id=451 无 V3 行**
  （逐行直查）；全台账唯一 V3 行=rowid 77/run 450（R1）；gates
  max rowid=83。
- trial_reservations 恰 1 条（R3/completed）；lockbox_uses 0 行、
  lockbox_budget 0 行；R1 closed/R3 **open**、trial_budget=2、
  toml_sha256==盘上钉 72dc6403…、approved_by 在案；candidate_checks
  max rowid=27（decision=pass，C005 前置门满足）；ledger_events
  max=3；G4 details：n_trials=2、seed=10640155159524715549、
  project_n_dedup_rounds=2。
- **预算机械口径 R3 = 1/2**（run#451=第 1 次去重研究试验）。
- 临时副本 verify_chain → **problems=[]**。

### 0.6 命令传代

- sha256(run1 command.txt) = **d0d4d081**f6526c95deed78b7bd3650da11bf
  7597cd62908a1ac950b475b9240b == 预期（主仓 .superpowers/sdd/
  2026-11-07-r3-main-test/command.txt 为权威源，未改动）；run5 副本
  cmp **逐字节 IDENTICAL**；原文不含 --params；日历 SHA 为 64 字符
  全文；cwd/解释器/bt.py 路径头字段在文（§1.3）。

### 0.7 计划任务先例与机器现状

- **先例任务 t121-release-v2-once-run-r2 现存**（Get-ScheduledTask
  直查）：State=**Ready**；动作=powershell.exe -NoProfile
  -ExecutionPolicy Bypass -File <…>/scripts/run_t121_release_v2_once.ps1
  （包装脚本形态）；触发器=一次性时间触发（2026-11-08T08:00:30+08）；
  Principal=Administrator/Interactive/Limited；Settings
  **ExecutionTimeLimit=PT6H**；LastTaskResult=267011（尚未运行）。机制
  在本机可注册、可武装已被该先例证明。
- **机器现状**：C: 盘 150G 总量、**4.5G 空闲（98% 用量）**——低于项目
  demo_run.ps1 预检 5G 阈值（聚焦评审 F6 同一状态）。→ §1.2 启动硬门。

### 0.8 single_spec 干跑预检（对 16c83e7 等效复跑）

以研究库 .venv 内存加载主仓工作树 bt.py（bt.py==065abf7 逐字节，§0.3）
真卡 H102 实测：selection_rule='single_spec'、param_grid()==[{}]（恰
1 组）；_gate_choose_params(ledger=None, args.params=None) 返回
({}, {"selection":"single_spec","rule":"single_spec","grid_points":1,
"params":{}})；_check_card_lineage→None；C2 直调防御 ValueError 消息
含卡 id 与 single_spec；RED-2 多点网格 stub raise 含"网格"；
_SELECTION_METRICS keys 仍恰 {max_net_profit, max_sharpe}。**全过，零
台账零 gate。**

---

## 1. 裁定量 1：run6 授权条款

### 1.1 运行前核验清单（run5 §1 全套对 16c83e7 适配；执行侧启动前重跑并落档）

以 run5 prerun-verify.py 为基础适配（脚本副本入 run6 证据目录），全部
通过方可启动；任何一项不过=停（不启动、不消耗任何东西）：

1. **主仓状态**：HEAD=16c83e7（若主会话已集成新 DOC 提交则 HEAD 前移
   属 docs-only 豁免，但下列内容不变量必须逐项成立）；worktree clean
   （允许的例外：本裁定文书及 run6 自身证据文件的 untracked 状态）；
   72d1014..HEAD 的非 docs 变更恰 events.py+tests/test_validation_events.py
   （ca626ae）。
2. **冻结源逐字节比对**（不变量为内容锚定，与 HEAD 文字无关）：
   - bt.py 现值 hash-object == 065abf7 版 blob；
   - events.py 现值 hash-object == ca626ae 版 blob；
   - §0.3 所列其余冻结文件对 bed580c 零 diff；validation 包内变更恰
     events.py；
   - strategy_fingerprint 重算==8742cd4f…；gates_sha256 重算==
     d0a8b2f3…；ENGINE_VERSION=="A-1.7"。
3. **11 钉绑定链**原始字节重算全 MATCH（§0.4 清单）；两钉定文件
   hash-object==HEAD blob（F4 形态）；.gitattributes 零漂移。
4. **台账前态**（mode=ro）：runs=451（H102 恰 1 条）、gates max=83 且
   run_id=451 无 V3 行、trial_reservations=1（completed）、lockbox 0、
   R3 open/trial_budget=2/toml_sha256==钉、candidate_checks max=27
   pass、verify_chain problems=[]（临时副本）。
5. **命令传代**：sha256(run1 command.txt)==d0d4d081…；run6 副本 cmp
   IDENTICAL；不含 --params；日历 SHA 64 字符。
6. **single_spec 干跑预检**：对启动时工作树等效复跑（§0.8 六断言）。
7. **磁盘硬门**：启动时刻 C: 空闲 **≥5.0G**（当前 4.5G，须先清理；
   理由见 §1.2）。
8. **载体就绪核验**（§2 机制落地）：包装脚本 sha 落档；任务 XML 导出
   落档并核对（动作路径/触发器/ExecutionTimeLimit=PT12H/Principal）；
   stdout/stderr 目标路径可写。
9. 全量证据写入 研究库 .superpowers/sdd/2026-11-07-r3-main-test-run6/
   （目录由执行侧创建，本裁定授权）。

### 1.2 磁盘空间条款（~5G 阈值风险的裁定处置）

- **启动硬门=5.0G**：与项目既有操作阈值（demo_run.ps1 预检 5G；聚焦
  评审 F6 记录 4.5G/4.7G 状态）一致，且理由是预算保护而非卫生：run#452
  在启动后数秒内落账（run5 实测 reservation started→completed 3 秒、
  run 行含完整 metrics），若此后因磁盘满出现写失败/异常退出，将以
  §3 Case B 形态**烧尽最后一个预算槽且无 V3 行**——最后一个预算槽的
  防护标准应取项目最高阈值。
- run6 自身写盘为 KB-MB 量级（先例实测：run5 全目录 424K、台账 2.1MB），
  **禁止**在 run6 目录落任何大体积中间产物（离线复现如需进行，属运行
  后另行留档且须先保证磁盘余量）。
- 当前 4.5G<5.0G → **先清理后启动**；清理动作用主仓/研究库既有
  清理惯例，不得触碰台账、钉定文件与证据目录。

### 1.3 命令与执行不变量（P4 落地）

- **命令=run1 command.txt 原文逐字**。"逐字"的机械定义（沿 run5 实践）
  ：**argv 恒等**——run1 command.txt 中 bash 反斜杠续行为排版，权威
  形态是下列 18 元 argv + cwd + PYTHONUTF8=1：
  - 解释器：`<研究库>/.venv/Scripts/python.exe`，实参首项 `-B`；
  - `<主仓>/scripts/bt.py`（16c83e7 工作树，内容不变量见 §1.1-2）；
  - 子命令 `gate`；
  - `--card H102`；
  - `--strategy mt5quant.strategies.library.fomc_preannouncement:PreFomcAnnouncementDrift`；
  - `--gates G0,G1,G2,G3,G4,V3`；
  - `--event-kind fomc_calendar`；
  - `--fomc-calendar <主仓>/.superpowers/h-new-candidates/n1-fomc-calendar/fomc_meetings.json`；
  - `--fomc-calendar-sha256 a53577d96c699a1a6e7bab2bfb5f80d43408380ea3e2f48c7e88c1e76ec971c5`；
  - `--event-days-start 1986-01-02`；
  - `--event-days-end 2021-05-17`；
  - 不传 --params、不传 --data-root、不传 --event-sims（run1 原文如此）；
  - cwd=研究库工作区（台账/成本缓存/研究数据缺省解析依赖）。
- **允许的唯一附加**：环境变量 `PYTHONUNBUFFERED=1`（§2.4，缓冲条款），
  以环境级注入，**不进入 argv、不修改命令文本**；PYTHONUTF8=1 原文
  在命令行内，保持原样。
- 引擎 stdout/stderr/exit code 全量留档（§2.4 实时落盘形态）。

### 1.4 预算与纯洁性

- run6 落账（run#452）后 R3 机械口径 **2/2**，close_rule (a)(b) 状态
  依 V3 行形态按 §3/§4 处置。
- 七元组不变（§0.4）→ run#452 与 run#451 去重=1 次研究试验，
  project_n_dedup_rounds 预期仍 2（以 G4 details 实际取数为准）、
  α/N 仍 0.025。
- 无参数挑选、无变体、无新卡：判定依据=冻结 R3.toml advance_rule 与
  gates.toml 预写规则，不按结果调整（advance_rule 元规则原文）。

---

## 2. 机制裁定（P1-P4 落地；裁定量 2）

### 2.1 载体裁定：Windows 计划任务一次性任务（备选 (i)），否决备选 (ii) 作为主载体

**裁定：采备选 (i)。**理由：

1. **存活属性论证（P1 要求）**：计划任务实例由 Task Scheduler 服务
   （svchost 托管）创建，进程树**不在任何会话 harness/对话任务的
   作业对象或进程树内**——run5 死因正是 harness 于 ~09:36-09:47 终止
   其后台任务包装 shell（exit 137）连带引擎；计划任务载体从创建关系
   上排除该杀伤路径。预期引擎运行时 ~6.1-6.4h（装配+基准+落账 ~1 分钟
   + G0-G4 ~18 分钟【run5 实测 08:47-09:05】+ V3 1999 抽取 ~5.7-5.9h
   【~10.3-10.7s/抽取实测】+ 诊断段秒级），载体无存活期上限错配。
2. **先例在场**：t121-release-v2-once-run-r2（§0.7）证明本机可注册、
   可武装一次性任务并以 powershell 包装脚本执行——机制非新发明。
3. **否决备选 (ii)（start/robocopy 类脱离+主会话轮询）作为主载体**：
   从 harness 会话内 start 的进程默认仍在会话作业对象谱系内，脱离
   语义（CREATE_BREAKAWAY_FROM_JOB）在 harness 环境下不可证成；run5
   的教训恰是"后台任务+事后补看"无存活论证不满足 P1。(ii) 仅在
   Case A 修复轮（§3）作为计划任务机制整体不可用时的替补，且须在
   重启前给出脱离证明（进程 parent 不属于 harness 子树）。
4. **对先例的关键适配（两处，均强制）**：
   - **ExecutionTimeLimit=PT12H（不得沿用先例 PT6H）**：引擎总时长
     ~6.1-6.4h，先例的 PT6H 上限会在计算途中杀掉引擎（伪装成正常
     完成的 Case B 事件）。PT12H≈1.9× 余量；到期杀灭按 §3 Case B
     处置（预算已耗，非研究结局）。
   - **LogonType=S4U 优先**（不依赖交互登录会话在场；Interactive 可
     接受——先例形态，本机为常驻 Administrator 会话，若选 Interactive
     须在启动后核实任务确在运行）。

### 2.2 可执行启动形态（注册→核验→启动）

任务名：`r3-main-test-run6-once`（一次性；用后由主会话裁量删除或
保留为工件）。注册命令（PowerShell，Administrator）：

```powershell
$action   = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument '-NoProfile -ExecutionPolicy Bypass -File "<workspace>/664d5945-068c-46aa-ba82-7141f134aa13/.superpowers/sdd/2026-11-07-r3-main-test-run6/run-engine.ps1"'
$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName 'r3-main-test-run6-once' -Action $action `
  -Trigger $trigger -Settings $settings -User 'Administrator' -LogonType S4U -RunLevel Limited
```

注册后、启动前核验（落档）：`Export-ScheduledTask -TaskName
'r3-main-test-run6-once'` XML 存 run6 证据目录，人工核对动作路径=
包装脚本、一次性触发、ExecutionTimeLimit PT12H、Principal/Administrator；
包装脚本 sha256 落档。启动：到点自启，或核验毕即
`Start-ScheduledTask -TaskName 'r3-main-test-run6-once'`（择一，落档
所用方式与时戳）。

### 2.3 包装脚本（run-engine.ps1）授权形态

研究库 run6 证据目录内新建（本裁定授权创建）。要点（完整骨架，
执行方可等价展开但不得引入台账写入/重试/任何额外动作）：

```powershell
$ErrorActionPreference = 'Stop'
$RESEARCH = '<workspace>/664d5945-068c-46aa-ba82-7141f134aa13'
$dir = Join-Path $RESEARCH '.superpowers/sdd/2026-11-07-r3-main-test-run6'
Set-Location $RESEARCH                                  # cwd=研究库工作区（P4）
$env:PYTHONUTF8 = '1'                                   # 原文命令行内既有
$env:PYTHONUNBUFFERED = '1'                             # 缓冲条款（§2.4），不进 argv
$py = "$RESEARCH/.venv/Scripts/python.exe"
$argv = @('-B','<workspace>/demo-next-20261105/scripts/bt.py','gate',
  '--card','H102',
  '--strategy','mt5quant.strategies.library.fomc_preannouncement:PreFomcAnnouncementDrift',
  '--gates','G0,G1,G2,G3,G4,V3',
  '--event-kind','fomc_calendar',
  '--fomc-calendar','<workspace>/demo-next-20261105/.superpowers/h-new-candidates/n1-fomc-calendar/fomc_meetings.json',
  '--fomc-calendar-sha256','a53577d96c699a1a6e7bab2bfb5f80d43408380ea3e2f48c7e88c1e76ec971c5',
  '--event-days-start','1986-01-02',
  '--event-days-end','2021-05-17')                      # §1.3 argv 恒等（全部实参无空格，数组拼接无损）
"START $(Get-Date -Format o) task=r3-main-test-run6-once" | Add-Content "$dir/gate-run6-timing.txt"
$p = Start-Process -FilePath $py -ArgumentList $argv `
  -RedirectStandardOutput "$dir/gate-run6-stdout.txt" `
  -RedirectStandardError  "$dir/gate-run6-stderr.txt" -PassThru
while (-not $p.HasExited) {                              # 60s 心跳：实时 CPU 轨迹（run5 只能事后重建）
  Start-Sleep -Seconds 60
  try { $p.Refresh(); "ALIVE $(Get-Date -Format o) cpu=$($p.CPU)" | Add-Content "$dir/gate-run6-timing.txt" } catch {}
}
"END $(Get-Date -Format o) exit=$($p.ExitCode)" | Add-Content "$dir/gate-run6-timing.txt"
exit $p.ExitCode
```

（等价授权形态：任务动作直接经 Git Bash 以 run1 command.txt 逐字节
命令行文本执行并 `>`/`2>` 重定向——前提同 §1.3 不变量；执行方择一
并在证据目录声明所用形态与 argv 对照。）

### 2.4 stdout 缓冲条款（run5 形态亡失的针对性防线）

- run5 的引擎 stdout 重定向至文件且块缓冲，进程被杀未刷出→**0 字节
  亡失**，台账成为唯一证据。run6 强制：**PYTHONUNBUFFERED=1（环境级）
  + 子进程句柄直通重定向文件**（Start-Process -RedirectStandardOutput
  将文件句柄交给子进程；unbuffered 下每次 print 即刻落盘）。stderr
  同覆盖。等效替代（bash 形态）：重定向前 export PYTHONUNBUFFERED=1。
- **不得**以向 argv 追加 `-u` 的方式实现（改 argv 违反 P4）。
- 心跳文件（§2.3）独立于 stdout，提供分钟级存活与 CPU 证据。

### 2.5 监控方式（主会话/任意后续会话，全程只读，P2）

每 ≤30 分钟一轮（建议 15 分钟），全部只读，留档可并入选证目录：

1. **任务层**：`Get-ScheduledTask -TaskName 'r3-main-test-run6-once'`
   （State）与 `Get-ScheduledTaskInfo`（LastRunTime/LastTaskResult）。
2. **进程层**：`Get-CimInstance Win32_Process -Filter "name='python.exe'"`
   过滤 CommandLine 含 bt.py——确认在场 + **ParentProcessId 溯源非
   harness 子树**（P1 存活独立性的运行时验证）；记录 CPU 时间增量。
3. **证据层**：gate-run6-timing.txt 尾读（ALIVE 行推进、cpu 单调增）；
   gate-run6-stdout.txt 增长观察（装配横幅/预检行；引擎不一定逐抽
   打印，CPU 轨迹为主证据）。
4. **台账层**：mode=ro 查 `runs max`（451→452 即落账）与 `gates max`
   （83→89 即 G0-G4+V3 六行写毕）。瞬时 `database is locked`（引擎
   写事务撞轮询读）按下轮重试处理，非异常。
5. **完成判定**：任务 State 回 Ready + timing END 行 + 台账 V3 行
   在场 → 进入 §4；END 行 exit≠0 或 V3 行缺位 → §3。
6. **禁止**：任何对引擎进程的暂停/终止/优先级干预；任何台账写；
   任何"替引擎补写"行为（P2：V3 行只能由引擎进程经生产 bt gate
   路径 append 写入；离线复现/补算产物只能作留档）。

### 2.6 P2 台账纪律（重申）

V3 gates 行只能由引擎进程本体写入；包装器/心跳/计划任务/轮询观察者
零台账写；任何一方合成、预写、回填该行=撤销条件（§5-2）。

---

## 3. 预立条款（P3：若 run6 再被外部终止/异常退出）

机械判据=台账内容（新 runs 行在否）+ END 行 exit code + 任务
LastTaskResult，不以任何口头/记忆陈述为准：

### Case A：无新 runs 行（启动失败/装配拒绝/落账前被终止）

- 预算机械口径不变（仍 1/2，零台账效应）。
- **允许同 argv 同授权重试恰一次**，前置三件：(i) run5 形态全量取证
  留档（timing/任务 LastTaskResult/进程取证/日志）；(ii) 载体缺陷
  定位与修复（可更换载体形态：任务参数修正、或备选 (ii) 脱离进程——
  须附脱离证明；**命令 argv 与 §1.3 不变量不变**）；(iii) §1.1 运行
  前核验全套重跑（含磁盘硬门）。
- **第二次 Case A**（重试仍无新 runs 行）：本裁定不再授权任何重试，
  回 A1 级新裁定。

### Case B：run#452 已落账，无 V3 行（落账后被终止/崩溃/PT12H 上限杀灭）

- 预算机械口径 **2/2**，close_rule (a) 触发（预算用尽）。
- **本裁定不授权第三次执行**：trial_budget=2 钉死于冻结 R3.toml
  （toml_sha256 不可变），第三次运行超出 R3 冻结预算结构；任何继续
  意图须主会话另起治理链处理轮次/预算结构问题（超出本裁定与既有
  全部授权）。
- 处置：run5 形态全量取证（本裁定载体下证据显著强于 run5：实时
  stdout 落盘+分钟级 CPU 心跳+任务 LastTaskResult）；可选离线复现
  留档（仅证据，P2）；R3 按处置裁定路径 B 语义收口：bt round close
  R3，结论登记"**无有效机器判定（基础设施事件：载体存活期内引擎被
  外部终止/异常退出）**"，统计证据在档供谱系引用，不得声明通过/
  advance，亦不得登记"未通过"（无判定载体）。

### Case C：V3 行出现（引擎完成判定写入）→ 按 §4 关轮路径处置。

### Case D（并入 C 的预立）：V3 行为 passed=0 且带 error 字段（新缺陷形态，非本次修复能救）

- 该行是 fail-closed 错误行，不是预注册规则的判定产物（处置裁定 §1
  已确立的定性对任何 error 行成立）→ **不是有效机器判定**。
- **无新修复授权**：范围裁定 §2 的修复授权恰一处 diff，已消费于
  ca626ae；任何新修复须全新治理链，且预算已 2/2——R3 内无修复重跑
  空间。
- 处置：全量取证；按 Case B 同款收口（结论"无有效机器判定（引擎
  新缺陷形态，缺陷编号由主会话按台账/文档惯例登记）"）；处置裁定
  §4.5 的 N2 开轮前置义务扩展覆盖该新缺陷形态。

---

## 4. 裁定量 3：run6 结果的关轮执行路径（V3 行出现后）

执行者=**主会话**（机械步骤；沿处置裁定 §6.2c/§6.4，本裁定不代行）：

1. **读账与身份核对**：直读 run#452 V3 行及 G0-G4 行；核对七元组
   （data_sha256/gates_hash/strategy_sha/cost_model_version/params）
   与 run#451 逐项一致；G4 details 的 N 组成（预期 project_n_
   dedup_rounds=2，以实际取数为准）。确定性预期值（主检验
   observed=+0.0035718、p=0.0085、成本 ×1.5 净均值 +0.0033856>0、
   seed=10640155159524715549）仅作异常检测对照：偏离不改变机械判定
   本身，但触发调查条款（身份不符=撤销条件级事件，回 A1）。
2. **advance_rule 机械判定**（R3.toml 原文逐字）：主检验单侧
   p ≤ α/N（α=0.05、N 以台账实际取数，预期 2 → 0.025）**且** G0
   通过 **且** 唯一适用否决项（成本 ×1.5 每事件净均值>0）通过；
   诊断四项只报告不否决——修复后 window_shift_1_bar 降级行
   passed=True 不参与关卡结论（钉定测试断言 5 + evaluate_v3 结构
   保证）；延迟一根/买入持有两项不适用不运行；G1/G2/G4 不在判定项。
3. **V3 passed=1**：
   a. E4 锁箱机械确认（当前 lockbox_uses/lockbox_budget 0 行直查在案）、
      E5 D33 按 runs 实际取数重列、E9 provisional 解除落档、E10 诊断
      段边界——四项开轮程序义务（沿处置裁定 §4.6，属主会话）；
   b. **G5/锁箱使用另经批准**（R3.toml 明文；lockbox_uses_max=1；
      本裁定不含该授权）→ 获批后执行（至多 1 次）；
   c. `bt round close R3` 生成轮次报告（含全部运行与按 advance_rule
      的判定）→ R3 advance 终态。
4. **V3 passed=0 真判定行**（完整结构、无 error 字段）：先完成 1 的
   身份核对；一致则如实登记"未通过"结论关闭（close_rule (b) 后句），
   不得以任何确定性预期改写判定。
5. **V3 passed=0 带 error 字段**：§3 Case D 预立条款。
6. **报告义务（任何收口形态均须）**：F2 读账义务——V3 行
   window_shift_1_bar.reason 只含迭代序首个失败标签（预期
   fomc_calendar:1996-07-03），**报告层必须并列真实计数 2/213**
   （1996-07-03 与 2002-01-30，范围裁定 §0.3 确证、聚焦评审 F2），
   不得把单标签误读为"仅一事件受影响"；A1-D（月末子集
   -0.0011462 vs 全集 +0.0035718，负贡献拖累项）与 E7（charged 夜数
   实测 214≠申报 213，1 事件 2 夜=fomc_calendar:2002-01-30；申报
   swap_free power 0.5949=pass 与 charged 参考 4.0740/0.31382/0.4475
   并列）按 §总-3 并列；run6 证据目录全量归档（命令副本/运行前核验/
   stdout/stderr/timing/任务 XML/台账前后状态/报告）。

---

## 5. 撤销条件（任一成立本裁定授权即失效，回 A1 级重审）

**并入上游**：

1. 处置裁定 §5 全部撤销条件（含其并入的整体裁量 2026-11-07-r3-run5-
   holistic-ruling §5 六项：绑定工件/钉定文件再变、冻结源新变更、
   链校验问题、证据被证伪、FRED 修订、used=true 伪声明；及其自身
   五项）；
2. 范围裁定 §7 全部六项（实施 diff 越界、TDD 证据不符、规范文本
   反证、未授权执行等）；
3. 聚焦评审结论被推翻（其 PASS 所依赖的独立复现被更高权威证伪）。

**新增（本裁定特有）**：

4. run6 未按 §1.3 命令不变量执行（argv 不恒等、cwd 不符、附加
   --params/--event-sims 等），或启动时 §1.1 核验任一项未通过而仍
   启动，或磁盘硬门（≥5.0G）被违反；
5. V3 行非引擎本体写入（任何合成/预写/回填），或轮询观察者/包装器
   出现任何台账写动作；
6. 载体违反 §2 授权形态：以 harness 后台任务/会话内联前台执行、
   ExecutionTimeLimit≠PT12H 形态注册后未修正即启动、或 Case A 重试
   未经前置三件；
7. 主仓/研究库出现本裁定未掌握的变更且影响冻结源、钉定文件或台账
   （新增人工台账行为）；
8. Case B/D 后未经新授权擅自第三次执行 R3 运行或对 events.py/bt.py
   的任何新变更。

---

## 6. 执行清单（谁做什么）

- **执行侧（主会话或其指定执行体；机械步骤）**：§1.1 运行前核验
  重跑落档 → 磁盘清理至 ≥5.0G → 建 run6 证据目录与包装脚本 →
  注册并核验计划任务 → 启动 → §2.5 轮询监控（只读）→ 完成后按
  §3/§4 报告与留档。零台账写入；不派任何修复动作。
- **主会话（裁量与集成）**：登记路径 A 执行决定；集成本裁定文书与
  push 裁量（沿先例）；V3 行出现后执行 §4 关轮路径（含 E4/E5/E9/
  E10 与 G5/锁箱另批）；Case B/D 时按预立条款收口。
- **本裁定者（A1）**：不执行上述任何机械步骤；本记录为唯一输出。

## 7. 签署（占位）

- 裁定者：A1 级独立裁定者（2026-11-07；只读核验留档于本记录 §0）
- approved_by：____________（占位，主会话裁量；本裁定记录不代填）

（裁定只写本文件；不 commit，主会话集成。）
