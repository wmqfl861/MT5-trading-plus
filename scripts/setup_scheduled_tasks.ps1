# MT5 量化系统每日计划任务安装脚本。
# 本脚本只负责"创建"，由主会话/运维手动执行；不随任何测试自动运行。
#
# 安装（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\setup_scheduled_tasks.ps1
#
# 删除（卸载全部任务；逐条执行）：
#   schtasks /Delete /TN "MT5Quant\snapshot_specs" /F
#   schtasks /Delete /TN "MT5Quant\refresh_bars"   /F
#   schtasks /Delete /TN "MT5Quant\refresh_ticks"  /F
#
# 任务一览（每日，本机时间）：
#   MT5Quant\snapshot_specs  00:05  品种参数快照（scripts\task_snapshot_specs.cmd）
#   MT5Quant\refresh_bars    00:10  K 线增量刷新（scripts\task_refresh_bars.cmd）
#   MT5Quant\refresh_ticks   00:15  tick 增量刷新（scripts\task_refresh_ticks.cmd；
#                                   仅当 data\ticks 已有品种目录时安装，否则跳过并提示）
#
# 实现说明：schtasks /TR 上限 261 字符，直接塞完整命令行必然超限——改为生成
# 三个 .cmd 包装器（cd 钉仓库根 + 输出落 data\logs\task_*.log），/TR 只填包装器路径。

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

$cfgPath = Join-Path $repoRoot "gateway.json"
$cfgArg  = ""
if (Test-Path $cfgPath) { $cfgArg = " --config `"$cfgPath`"" }

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

Write-Host ""
Write-Host "完成。删除命令见本文件头部注释（三条 schtasks /Delete）。"
