# MT5 量化系统计划任务安装脚本（日报每日；数据采集每周六）。
# 本脚本只负责"创建"，由主会话/运维手动执行；测试只在临时目录替代 schtasks。
# 默认仅日报与周报；-IncludeDataTasks 才另外覆盖既有数据采集任务。
# 数据任务没有日报的 Demo 身份检查，须另行核对终端与配置后才可选择。
# 安装脚本按自身所在位置定位仓库根与 .venv——必须从部署树（定版 SHA）运行。
#
# 安装（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\setup_scheduled_tasks.ps1 -Mode Demo
# 数据任务须显式追加 -IncludeDataTasks（T182：改周六后仍然如此）。
#
# 删除（卸载全部任务；逐条执行）：
#   schtasks /Delete /TN "MT5Quant\snapshot_specs"     /F
#   schtasks /Delete /TN "MT5Quant\refresh_bars"       /F
#   schtasks /Delete /TN "MT5Quant\refresh_ticks"      /F
#   schtasks /Delete /TN "MT5Quant\live_daily_report"  /F
#   schtasks /Delete /TN "MT5Quant\demo_weekly_check"  /F
# T35 的 watch_swap 已过期（原定 2026-10-31 后删除），本脚本不再安装；
# 若机器上仍残留该任务，由主会话在维护窗口手动删除：
#   schtasks /Delete /TN "MT5Quant\watch_swap"         /F
#
# 任务一览：
#
# 数据采集（T182：改为每周六本机白天运行——本机周六 >=06:00 处于周末休市窗内（周六
# 凌晨 00:00-05:59 仍是纽约周五盘中，故时刻选在白天；从根上
# 避开纽约盘中，不再与实盘三进程抢终端；本机固定台北时间 UTC+8、无夏令时，
# UTC 换算写死如下：周五收盘 = 21:00 UTC（夏）/22:00 UTC（冬），周日开盘
# = 21:00 UTC（夏）/22:00 UTC（冬），周六任何时刻都在两者之间）：
#   MT5Quant\snapshot_specs    周六 10:00 本地 = 02:00 UTC（品种参数快照，
#                              数分钟即完；距周日开盘 ≥43 小时）
#   MT5Quant\refresh_bars      周六 11:00 本地 = 03:00 UTC（K 线增量刷新；
#                              与前后任务错峰 1 小时以上）
#   MT5Quant\refresh_ticks     周六 13:00 本地 = 05:00 UTC（tick 增量刷新，
#                              最耗时故排最后；即使连续跑十数小时也远早于
#                              周日开盘（周一 05:00/06:00 本地）；
#                              仅当 data\ticks 已有品种目录时安装，否则跳过并提示）
#
# 日报（每日）：
#   MT5Quant\live_daily_report 05:57 本地  实盘日报（scripts\task_live_daily_report.cmd；
#                              T24：render_daily_report → data\reports\live\。
#                              显式 -Mode Demo 或 PaperSoak：分别读模拟账户
#                              或 --paper-state 影子状态，并做点差比对；
#                              影子目录缺失不改变模式，禁止真实账户。
#                              T182：时刻由 00:20 移入 XAUUSD 换日窗。窗口唯一
#                              定义源 = src/mt5data/sessions.py SessionPolicy
#                              （config/sessions.toml）：换日 17:00 America/
#                              New_York、前 5 分后 60 分 → 换日窗 16:55–18:00
#                              纽约；实测 XAUUSD 每日休市 = 纽约 17:00 起 60
#                              分钟、锚点随美国 DST（docs/research/
#                              rollover-dst-2026-10-25.md §3.1）。本机台北
#                              UTC+8：夏令时换日窗 = 04:55–06:00 本地（休市
#                              05:00–06:00）、冬令时 = 05:55–07:00 本地（休市
#                              06:00–07:00）——全年交集只有 05:55–06:00 本地，
#                              取 05:57（夏窗内距终点 3 分、冬窗内距起点 2 分，
#                              两季都落在窗内，无需随 DST 改计划）。换日窗内
#                              运行器被禁入规则禁止开仓（同一窗口定义源），
#                              日报连 Demo 终端只读账户/成交，不与实盘决策抢
#                              终端；T35 watch_swap 已实测换日窗内终端查询正常。
#                              计划"或"字二选一中不改为 --paper-state 只读：
#                              Demo 日报验证的就是 Demo 账户本身（demo_check
#                              验收依赖），换数据源会改变验收语义，且影子缺失
#                              时 fail-closed 会直接断日报——只挪时刻、数据源
#                              不动更稳妥。）
#
# 周任务（G2/T46 阶段 F 演练验收）：
#   MT5Quant\demo_weekly_check 周日 23:00 UTC（本机 UTC+8 = 周一 07:00）
#                             demo_check --summary 全周逐日验收汇总 →
#                             data\demo\weekly-check.md（每周覆盖写）。
#                             演练结束后随卸载清单删除。
#
# 实现说明：schtasks /TR 上限 261 字符，直接塞完整命令行必然超限——改为生成
# .cmd 包装器（cd 钉仓库根 + 输出落 data\logs\task_*.log），/TR 只填包装器路径。

