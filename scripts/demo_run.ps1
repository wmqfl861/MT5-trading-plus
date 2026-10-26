<#
.SYNOPSIS
    阶段 F 模拟盘演练一键启动（T45，蓝图 §7）。

.DESCRIPTION
    对 data/demo-configs/ 下每个 .toml 配置启动一个 --paper 运行器（真终端
    行情 + PaperBroker 影子券商，绝不连真实下单通道），另启动一个风控
    --loop（--paper-state 聚合读全部 state-<magic>.json）和一个对账 --loop
    （同 --paper-state）。日志与 PID 清单落 data/demo/。

    启动前检查：点差模型缓存存在、研究库缓存副本存在、合约快照存在、
    会话配置存在、磁盘剩余空间足够、配置目录非空且 magic 唯一。
    任一不满足 → 不启动任何进程（exit 1）。

    启动顺序：先全部运行器（写出 expected-<magic>.json / 心跳 / paper
    状态），再风控（fail-closed：风控先于运行器启动会把心跳缺失写成
    halt.json），最后对账。

    演练验收（蓝图 §7）用 scripts/demo_check.py 逐日检查：
        .venv/Scripts/python.exe scripts/demo_check.py --day 2026-11-10
        .venv/Scripts/python.exe scripts/demo_check.py --summary

.STOP
    停止方式一（killSwitch，先停开仓再杀进程；演练项"一键停机"即此）：
        New-Item -ItemType File -Path data/live/killSwitch
        # 风控进程下一轮写 data/live/halt.json（一键停机，不自动平仓），
        # 各执行器开仓腿随即被拒；确认后按方式二结束进程。
        # 恢复演练：删除 data/live/killSwitch 与 data/live/halt.json 后重跑本脚本。

    停止方式二（taskkill，按 PID 清单直接结束）：
        Get-Content data/demo/pids.txt | ForEach-Object {
            taskkill /PID ($_ -split "`t")[0] /F }
        # （本脚本用 Start-Process 直启 python，PID 即 python 进程，无需 /T。）

.PARAMETER ConfigDir
    策略配置目录（每个 .toml 一个策略；缺省 data/demo-configs）。
.PARAMETER DataDir
    数据根（live 目录 = <DataDir>/live；缺省 data）。
.PARAMETER LogDir
    日志目录（缺省 data/demo；每次启动一个带时间戳的新日志文件，
    重启即新文件——demo_check 据此识别重启并要求上一份日志留有说明）。
.PARAMETER RiskConfig
    风控配置 TOML（缺省 config/risk.toml）。
.PARAMETER SpreadModel
    点差模型缓存（缺省 data/costs/spread_model.json）。
.PARAMETER ResearchCache
    研究库缓存副本（缺省 data_research/costs/spread_model.json）。
.PARAMETER PythonExe
    Python 解释器（缺省 .venv/Scripts/python.exe，不存在时退回 python）。
.PARAMETER MinFreeGB
    磁盘剩余空间下限 GB（缺省 5）。
