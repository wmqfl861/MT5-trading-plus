# MT5 量化系统计划任务安装脚本（日报每日；数据采集每周六）。
# 本脚本只负责"创建"，由主会话/运维手动执行；测试只在临时目录替代 schtasks。
# 默认仅日报与周报；-IncludeDataTasks 才另外覆盖既有数据采集任务。
# 数据任务没有日报的 Demo 身份检查，须另行核对终端与配置后才可选择。
# 安装脚本从部署树（定版 SHA）运行：脚本和部署参数取自部署树。解释器和数据
# 目录不一定在部署树里，用 -PythonExe、-DataDir 等显式给出（T213，见下文）。
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
#
# T213（2026-11-23）：部署树是纯检出，没有 .venv，也不放运行数据。解释器、
# 网关配置、数据、运行器日志、任务日志、日报、点差模型、会话配置都可以用参数
# 显式给出；不给的沿用仓库根下的旧位置。解释器、会话配置、点差模型、Demo 模式的
# 网关配置不存在，或者路径写不进能跑起来的 .cmd 包装器（含系统 ANSI 代码页表示
# 不了的字符；或者两个系统代码页不同而路径里有非 ASCII 字符），就拒绝安装
#（不建目录、不写包装器、不建任务）——11-17 起日报和周检任务因为命令指向部署树里
# 不存在的 .venv，每次都以 rc=1 失败，Python 根本没有启动。安装结束打印一行
# TASK_PARAMETERS {…}，任务参数随部署形态写进部署记录。

[CmdletBinding()]
param(
    [string]$Mode = "",
    [switch]$IncludeDataTasks,
    # T213：以下各项可以不在部署树里；不给就沿用仓库根下的旧位置。
    [string]$PythonExe = "",
    [string]$GatewayConfig = "",
    [string]$DataDir = "",
    [string]$RunnerLogDir = "",
    [string]$TaskLogDir = "",
    [string]$ReportsDir = "",
    [string]$SpreadModel = "",
    [string]$SessionsConfig = ""
)

$ErrorActionPreference = "Stop"
if ($Mode -notin @("Demo", "PaperSoak")) {
    throw "须显式指定 -Mode Demo 或 -Mode PaperSoak；不自动回退到终端。"
}

$scriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot   = Split-Path -Parent $scriptsDir
function Resolve-Location([string]$given, [string]$fallback) {
    if ([string]::IsNullOrWhiteSpace($given)) { return $fallback }
    return [System.IO.Path]::GetFullPath($given)
}
$dataDir      = Resolve-Location $DataDir (Join-Path $repoRoot "data")
$python       = Resolve-Location $PythonExe (Join-Path $repoRoot ".venv\Scripts\python.exe")
$logsDir      = Resolve-Location $TaskLogDir (Join-Path $dataDir "logs")
$runnerLogs   = Resolve-Location $RunnerLogDir (Join-Path $dataDir "demo")
$reportsDir   = Resolve-Location $ReportsDir (Join-Path $dataDir "reports")
$spreadPath   = Resolve-Location $SpreadModel (Join-Path $dataDir "costs\spread_model.json")
$sessionsPath = Resolve-Location $SessionsConfig (Join-Path $repoRoot "config\sessions.toml")

# 配置文件统一为 config.json（README 约定）；兼容旧名 gateway.json；T213 起可用 -GatewayConfig 指定。
$cfgPath = $null
if (-not [string]::IsNullOrWhiteSpace($GatewayConfig)) {
    $cfgPath = [System.IO.Path]::GetFullPath($GatewayConfig)
} else {
    foreach ($name in @("config.json", "gateway.json")) {
        $candidate = Join-Path $repoRoot $name
        if (Test-Path $candidate) { $cfgPath = $candidate; break }
    }
}

