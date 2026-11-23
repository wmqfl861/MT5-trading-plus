<#
.SYNOPSIS
    T209 终端看护（P0，运维工具，D111）：交易终端进程不在就启动它，别的什么都不做。

.DESCRIPTION
    由独立的计划任务每分钟运行一次（安装见 docs/ops/demo-runbook.md §6.5）。
    背景：网关在终端进程不在时不再调用 initialize（T209）——真实的 MetaTrader5
    包会在这时把终端拉起来，终端从此是 worker 的子进程，supervisor 拒绝这棵
    进程树（11-21）。终端改由本脚本启动，worker 的进程树里不会出现终端。

    做什么：
    - 按 -TerminalPath 的完整映像路径判断终端在不在。同名但路径不同的
      terminal64.exe 是另一个实例，不算这一个在运行。
    - 不在就启动它：Start-Process，不带参数，工作目录是它所在的目录——和在
      资源管理器里双击一样。启动之后不连接、不登录、不核对。
    - -DryRun：只报告判断结果，不启动。

    不做什么：
    - 不 import MetaTrader5、不调用终端接口、不下单、不读写终端数据目录
      （D82：终端专用）。
    - 不结束任何进程，不重启已在运行的终端，不碰计划任务和防火墙。

    判断不了就不动：有 terminal64.exe 进程、却读不到它的映像路径（权限等）时，
    既不当作在运行，也不启动——宁可这一分钟不启动，也不重复启动（11-21 的日志
    里有两次启动相隔 0.27 秒）。这时退出码 1，由值守发现。

    两个实例撞车：计划任务每分钟一次，任务本身应设"不启动新实例"（安装脚本
    负责）；本脚本自己再用一个命名互斥量挡一道，拿不到就安静退出。

    退出码：0 = 终端在运行、已启动、或 -DryRun；1 = 拒绝执行或启动失败。
    每次运行输出一行 KEEPER <动作> …，不含本机路径；状态变化（启动、拒绝）
    另写进 -LogPath。

    时刻一律取系统时钟的 UTC（PS5.1 没有 -AsUTC，用 [DateTime]::UtcNow）。

.PARAMETER TerminalPath
    terminal64.exe 的完整路径，必填。不探测、不猜、不取默认值：文件名必须是
    terminal64.exe，文件必须存在。

.PARAMETER DryRun
    只报告，不启动。

.PARAMETER LogPath
    日志文件；缺省不写文件，只输出到标准输出（任务包装器会收）。

.EXAMPLE
    先看判断结果（不启动）：
    powershell -ExecutionPolicy Bypass -File scripts/ops/terminal_keeper.ps1 -TerminalPath "<终端安装目录>\terminal64.exe" -DryRun
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TerminalPath,
    [switch]$DryRun,
    [string]$LogPath
)

$ErrorActionPreference = 'Stop'

function Write-Keeper {
    param([string]$Text, [switch]$StateChange)
    if ($StateChange -and $LogPath) {
        try {
            $dir = Split-Path -Parent $LogPath
            if ($dir -and -not (Test-Path -LiteralPath $dir)) {
                New-Item -ItemType Directory -Path $dir | Out-Null
            }
            Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value ("{0}`t{1}" -f [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffZ'), $Text)
        } catch {
            Write-Warning ("log write failed: " + $_.Exception.GetType().Name)
        }
    }
    Write-Output ("KEEPER " + $Text)
}

function Refuse {
    param([string]$Reason, [string]$Detail)
    $text = "refused reason=" + $Reason
    if ($Detail) { $text += " " + $Detail }
    Write-Keeper $text -StateChange
    exit 1
}

function Same-Image {
    param([string]$Seen, [string]$Wanted)
    if (-not $Seen) { return $false }
    try { $full = [System.IO.Path]::GetFullPath($Seen).TrimEnd('\') } catch { return $false }
    return [string]::Equals($full, $Wanted.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)
}

# ---- 1. 参数：只接受一个存在的 terminal64.exe 的完整路径 --------------------
if (-not [System.IO.Path]::IsPathRooted($TerminalPath)) { Refuse "not_absolute" }
if ([System.IO.Path]::GetFileName($TerminalPath) -ine 'terminal64.exe') { Refuse "not_terminal64" }
try { $target = [System.IO.Path]::GetFullPath($TerminalPath).TrimEnd('\') } catch { Refuse "bad_path" }
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { Refuse "file_missing" }

# ---- 2. 一次只让一个看护实例判断和启动 --------------------------------------
$mutex = New-Object System.Threading.Mutex($false, 'Local\MT5Quant_terminal_keeper')
$held = $false
try {
    try { $held = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) {
        Write-Keeper "busy another keeper instance holds the lock"
        exit 0
    }

    # ---- 3. 这一个终端在不在 -------------------------------------------------
    $procs = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='terminal64.exe'")
    $unknown = @($procs | Where-Object { -not $_.ExecutablePath })
    if ($unknown.Count -gt 0) {
        Refuse "cannot_tell" ("terminal64.exe process without a readable image path, pids=" + (($unknown | ForEach-Object { $_.ProcessId }) -join ','))
    }
    $running = @($procs | Where-Object { Same-Image $_.ExecutablePath $target })
    $others = @($procs | Where-Object { -not (Same-Image $_.ExecutablePath $target) })
    if ($running.Count -gt 0) {
        Write-Keeper ("running pids=" + (($running | ForEach-Object { $_.ProcessId }) -join ',') + " other_instances=" + $others.Count)
        exit 0
    }

    # ---- 4. 不在：启动（-DryRun 只报告） --------------------------------------
    if ($DryRun) {
        Write-Keeper ("would-start other_instances=" + $others.Count)
        exit 0
    }
    try {
        $proc = Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target) -PassThru
    } catch {
        Write-Keeper ("start-failed error=" + $_.Exception.GetType().Name) -StateChange
        exit 1
    }
    Write-Keeper ("started pid=" + $proc.Id + " other_instances=" + $others.Count) -StateChange
    exit 0
} finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
