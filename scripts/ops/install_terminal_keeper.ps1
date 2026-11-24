<#
.SYNOPSIS
    终端看护任务（scripts\ops\terminal_keeper.ps1 的计划任务）：打印将要注册的内容、注册、注销、查看状态。

.DESCRIPTION
    不带开关时，脚本只做检查、只打印：动作、触发器、主体、设置，外加一行给部署记录抄用的
    参数。这条路径不碰任务计划程序——不查询、不注册、不注销，只构造任务对象
    （New-ScheduledTask*，纯内存）。真的动任务要明说：-Install 注册，-Uninstall 注销。
    -Status 是只读的状态查询。三个开关最多给一个。

    看护脚本由一个独立的计划任务每分钟运行一次，经通用的隐藏启动器
    scripts\ops\hidden_task.vbs 调用——和 supervisor 任务同一个启动器：wscript 没有控制台，
    动作是控制台程序的任务每分钟会在交互桌面上弹一次窗口（启动器自己的说明里写了原因和
    它的退出码）。

    安装只做一件事：注册任务。不往部署树里写任何文件（supervisor 要求部署树是干净的），
    也不建日志目录——日志由看护脚本在第一次有状态变化时写到
    <数据目录>\ops-evidence\terminal_keeper\terminal_keeper.log。

    所有的检查都在注册之前。缺哪一项、哪一项不对，都拒绝并一次列全：不建任务，不建任何东西。

    任务的设置：
    - 每分钟触发一次，第一次在一分钟之后；
    - "不启动新实例"（IgnoreNew）：看护一次只要一两秒，上一次没跑完就不再起一个；
    - 单次运行的时间上限 2 分钟；
    - 主体是当前用户，登录类型 Interactive、运行级别 Highest——和 supervisor 任务一样：终端
      要在交互桌面上启动，看护要读得到同一个用户的进程的映像路径。

    装之前，在终端运行着的时候用看护脚本自己的 -DryRun 核对一次，必须输出 running
    （docs/ops/demo-runbook.md §6.5）。看护脚本的 -DryRun 不启动终端；本脚本不带开关时
    不碰任务计划程序。

    维护时要关终端，先停用这个任务，否则一分钟内它会把终端拉起来。

.PARAMETER TerminalPath
    terminal64.exe 的完整路径：打印和 -Install 必填。文件名必须是 terminal64.exe，文件必须
    存在。原样交给看护脚本，不解析、不改拼写。

.PARAMETER DeployDir
    部署树：打印和 -Install 必填。里面要有 scripts\ops\terminal_keeper.ps1 和
    scripts\ops\hidden_task.vbs。

.PARAMETER DataDir
    数据目录：打印和 -Install 必填。看护的日志放在它下面的 ops-evidence\terminal_keeper\。
    这里只决定日志的位置，安装时不建它。-Uninstall 时可给可不给，只为说明日志留在哪里。

.PARAMETER TaskPath
    任务路径，缺省 \MT5Quant\。

.PARAMETER TaskName
    任务名，缺省 terminal_keeper。

.PARAMETER Install
    真的注册任务。不给：只打印将要注册的内容，不碰任务计划程序。同名任务已经在了就拒绝，
    不覆盖——换参数先 -Uninstall。

.PARAMETER Uninstall
    注销任务。只注销，不删任何文件。任务没有安装时什么都不做，退出码 4。

.PARAMETER Status
    只读：查询任务现在的状态、上次运行的时刻和上次结果。不注册、不注销、不改。