# T213：先核对任务命令里要用的文件都在、路径写得进包装器，再产生任何副作用（建目录、
# 写包装器、建任务）。缺哪一项都拒绝安装并说明缺什么，一个目录都不建。
$missing = @()
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $missing += "解释器 $python（用 -PythonExe 给出实际的 python.exe）"
}
if (-not (Test-Path -LiteralPath $sessionsPath -PathType Leaf)) {
    $missing += "会话配置 $sessionsPath（用 -SessionsConfig 给出部署树里的 sessions.toml）"
}
if (-not (Test-Path -LiteralPath $spreadPath -PathType Leaf)) {
    $missing += "点差模型 $spreadPath（用 -SpreadModel 给出部署实际使用的 spread_model.json）"
}
if ($null -ne $cfgPath -and -not (Test-Path -LiteralPath $cfgPath -PathType Leaf)) {
    $missing += "网关配置 $cfgPath"
}
if ($Mode -eq "Demo" -and $null -eq $cfgPath) {
    $missing += "网关配置（Demo 日报必需）：部署树里没有 config.json 或 gateway.json，请用 -GatewayConfig 指定"
}
if ($missing.Count -gt 0) {
    throw ("任务命令引用的文件不存在，拒绝安装：" + ($missing -join "；"))
}

# 评审 R7（2026-11-23）：.cmd 包装器由 cmd.exe 按控制台代码页读取，新开控制台的缺省值
# 是系统 OEM 代码页。原先按 ASCII 写，中文等字符全变成 "?"，任务装得上却跑不起来。
# 现在分两种情形，都在产生副作用之前判断：
#   - ANSI 和 OEM 两个代码页相同：包装器按这个代码页写；任何路径里有它表示不了的
#     字符（往返编码后不相等）就拒绝安装。
#   - 两个代码页不同：不去猜哪种编码对 cmd 有效，路径里只要有一个非 ASCII 字符就
#     拒绝安装；纯 ASCII 路径照常。
# 两个代码页做成可以替身的命令：调用方事先定义同名函数，脚本就用它（和 schtasks 的
# 替身同一个方式）；没有定义才用这里的真实实现。
if (-not (Get-Command Get-SystemCodePages -ErrorAction SilentlyContinue)) {
    function Get-SystemCodePages {
        if (-not ("MT5Quant.SetupCodePages" -as [type])) {
            Add-Type -Namespace MT5Quant -Name SetupCodePages -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint GetACP();
[DllImport("kernel32.dll")] public static extern uint GetOEMCP();
'@
        }
        return [pscustomobject]@{
            ACP   = [int][MT5Quant.SetupCodePages]::GetACP()
            OEMCP = [int][MT5Quant.SetupCodePages]::GetOEMCP()
        }
    }
}
$codePages = Get-SystemCodePages
$acp = [int]$codePages.ACP
$oemcp = [int]$codePages.OEMCP
if ($acp -eq 65001) {
    # UTF-8 不带 BOM：BOM 会被 cmd 当成第一条命令的一部分
    $wrapperEncoding = New-Object System.Text.UTF8Encoding($false)
} else {
    $wrapperEncoding = [System.Text.Encoding]::GetEncoding($acp)
}
foreach ($location in @($python, $repoRoot, $dataDir, $logsDir, $runnerLogs, $reportsDir, $spreadPath, $sessionsPath, $cfgPath)) {
    if ($null -eq $location) { continue }
    if ($acp -ne $oemcp -and $location -match '[^\x00-\x7F]') {
        throw ("这台机器的控制台代码页（OEM $oemcp）和系统 ANSI 代码页（$acp）不同，带非 ASCII 字符的路径装出来的任务跑不起来，拒绝安装：$location。请把项目放在纯 ASCII 路径下。")
    }
    if ($wrapperEncoding.GetString($wrapperEncoding.GetBytes($location)) -cne $location) {
        throw ("路径含系统 ANSI 代码页（$acp）表示不了的字符，写出的 .cmd 包装器跑不起来，拒绝安装：$location。请改用这个代码页能表示的路径（建议只用 ASCII 字符）。")
    }
}
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
$cfgArg = ""
if ($null -ne $cfgPath) { $cfgArg = " --config `"$cfgPath`"" }

function New-TaskWrapper($name, $pyScript, $pyArgs) {
    # 生成 scripts\task_<name>.cmd：cd 仓库根 → 跑 python → 输出落日志。按系统 ANSI 代码页写（见上）。
    $cmd = Join-Path $scriptsDir ("task_" + $name + ".cmd")
    $lines = [string[]]@(
        "@echo off",
        "cd /d `"$repoRoot`"",
        "`"$python`" `"$scriptsDir\$pyScript`"$pyArgs >> `"$logsDir\task_$name.log`" 2>&1"
    )
    [System.IO.File]::WriteAllLines($cmd, $lines, $wrapperEncoding)
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
    # 换算依据见文件头"任务一览"——所选白天时刻（>=06:00）处于周末休市段，无纽约盘中重叠）。
    schtasks /Create /F /TN "MT5Quant\$name" /SC WEEKLY /D SAT /ST $time /TR "`"$wrapper`""
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks 创建 MT5Quant\$name 失败（退出码 $LASTEXITCODE）"
    }
    Write-Host "已创建 MT5Quant\$name（每周六 $time 本地 = $utcTime UTC）→ $wrapper"
}

# 默认只安装演练日报/周报；数据任务需运维另行显式选择（T182 后仍每周六运行）。
if ($IncludeDataTasks) {
New-SaturdayTask "snapshot_specs" "10:00" "02:00" `
    (New-TaskWrapper "snapshot_specs" "snapshot_specs.py" $cfgArg)