[CmdletBinding()]
param(
    [string]$Mode = "",
    [switch]$IncludeDataTasks
)

$ErrorActionPreference = "Stop"
if ($Mode -notin @("Demo", "PaperSoak")) {
    throw "须显式指定 -Mode Demo 或 -Mode PaperSoak；不自动回退到终端。"
}

$scriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot   = Split-Path -Parent $scriptsDir
$python     = Join-Path $repoRoot ".venv\Scripts\python.exe"
$logsDir    = Join-Path $repoRoot "data\logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function New-TaskWrapper($name, $pyScript, $pyArgs) {
    # 生成 scripts\task_<name>.cmd：cd 仓库根 → 跑 python → 输出落日志
    $cmd = Join-Path $scriptsDir ("task_" + $name + ".cmd")
    $lines = @(
        "@echo off",
        "cd /d `"$repoRoot`"",
        "`"$python`" `"$scriptsDir\$pyScript`"$pyArgs >> `"$logsDir\task_$name.log`" 2>&1"
    )
    Set-Content -Path $cmd -Value $lines -Encoding ASCII
    return $cmd
}

function New-DailyTask($name, $time, $wrapper) {
    schtasks /Create /F /TN "MT5Quant\$name" /SC DAILY /ST $time /TR "`"$wrapper`""
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks 创建 MT5Quant\$name 失败（退出码 $LASTEXITCODE）"
    }
    Write-Host "已创建 MT5Quant\$name（每日 $time）→ $wrapper"
}

function New-SaturdayTask($name, $time, $utcTime, $wrapper) {
    # T182：数据采集改每周六本机白天（$utcTime 为对应 UTC 时刻，仅供输出核对；
    # 换算依据见文件头"任务一览"——周六全天休市，无纽约盘中重叠）。
    schtasks /Create /F /TN "MT5Quant\$name" /SC WEEKLY /D SAT /ST $time /TR "`"$wrapper`""
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks 创建 MT5Quant\$name 失败（退出码 $LASTEXITCODE）"
    }
    Write-Host "已创建 MT5Quant\$name（每周六 $time 本地 = $utcTime UTC）→ $wrapper"
}

# 配置文件统一为 config.json（README 约定）；兼容旧名 gateway.json
$cfgArg  = ""
foreach ($name in @("config.json", "gateway.json")) {
    $cfgPath = Join-Path $repoRoot $name
    if (Test-Path $cfgPath) { $cfgArg = " --config `"$cfgPath`""; break }
}

if ($Mode -eq "Demo" -and [string]::IsNullOrWhiteSpace($cfgArg)) {
    throw "Demo 日报缺少网关配置；请配置 config.json 或 gateway.json。"
}

