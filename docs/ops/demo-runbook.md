# 模拟盘演练运维手册（阶段 F，两周）

演练 = `demo_run.ps1` 启动的 `--paper` 影子盘集群（真实行情 + PaperBroker
本地记账，**绝不连真实下单通道**）：每策略一个运行器、一个风控 `--loop`、
一个对账 `--loop`，全部只读聚合 `data\live\paper\state-<magic>.json`。
验收标准见蓝图 §7；逐条检查器是 `scripts/demo_check.py`。

## 1. 启动（第 0 天）

```powershell
# 一次性：安装每日/每周计划任务（演练期会自动检测 paper 状态切日报口径）
powershell -ExecutionPolicy Bypass -File scripts\setup_scheduled_tasks.ps1

# 启动演练集群（启动前自检：点差模型/研究缓存/合约快照/会话配置/风控配置/
# 磁盘/magic 唯一，任一不过不启动任何进程）
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1
```

- 产物：`data\demo\pids.txt`（PID 清单）、`data\demo\demo-run-*.json`（清单）、
  每进程一个 `runner|risk|reconcile-<name>-<stamp>.log`（+ `.err`）。
- 顺序已内建：先运行器（写意图/心跳/paper 状态）→ 8 秒后风控 → 对账。
  **不要**先起风控（心跳未落盘会被写成 halt.json 一键停机）。
- 演练期 `data\live\paper` 存在，`MT5Quant\live_daily_report` 计划任务自动改读
  `--paper-state`（不连真实账户）；演练结束归档 paper 目录后重跑安装脚本恢复
  真账户口径。
- 重启机器后：直接重跑 `demo_run.ps1`（paper 状态文件恢复全部记账，票据序号
  接着发）。**每次重启都会生成新日志文件**——这是 demo_check 识别"重启"的
  依据，属正常现象。

## 2. 每日检查（每个交易日，5 分钟）

```powershell
# 当日（昨日 UTC 日）验收：7 项检查 + 退出码（0 过 / 1 有不通过 / 2 数据错）
.venv/Scripts/python.exe scripts/demo_check.py --day 2026-11-10

# 日报（00:20 计划任务自动生成；演练期读 paper 状态 + 决策日志点差比对）
type data\reports\live\daily-2026-11-10.md
type data\logs\task_live_daily_report.log      # 任务输出（排查用）
```

- 重点看：心跳连续性、无法解释持仓差异=0、日报存在且点差无 `**超标**`。
- 心跳异常先看 `data\live\alerts.log` 与对应 `.err` 日志，再按 §3 处理。
- 每周日 23:00 UTC（本机周一 07:00）`MT5Quant\demo_weekly_check` 自动跑
  `demo_check --summary` 全周汇总，落 `data\demo\weekly-check.md`（覆盖写）；
  平时手动：`.venv/Scripts/python.exe scripts/demo_check.py --summary`。

## 3. 常见问题

**killSwitch 一键停机后恢复**（演练项，预期会触发一次）：
```powershell
New-Item -ItemType File data\live\killSwitch      # 停：风控下一轮写 halt.json
#   各执行器开仓腿随即被拒；这是"一键停机"演练项的证据（RISK_HALT 告警）
Remove-Item data\live\killSwitch, data\live\halt.json -ErrorAction SilentlyContinue
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1   # 重启集群
```

**进程重启**：
```powershell
Get-Content data\demo\pids.txt                     # 看 PID 清单
taskkill /PID <pid> /F                             # 紧急停单个进程
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1   # 重启
```
注意：`taskkill /F` 不留退出日志，demo_check 的"重启有说明"检查会不通过。
计划内停止优先 Ctrl-C（日志留"收到中断，退出"）或先放 killSwitch；紧急
强杀后，在 `alerts.log` 有当日 HEARTBEAT_TIMEOUT/RISK_HALT 佐证也可接受。

**日报缺失/内容异常排查**（`data\reports\live\daily-*.md` 没生成）：
1. `type data\logs\task_live_daily_report.log`——最常见：影子状态不可用
   （退出码 2，fail-closed 不落半截日报）→ 确认运行器活着、
   `data\live\paper\state-*.json` 存在且非空；
2. 点差比对报 `model_sha256` 不一致 → 重跑 `scripts/build_cost_model.py`
   重建 `data\costs\spread_model.json`（日报拒绝用被动过的模型）；
3. 手动补跑（与计划任务同参）：
```powershell
.venv/Scripts/python.exe scripts/live_daily_report.py --date 2026-11-10 `
    --paper-state data\live\paper `
    --spread-model data\costs\spread_model.json --log-dir data\demo
```

## 4. 结束（第 10 个交易日后）

```powershell
# 1) 逐日验收全过 + 演练证据汇总，落最终报告
.venv/Scripts/python.exe scripts/demo_check.py --summary > data\demo\final-check.md
#   §7 证据要求：RISK_FLATTEN ≥ 2（日亏全平连续两次）、禁入拒单 ≥ 1、
#   覆盖 ≥ 10 个交易日、一键停机/心跳超时有重启恢复记录

# 2) 停止全部演练进程
New-Item -ItemType File data\live\killSwitch       # 先停开仓（留演练证据）
Start-Sleep 15                                     # 等风控写 halt.json
Get-Content data\demo\pids.txt | ForEach-Object { taskkill /PID ($_ -split "`t")[0] /F }
Remove-Item data\live\killSwitch, data\live\halt.json -ErrorAction SilentlyContinue

# 3) 收尾：删演练周任务；归档 paper 状态后重跑安装脚本（日报恢复真账户）
schtasks /Delete /TN "MT5Quant\demo_weekly_check" /F
Move-Item data\live\paper data\demo\paper-final     # 归档（重跑安装脚本前）
powershell -ExecutionPolicy Bypass -File scripts\setup_scheduled_tasks.ps1
```

最终交付物：`data\demo\final-check.md`（逐日 7 项 + 演练证据）、
`data\reports\live\daily-*.md`（每日日报）、`data\demo\*.log`（全程日志）。
