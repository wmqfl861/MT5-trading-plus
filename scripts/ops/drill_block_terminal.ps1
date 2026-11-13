<#
.SYNOPSIS
    T205 断线演练脚本（P0，运维工具，D91）：对交易终端程序做**有界**的出站阻断，
    用于演练断线-重连路径（周六 PaperSoak 拦 120 秒；窗口内"重连"演练拦 60 秒）。

.DESCRIPTION
    范围（只拦终端、绝不拦整机）：
    - 仅对 terminal64.exe 这一个程序建出站阻断规则
      （New-NetFirewallRule -Direction Outbound -Program <终端> -Action Block，
      规则名固定 MT5Quant-drill-block-terminal）；
    - 不按地址/端口/协议/接口拦，不动网卡、不改防火墙Profile——整机联网不受
      影响，其它进程（含三进程本体与 Python）出站照常，被拦的只有终端到
      交易服务器的连接。

    安全设计（三重回收，任何一层失效规则都会消失）：
    1. -Seconds 上限 120：超过直接拒绝执行（要的是有界断线，不是长时间失联）；
    2. 开始等待前先注册一个 10 分钟后一次性触发的兜底计划任务
       （MT5Quant-drill-block-fallback，动作 = Remove-NetFirewallRule 同名规则并
       自删任务，以 NT AUTHORITY\SYSTEM 无头运行——操作员注销也能触发）：
       脚本被杀、窗口被关、断电时，规则最迟 10 分钟自动消失；
    3. 等待结束（或任何一步抛错）都在 finally 里删除规则、注销兜底任务；
       删规则失败时按"兜底任务是否建成"如实分报，并在运行时输出人工清理
       命令原文（不藏在注释帮助里）。

    -TerminalPath 的保守实现（规格允许"探测常见路径"或"必填"二选一，本脚本
    取保守结合）：缺省时探测常见安装路径（Program Files、Program Files (x86)、
    LOCALAPPDATA 下 Program 里的 MetaTrader 5\terminal64.exe）；一个都没探测到、
    或探测到多个（无法确定哪个是演练终端）→ 一律报错退出，要求用
    -TerminalPath 显式指定。绝不猜测、绝不默认取第一个。

    全过程写日志（时间戳+步骤，时刻一律系统时钟 UTC（G19-1），PS5.1 无
    -AsUTC 故用 [DateTime]::UtcNow）：默认 <仓库根>\data\ops-evidence\drill-block\，
    可用 -LogDir 另指。每次运行一个 drill-block_时间戳.log。

    红线：本脚本会创建真实防火墙规则与计划任务——只由周六演练主会话/运维在
    演练窗内手动执行；仓库测试对它只做静态源码断言，绝不执行本脚本。

.PARAMETER Seconds
    阻断秒数，1..120；超过 120 秒直接拒绝执行（不截断、不放行）。

.PARAMETER TerminalPath
    terminal64.exe 完整路径；缺省探测常见安装路径，探测不到或有歧义时必填
    报错退出。目标文件名必须是 terminal64.exe（只允许拦交易终端程序）。

.PARAMETER LogDir
    日志目录；缺省 <仓库根>\data\ops-evidence\drill-block。

.EXAMPLE
    管理员 PowerShell（周六演练主会话执行；example 用占位路径）：
    powershell -ExecutionPolicy Bypass -File scripts\ops\drill_block_terminal.ps1 -Seconds 120 -TerminalPath "<终端安装目录>\terminal64.exe"

    异常中断后的人工遗留清理（先核对再执行）：
    Remove-NetFirewallRule -Name "MT5Quant-drill-block-terminal"
    Unregister-ScheduledTask -TaskName "MT5Quant-drill-block-fallback" -Confirm:$false
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [int]$Seconds,

    [string]$TerminalPath = "",

    [string]$LogDir = ""
)

$ErrorActionPreference = "Stop"

# 固定名称（规格：规则名固定）：规则与兜底任务都只用这两个常量，演练结束后按名回收。
$RuleName         = "MT5Quant-drill-block-terminal"
$FallbackTaskName = "MT5Quant-drill-block-fallback"

# 仓库根（scripts\ops 上两级）只用于默认日志目录定位。
$opsDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptsDir = Split-Path -Parent $opsDir
$repoRoot   = Split-Path -Parent $scriptsDir