#>
[CmdletBinding()]
param(
    [string]$ConfigDir = "data/demo-configs",
    [string]$DataDir = "data",
    [string]$LogDir = "data/demo",
    [string]$RiskConfig = "config/risk.toml",
    [string]$SpreadModel = "data/costs/spread_model.json",
    [string]$ResearchCache = "data_research/costs/spread_model.json",
    [string]$PythonExe = ".venv/Scripts/python.exe",
    [double]$MinFreeGB = 5
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

function Resolve-RepoPath([string]$p) {
    if ([System.IO.Path]::IsPathRooted($p)) { return $p }
    return (Join-Path $RepoRoot $p)
}

$failures = @()
function Assert-Ok([bool]$cond, [string]$message) {
    if ($cond) {
        Write-Host ("  [OK] " + $message)
    } else {
        Write-Host ("  [X]  " + $message) -ForegroundColor Red
        $script:failures += $message
    }
}

Write-Host "== 模拟盘演练启动前检查（阶段 F，蓝图 §7） =="

# 1) Python 解释器
$PythonExeAbs = Resolve-RepoPath $PythonExe
if (-not (Test-Path $PythonExeAbs)) {
    Write-Host "  [!]  $PythonExe 不存在，退回 PATH 里的 python" -ForegroundColor Yellow
    $PythonExeAbs = "python"
}
Assert-Ok ($PythonExeAbs -eq "python" -or (Test-Path $PythonExeAbs)) "Python 解释器可用：$PythonExeAbs"

# 2) 点差模型缓存（点差闸门 + 成本口径；live_runner --paper 缺文件只告警，
#    但全链路演练要求重建后的模型在场）
$SpreadModelAbs = Resolve-RepoPath $SpreadModel
Assert-Ok (Test-Path $SpreadModelAbs) "点差模型缓存存在：$SpreadModel"

# 3) 研究库缓存副本（T27 研究库隔离；演练期间的成本口径应与研究库一致）
$ResearchCacheAbs = Resolve-RepoPath $ResearchCache
Assert-Ok (Test-Path $ResearchCacheAbs) "研究库缓存存在：$ResearchCache"

# 4) 合约快照（PaperBroker 盈亏换算与定手数必需）
$SpecsAbs = Resolve-RepoPath (Join-Path $DataDir "specs/latest.json")
Assert-Ok (Test-Path $SpecsAbs) "合约快照存在：$SpecsAbs"

# 5) 会话配置（换日禁入窗唯一来源）
$SessionsAbs = Resolve-RepoPath "config/sessions.toml"
Assert-Ok (Test-Path $SessionsAbs) "会话配置存在：$SessionsAbs"

# 6) 风控配置
$RiskConfigAbs = Resolve-RepoPath $RiskConfig
Assert-Ok (Test-Path $RiskConfigAbs) "风控配置存在：$RiskConfig"

# 7) 磁盘剩余空间
$DataDirAbs = Resolve-RepoPath $DataDir
$drive = (Get-Item $DataDirAbs -ErrorAction SilentlyContinue).PSDrive
if ($null -eq $drive) { $drive = Get-PSDrive -Name ((Split-Path $DataDirAbs -Qualifier).TrimEnd(":")) }
$freeGB = [math]::Round($drive.Free / 1GB, 2)
Assert-Ok ($drive.Free / 1GB -ge $MinFreeGB) "磁盘剩余 ${freeGB} GB ≥ ${MinFreeGB} GB（盘 $($drive.Name):）"

# 8) 配置目录与 magic 唯一性
$ConfigDirAbs = Resolve-RepoPath $ConfigDir
$configs = @()
if (Test-Path $ConfigDirAbs) {
    $configs = @(Get-ChildItem (Join-Path $ConfigDirAbs "*.toml") | Sort-Object Name)
}
Assert-Ok ($configs.Count -ge 1) "配置目录 $ConfigDir 有 $($configs.Count) 个 .toml"

$magics = @{}
foreach ($c in $configs) {
    $m = (Select-String -Path $c.FullName -Pattern '^\s*magic\s*=\s*(\d+)').Matches
    if ($m.Count -eq 0) {
        Assert-Ok $false "$($c.Name) 读不到 magic"
        continue
    }
    $magic = [int]$m[0].Groups[1].Value
    if ($magics.ContainsKey($magic)) {
        Assert-Ok $false "magic $magic 重复（$($magics[$magic]) 与 $($c.Name)）"
    } else {
        $magics[$magic] = $c.Name
        Write-Host "  [OK] $($c.Name) magic=$magic"
    }
}

if ($failures.Count -gt 0) {
    Write-Host ""
    Write-Host ("启动前检查未通过（" + $failures.Count + " 项），未启动任何进程。") -ForegroundColor Red
    exit 1
}

# ---- 启动 ----
$LogDirAbs = Resolve-RepoPath $LogDir
New-Item -ItemType Directory -Force -Path $LogDirAbs | Out-Null
$LiveDirAbs = Join-Path $DataDirAbs "live"
$PaperStateAbs = Join-Path $LiveDirAbs "paper"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$manifest = @()
$pidLines = @()

function Start-DemoProcess([string]$kind, [string]$name, [string[]]$argList) {
    $log = Join-Path $LogDirAbs ("{0}-{1}-{2}.log" -f $kind, $name, $stamp)
    $errLog = "$log.err"
    Write-Host ("启动 {0} {1} -> {2}" -f $kind, $name, (Split-Path $log -Leaf))
    $p = Start-Process -FilePath $PythonExeAbs -ArgumentList $argList `
        -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError $errLog
    $script:manifest += [ordered]@{
        kind = $kind; name = $name; pid = $p.Id
        log = $log; err_log = $errLog
        started_utc = (Get-Date).ToUniversalTime().ToString("o")
        command = ($PythonExeAbs + " " + ($argList -join " "))
    }
    $script:pidLines += ("{0}`t{1}`t{2}`t{3}" -f $p.Id, $kind, $name, $log)
    return $p.Id
}

Write-Host ""
Write-Host "== 启动模拟盘进程（--paper 影子券商，绝不连真实下单通道） =="

# 1) 每配置一个 --paper 运行器（先启动：写出意图/心跳/paper 状态）
$runnerPids = @()
foreach ($c in $configs) {
    $magic = [int]((Select-String -Path $c.FullName -Pattern '^\s*magic\s*=\s*(\d+)').Matches[0].Groups[1].Value)
    $runnerPids += Start-DemoProcess "runner" "$magic" @(
        "scripts/live_runner.py",
        "--config", $c.FullName,
        "--loop", "--paper",
        "--risk-config", $RiskConfigAbs,
        "--spread-model", $SpreadModelAbs
    )
}

# 运行器先落盘：意图 expected-<magic>.json、心跳、paper/state-<magic>.json
Write-Host "等待运行器落盘（8 秒）……"
Start-Sleep -Seconds 8

# 2) 一个风控 --loop（--paper-state 聚合读全部 state-<magic>.json，不连终端）
Start-DemoProcess "risk" "account" @(
    "scripts/risk_process.py",
    "--config", $RiskConfigAbs,
    "--data-dir", $DataDirAbs,
    "--loop", "--interval", "10",
    "--paper-state", $PaperStateAbs
) | Out-Null
Start-Sleep -Seconds 2

# 3) 一个对账 --loop（--paper-state 同上；意图 vs 实际，按策略暂停）
Start-DemoProcess "reconcile" "all" @(
    "scripts/live_reconcile.py",
    "--data-dir", $DataDirAbs,
    "--loop", "--interval", "30",
    "--paper-state", $PaperStateAbs
) | Out-Null

# ---- 清单 ----
$pidsFile = Join-Path $LogDirAbs "pids.txt"
$pidLines | Set-Content -Encoding UTF8 $pidsFile
$manifestFile = Join-Path $LogDirAbs ("demo-run-{0}.json" -f $stamp)
@{ started_utc = (Get-Date).ToUniversalTime().ToString("o")
   repo_root = $RepoRoot
   config_dir = $ConfigDirAbs
   paper_state_dir = $PaperStateAbs
   processes = $manifest } | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $manifestFile

Write-Host ""
Write-Host ("已启动 {0} 个进程（PID 清单：{1}；清单：{2}）" -f $manifest.Count, $pidsFile, $manifestFile)
Write-Host "停止（killSwitch）：New-Item -ItemType File data/live/killSwitch   （恢复：删除 killSwitch 与 data/live/halt.json）"
Write-Host "停止（taskkill）  ：Get-Content $pidsFile | ForEach-Object { taskkill /PID (`$_ -split `"`t`")[0] /F }"
Write-Host "逐日验收        ：$PythonExeAbs scripts/demo_check.py --day YYYY-MM-DD   或 --summary"