New-SaturdayTask "refresh_bars" "11:00" "03:00" `
    (New-TaskWrapper "refresh_bars" "refresh_all.py" ($cfgArg + " --data-dir `"$dataDir`""))

$ticksDir = Join-Path $dataDir "ticks"
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
# T213：--sessions-config 是换日窗的定义源；日报只在窗内连终端——每一次尝试
#       （含每个目标日的第一次）开始之前判窗，重试开始的时刻落在窗外就不再试
#       （夏令时窗到 06:00 本地为止，05:57 启动后放不下第二次；冬令时窗到 07:00，
#       可以重试到第 3 次）。
$reportArgs = " --date yesterday --data-dir `"$dataDir`"" +
    " --reports-dir `"$reportsDir`"" +
    " --spread-model `"$spreadPath`"" +
    " --log-dir `"$runnerLogs`"" +
    " --sessions-config `"$sessionsPath`""
$paperDir = Join-Path $dataDir "live\paper"
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
$weeklyLines = [string[]]@(
    "@echo off",
    "cd /d `"$repoRoot`"",
    "set PYTHONUTF8=1",
    "if not exist `"$runnerLogs`" mkdir `"$runnerLogs`"",
    "`"$python`" `"$scriptsDir\demo_check.py`" --summary --data-dir `"$dataDir`" --log-dir `"$runnerLogs`" --reports-dir `"$reportsDir`" > `"$runnerLogs\weekly-check.md`" 2>&1"
)
[System.IO.File]::WriteAllLines($weeklyCmd, $weeklyLines, $wrapperEncoding)
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

# T213：任务参数随部署形态写进部署记录（F-8）。这一行是给部署记录抄用的。里面是
# 本机的绝对路径、任务名和两个时刻字段（daily_report.local_time、weekly_check.utc）。
# 抄进入库的部署记录之前，路径要换成占位符。
$taskParameters = [ordered]@{
    mode            = $Mode
    python          = $python
    gateway_config  = $cfgPath
    data_dir        = $dataDir
    runner_log_dir  = $runnerLogs
    task_log_dir    = $logsDir
    reports_dir     = $reportsDir
    spread_model    = $spreadPath
    sessions_config = $sessionsPath
    daily_report    = [ordered]@{
        task = "MT5Quant\live_daily_report"; local_time = "05:57"
        wrapper = (Join-Path $scriptsDir "task_live_daily_report.cmd") }
    weekly_check    = [ordered]@{
        task = "MT5Quant\demo_weekly_check"; utc = "Sunday 23:00"; wrapper = $weeklyCmd }
}
Write-Host ("TASK_PARAMETERS " + ($taskParameters | ConvertTo-Json -Compress -Depth 3))