if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path $repoRoot "data\ops-evidence\drill-block"
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$script:LogFilePath = Join-Path $LogDir ("drill-block_" + [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss") + ".log")

function Write-DrillLog {
    # 时间戳+步骤各一行（时刻一律系统时钟 UTC，G19-1；PS5.1 无 -AsUTC 故用
    # [DateTime]::UtcNow）；文件写失败降级为仅控制台输出——清理路径绝不因日志抛错。
    param([Parameter(Mandatory = $true)][string]$Step)
    $line = "{0} {1}" -f [DateTime]::UtcNow.ToString("yyyy-MM-dd HH:mm:ss"), $Step
    Write-Host $line
    try {
        Add-Content -LiteralPath $script:LogFilePath -Value $line -Encoding UTF8
    } catch {
        Write-Host ("日志文件写入失败，仅控制台输出：" + $_.Exception.Message)
    }
}

Write-DrillLog ("START pid=" + $PID + " Seconds=" + $Seconds + " TerminalPath=[" + $TerminalPath + "] LogDir=" + $LogDir)

# ---- 参数校验：120 秒上限（超过拒绝执行，绝不截断成 120 继续跑） ----
if ($Seconds -gt 120) {
    Write-DrillLog ("REJECT -Seconds=" + $Seconds + " 超过上限 120，拒绝执行")
    throw "拒绝执行：-Seconds 必须在 1..120（断线演练最长拦 120 秒）"
}
if ($Seconds -lt 1) {
    Write-DrillLog ("REJECT -Seconds=" + $Seconds + " 不是正整数，拒绝执行")
    throw "拒绝执行：-Seconds 必须是正整数秒（1..120）"
}

# ---- 权限校验：防火墙规则与计划任务都需要管理员令牌 ----
$winIdentity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$winPrincipal = New-Object Security.Principal.WindowsPrincipal($winIdentity)
if (-not $winPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-DrillLog "REJECT 当前 PowerShell 不是管理员（建规则/计划任务都会失败）"
    throw "拒绝执行：请在管理员 PowerShell 中运行本脚本"
}

# ---- 终端定位：保守实现（探测常见路径；探不到或有歧义 → 必填报错） ----
if ([string]::IsNullOrWhiteSpace($TerminalPath)) {
    Write-DrillLog "STEP0 -TerminalPath 未提供，探测常见安装路径中的 terminal64.exe"
    $probeCandidates = @()
    foreach ($baseDir in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($baseDir) {
            $probeCandidates += (Join-Path $baseDir "MetaTrader 5\terminal64.exe")
        }
    }
    if ($env:LOCALAPPDATA) {
        $probeCandidates += (Join-Path $env:LOCALAPPDATA "Program\MetaTrader 5\terminal64.exe")
    }
    Write-DrillLog ("STEP0 探测清单：" + ($probeCandidates -join " ; "))
    $probeFound = @($probeCandidates | Where-Object { Test-Path -LiteralPath $_ })
    if ($probeFound.Count -eq 0) {
        Write-DrillLog "STEP0 未探测到任何 terminal64.exe——必填报错，不猜测默认"
        throw "未在常见安装路径探测到 terminal64.exe；请用 -TerminalPath 显式指定终端完整路径后重跑"
    }
    if ($probeFound.Count -gt 1) {
        Write-DrillLog ("STEP0 探测到多个候选——必填报错，不默认取第一个：" + ($probeFound -join " ; "))
        throw "探测到多个 terminal64.exe，无法确定演练目标；请用 -TerminalPath 显式指定其一"
    }
    $TerminalPath = $probeFound[0]
}
if (-not (Test-Path -LiteralPath $TerminalPath -PathType Leaf)) {
    Write-DrillLog ("REJECT 终端路径不存在：" + $TerminalPath)
    throw ("拒绝执行：终端路径不存在：" + $TerminalPath)
}
if ((Split-Path -Leaf $TerminalPath) -ine "terminal64.exe") {
    Write-DrillLog ("REJECT 目标不是 terminal64.exe（只允许拦交易终端程序）：" + $TerminalPath)
    throw ("拒绝执行：只允许对 terminal64.exe 建规则，收到：" + $TerminalPath)
}
$terminal = (Resolve-Path -LiteralPath $TerminalPath).Path
Write-DrillLog ("STEP0 演练目标终端：" + $terminal)

# ---- 残留预检：同名规则已在（终端可能已被拦）→ 拒绝，防止叠加或误测 ----
if (Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue) {
    Write-DrillLog ("REJECT 已存在同名规则 " + $RuleName + "——终端可能已被拦，请先人工核查清理")
    throw ("已存在同名规则 " + $RuleName + "；确认无演练进行后先执行 Remove-NetFirewallRule -Name " + $RuleName + " 清理再重跑")
}
if (Get-ScheduledTask -TaskName $FallbackTaskName -ErrorAction SilentlyContinue) {
    Write-DrillLog ("STEP0 发现残留兜底任务 " + $FallbackTaskName + "，稍后注册时直接覆盖（-Force）")
}

$ruleCreated = $false
$taskCreated = $false
$hadError    = $false

try {
    # STEP1（规格①）：只对终端程序建出站阻断规则——不按地址/端口/接口，不动整机。
    Write-DrillLog ("STEP1 建出站阻断规则 " + $RuleName + "（只拦 " + $terminal + "）")
    New-NetFirewallRule -Name $RuleName -DisplayName $RuleName -Direction Outbound -Program $terminal -Action Block | Out-Null
    $ruleCreated = $true
    Write-DrillLog "STEP1 规则已生效：出站 Block 仅作用于该程序，整机联网不受影响"

    # STEP2（规格②）：先注册 10 分钟兜底一次性任务，再开始等待——脚本被杀也能自动回收。
    # Principal 用 NT AUTHORITY\SYSTEM + ServiceAccount（审核 M2）：无头免密触发，
    # 不依赖操作员交互登录——脚本被杀后即使操作员注销，+10 分钟照样删规则
    #（SYSTEM 删防火墙规则无障碍；Low-1：触发时自删任务，不留空任务残留）。
    Write-DrillLog ("STEP2 注册兜底一次性计划任务 " + $FallbackTaskName + "（10 分钟后自动删除同名规则并自删任务，SYSTEM 无头触发，不依赖操作员登录）")
    $fallbackAction   = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ("-NoProfile -ExecutionPolicy Bypass -Command `"Remove-NetFirewallRule -Name '" + $RuleName + "'; Unregister-ScheduledTask -TaskName '" + $FallbackTaskName + "' -Confirm:`$false`"")
    $fallbackTrigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(10)
    $fallbackSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    $fallbackPrincipal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $FallbackTaskName -Action $fallbackAction -Trigger $fallbackTrigger -Settings $fallbackSettings -Principal $fallbackPrincipal -Force | Out-Null
    $taskCreated = $true
    Write-DrillLog "STEP2 兜底任务已注册（若本脚本异常中断，规则最迟 10 分钟自动消失）"

    # STEP3（规格③）：有界等待；到点后的删除在 finally 里做（见下）。
    Write-DrillLog ("STEP3 阻断开始：等待 " + $Seconds + " 秒（上限 120）")
    Start-Sleep -Seconds $Seconds
    Write-DrillLog "STEP3 等待结束"
} catch {
    $hadError = $true
    Write-DrillLog ("ERROR 步骤失败：" + $_.Exception.Message)
} finally {
    # STEP4（规格③④）：无论成功、抛错还是中途停止，都在 finally 里删规则、删兜底任务。
    # 人工清理命令原文（审核 M1）：失败分支必须在运行时输出，不藏在注释帮助里。
    $cleanupFailed = $false
    $manualRuleCmd = "Remove-NetFirewallRule -Name '" + $RuleName + "'"
    $manualTaskCmd = "Unregister-ScheduledTask -TaskName '" + $FallbackTaskName + "' " + '-Confirm:$false'
    try {
        if (Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue) {
            Remove-NetFirewallRule -Name $RuleName -ErrorAction Stop
            Write-DrillLog ("FINALLY 已删除阻断规则 " + $RuleName)
        } else {
            Write-DrillLog ("FINALLY 规则 " + $RuleName + " 不存在，无需删除")
        }
    } catch {
        # 审核 M1：陈述必须按 $taskCreated 分支——兜底未建成时不得谎称"会自动删除"。
        $cleanupFailed = $true
        if ($taskCreated) {
            Write-DrillLog ("FINALLY-ERROR 删除规则失败：" + $_.Exception.Message + "——兜底任务仍在，将在 10 分钟内自动删除它")
        } else {
            Write-DrillLog ("FINALLY-ERROR 删除规则失败：" + $_.Exception.Message + "——兜底任务未建成，无任何自动回收，请立即人工执行")
        }
        Write-DrillLog ("MANUAL-CLEANUP 人工清理命令原文：" + $manualRuleCmd + " ; " + $manualTaskCmd)
    }
    if ($taskCreated) {
        try {
            Unregister-ScheduledTask -TaskName $FallbackTaskName -Confirm:$false -ErrorAction Stop
            Write-DrillLog ("FINALLY 已删除兜底任务 " + $FallbackTaskName)
        } catch {
            $cleanupFailed = $true
            Write-DrillLog ("FINALLY-ERROR 删除兜底任务失败：" + $_.Exception.Message + "（该任务 10 分钟内触发时会删同名规则并自删，无其它副作用；若仍残留请人工执行：" + $manualTaskCmd + "）")
        }
    }
    if ($cleanupFailed) {
        $hadError = $true
    }
    Write-DrillLog "FINALLY 清理结束"
}

# ---- 收尾：清理失败或步骤出错都必须以非零退出码暴露，不静默 ----
if ($hadError) {
    Write-DrillLog "END exitCode=1（有步骤失败或清理失败，见上文 ERROR/FINALLY-ERROR 行）"
    exit 1
}
Write-DrillLog "END exitCode=0"
exit 0
