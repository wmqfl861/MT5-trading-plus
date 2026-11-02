# 演练运维手册（阶段 F，两周；T45/T49）

`demo_run.ps1` 有两种**互斥模式**（无 `-Mode` 拒绝裸跑）——成交路径、手数
口径与验收边界完全不同，不能混用：

| | `-Mode PaperSoak`（影子盘延长观察） | `-Mode Demo`（MT5 模拟盘，蓝图阶段 F 正式口径） |
|---|---|---|
| 成交路径 | `--paper` PaperBroker 影子券商（真终端行情，**绝不连真实下单通道**） | 真实 demo 账户：下单经 MT5 网关（保险丝只放行 trade_mode=DEMO） |
| 手数 | 按配置（示例 `risk_fraction=0.005`） | **固定 `volume_lots=0.01`**；出现 `risk_fraction` 或非 0.01 → 拒绝启动 |
| 风控/对账 | `--paper-state` 聚合读 `data\live\paper\state-<magic>.json` | 直连 demo 终端（`--gw-config`），不用 paper 状态 |
| 启动前专项预检 | 七项通用预检 | 七项通用预检 + 配置手数校验 + **DEMO 保险丝与网关预检**（只读 dry：连接、读账户核对 trade_mode=0、逐品种报价；绝不下单） |
| 产生的证据 | 无经纪商成交滑点/拒单/重连证据 | 真实成交滑点、服务器端 SL/TP、拒单与重连均可观测 |
| 验收口径 | 只做日常检查（`--day`/`--summary`）；**PaperSoak 的证据不被 `--accept` 采信**（验收按运行窗登记要求 mode=="Demo" 与真实成交证据）——单独跑它不能通过阶段 F 验收 | 日常检查 + 第 10 个交易日后跑 `demo_check.py --accept`（严格验收） |

共同骨架：每策略一个运行器、一个风控 `--loop`、一个对账 `--loop`。验收
标准见蓝图 §7；检查器是 `scripts/demo_check.py`（两档口径，见 §2）。

## 1. 启动（第 0 天）

```powershell
# 一次性：在维护窗口显式指定模式安装每日/每周计划任务
powershell -ExecutionPolicy Bypass -File scripts\setup_scheduled_tasks.ps1 -Mode Demo

# 影子盘延长观察（PaperSoak；示例配置 demo-configs\MA-XAUUSD-M15.toml）
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1 -Mode PaperSoak

# MT5 模拟盘（Demo；示例配置 demo-configs\DEMO-MA-XAUUSD-M15.toml，固定 0.01 手）
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1 -Mode Demo

# 只做预检与生命周期检测（不启动任何进程；Demo 模式含网关 dry 预检）
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1 -Mode Demo -PrecheckOnly

# 停止当前登记的演练进程（只处理 instances.json 当前项，逐项核验存活）
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1 -Stop
```

- 通用预检（两种模式同）：点差模型/研究缓存/合约快照/会话配置/风控配置/
  磁盘/magic 唯一，任一不过不启动任何进程。Demo 另有手数校验（走
  `scripts/config_check.py` 的 LiveConfig 真解析器按**实际加载值**比对
  固定 `volume_lots=0.01` 且无 `risk_fraction`——注释/多行字符串/表
  作用域骗不过）与 DEMO 保险丝/网关预检（账户非模拟盘或读不到账户
  状态 → fail-closed 拒启动）。
- **生命周期（单实例，R9-I5/T55）**：实例登记在 `data\live\instances.json`
  （字段 pid/kind/magic/mode/started_utc/log/account_hint），**跨 LogDir**
  生效——换日志目录不会漏检。存活判定三要素：PID 存在 + 进程创建时间
  与登记时间容差 5 秒（防 PID 复用）+ 命令行特征（live_runner.py /
  risk_process.py / live_reconcile.py）。同 magic 或同角色且**同模式**的
  存活实例 → 跳过并告警，**绝不并行第二份**（同 magic 双写 state/意图/
  下单日志会互相覆盖）；**Mode 不符的存活实例 → mode conflict 拒绝启动**
  （先 `-Stop` 旧实例）。检测→启动→登记→运行窗追加在
  `data\live\instances.lock` 临界区内完成，并发启动的第二个调用立即
  失败。`data\demo\pids.txt` 是**追加式**启动历史日志（不参与检测）。
