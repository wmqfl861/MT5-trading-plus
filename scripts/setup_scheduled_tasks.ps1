# MT5 量化系统每日计划任务安装脚本。
# 本脚本只负责"创建"，由主会话/运维手动执行；不随任何测试自动运行。
#
# 安装（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\setup_scheduled_tasks.ps1
#
# 删除（卸载全部任务；逐条执行）：
#   schtasks /Delete /TN "MT5Quant\snapshot_specs"   /F
#   schtasks /Delete /TN "MT5Quant\refresh_bars"     /F
#   schtasks /Delete /TN "MT5Quant\refresh_ticks"    /F
#   schtasks /Delete /TN "MT5Quant\live_daily_report" /F
#   schtasks /Delete /TN "MT5Quant\watch_swap"       /F
#
# 任务一览（每日，本机时间）：
#   MT5Quant\snapshot_specs    00:05  品种参数快照（scripts\task_snapshot_specs.cmd）
#   MT5Quant\refresh_bars      00:10  K 线增量刷新（scripts\task_refresh_bars.cmd）
#   MT5Quant\refresh_ticks     00:15  tick 增量刷新（scripts\task_refresh_ticks.cmd；
#                                     仅当 data\ticks 已有品种目录时安装，否则跳过并提示）
#   MT5Quant\live_daily_report 00:20  实盘日报（scripts\task_live_daily_report.cmd；
#                                     T24：render_daily_report → data\reports\live\）
#
# 限时任务（T35：只在本周，10-31 之后由主会话删除）：
#   MT5Quant\watch_swap        UTC 2026-10-26..10-30 每天 20:30–23:30，每 60 秒
#                             读一次校准持仓（magic 20261025）的 swap 字段并落
#                             data\reports\costs\swap-watch-<UTC日期>.csv
#                             （scripts\task_watch_swap.cmd → watch_swap.py --once）。
#                             本机为台北时间（UTC+8、无夏令时）→ 本地 10-27..10-31
#                             每天 04:30 起重复 3 小时；schtasks /SC DAILY 无时段内
#                             重复 → /RI 1 + /DU 03:00，/SD//ED 限定只在本周。
#
# 实现说明：schtasks /TR 上限 261 字符，直接塞完整命令行必然超限——改为生成
# .cmd 包装器（cd 钉仓库根 + 输出落 data\logs\task_*.log），/TR 只填包装器路径。

$ErrorActionPreference = "Stop"

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

# 配置文件统一为 config.json（README 约定）；兼容旧名 gateway.json
$cfgArg  = ""
foreach ($name in @("config.json", "gateway.json")) {
    $cfgPath = Join-Path $repoRoot $name
    if (Test-Path $cfgPath) { $cfgArg = " --config `"$cfgPath`""; break }
}

New-DailyTask "snapshot_specs" "00:05" `
    (New-TaskWrapper "snapshot_specs" "snapshot_specs.py" $cfgArg)
New-DailyTask "refresh_bars" "00:10" `
    (New-TaskWrapper "refresh_bars" "refresh_all.py" ($cfgArg + " --data-dir `"$repoRoot\data`""))

$ticksDir = Join-Path $repoRoot "data\ticks"
if (Test-Path $ticksDir) {
    $tickSymbols = (Get-ChildItem $ticksDir -Directory | ForEach-Object { $_.Name }) -join ","
    if ($tickSymbols) {
        New-DailyTask "refresh_ticks" "00:15" `
            (New-TaskWrapper "refresh_ticks" "download_ticks.py" `
                ($cfgArg + " --mode refresh --symbols `"$tickSymbols`""))
    } else {
        Write-Host "data\ticks 下无品种目录，跳过 MT5Quant\refresh_ticks（建 tick 库后重跑本脚本安装）"
    }
} else {
    Write-Host "data\ticks 不存在，跳过 MT5Quant\refresh_ticks（建 tick 库后重跑本脚本安装）"
}

# T24：实盘日报（昨天的 UTC 日；render_daily_report 落 data\reports\live\）
New-DailyTask "live_daily_report" "00:20" `
    (New-TaskWrapper "live_daily_report" "live_daily_report.py" `
        ($cfgArg + " --date yesterday"))

# T35（H5）：本周 swap 计费时刻观测——UTC 10-26..10-30 每天 20:30–23:30 每 60 秒
# 一次（watch_swap.py --once，只读）。本机时区固定 UTC+8（台北时间，无夏令时），
# 用 UTC 时刻换算本机 /ST//SD//ED，避免手算错日；/SC DAILY 无时段内重复 →
# /RI 1 + /DU 03:00。10-31 之后本任务不再触发（/ED 已限制），但仍应手动删除。
$watchUtcStart = [DateTime]::SpecifyKind([DateTime]::Parse("2026-10-26 20:30"), [DateTimeKind]::Utc)
$watchUtcEnd   = [DateTime]::SpecifyKind([DateTime]::Parse("2026-10-30 23:30"), [DateTimeKind]::Utc)
if ((Get-Date).ToUniversalTime() -le $watchUtcEnd) {
    $localStart = $watchUtcStart.ToLocalTime()
    $localLast  = $watchUtcEnd.ToLocalTime()      # 最后一天的 23:30 UTC → 本机日期
    $watchST    = $localStart.ToString("HH:mm")
    $watchSD    = $localStart.ToString("d")       # /SD//ED 用系统短日期格式
    $watchED    = ([DateTime]::new($localLast.Year, $localLast.Month, $localLast.Day)).ToString("d")
    $watchWrapper = New-TaskWrapper "watch_swap" "watch_swap.py" ""
    schtasks /Create /F /TN "MT5Quant\watch_swap" /SC DAILY /ST $watchST `
        /RI 1 /DU 03:00 /SD $watchSD /ED $watchED /TR "`"$watchWrapper`""
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks 创建 MT5Quant\watch_swap 失败（退出码 $LASTEXITCODE）"
    }
    Write-Host "已创建 MT5Quant\watch_swap（本地 $watchSD $watchST 起每日重复 1 分钟×3 小时，至 $watchED；"
    Write-Host "  = UTC 10-26..10-30 20:30–23:30）。本周结束后请手动删除（见文件头注释）。"
} else {
    Write-Host "观测周（UTC 2026-10-26..10-30）已过，跳过 MT5Quant\watch_swap（T35 历史任务）"
}

Write-Host ""
Write-Host "完成。删除命令见本文件头部注释（五条 schtasks /Delete）。"
