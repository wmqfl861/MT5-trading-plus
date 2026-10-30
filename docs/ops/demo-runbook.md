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
# 一次性：安装每日/每周计划任务（演练期会自动检测 paper 状态切日报口径）
powershell -ExecutionPolicy Bypass -File scripts\setup_scheduled_tasks.ps1

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
- PaperSoak 演练期 `data\live\paper` 存在，`MT5Quant\live_daily_report`
  计划任务自动改读 `--paper-state`（不连真实账户）；演练结束归档 paper
  目录后重跑安装脚本恢复真账户口径。
- 重启机器后：直接重跑同模式命令即可——脚本按 `data\live\instances.json`
  （三要素核验）检测并跳过仍存活的进程，只补已退出的角色（绝不满组
  重跑，见 §3）。**每次启动都生成新日志文件**——这是 demo_check 识别
  "重启"的依据，属正常现象；恢复重启会向 run-manifest.json 追加
  kind=restart 条目（运行窗不清）。

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
3. 手动补跑（与计划任务同参）：
```powershell
.venv/Scripts/python.exe scripts/live_daily_report.py --date 2026-11-10 `
    --paper-state data\live\paper `
    --spread-model data\costs\spread_model.json --log-dir data\demo
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

# 4) 收尾：删演练周任务；归档 paper 状态后重跑安装脚本（日报恢复真账户）
schtasks /Delete /TN "MT5Quant\demo_weekly_check" /F
Move-Item data\live/paper data\demo\paper-final     # 归档（重跑安装脚本前）
powershell -ExecutionPolicy Bypass -File scripts\setup_scheduled_tasks.ps1
```

最终交付物：`data\demo\final-check.md`（--accept 验收结论 + 全期判据 +
逐日严格复核）、`data\demo\weekly-final.md`（日常汇总）、
`data\reports\live\daily-*.md`（每日日报）、`data\demo\*.log`（全程日志）、
`data\demo\pids.txt`（追加式完整进程清单）、`data\live\run-manifest.json`
（运行窗登记，追加式）、`data\live\instances.json`（进程登记）。

## UNKNOWN 订单结果人工核对（H2-01）

如果执行器状态文件出现 `execution_unknown`，表示订单请求可能已送达券商，但本地没有拿到可确认的最终回报。此状态下禁止自动重发开仓。

解除流程必须在维护窗口执行：

1. 停止该 MT5 账户/magic 的所有 Runner（包括使用不同 data_root 的实例）；专用脚本会尝试获取按 MT5 server/login + magic 派生的 OS 用户级账户锁；实盘 Runner 在所有 data_root 之间共享此锁。若任一数据目录下该账户/magic 的 Runner 仍持锁则拒绝继续。paper/offline 使用各自 data_root 的本地锁。
2. 在 MT5 终端核对该 decision 对应的成交历史、当前持仓和挂单；确认没有仍在途的请求。
3. 确认当前策略 magic 下没有挂单。脚本会再次查询并在发现挂单时拒绝解除。
4. 使用状态文件中的精确 `decision_id` 和人工核对原因执行：

```powershell
.venv/Scripts/python.exe scripts/live_ack_execution.py `
  --data-dir data `
  --gw-config config.json `
  --magic <MAGIC> `
  --decision-id "<DECISION_ID>" `
  --reason "人工核对 MT5 成交/持仓/挂单，确认该请求已终结" `
  --confirm-broker-reconciled
```

该命令只记录券商持仓/成交快照并解除 UNKNOWN，不发送、修改或平仓任何订单。若无法证明请求已终结，**不要执行解除**；保持 UNKNOWN 并暂停新开仓，先联系维护人员。解除后再按本手册启动 Runner，并运行对账确认一致。

## 风控主循环卡死 watchdog（阶段 F）

`risk_process.py --loop` 增加独立 watchdog。默认主循环连续 120 秒没有进展（且超时阈值不会低于轮询间隔的两倍加 5 秒）时，写入 `halt.json` 并告警。watchdog 不提交订单，也不自动删除 halt；必须检查账户轮询、Runner/Risk/Reconcile 日志和券商状态后，按维护流程人工恢复。