- **运行窗登记（C-1）**：每次启动（含恢复重启）向
  `data\live\run-manifest.json` **追加**一条 runs（kind=start|restart）；
  该文件读失败或 JSON 损坏时脚本 fail-closed 拒绝启动。`--accept` 的
  运行窗 = runs 中**当前 mode** 的最早 started_utc → now（重启不清窗）。
- 产物：`data\live\instances.json`（当前登记）、`data\live\run-manifest.json`
  （运行窗，追加式）、`data\demo\pids.txt`（追加式 PID 历史）、
  `data\demo\demo-run-<Mode>-*.json`（本次清单，含跳过的存活运行器）、
  每进程一个 `runner|risk|reconcile-<name>-<stamp>.log`（+ `.err`）。
- 顺序已内建：先运行器（写意图/心跳/paper 状态）→ 8 秒后风控 → 对账。
  **不要**先起风控（心跳未落盘会被写成 halt.json 一键停机）。
- 安装器默认只创建日报和周报任务，不覆盖快照、K 线、tick 或 swap 任务。
  历史数据任务需另行核对终端身份与配置后显式加 `-IncludeDataTasks`，
  当前阶段 F 安装命令不加该参数。
- 日报模式由安装时的 `-Mode Demo` / `-Mode PaperSoak` 显式固定。Demo
  使用 `--demo --gw-config`，拒绝实盘解锁配置，核验 `trade_mode=0` 后才读
  成交流水；PaperSoak 只读 `--paper-state`。影子目录归档或缺失不会自动
  切换到终端。当前阶段禁止真实账户连接与下单；模式变更只在维护窗口执行。
- 重启机器后：直接重跑同模式命令即可——脚本按 `data\live\instances.json`
  （三要素核验）检测并跳过仍存活的进程，只补已退出的角色（绝不满组
  重跑，见 §3）。**每次启动都生成新日志文件**——这是 demo_check 识别
  "重启"的依据，属正常现象；恢复重启会向 run-manifest.json 追加
  kind=restart 条目（运行窗不清）。

### 1a. runner 启动二次确认 trade_mode=0（3B 规格，H 线基石阶段 3）

`demo_run.ps1` 的 DEMO 保险丝/网关预检（只读 dry：连接、读账户核对
trade_mode=0）在**启动前**把过一道关；但预检通过与 runner 实际连上
网关之间存在空窗——终端可能被切到另一账户、配置可能指向别的
`--gw-config`、多终端场景下预检与运行器甚至可能不是同一连接。因此
**规格要求（实现落在 `scripts/live_runner.py`，属阶段 1+2 批次）**：

1. runner 完成网关连接后、**进入决策循环之前**，必须重新读取账户状态，
   核对 `trade_mode == 0`（DEMO）；Demo 演练配置下这是第二道独立防线，
   不替代 `demo_run.ps1` 预检。
2. 核对结果 ≠ 0（含 CONTEST=1 / REAL=2），或**读不到账户状态** →
   fail-closed：写告警（`data\live\alerts.log`，TRADE_MODE 语义），
   不发送任何下单请求，直接退出（非零退出码）——绝不"先跑起来再说"。
3. 该二次确认只在**启动时**做一次即可，不要求逐决策轮核对（逐轮口径
   属实盘批次，另行评审）。
4. 验证口径：假网关账户 `trade_mode=2` → runner 拒绝进入循环且零下单；
   `trade_mode=0` → 正常启动；账户状态不可读 → 同 REAL 处理（fail-closed）。