# 默认只安装演练日报/周报；数据任务需运维另行显式选择（T182 后仍每周六运行）。
if ($IncludeDataTasks) {
New-SaturdayTask "snapshot_specs" "10:00" "02:00" `
    (New-TaskWrapper "snapshot_specs" "snapshot_specs.py" $cfgArg)
New-SaturdayTask "refresh_bars" "11:00" "03:00" `
    (New-TaskWrapper "refresh_bars" "refresh_all.py" ($cfgArg + " --data-dir `"$repoRoot\data`""))

$ticksDir = Join-Path $repoRoot "data\ticks"
if (Test-Path $ticksDir) {
    $tickSymbols = (Get-ChildItem $ticksDir -Directory | ForEach-Object { $_.Name }) -join ","
    if ($tickSymbols) {
        New-SaturdayTask "refresh_ticks" "13:00" "05:00" `
            (New-TaskWrapper "refresh_ticks" "download_ticks.py" `
                ($cfgArg + " --mode refresh --symbols `"$tickSymbols`""))
    } else {
        Write-Host "data\ticks 下无品种目录，跳过 MT5Quant\refresh_ticks（建 tick 库后重跑本脚本安装）"
    }
} else {
    Write-Host "data\ticks 不存在，跳过 MT5Quant\refresh_ticks（建 tick 库后重跑本脚本安装）"
}
}

# 日报模式显式固定；影子状态缺失时保留路径，由日报拒绝，不回退到终端。
# T182：时刻 05:57 = XAUUSD 换日窗内的全年交集时刻（推导见文件头"任务一览"）。
$reportArgs = " --date yesterday --data-dir `"$repoRoot\data`"" +
    " --reports-dir `"$repoRoot\data\reports`"" +
    " --spread-model `"$repoRoot\data\costs\spread_model.json`"" +
    " --log-dir `"$repoRoot\data\demo`""
$paperDir = Join-Path $repoRoot "data\live\paper"
if ($Mode -eq "PaperSoak") {
    $reportArgs += " --paper-state `"$paperDir`""
    Write-Host "PaperSoak 日报只读影子状态（缺失时拒绝，不连终端）"
} else {
    $reportArgs += " --demo --gw-config `"$cfgPath`""
    Write-Host "Demo 日报只读模拟账户（trade_mode=0；禁止真实账户）"
}
New-DailyTask "live_daily_report" "05:57" `
    (New-TaskWrapper "live_daily_report" "live_daily_report.py" $reportArgs)

# G2/T46：演练周报——周日 23:00 UTC 跑 demo_check --summary（全周逐日验收 +
# §7 演练证据汇总），输出覆盖写 data\demo\weekly-check.md。本机固定 UTC+8
#（台北时间，无夏令时）→ 周一 07:00 本地；用 UTC 时刻换算本地日/星期，
# 避免手算错日。
$weeklyUtc = [DateTime]::SpecifyKind([DateTime]::Parse("2026-11-01 23:00"), [DateTimeKind]::Utc)
$weeklyLocal = $weeklyUtc.ToLocalTime()
$weeklyDow = @{ [DayOfWeek]::Sunday = "SUN"; [DayOfWeek]::Monday = "MON";
                [DayOfWeek]::Tuesday = "TUE"; [DayOfWeek]::Wednesday = "WED";
                [DayOfWeek]::Thursday = "THU"; [DayOfWeek]::Friday = "FRI";
                [DayOfWeek]::Saturday = "SAT" }[$weeklyLocal.DayOfWeek]
$weeklyCmd = Join-Path $scriptsDir "task_demo_weekly_check.cmd"
Set-Content -Path $weeklyCmd -Encoding ASCII -Value @(
    "@echo off",
    "cd /d `"$repoRoot`"",
    "set PYTHONUTF8=1",
    "if not exist `"$repoRoot\data\demo`" mkdir `"$repoRoot\data\demo`"",
    "`"$python`" `"$scriptsDir\demo_check.py`" --summary --data-dir `"$repoRoot\data`" --log-dir `"$repoRoot\data\demo`" --reports-dir `"$repoRoot\data\reports`" > `"$repoRoot\data\demo\weekly-check.md`" 2>&1"
)
schtasks /Create /F /TN "MT5Quant\demo_weekly_check" /SC WEEKLY /D $weeklyDow `
    /ST $weeklyLocal.ToString("HH:mm") /TR "`"$weeklyCmd`""
if ($LASTEXITCODE -ne 0) {
    throw "schtasks 创建 MT5Quant\demo_weekly_check 失败（退出码 $LASTEXITCODE）"
}
Write-Host ("已创建 MT5Quant\demo_weekly_check（每周本地 {0} {1:HH:mm} = 周日 23:00 UTC）→ data\demo\weekly-check.md" -f $weeklyDow, $weeklyLocal)

# T35 的 watch_swap（原观测周 2026-10-26..10-30）已过期：本脚本不再安装该任务
#（2026-11-08 T182 起移除）；机器上若仍残留，由主会话在维护窗口手动删除
#（删除命令见本文件头部注释）。

Write-Host ""
Write-Host "完成。删除命令见本文件头部注释（五条 schtasks /Delete；watch_swap 残留清理另见头部）。"