.EXAMPLE
    只打印将要注册的内容（不碰任务计划程序）：
    powershell -ExecutionPolicy Bypass -File scripts\ops\install_terminal_keeper.ps1 `
      -TerminalPath "<终端安装目录>\terminal64.exe" -DeployDir "<部署树>" -DataDir "<数据目录>"
    同一条命令加 -Install 才注册；-Status 查看状态；-Uninstall 注销。

.OUTPUTS
    KEEPER_TASK_PARAMETERS {…}：不带开关和 -Install 都打印，给部署记录抄用，mode 一项写明是
    plan 还是 install。里面是本机的绝对路径和账户：抄进入库的部署记录之前要脱敏
    （docs/ops/demo-runbook.md §6.5 写了要记什么）。
    KEEPER_TASK_STATUS {…}：-Status 打印，时刻是 UTC。给了 -DataDir 时 -Status 还打印一行
    KEEPER_STATE {…}：看护脚本状态文件里最近的一条（ts、outcome、reason、age_s），只读——装完当场
    就能看到看护有没有跑成。
    退出码：0 做完了（打印、注册、注销、查到了）；1 拒绝或出错，原因在错误输出里；
    4 -Status 或 -Uninstall 时任务没有安装。
#>

[CmdletBinding()]
param(
    [string]$TerminalPath = "",
    [string]$DeployDir = "",
    [string]$DataDir = "",
    [string]$TaskPath = "\MT5Quant\",
    [string]$TaskName = "terminal_keeper",
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
Import-Module ScheduledTasks

function Strip-TrailingSeparator([string]$path) {
    # 去掉路径末尾的分隔符（和 setup_scheduled_tasks.ps1 同一个做法）。盘符根目录（C:\）原样返回；UNC 共享
    # 根目录返回的是去掉末尾分隔符的 \\主机\共享。两种结果都由下面的检查按根目录拒绝。
    $root = [System.IO.Path]::GetPathRoot($path)
    $trimmed = $path.TrimEnd([System.IO.Path]::DirectorySeparatorChar,
                             [System.IO.Path]::AltDirectorySeparatorChar)
    if ($trimmed.Length -lt $root.Length) { return $root }
    return $trimmed
}

# 两个系统代码页做成可以替身的命令：调用方事先定义同名函数，脚本就用它（和
# setup_scheduled_tasks.ps1 同一个方式）；没有定义才用这里的真实实现。
if (-not (Get-Command Get-SystemCodePages -ErrorAction SilentlyContinue)) {
    function Get-SystemCodePages {
        if (-not ("MT5Quant.KeeperInstallerCodePages" -as [type])) {
            Add-Type -Namespace MT5Quant -Name KeeperInstallerCodePages -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint GetACP();
[DllImport("kernel32.dll")] public static extern uint GetOEMCP();
'@
        }
        return [pscustomobject]@{
            ACP   = [int][MT5Quant.KeeperInstallerCodePages]::GetACP()
            OEMCP = [int][MT5Quant.KeeperInstallerCodePages]::GetOEMCP()
        }
    }
}

function Get-Sha256([string]$path) {
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower()
}

function Format-Utc($moment) {
    if ($null -eq $moment) { return $null }
    return ([DateTime]$moment).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

function Get-KeeperLogLocation {
    if ([string]::IsNullOrWhiteSpace($DataDir) -or -not [System.IO.Path]::IsPathRooted($DataDir)) {
        return "<数据目录>\ops-evidence\terminal_keeper\terminal_keeper.log"
    }
    return Join-Path (Strip-TrailingSeparator ([System.IO.Path]::GetFullPath($DataDir))) "ops-evidence\terminal_keeper\terminal_keeper.log"
}

function Get-KeeperStateLine {
    # 看护脚本的状态文件（terminal_keeper.ps1 每次做过判断的运行写一次：-LogPath 所在目录下的
    # state.json）。只读，而且只在 -Status 里打印：给了 -DataDir 才看。outcome、reason 只有长得
    # 像标识符的才照原样打印，其余写 other——这个文件不是本脚本写的。
    if ([string]::IsNullOrWhiteSpace($DataDir) -or -not [System.IO.Path]::IsPathRooted($DataDir)) { return $null }
    $stateFile = Join-Path (Strip-TrailingSeparator ([System.IO.Path]::GetFullPath($DataDir))) "ops-evidence\terminal_keeper\state.json"
    if (-not (Test-Path -LiteralPath $stateFile -PathType Leaf)) {
        return "KEEPER_STATE " + (@{ state_file = "missing" } | ConvertTo-Json -Compress)
    }
    try {
        $state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
        $shown = [ordered]@{ ts = $null; outcome = $null; reason = $null; age_s = $null }
        $moment = [DateTime]::MinValue
        if ([DateTime]::TryParse("$($state.ts)", [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::RoundtripKind, [ref]$moment)) {
            $shown.ts = $moment.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            $shown.age_s = [long]([DateTime]::UtcNow - $moment.ToUniversalTime()).TotalSeconds
        }
        foreach ($key in @("outcome", "reason")) {
            $value = "$($state.$key)"
            if ($value -cmatch '^[A-Za-z][A-Za-z0-9_]{0,63}$') { $shown[$key] = $value }
            elseif ($value) { $shown[$key] = "other" }
        }
        return "KEEPER_STATE " + ($shown | ConvertTo-Json -Compress)
    } catch {
        return "KEEPER_STATE " + (@{ state_file = "unreadable" } | ConvertTo-Json -Compress)
    }
}

# 任务路径：首尾各一个反斜杠（根路径就是单独一个反斜杠）。
$taskFolder = "\" + $TaskPath.Trim("\") + "\"
if ($taskFolder -eq "\\") { $taskFolder = "\" }
if ([string]::IsNullOrWhiteSpace($TaskName)) { throw "拒绝：-TaskName 没有给。" }
$modes = @(@($Install, $Uninstall, $Status) | Where-Object { $_ })
if ($modes.Count -gt 1) { throw "拒绝：-Install、-Uninstall、-Status 最多给一个。" }

# -Status：只读。下面三段（Status、Uninstall、Install 的注册）是脚本里仅有的接触任务计划
# 程序的地方，各自在自己的开关里面；不带开关时一步都走不到。
if ($Status) {
    $stateLine = Get-KeeperStateLine
    if ($stateLine) { Write-Host $stateLine }
    $task = Get-ScheduledTask -TaskPath $taskFolder -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host ("KEEPER_TASK_STATUS " + ([ordered]@{
            task_path = $taskFolder; task_name = $TaskName; state = "not-installed"
        } | ConvertTo-Json -Compress))
        Write-Host "任务 $taskFolder$TaskName 没有安装。"
        exit 4
    }
    $info = Get-ScheduledTaskInfo -TaskPath $taskFolder -TaskName $TaskName
    Write-Host ("KEEPER_TASK_STATUS " + ([ordered]@{
        task_path        = $taskFolder
        task_name        = $TaskName
        state            = "$($task.State)"
        last_run_time    = Format-Utc $info.LastRunTime
        last_task_result = $info.LastTaskResult
        next_run_time    = Format-Utc $info.NextRunTime
        missed_runs      = $info.NumberOfMissedRuns
    } | ConvertTo-Json -Compress))
    exit 0
}

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskPath $taskFolder -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "任务 $taskFolder$TaskName 没有安装，什么都不做。"
        exit 4
    }
    Unregister-ScheduledTask -TaskPath $taskFolder -TaskName $TaskName -Confirm:$false
    Write-Host "已注销任务 $taskFolder$TaskName。卸载只注销任务，不删文件：日志留在 $(Get-KeeperLogLocation)"
    exit 0
}

# ---- 1. 参数和路径：全部检查完了才往下走，缺的、不对的一次列全 -------------------
$problems = @()
foreach ($name in @("TerminalPath", "DeployDir", "DataDir")) {
    if ([string]::IsNullOrWhiteSpace((Get-Variable -Name $name).Value)) { $problems += "-$name 没有给" }
}
if ($problems.Count -gt 0) { throw ("拒绝，什么都没有做：" + ($problems -join "；")) }

$terminal = $null
$deploy = $null
$data = $null
if (-not [System.IO.Path]::IsPathRooted($TerminalPath)) {
    $problems += "-TerminalPath 不是完整路径：$TerminalPath"
} else {
    $terminal = Strip-TrailingSeparator ([System.IO.Path]::GetFullPath($TerminalPath))
    if ([System.IO.Path]::GetFileName($terminal) -ine "terminal64.exe") {
        $problems += "-TerminalPath 的文件名不是 terminal64.exe：$terminal"
    } elseif (-not (Test-Path -LiteralPath $terminal -PathType Leaf)) {
        $problems += "-TerminalPath 的文件不存在：$terminal"
    }
}
foreach ($pair in @(@("DeployDir", $DeployDir), @("DataDir", $DataDir))) {
    $name, $supplied = $pair
    if (-not [System.IO.Path]::IsPathRooted($supplied)) {
        $problems += "-$name 不是完整路径：$supplied"
        continue
    }
    $full = Strip-TrailingSeparator ([System.IO.Path]::GetFullPath($supplied))
    if ($full -eq [System.IO.Path]::GetPathRoot($full)) {
        $problems += "-$name 不能是文件系统的根目录：$full"
        continue
    }
    if (-not (Test-Path -LiteralPath $full -PathType Container)) {
        $problems += "-$name 不存在：$full"
        continue
    }
    if ($name -eq "DeployDir") { $deploy = $full } else { $data = $full }
}
if ($deploy) {
    foreach ($relative in @("scripts\ops\terminal_keeper.ps1", "scripts\ops\hidden_task.vbs")) {
        if (-not (Test-Path -LiteralPath (Join-Path $deploy $relative) -PathType Leaf)) {
            $problems += "-DeployDir 里没有 $relative：$deploy"
        }
    }
}
$wscript = Join-Path $env:SystemRoot "System32\wscript.exe"
if (-not (Test-Path -LiteralPath $wscript -PathType Leaf)) {
    $problems += "找不到 wscript.exe：$wscript"
}
foreach ($location in @($terminal, $deploy, $data)) {
    if ($null -eq $location) { continue }
    if ($location.Contains("%")) {
        # 启动器遇到 % 就拒绝（WScript.Shell.Run 会展开 %名字%），装上去的任务一分钟也跑不了。
        $problems += "路径里有百分号（%），看护任务的动作经 hidden_task.vbs 传不下去：$location"
    }
}
if ($problems.Count -gt 0) { throw ("拒绝，什么都没有做：" + ($problems -join "；")) }

# ---- 2. 路径里只用系统 ANSI 代码页表示得了的字符（和 setup_scheduled_tasks.ps1 同一条规矩）。
# 两个系统代码页不同的机器上，带非 ASCII 字符的路径一律拒绝；纯 ASCII 路径照常。
$codePages = Get-SystemCodePages
$acp = [int]$codePages.ACP
$oemcp = [int]$codePages.OEMCP
if ($acp -eq 65001) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
} else {
    $encoding = [System.Text.Encoding]::GetEncoding($acp)
}
foreach ($location in @($terminal, $deploy, $data)) {
    if ($acp -ne $oemcp -and $location -match '[^\x00-\x7F]') {
        throw ("这台机器的控制台代码页（OEM $oemcp）和系统 ANSI 代码页（$acp）不同，带非 ASCII 字符的路径拒绝安装：$location。请把项目放在纯 ASCII 路径下。")
    }
    if ($encoding.GetString($encoding.GetBytes($location)) -cne $location) {
        throw ("路径含系统 ANSI 代码页（$acp）表示不了的字符，拒绝安装：$location。请改用这个代码页能表示的路径（建议只用 ASCII 字符）。")
    }
}

# ---- 3. 任务的动作、触发器、设置、主体。只构造对象，不碰任务计划程序 ----------------
$launcher = Join-Path $deploy "scripts\ops\hidden_task.vbs"
$keeper = Join-Path $deploy "scripts\ops\terminal_keeper.ps1"
$log = Join-Path $data "ops-evidence\terminal_keeper\terminal_keeper.log"

$arguments = "//B //Nologo `"$launcher`" `"$keeper`" -TerminalPath `"$terminal`" -LogPath `"$log`""
$action = New-ScheduledTaskAction -Execute $wscript -Argument $arguments -WorkingDirectory $deploy
# 起始时刻用 UTC：New-ScheduledTaskTrigger 认得带 UTC 标记的时刻，存下来是同一个时刻。
$trigger = New-ScheduledTaskTrigger -Once -At ([DateTime]::UtcNow.AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest

# 这一行是给部署记录抄用的。里面是本机的绝对路径和账户：抄进入库的部署记录之前要脱敏。
$parameters = [ordered]@{
    mode                = "plan"
    task_path           = $taskFolder
    task_name           = $TaskName
    execute             = $wscript
    arguments           = $arguments
    working_directory   = $deploy
    start_boundary      = "$($trigger.StartBoundary)"
    repeat_interval     = "$($trigger.Repetition.Interval)"
    multiple_instances  = "$($settings.MultipleInstances)"
    execution_time_limit = "$($settings.ExecutionTimeLimit)"
    logon_type          = "$($principal.LogonType)"
    run_level           = "$($principal.RunLevel)"
    user                = $user
    terminal_path       = $terminal
    deploy_dir          = $deploy
    data_dir            = $data
    log_path            = $log
    launcher_sha256     = Get-Sha256 $launcher
    keeper_sha256       = Get-Sha256 $keeper
}

if ($Install) {
    # ---- 4. 注册。同名的任务已经在了就不覆盖——本脚本只负责创建，换配置先卸载 ----
    $existing = Get-ScheduledTask -TaskPath $taskFolder -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        throw "任务 $taskFolder$TaskName 已经在了，拒绝安装。要换参数先卸载（-Uninstall）。"
    }
    Register-ScheduledTask -TaskPath $taskFolder -TaskName $TaskName `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
    $parameters["mode"] = "install"
    Write-Host "已注册任务 $taskFolder$TaskName（每分钟，经 hidden_task.vbs 运行 terminal_keeper.ps1）。"
    Write-Host ("KEEPER_TASK_PARAMETERS " + ($parameters | ConvertTo-Json -Compress))
    Write-Host "部署记录里要抄的见 runbook §6.5。维护时要关终端，先停用这个任务（Disable-ScheduledTask -TaskPath $taskFolder -TaskName $TaskName）。"
    exit 0
}

# ---- 5. 不带开关：只打印 ----
Write-Host "只打印：没有查询、没有注册，没有碰任务计划程序。下面是加 -Install 将会注册的内容。"
Write-Host ("KEEPER_TASK_PARAMETERS " + ($parameters | ConvertTo-Json -Compress))
Write-Host "要注册就在同一条命令上加 -Install；任务现在的状态用 -Status 查询。"
exit 0