5. 实跑单实例锁按 `server|login|magic` 哈希，存放于
   `%LOCALAPPDATA%\mt5-trading-plus\locks\runner-<hash>.lock`，不随
   `data_root` 改变；连接前另有 magic-only 临时锁，防启动竞速。
6. **跨版本升级保护**：新 runner 同时取得身份锁与旧版
   `data\live\runner-<magic>.lock`。若旧版进程仍持有目录锁，新版拒绝
   启动并提示先停止旧进程；不得手工删除锁文件来绕过内核锁。退出时内核
   自动释放锁，锁文件本身常驻磁盘。不同数据目录的并发实例由身份锁阻止。

## 2. 每日检查（每个交易日，5 分钟；两档口径）

```powershell
# 日常档 --day（宽松：无异常证据即退出码 0；"无样本"如实标注、不判不通过）
.venv/Scripts/python.exe scripts/demo_check.py --day 2026-11-10

# 日报（00:20 计划任务自动生成；PaperSoak 读 paper 状态 + 决策日志点差比对）
type data\reports\live\daily-2026-11-10.md
type data\logs\task_live_daily_report.log      # 任务输出（排查用）
```

- 日常档返回 0 只说明"未发现异常证据"，**不等于取得验收证据**；汇总口径
  同（`--summary` 不再宣称"全部通过"，只说"未发现异常证据（日常口径）"）。
- 重点看：心跳连续性、无法解释持仓差异=0、日报存在且点差无 `**超标**`。
- 心跳异常先看 `data\live\alerts.log` 与对应 `.err` 日志，再按 §3 处理。
- 每周日 23:00 UTC（本机周一 07:00）`MT5Quant\demo_weekly_check` 自动跑
  `demo_check --summary` 全周汇总，落 `data\demo\weekly-check.md`（覆盖写）；
  平时手动：`.venv/Scripts/python.exe scripts/demo_check.py --summary`。

## 3. 常见问题（恢复原则：只移除停机信号；仅重启已确认退出的进程）

**killSwitch 一键停机后恢复**（演练项，预期会触发一次）：
```powershell
New-Item -ItemType File data\live\killSwitch      # 停：风控下一轮写 halt.json
#   各执行器开仓腿随即被拒；这是"一键停机"演练项的证据（RISK_HALT 告警）
Remove-Item data\live\killSwitch, data\live\halt.json -ErrorAction SilentlyContinue
# 恢复 = 只移除停机信号；进程若还活着就什么都不用再启动（killSwitch 不杀进程）
```

**进程重启（R8-I6/R9-I5：绝不满组重跑）**：
```powershell
# ① 核对哪些进程真的退了：instances.json 逐项核验 + 对应日志尾部
Get-Content data\live\instances.json                # 当前登记（pid/kind/magic/mode）
Get-Process -Id <pid>            # 在 = 还活着，什么都不做
Get-Content data\demo\runner-<magic>-<stamp>.log -Tail 20   # 尾部有
#   "收到中断，退出"/Traceback = 真退出；强杀不留退出说明

# ② 只重启已确认退出的进程：重跑同模式命令即可——脚本按 instances.json
#    （三要素存活判定）检测存活实例并自动跳过，只补缺的运行器/风控/对账；
#    跨模式（登记 Mode 与本次 -Mode 不符）会因 mode conflict 被拒绝
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1 -Mode Demo   # 例

# ③ 停止单个/全部当前登记进程（推荐 -Stop：逐项核验存活后才停止，
#    不遍历 pids.txt 历史 PID——历史 PID 可能已被系统复用）
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1 -Stop
#   紧急停单个进程（核验后确属演练实例）：taskkill /PID <pid> /F
#   注意：强杀不留退出日志，demo_check 的"重启有说明"检查会不通过。
#   计划内停止优先 Ctrl-C（日志留"收到中断，退出"）或先放 killSwitch；
#   紧急强杀后，在 alerts.log 有当日 HEARTBEAT_TIMEOUT/RISK_HALT 佐证
#   也可接受。

# ④ 禁止：清掉 instances.json 登记后全组重跑——同 magic 并行第二份运行器
#    会双写 state-<magic>.json、意图与下单日志（R8-I6）。instances.json
#    由脚本维护（死项自动剔除），pids.txt 是追加式历史，永远不要删。
```

**日报缺失/内容异常排查**（`data\reports\live\daily-*.md` 没生成）：
1. `type data\logs\task_live_daily_report.log`——最常见：影子状态不可用
   （退出码 2，fail-closed 不落半截日报）→ 确认运行器活着、
   `data\live\paper\state-*.json` 存在且非空；
2. 点差比对报 `model_sha256` 不一致 → 重跑 `scripts/build_cost_model.py`
   重建 `data\costs/spread_model.json`（日报拒绝用被动过的模型）；
3. 手动补跑（保持已生成 `scripts/task_live_daily_report.cmd` 中的模式和配置路径；首次生成前使用与安装器相同的配置选择顺序）：
```powershell
$gwConfig = @("config.json", "gateway.json") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $gwConfig) { throw "缺少 Demo 网关配置" }
.venv/Scripts/python.exe scripts/live_daily_report.py --date 2026-11-10 `
    --demo --gw-config $gwConfig `
    --spread-model data\costs\spread_model.json --log-dir data\demo
# PaperSoak 仅将 --demo --gw-config ... 换成 --paper-state data\live\paper。
```

## 4. 结束与最终验收（第 10 个交易日后；Demo 模式）

```powershell
# 1) 阶段 F 最终验收（--accept 严格口径：任何"无样本/未覆盖"= 未验收，
#    退出码非 0）——运行窗（data\live\run-manifest.json 的 runs 中当前
#    mode 最早 started_utc → now）之上的全期判据 + 逐日严格复核：
#      · 连续交易日覆盖 ≥10（工作日无缺日）
#      · 各进程存活证据（风控心跳日志覆盖当日**逐小时**——"每日有日志"
#        ≠ "整日连续运行"，逐小时心跳仍非绝对证明，属 --accept 的残余
#        边界：进程整日内反复"活着但卡死在小时之间"理论上仍可漏检，
#        只能靠告警与对账差异兜底）
#      · SL 与 TP 样本均存在（读 data\live\deals-<magic>.jsonl 真实成交）
#      · 双全平真实完成（drill-archive 指令 + 全部参与 magic 的 ack）
#      · 实际点差样本存在且无超标小时；滑点=成交价 vs 决策价分布
.venv/Scripts/python.exe scripts/demo_check.py --accept > data\demo\final-check.md
#   通过（退出码 0）才算阶段 F 验收证据齐全。PaperSoak 的证据**不被
#   --accept 采信**：验收按运行窗登记要求 mode=="Demo" 与真实成交证据；
#   PaperSoak 期间产生的 paper 状态/影子成交不在采信范围（那正是两种
#   模式的边界，不是工具缺陷）。

# 2) 日常全周汇总留档（宽松口径，供运维浏览）
.venv/Scripts/python.exe scripts/demo_check.py --summary > data\demo\weekly-final.md

# 3) 停止全部演练进程（-Stop：只处理 instances.json 当前项，逐项核验
#    存活后停止；不再遍历 pids.txt 历史 PID）
New-Item -ItemType File data\live\killSwitch       # 先停开仓（留演练证据）
Start-Sleep 15                                     # 等风控写 halt.json
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1 -Stop
Remove-Item data\live/killSwitch, data\live/halt.json -ErrorAction SilentlyContinue

# 4) 收尾：卸载演练周报与日报任务；只归档确实存在的影子状态
schtasks /Delete /TN "MT5Quant\demo_weekly_check" /F
schtasks /Delete /TN "MT5Quant\live_daily_report" /F
if (Test-Path data\live\paper) { Move-Item data\live\paper data\demo\paper-final }
# 不自动切换账户或重装任务。需要继续 Demo 时另按维护窗口执行本手册 §1。
```

最终交付物：`data\demo\final-check.md`（--accept 验收结论 + 全期判据 +
逐日严格复核）、`data\demo\weekly-final.md`（日常汇总）、
`data\reports\live\daily-*.md`（每日日报）、`data\demo\*.log`（全程日志）、
`data\demo\pids.txt`（追加式完整进程清单）、`data\live\run-manifest.json`
（运行窗登记，追加式）、`data\live\instances.json`（进程登记）。

## 5. 故障恢复（H 线基石阶段 1/2 落地后的新机制）

本节针对阶段 1（交易安全）与阶段 2（平仓安全）引入的持久化机制给出
运维口径。**总原则不变：只移除停机信号、只重启已确认退出的进程；
新增文件的任何异常都优先 fail-closed，宁可停着也不猜。**

### 5.1 execution journal（`data\live\execution-journal-<magic>.jsonl`）

- 每次下单**发送前**追加一行：decision_id / request_uuid / 腿 / 状态
  （pending → sent → known_result）；同一 decision_id 的重发会被 journal
  幂等拒绝——这是"崩溃重启后同一决策不重复开仓"的主防线。
- 崩溃/强杀后重启：runner 先读 journal 的 pending/sent/unknown 行，
  核对券商持仓 + 挂单 + 成交历史。**confirmed 必须有与本请求关联的成交**
  （优先匹配 broker order_ticket，其次匹配本决策唯一 comment）；同
  magic/symbol 的其他成交或单纯出现新持仓，不足以确认本请求已成交。
- **空历史 + 空持仓 + 空挂单不是 rejected 的正面证据**：订单可能已成交
  后又平仓，或历史归档延迟。只有明确 TradeRejectedError 或 §5.4 人工
  仲裁可写 rejected；其他情况无限保持 unknown，暂停开新仓（平仓不受限）。
- MT5 `PLACED` 只代表请求已受理，不代表成交；即使返回 volume 非零，
  也不得记 fill/confirmed。任何本 magic 的未完成挂单（包括旧版本、
  人工或其他路径创建、未关联 journal 的挂单）都会阻止新开仓；平仓/
  全平仍可执行，直到挂单终态得到核实。
- journal 尾部撕裂行（写到一半断电/强杀）按 unknown 处理，程序不 crash；
  不要手工编辑或截断 journal——撕裂行会在下次仲裁/对账中被消化。
- **禁止**删除 journal 来"清掉"待决项：待决项只能通过 §5.4 仲裁工具或
  券商侧核对闭环。

### 5.2 consumed deals（`data\live\consumed-deals-<magic>.jsonl`）

- 每笔**已用于解释持仓变化**的成交（ticket / volume / time / reason）
  的水位文件；部分减量（同 ticket 持仓量减少）同样要消费——旧的部分
  SL deal 永远不允许拿来解释新消失的仓位。
- **丢失或损坏 → fail-closed**：防线（pending exit）视为仍在 + 写告警，
  等同 state_corrupt 处理。恢复动作不是删文件重跑，而是：保持停机/
  暂停开仓状态，先人工核对券商成交历史，再由维护流程显式重建水位并
  留痕（重建期间开仓通道保持关闭）。

### 5.3 跨数据目录进程锁

- 实例锁按 `server + login + magic` 哈希（login 跨券商可撞号，必须带
  server），锁文件位于 OS 状态目录：
  `%LOCALAPPDATA%\mt5-trading-plus\locks\`——**换数据目录（LogDir/
  data root）绕不开**。锁获取时机在网关连接之后（需读 login）。
- 锁冲突 = 同 server+login+magic 已有存活实例：跳过并告警，绝不并行
  第二份（同 magic 双写 state/journal/consumed 会互相覆盖）。
- 强杀进程后锁残留：核验锁文件内登记的 PID 确已不存在（`Get-Process
  -Id`），且 `data\live\instances.json` 三要素判定该实例已死，方可删除
  对应锁文件；删除前把锁文件内容抄进当日处置记录（`data\demo\` 下）。

### 5.4 人工仲裁工具（`scripts\live_ack_execution.py`）

- 用途：**永久 unknown** 项的唯一出口。程序无法自动判定时（历史窗口
  不足、券商回报缺失），由人工在券商终端核对成交/持仓后，用该工具把
  journal 条目置 confirmed / rejected，操作写回 journal 留痕（含操作者
  与核对依据）。
- 使用前提：先停开仓（killSwitch 或 -Stop），再仲裁；仲裁完成后按 §3
  只重启已退出的进程。
- 严禁用"编辑 jsonl 手改状态"替代该工具——绕过留痕的修改会让对账
  失去依据。

- 仲裁前先读取并校验券商持仓；查询失败时不写 journal 终态，可直接
  修复连接后重试。若 journal 仲裁终态已落但意图文件写入失败，只允许用
  **相同 decision_id + 相同 status + 相同 reason** 重试补写意图；不同
  结论/原因或普通执行终态均拒绝改写。工具会在意图成功恢复后才写
  ACK_EXECUTION 告警。

### 5.5 重启对账三步（每次崩溃恢复自动执行，人工复核同序）

1. journal 待决项（pending/sent/unknown）逐条核对券商实际持仓/挂单/
   成交历史；
2. expected（`expected-<magic>.json`）vs 券商实际持仓比对——expected
   损坏/缺失时从 journal + 券商实际持仓重建，并写差异告警；
3. 差异写 pause（暂停开仓），人工按 §5.4 闭环后才恢复。

### 5.6 维护窗口刷新授权快照（3A 后续，R8）

快照链路已支持 `currency_base`（基础币种）字段：mode=2 隔夜费校验
只在快照带该字段时才能给出确定判定，**旧快照（无该字段）会被显式
拒绝而不是退回利润币种代算**。因此下一次维护窗口应重跑
`scripts/snapshot_specs.py` 刷新授权快照（`data\specs\latest.json`），
并用 `snapshot_specs.py` 的 diff 输出确认只新增了 `currency_base` 一类
字段、其余合约参数无漂移；刷新前后各留一份 history 版本供审计。

## 6. 已知限制与监控建议

### 6.1 风控进程"活着但冻结"的漏检风险（2026-10-30 审计发现）

`demo_check --accept` 的存活判据是**风控心跳日志覆盖当日逐小时**。
这**不是**连续存活的绝对证明：进程在写出某小时的心跳行之后立刻卡死
（日志冻结、循环不再推进、但 PID 仍在），到下一次小时检查前都不会
暴露——runbook §4 已把该残余边界写进验收注记。进程内的 risk
watchdog 线程与被冻结的进程同生共死，不能自证存活。

建议监控（演练期部署，成本极低）：

1. **外部新鲜度探测**：新增一个独立计划任务（每 5–10 分钟），检查
   `data\live\heartbeat-<magic>.json` 的内容时间戳与各风控日志的文件
   mtime，超过阈值（建议 2× 心跳周期，如 240s）即向
   `data\live\alerts.log` 写 STALE 告警——外部探测与被监控进程无
   同生共死关系，是唯一能抓"活着但冻结"的手段。
2. 日常档 `demo_check --day` 重点关注心跳连续性与 HEARTBEAT_TIMEOUT
   类告警；收到即按 §3 检查进程实际状态（任务管理器看 CPU/线程，
   不只看 PID 存在）。
3. **兜底**：无法解释持仓差异 ≠ 0 与对账告警是最终防线——冻结的
   风控最终会表现为对账差异；每日检查不可因为"有外部探测"而跳过。
