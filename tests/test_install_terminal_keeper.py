"""scripts/ops/install_terminal_keeper.ps1：终端看护任务的打印、安装、卸载、状态。

**安装脚本在这份测试里从来到不了真的任务计划程序。两层保证：**

1. 脚本自己的结构。不带 -Install / -Uninstall 时它只打印将要注册的内容（动作、触发器、
   主体、设置），不查询、不注册、不注销；接触任务计划程序的四个命令各自在自己的开关里面，
   开关是脚本的顶层语句。按语法树核对：脚本里出现的每一个命令名、每一个类型、每一处静态
   成员调用、Add-Type 的 C#、New-Object 的参数，都在一份允许清单上——清单之外的，不管是
   别的 cmdlet、别名（ipmo、iex）、COM、子进程还是别的，一律不放。静态检查不过，这份文件里
   没有任何一条用例运行安装脚本。
2. 测试进程的沙箱。驱动脚本所在的 PowerShell 进程，PSModulePath 里**只有**沙箱里放模块的
   那一个目录（沙箱模块装进来时自己核对这一条）。这保证了按名字**导入**模块
   （Import-Module ScheduledTasks）只找得到沙箱里的那一个。它不保证按名字**查找命令**：
   Get-Command，或者调用一个还没解析过的命令，会从系统模块目录自动装入导出这个命令的真模块，
   PSModulePath 里有没有它都一样（定版前第二次复核实测）。沙箱模块里——
   - New-* 是真的：按完整路径导入真模块，但只导入 New-*（它们只造内存对象，不碰任务计划
     程序），所以记下来的动作、触发器、主体、设置是真的 CIM 对象的属性；
   - 真模块导出的其余命令（Register-、Unregister-、Set-、Enable-、Disable-、Start-、
     Stop-ScheduledTask，以及 Get-*）全是替身：只把调用记进 calls.jsonl。

驱动脚本在调用安装脚本之前核对这一切：装进来的 ScheduledTasks 只有一个、在沙箱里；真模块
导出的每一个命令沙箱都有回应；New-* 是真的、其余的都定义在沙箱的模块文件里；每一个名字
都只解析到沙箱模块。任何一项不对就以 99 退出，安装脚本根本不跑——测试失败，不是跳过。
安装脚本跑完之后同样的核对再来一遍。核对自己带变异验证（把沙箱模块从 PSModulePath 拿掉、
少导出一个替身、不导出 New-*、用一个普通函数冒充 New-*）；变异的那几条用例给安装脚本的
参数是 -Install -Uninstall：万一核对没拦住，安装脚本也因为两个开关互斥而拒绝。

**所以核对的次序不能动：先看沙箱模块的导出清单，清单齐了才按名字解析。** 命令查找会自动
装入导出这个命令的模块，而且是从系统模块目录：沙箱少导出一个名字的时候，查一下这个名字，
真模块就整个装进了全局作用域，后来的导入盖过先前的，替身全部失效；沙箱目录里没有模块的
时候，Import-Module 报找不到，Get-Command 却照样把真模块装进来。先看导出清单，是为了在解析
任何一个名字之前就知道每个名字都已经有沙箱的回答——这时再按名字解析，触发不了自动装入。
第一版测试真的注册过一次任务，起因相近——被调脚本自己 Import-Module，装进它脚本作用域的真
cmdlet 盖过了调用方事先定义的全局函数替身。（提交 2fbd2a8 的说明里"按名字找得到的
ScheduledTasks 只可能是沙箱里的那个"只对导入这一半成立，以这里为准。）

沙箱在临时目录里：部署树的副本（看护脚本、隐藏启动器、安装脚本）、一个空的
terminal64.exe、一个数据目录。有一条用例把脚本打印出来的动作参数原样交给 cscript
（任务交给 wscript 的是同一条命令行），末尾加看护脚本自己的 -DryRun——只报告、不启动终端；
看护脚本不导入 ScheduledTasks（有用例核对）。
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "ops"
INSTALLER = OPS / "install_terminal_keeper.ps1"
KEEPER = OPS / "terminal_keeper.ps1"
LAUNCHER = OPS / "hidden_task.vbs"
FILES = (INSTALLER.name, KEEPER.name, LAUNCHER.name)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
requires_powershell = pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")

# Every command of the real module that changes a task, and the two that read: each one must be
# answered by a stand-in from the sandbox.
STATE_CHANGING = ("Register-ScheduledTask", "Unregister-ScheduledTask", "Set-ScheduledTask",
                  "Enable-ScheduledTask", "Disable-ScheduledTask", "Start-ScheduledTask",
                  "Stop-ScheduledTask")
READING = ("Get-ScheduledTask", "Get-ScheduledTaskInfo")

# The sandbox ScheduledTasks module (one file, written into the test's temp directory).
EXPORT_FUNCTIONS = "Export-ModuleMember -Function ($news + $standins)"
EXPORT = EXPORT_FUNCTIONS + " -Variable RealExports"
STANDINS = r'''
$ErrorActionPreference = 'Stop'
# This module may live only in a process whose PSModulePath holds nothing but its own directory:
# Import-Module ScheduledTasks then finds this module and nothing else. That does not keep the real
# module out. Looking a command name up -- Get-Command, or calling a name that is not resolved yet --
# auto-loads the module that exports it from the system module directory, whatever PSModulePath holds.
# Hence the order the driver keeps: this module answers every name the real module exports, and the
# driver reads this module's export list before it resolves a single name.
$mine = (Split-Path -Parent $PSScriptRoot).TrimEnd('\')      # the directory PSModulePath must hold, and nothing else
$others = @($env:PSModulePath -split ';' | Where-Object { $_ -and -not $_.TrimEnd('\').Equals($mine, [System.StringComparison]::OrdinalIgnoreCase) })
if ($others.Count -gt 0) { throw "the sandbox module requires a PSModulePath that holds only its own directory" }
# The real cmdlets, by full path, and New-* only: they build in-memory objects and touch nothing.
$script:RealDirectory = Join-Path $PSHOME 'Modules\ScheduledTasks'
$real = Import-Module -Name (Join-Path $script:RealDirectory 'ScheduledTasks.psd1') -Function 'New-ScheduledTask*' -PassThru
$script:RealExports = @($real.ExportedCommands.Keys | Sort-Object)
$news = @($script:RealExports | Where-Object { $_ -like 'New-ScheduledTask*' })
$standins = @($script:RealExports | Where-Object { $_ -notlike 'New-ScheduledTask*' })
$letIn = @($ExecutionContext.InvokeCommand.GetCommands('*-ScheduledTask*', [System.Management.Automation.CommandTypes]::All, $true) |
    Where-Object { $_.Name -notlike 'New-ScheduledTask*' })
if ($letIn.Count -gt 0) { throw "the filtered import let in: $($letIn.Name -join ', ')" }

function ConvertTo-Plain($value) {
    # What PowerShell shows for the object: booleans stay booleans, everything scalar else becomes
    # its text (so an enumeration such as MultipleInstances is recorded as IgnoreNew, not as 2).
    if ($null -eq $value) { return $null }
    if ($value -is [bool]) { return $value }
    if ($value -is [System.Management.Automation.SwitchParameter]) { return [bool]$value }
    if ($value -is [string] -or $value -is [ValueType]) { return "$value" }
    if ($value -is [Microsoft.Management.Infrastructure.CimInstance]) {
        $plain = [ordered]@{ cim_class = $value.CimClass.CimClassName }
        foreach ($property in $value.CimInstanceProperties) {
            if ($null -ne $property.Value) { $plain[$property.Name] = ConvertTo-Plain $value.($property.Name) }
        }
        return [pscustomobject]$plain
    }
    return , @(@($value) | ForEach-Object { ConvertTo-Plain $_ })
}

function Write-Call([string]$name, $bound, $rest) {
    $parameters = [ordered]@{}
    foreach ($key in @($bound.Keys | Sort-Object)) { $parameters[$key] = ConvertTo-Plain $bound[$key] }
    $record = [ordered]@{
        call = $name; parameters = $parameters
        rest = @(@($rest) | Where-Object { $null -ne $_ } | ForEach-Object { "$_" })      # anything not declared
    }
    [System.IO.File]::AppendAllText($env:KEEPER_TEST_TRACE, ($record | ConvertTo-Json -Depth 6 -Compress) + "`n",
        (New-Object System.Text.UTF8Encoding($false)))
}

function Register-ScheduledTask {
    param($TaskPath, $TaskName, $Action, $Trigger, $Settings, $Principal, $Description, $User, $Password,
          [switch]$Force, [Parameter(ValueFromRemainingArguments)] $rest)
    Write-Call 'Register-ScheduledTask' $PSBoundParameters $rest
}
function Unregister-ScheduledTask {
    # SupportsShouldProcess like the real cmdlet: -Confirm and -WhatIf are its common parameters.
    [CmdletBinding(SupportsShouldProcess = $true)]
    param($TaskPath, $TaskName, $InputObject, [Parameter(ValueFromRemainingArguments)] $rest)
    Write-Call 'Unregister-ScheduledTask' $PSBoundParameters $rest
}
function Get-ScheduledTask {
    param($TaskPath, $TaskName, [Parameter(ValueFromRemainingArguments)] $rest)
    Write-Call 'Get-ScheduledTask' $PSBoundParameters $rest
    if ($env:KEEPER_TEST_TASK_EXISTS -eq '1') {
        [pscustomobject]@{ TaskPath = "$TaskPath"; TaskName = "$TaskName"; State = 'Ready' }
    }
}
function Get-ScheduledTaskInfo {
    param($TaskPath, $TaskName, [Parameter(ValueFromRemainingArguments)] $rest)
    Write-Call 'Get-ScheduledTaskInfo' $PSBoundParameters $rest
    [pscustomobject]@{
        TaskPath = "$TaskPath"; TaskName = "$TaskName"
        LastRunTime = [DateTime]::Parse('2026-11-24T12:00:00Z').ToLocalTime()
        LastTaskResult = [uint32]$env:KEEPER_TEST_LAST_RESULT
        NextRunTime = [DateTime]::Parse('2026-11-24T12:01:00Z').ToLocalTime()
        NumberOfMissedRuns = [uint32]0
    }
}

# Whatever else the real module exports on this version of Windows: recorded, nothing more.
$recorder = { Write-Call $MyInvocation.InvocationName $MyInvocation.BoundParameters $args }
$explicit = @('Register-ScheduledTask', 'Unregister-ScheduledTask', 'Get-ScheduledTask', 'Get-ScheduledTaskInfo')
foreach ($name in $standins) {
    if ($explicit -notcontains $name) { Set-Item -Path ("function:script:" + $name) -Value $recorder }
}
''' + EXPORT + "\n"

# The driver's guard, before the installer runs and again after it. It looks at what the loaded
# module exports before it resolves a single name by Get-Command (see the module docstring).
GUARD = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
function Stop-ForStandIn([string]$when, [string]$why) {
    Write-Output "STAND-IN GUARD FAILED ($when): $why"
    exit 99
}
function Assert-StandIn([string]$when) {
    try { Import-Module ScheduledTasks -ErrorAction Stop }
    catch { Stop-ForStandIn $when "ScheduledTasks cannot be imported from the sandbox: $($_.Exception.Message)" }
    $loaded = @(@(Get-Module) | Where-Object { $_.Name -eq 'ScheduledTasks' })
    if ($loaded.Count -ne 1) { Stop-ForStandIn $when "modules named ScheduledTasks loaded: $($loaded.Count)" }
    $sandbox = $loaded[0]
    if (-not $sandbox.ModuleBase.Equals((Join-Path $env:KEEPER_TEST_SANDBOX 'ScheduledTasks'), [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-ForStandIn $when "ScheduledTasks was loaded from $($sandbox.ModuleBase)"
    }
    $file = Join-Path $sandbox.ModuleBase 'ScheduledTasks.psm1'
    $exported = @($sandbox.ExportedCommands.Keys)
    $realExports = @($RealExports | Where-Object { $_ })
    if ($realExports.Count -eq 0) { Stop-ForStandIn $when "the sandbox module did not report what the real module exports" }
    foreach ($name in @($realExports + $required | Sort-Object -Unique)) {
        if ($exported -notcontains $name) { Stop-ForStandIn $when "nothing from the sandbox answers for $name" }
    }
    foreach ($info in $sandbox.ExportedCommands.Values) {
        $defined = "$($info.ScriptBlock.File)"
        if ($info.Name -like 'New-ScheduledTask*') {
            if ($defined) { Stop-ForStandIn $when "$($info.Name) is not the real cmdlet: it is defined in $defined" }
        } elseif ($defined -ne $file) {
            Stop-ForStandIn $when "$($info.Name) is not the sandbox stand-in: it is defined in [$defined]"
        }
    }
    # New-* must be the real ones: a plain function cannot build a task object.
    $built = @(
        (New-ScheduledTaskAction -Execute 'wscript.exe').CimClass.CimClassName
        (New-ScheduledTaskTrigger -Once -At ([DateTime]::UtcNow.AddMinutes(1))).CimClass.CimClassName
        (New-ScheduledTaskSettingsSet).CimClass.CimClassName
        (New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)).CimClass.CimClassName
    )
    foreach ($class in $built) { if ("$class" -notlike 'MSFT_Task*') { Stop-ForStandIn $when "New-* did not build a task object: [$class]" } }
    # Only now, with every name answered by the sandbox, is it harmless to resolve names.
    foreach ($name in $exported) {
        $found = @(Get-Command -Name $name -ErrorAction SilentlyContinue)
        if ($found.Count -ne 1 -or -not $found[0].Module -or $found[0].Module.ModuleBase -ne $sandbox.ModuleBase) {
            Stop-ForStandIn $when "$name does not resolve to the sandbox module alone ($($found.Count) found)"
        }
    }
    $outside = @(Get-Command -Name '*-ScheduledTask*' -ErrorAction SilentlyContinue | Where-Object { $exported -notcontains $_.Name })
    if ($outside.Count -gt 0) { Stop-ForStandIn $when "commands of the Task Scheduler family outside the sandbox: $($outside.Name -join ', ')" }
    Write-Output "STAND-IN GUARD OK ($when): $($exported.Count) commands, all from the sandbox module"
}
$required = @('__REQUIRED__')
Assert-StandIn 'before'
'''
AFTER = r'''
if ($null -eq $code) { Write-Output "DRIVER-ERROR: the installer returned without an exit code"; $code = 1 }
Assert-StandIn 'after'
exit $code
'''

EXPECTED_ACTION = ('//B //Nologo "{deploy}\\scripts\\ops\\hidden_task.vbs" "{deploy}\\scripts\\ops\\terminal_keeper.ps1"'
                   ' -TerminalPath "{terminal}" -LogPath "{data}\\ops-evidence\\terminal_keeper\\terminal_keeper.log"')


def quote(text) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def one(value):
    """真 cmdlet 收到的是数组，安装脚本传的是单个对象：两种都取成单个。"""
    return value[0] if isinstance(value, list) else value


def entries(root):
    """root 下的全部文件和目录（相对路径集合，不算测试自己的驱动脚本和记录）：断言"拒绝或只打印
    时什么都没有新建"，也断言安装、卸载不建、不删任何文件。"""
    return {p.relative_to(root).as_posix() for p in root.rglob("*")
            if "queries" not in p.relative_to(root).parts                 # 测试自己的一次性查询脚本
            and p.name not in ("calls.jsonl", "invoke.ps1")}              # 和驱动脚本、调用记录


def sandbox(tmp_path, name="deploy tree (x) & y", module_text=STANDINS):
    """部署树的副本、一个空的 terminal64.exe（安装脚本只核对它在不在）、一个数据目录、沙箱的
    ScheduledTasks 模块。→ (部署树, 终端, 数据目录, 放沙箱模块的目录)."""
    ops = tmp_path / name / "scripts" / "ops"
    ops.mkdir(parents=True)
    for file in FILES:
        shutil.copyfile(OPS / file, ops / file)
    terminal = tmp_path / "MT 5" / "terminal64.exe"
    terminal.parent.mkdir()
    terminal.write_bytes(b"")
    data = tmp_path / "data dir"
    data.mkdir()
    modules = tmp_path / "sandbox modules"
    (modules / "ScheduledTasks").mkdir(parents=True)
    (modules / "ScheduledTasks" / "ScheduledTasks.psm1").write_bytes(
        b"\xef\xbb\xbf" + module_text.replace("\n", "\r\n").encode("utf-8"))
    return tmp_path / name, terminal, data, modules


def located(deploy, terminal, data):
    """安装脚本自己的三个路径参数，加上引号。"""
    return [f"-TerminalPath {quote(terminal)}", f"-DeployDir {quote(deploy)}", f"-DataDir {quote(data)}"]


def environment(modules, **extra):
    """驱动进程的环境：PSModulePath 里只有放沙箱模块的那一个目录。环境变量名的大小写照父进程里
    已有的那个写——Windows 上名字不分大小写，放两个只差大小写的键，子进程读到哪一个不确定。"""
    env = dict(os.environ)
    key = next((key for key in env if key.upper() == "PSMODULEPATH"), "PSModulePath")
    env[key] = str(modules)
    env.update(extra)
    return env


def run(tmp_path, modules, *flags, code_pages=None, exists=False, last_result=None):
    """在一个 PowerShell 进程里：先核对替身在位（不在位以 99 退出，安装脚本不跑），再运行沙箱里的
    安装脚本，跑完再核对一遍。flags 是安装脚本的参数（已经加上引号）；exists 让替身报告"同名的
    任务已经在了"。→ (进程, 运行前的目录快照, 记下来的调用)."""
    trace, driver = tmp_path / "calls.jsonl", tmp_path / "invoke.ps1"
    trace.unlink(missing_ok=True)
    pages = ""
    if code_pages:
        pages = f"function global:Get-SystemCodePages {{ [pscustomobject]@{{ ACP = {code_pages[0]}; OEMCP = {code_pages[1]} }} }}\n"
    required = "', '".join(STATE_CHANGING + READING)
    installer = tmp_path / "deploy tree (x) & y" / "scripts" / "ops" / "install_terminal_keeper.ps1"
    if not installer.exists():
        installer = next(tmp_path.rglob("install_terminal_keeper.ps1"))
    driver.write_text(GUARD.replace("__REQUIRED__", required) + pages + "$global:LASTEXITCODE = $null\n"
                      + "try {\n    & " + quote(installer) + "".join(" " + flag for flag in flags) + "\n"
                      + "    $code = $LASTEXITCODE\n}\n"
                      + "catch { Write-Output ('INSTALLER-ERROR: ' + $_.Exception.Message); $code = 1 }\n" + AFTER,
                      encoding="utf-8-sig")
    before = entries(tmp_path)
    extra = {"KEEPER_TEST_SANDBOX": str(modules), "KEEPER_TEST_TRACE": str(trace),
             "KEEPER_TEST_TASK_EXISTS": "1" if exists else "0"}
    if last_result is not None:
        extra["KEEPER_TEST_LAST_RESULT"] = str(last_result)
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(driver)],
                          capture_output=True, timeout=120, creationflags=NO_WINDOW, env=environment(modules, **extra))
    calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()] if trace.exists() else []
    return done, before, calls


def text(done):
    return done.stdout.decode("utf-8", errors="replace") + done.stderr.decode("utf-8", errors="replace")


def lines_starting_with(done, marker):
    return [line for line in done.stdout.decode("utf-8", errors="replace").splitlines() if line.startswith(marker)]


def parameters(done):
    return [json.loads(line.removeprefix("KEEPER_TASK_PARAMETERS "))
            for line in lines_starting_with(done, "KEEPER_TASK_PARAMETERS ")]


def status(done):
    return [json.loads(line.removeprefix("KEEPER_TASK_STATUS "))
            for line in lines_starting_with(done, "KEEPER_TASK_STATUS ")]


def state_lines(done):
    return [json.loads(line.removeprefix("KEEPER_STATE "))
            for line in lines_starting_with(done, "KEEPER_STATE ")]


def names(calls):
    return [call["call"] for call in calls]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def powershell(folder, script, env=None):
    """跑一段测试自己的 PowerShell（不是安装脚本），用的是这个进程平常的环境。一次性的 .ps1 写在 folder
    下面的 queries 里——测试自己的临时目录——不往系统临时目录的根上写。"""
    queries = Path(folder) / "queries"
    queries.mkdir(exist_ok=True)
    path = queries / f"query-{1 + len(list(queries.iterdir()))}.ps1"
    path.write_bytes(b"\xef\xbb\xbf" + script.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)],
                          capture_output=True, timeout=120, creationflags=NO_WINDOW, env=env or dict(os.environ))
    return done.returncode, done.stdout.decode("utf-8", errors="replace"), done.stderr.decode("utf-8", errors="replace")


def real_tasks(folder):
    """本机全部计划任务的路径加名字（只读查询，和安装脚本不在一个进程）。安装、卸载两条用例在运行前后各取
    一次：必须相同——万一替身没有挡住，不管登记到了哪个路径下，这里当场发现。"""
    rc, out, err = powershell(folder, "@(Get-ScheduledTask) | ForEach-Object { $_.TaskPath + $_.TaskName } | Sort-Object\n")
    assert rc == 0, err
    return sorted(line for line in out.splitlines() if line.strip())


# ---- the static check: what the installer may contain, by its syntax tree ---------------------------------
# Every command the installer may run, by the name it is written with. Anything else is refused on
# sight -- another cmdlet of the Task Scheduler, an alias (ipmo, iex), a shell, a program -- whether
# or not anybody thought of that particular one. The script's own functions are allowed by name.
# The list is an inventory: a command on it that the script no longer uses fails the check too.
ALLOWED_COMMANDS = frozenset({
    "Import-Module",
    "Get-ScheduledTask", "Get-ScheduledTaskInfo", "Unregister-ScheduledTask", "Register-ScheduledTask",
    "New-ScheduledTaskAction", "New-ScheduledTaskTrigger", "New-ScheduledTaskSettingsSet", "New-ScheduledTaskPrincipal",
    "Write-Host", "Get-Variable", "Get-Command", "Where-Object", "Join-Path", "Test-Path", "Get-FileHash", "Get-Content",
    "Out-Null", "ConvertTo-Json", "ConvertFrom-Json", "New-TimeSpan", "Add-Type", "New-Object",
})
# The commands whose arguments are part of the check: each is allowed exactly as written here, as often.
# Import-Module by its bare name is what finds only the sandbox module; by a path it would find the real
# one (and so would its alias ipmo, which is not on the list above). A command looked up by name loads the
# module that exports it. Add-Type compiles whatever it is given. New-Object can make a COM object.
CODE_PAGES = ("@'\n[DllImport(\"kernel32.dll\")] public static extern uint GetACP();\n"
              "[DllImport(\"kernel32.dll\")] public static extern uint GetOEMCP();\n'@")
ALLOWED_ARGUMENTS = {
    "Import-Module": [["ScheduledTasks"]],
    "Get-Command": [["Get-SystemCodePages", "-ErrorAction", "SilentlyContinue"]],
    "Add-Type": [["-Namespace", "MT5Quant", "-Name", "KeeperInstallerCodePages", "-MemberDefinition", CODE_PAGES]],
    "New-Object": [["System.Text.UTF8Encoding", "($false)"]],
}
# Every [type] the script may name, and every static member it may call on one. [Activator],
# [type]::GetTypeFromProgID, [System.Runtime.InteropServices.Marshal] and the like are not on them.
ALLOWED_TYPES = frozenset({
    "CmdletBinding", "string", "switch", "ordered", "pscustomobject", "int", "long", "ref", "type", "DateTime",
    "System.IO.Path", "System.Security.Principal.WindowsIdentity", "System.Globalization.CultureInfo",
    "System.Globalization.DateTimeStyles", "System.Text.Encoding", "MT5Quant.KeeperInstallerCodePages",
})
ALLOWED_MEMBERS = frozenset({
    ("string", "IsNullOrWhiteSpace"),
    ("System.IO.Path", "GetPathRoot"), ("System.IO.Path", "GetFullPath"), ("System.IO.Path", "GetFileName"),
    ("System.IO.Path", "IsPathRooted"), ("System.IO.Path", "DirectorySeparatorChar"),
    ("System.IO.Path", "AltDirectorySeparatorChar"),
    ("System.Security.Principal.WindowsIdentity", "GetCurrent"),
    ("DateTime", "UtcNow"), ("DateTime", "MinValue"), ("DateTime", "TryParse"),
    ("System.Globalization.CultureInfo", "InvariantCulture"), ("System.Globalization.DateTimeStyles", "RoundtripKind"),
    ("System.Text.Encoding", "GetEncoding"),
    ("MT5Quant.KeeperInstallerCodePages", "GetACP"), ("MT5Quant.KeeperInstallerCodePages", "GetOEMCP"),
})
# Members that make or run something else, refused on any object.
REFUSED_MEMBERS = frozenset({"CreateInstance", "GetTypeFromProgID", "GetActiveObject", "InvokeMember",
                             "InvokeCommand", "InvokeScript", "AddScript"})
# The four commands that read or change tasks, and the switch each call sits directly in, in the order
# the script has them. The switch's if has to be a top-level statement of the script: not inside a
# function (from where it could be called at any time), not inside another branch.
TOUCHING = frozenset({"Get-ScheduledTask", "Get-ScheduledTaskInfo", "Unregister-ScheduledTask", "Register-ScheduledTask"})
EXPECTED_TOUCHING = [
    ("Get-ScheduledTask", "$Status"), ("Get-ScheduledTaskInfo", "$Status"),
    ("Get-ScheduledTask", "$Uninstall"), ("Unregister-ScheduledTask", "$Uninstall"),
    ("Get-ScheduledTask", "$Install"), ("Register-ScheduledTask", "$Install"),
]

# Reads a script by its syntax tree and prints one line of JSON. The script is parsed, never run.
SURVEY = r'''
# Survey of a PowerShell script by its syntax tree: every command (name as written, invocation
# operator, arguments, and the nearest branch or scope that contains it), every type named, every
# static member called on a type, every function defined. Read-only: the script is parsed, never run.
param([string]$Script)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$tokens, [ref]$errors)
$broken = @($errors | Where-Object { $_ })
if ($broken.Count -gt 0) {
    @{ parse_errors = @($broken | ForEach-Object { $_.Message }); commands = @(); types = @(); members = @(); instance_members = @(); functions = @() } | ConvertTo-Json -Compress -Depth 5
    exit 0
}
$language = 'System.Management.Automation.Language.'

function Container($node) {
    # What encloses the command, from the inside out: the first branch or scope. An if is
    # "top-level" when the if statement itself sits directly in the script body; the condition
    # reported is the one of the clause the command is in (an else block is "else-of").
    $current, $below = $node.Parent, $node
    while ($current) {
        $kind = $current.GetType().Name
        if ($kind -eq 'FunctionDefinitionAst') { return 'function:' + $current.Name }
        if ($kind -eq 'IfStatementAst') {
            # the clause block the command is directly under; not one of them: the else block, or the condition itself
            $condition = $null
            for ($i = 0; $i -lt $current.Clauses.Count; $i++) {
                # reference equality: -eq between two AST objects compares their string forms
                if ([object]::ReferenceEquals($current.Clauses[$i].Item2, $below)) { $condition = $current.Clauses[$i].Item1.Extent.Text }
            }
            if (-not $condition) { $condition = 'else-or-condition:' + $current.Clauses[0].Item1.Extent.Text }
            $parent = $current.Parent
            if ($parent.GetType().Name -eq 'NamedBlockAst' -and $parent.Parent -is ($language + 'ScriptBlockAst' -as [type]) -and $null -eq $parent.Parent.Parent) {
                return 'top-level-if:' + $condition
            }
            return 'nested-if:' + $condition
        }
        if ($kind -match '^(For|ForEach|While|Do)StatementAst$') { return 'loop' }
        if ($kind -in @('SwitchStatementAst', 'TryStatementAst', 'ScriptBlockExpressionAst', 'TrapStatementAst', 'DataStatementAst')) {
            return ($kind -replace 'StatementAst$|ExpressionAst$', '').ToLower()
        }
        $below, $current = $current, $current.Parent
    }
    return 'top-level'
}

$commands = @($ast.FindAll({ param($node) $node -is ($language + 'CommandAst' -as [type]) }, $true) | ForEach-Object {
    $elements = @($_.CommandElements)
    [ordered]@{
        name      = $_.GetCommandName()
        operator  = $_.InvocationOperator.ToString()
        line      = $_.Extent.StartLineNumber
        arguments = @($elements | Select-Object -Skip 1 | ForEach-Object { $_.Extent.Text })
        container = Container $_
    }
})
# Types, wherever they are named: a member access or a literal type, an attribute, a cast, an
# -is / -as operand (FindAll does not walk into casts), and -- as a cross-check -- every identifier
# that follows an opening bracket in the token stream.
$types = @($ast.FindAll({ param($node) $node -is ($language + 'TypeExpressionAst' -as [type]) }, $true) |
    ForEach-Object { $_.TypeName.FullName })
$types += @($ast.FindAll({ param($node) $node -is ($language + 'AttributeAst' -as [type]) }, $true) |
    ForEach-Object { $_.TypeName.FullName })
foreach ($cast in @($ast.FindAll({ param($node) $node -is ($language + 'ConvertExpressionAst' -as [type]) }, $true))) {
    if ($cast.Type) { $types += $cast.Type.TypeName.FullName }
}
foreach ($test in @($ast.FindAll({ param($node) $node -is ($language + 'BinaryExpressionAst' -as [type]) -and $node.Operator.ToString() -in @('Is', 'As') }, $true))) {
    foreach ($side in @($test.Left, $test.Right)) {
        if ($side -is ($language + 'ConvertExpressionAst' -as [type]) -and $side.Type) { $types += $side.Type.TypeName.FullName }
        if ($side -is ($language + 'TypeExpressionAst' -as [type])) { $types += $side.TypeName.FullName }
    }
}
for ($i = 0; $i -lt $tokens.Count; $i++) {
    if ($tokens[$i].Kind.ToString() -eq 'LBracket' -and $i + 1 -lt $tokens.Count -and $tokens[$i + 1].Kind.ToString() -eq 'Identifier') {
        $types += $tokens[$i + 1].Text
    }
}
$members = @($ast.FindAll({ param($node) $node -is ($language + 'MemberExpressionAst' -as [type]) -and $node.Static }, $true) |
    ForEach-Object { $_.Expression.TypeName.FullName + '::' + $_.Member.Extent.Text } | Sort-Object -Unique)
$instance_members = @($ast.FindAll({ param($node) $node -is ($language + 'MemberExpressionAst' -as [type]) -and -not $node.Static }, $true) |
    ForEach-Object { $_.Member.Extent.Text } | Sort-Object -Unique)
$functions = @($ast.FindAll({ param($node) $node -is ($language + 'FunctionDefinitionAst' -as [type]) }, $true) |
    ForEach-Object { $_.Name })
@{ commands = $commands; types = @($types | Sort-Object -Unique); members = $members; instance_members = $instance_members; functions = $functions } | ConvertTo-Json -Compress -Depth 5
'''


def survey(script, folder) -> dict:
    """→ {commands: [{name, operator, line, arguments, container}], types, members, functions}。container 是命令
    外面最近的分支或作用域：top-level-if:<条件>、nested-if:<条件>、function:<名字>、loop、try……"""
    script_file = Path(folder) / "survey.ps1"
    script_file.write_bytes(b"\xef\xbb\xbf" + SURVEY.replace("\n", "\r\n").encode("utf-8"))
    rc, out, err = powershell(folder, "& " + quote(script_file) + " -Script " + quote(script) + "\n")
    assert rc == 0, err
    return json.loads(out.splitlines()[-1])


def static_problems(report: dict) -> list:
    """→ 不合允许清单的地方，一行一条；空列表就是过了。"""
    if report.get("parse_errors"):
        return [f"does not parse: {error}" for error in report["parse_errors"]]
    bad, listed = [], {}
    for command in report["commands"]:
        name, operator = command["name"], command["operator"]
        shown = f"line {command['line']}: {'' if operator == 'Unknown' else operator + ' '}{name or '<not a plain command name>'}"
        if operator != "Unknown" or name not in ALLOWED_COMMANDS and name not in report["functions"]:
            bad.append(f"command not on the allowed list: {shown}")
        if name in ALLOWED_ARGUMENTS:
            listed.setdefault(name, []).append([argument.replace("\r\n", "\n") for argument in command["arguments"]])
    for name, allowed in ALLOWED_ARGUMENTS.items():
        if sorted(listed.get(name, [])) != sorted(allowed):
            bad.append(f"the arguments of {name} are not exactly the allowed ones: {listed.get(name, [])!r}")
    bad += [f"type not on the allowed list: [{type_name}]" for type_name in report["types"] if type_name not in ALLOWED_TYPES]
    for item in report["members"]:
        type_name, member = item.split("::", 1)
        if member in REFUSED_MEMBERS or (type_name, member) not in ALLOWED_MEMBERS:
            bad.append(f"static member not on the allowed list: [{item}]")
    bad += [f"a member that can make or run something else: .{member}" for member in report["instance_members"]
            if member in REFUSED_MEMBERS]
    touching = [(command["name"], command["container"]) for command in report["commands"] if command["name"] in TOUCHING]
    if touching != [(name, "top-level-if:" + switch) for name, switch in EXPECTED_TOUCHING]:
        bad.append("the commands that touch the Task Scheduler are not each directly in their own top-level switch: "
                   f"{touching!r}")
    return bad


@pytest.fixture(scope="module", autouse=True)
def the_installer_passes_the_static_checks(tmp_path_factory):
    """这一份文件里每一条用例之前先过静态检查。不过：安装脚本一次也不运行——这份文件里的用例全部报错，
    不是跳过。所以静态检查没过的时候，不存在安装、卸载的用例在真的任务计划程序上跑这回事。"""
    if shutil.which("powershell") is None:
        pytest.skip("requires PowerShell")
    problems = static_problems(survey(INSTALLER, tmp_path_factory.mktemp("installer-static")))
    assert problems == [], "安装脚本没有过静态检查，这份文件里没有一条用例运行它：\n" + "\n".join(problems)


def test_the_keeper_script_does_not_use_the_task_scheduler_module():
    """有一条用例在沙箱之外运行看护脚本（它自己的 -DryRun）：前提是它碰不到任务计划程序。"""
    raw = KEEPER.read_bytes()
    code = raw.decode("utf-8-sig")
    assert "ScheduledTask" not in code and "schtasks" not in code and "Import-Module" not in code


def test_the_installer_has_the_three_switches_and_no_dry_run():
    """UTF-8 带 BOM（PowerShell 5.1 没有 BOM 读不好中文说明）；开关只有 -Install、-Uninstall、-Status，没有
    -DryRun 这个名字（免得和"真的去查一遍"混在一起）。"""
    raw = INSTALLER.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"
    whole = raw.decode("utf-8-sig")
    block = whole.split("param(", 1)[1].split("\n)", 1)[0]
    assert re.findall(r"\[switch\]\$(\w+)", block) == ["Install", "Uninstall", "Status"]
    assert "DryRun" not in whole.split("#>", 1)[1]


@requires_powershell
def test_the_installer_contains_only_what_is_on_the_allowed_lists(tmp_path):
    """按语法树清点安装脚本：命令名、带参数的几个命令、类型、静态成员、接触命令的位置，每一项都过允许
    清单。清单就是库存——脚本不再用到的，也要从清单上拿掉，清单才不会越积越大。"""
    report = survey(INSTALLER, tmp_path)
    assert static_problems(report) == []
    assert {command["name"] for command in report["commands"] if command["name"] in ALLOWED_COMMANDS} == ALLOWED_COMMANDS
    assert set(report["types"]) == ALLOWED_TYPES
    assert set(report["members"]) == {"::".join(pair) for pair in ALLOWED_MEMBERS}


@requires_powershell
def test_only_the_three_mode_blocks_touch_the_task_scheduler(tmp_path):
    """结构保护：安装脚本里接触任务计划程序的命令只有 Get-ScheduledTask、Get-ScheduledTaskInfo、
    Register-ScheduledTask、Unregister-ScheduledTask；每一个外面最近的分支就是它自己的开关
    （-Status / -Uninstall / -Install），而这个 if 是脚本的顶层语句——不在函数里，不在别的分支里。
    不带开关时一步都走不到。按 PowerShell 自己的语法树核对。"""
    report = survey(INSTALLER, tmp_path)
    where = [(command["name"], command["container"]) for command in report["commands"] if command["name"] in TOUCHING]
    assert where == [(name, "top-level-if:" + switch) for name, switch in EXPECTED_TOUCHING]


@requires_powershell
def test_without_a_switch_it_only_prints_what_would_be_registered(tmp_path):
    """不带 -Install / -Uninstall：过检查、构造对象、打印，任务计划程序一次都没有被碰（替身连
    Get-ScheduledTask 都没有记到调用），什么也不建。"""
    deploy, terminal, data, modules = sandbox(tmp_path)
    started = datetime.now(timezone.utc)
    done, before, calls = run(tmp_path, modules, *located(deploy, terminal, data))
    assert done.returncode == 0, text(done)
    assert calls == [] and entries(tmp_path) == before
    assert not (data / "ops-evidence").exists()
    out = text(done)
    assert "没有碰任务计划程序" in out and "-Install" in out and "-Status" in out
    assert out.count("STAND-IN GUARD OK") == 2                      # before and after the installer ran
    (line,) = parameters(done)
    assert line["mode"] == "plan"
    check_reported_task(line, started, deploy, terminal, data)

    before = entries(tmp_path)
    done, _, calls = run(tmp_path, modules, f"-TerminalPath {quote(tmp_path / 'no such terminal' / 'terminal64.exe')}",
                         f"-DeployDir {quote(deploy)}", f"-DataDir {quote(data)}")
    assert done.returncode == 1 and calls == [] and entries(tmp_path) == before     # 只打印也要先过检查
    assert "-TerminalPath 的文件不存在" in text(done) and parameters(done) == []
    assert "DRIVER-ERROR" not in text(done)                         # 拒绝的退出码是 1，不是"没有退出码"


def check_reported_task(line, started, deploy, terminal, data):
    assert line["task_path"] == "\\MT5Quant\\" and line["task_name"] == "terminal_keeper"
    assert line["execute"] == os.path.join(os.environ["SystemRoot"], "System32", "wscript.exe")
    assert line["arguments"] == EXPECTED_ACTION.format(deploy=deploy, terminal=terminal, data=data)
    assert line["working_directory"] == str(deploy)
    boundary = datetime.fromisoformat(line["start_boundary"].replace("Z", "+00:00"))
    assert started + timedelta(seconds=20) < boundary < started + timedelta(minutes=3)   # 一分钟之后，UTC
    assert line["repeat_interval"] == "PT1M"
    assert line["multiple_instances"] == "IgnoreNew" and line["execution_time_limit"] == "PT2M"
    assert line["logon_type"] == "Interactive" and line["run_level"] == "Highest"
    assert line["user"].endswith("\\" + getpass.getuser())
    assert line["terminal_path"] == str(terminal) and line["deploy_dir"] == str(deploy) and line["data_dir"] == str(data)
    assert line["log_path"] == str(data / "ops-evidence" / "terminal_keeper" / "terminal_keeper.log")
    assert line["launcher_sha256"] == sha256(LAUNCHER)
    assert line["keeper_sha256"] == sha256(KEEPER)


@requires_powershell
def test_install_registers_one_task_that_runs_the_keeper_through_the_hidden_launcher(tmp_path):
    """-Install：传给 Register-ScheduledTask 的任务路径、动作、触发器、主体、设置——都是真 cmdlet
    造出来的对象，替身记下它们的属性。安装不建任何东西，本机的任务清单不变。"""
    deploy, terminal, data, modules = sandbox(tmp_path)
    started = datetime.now(timezone.utc)
    tasks_before = real_tasks(tmp_path)
    done, before, calls = run(tmp_path, modules, *located(deploy, terminal, data), "-Install")
    assert done.returncode == 0, text(done)
    assert real_tasks(tmp_path) == tasks_before
    assert entries(tmp_path) == before                                  # 没有日志目录，部署树里没有新文件
    assert names(calls) == ["Get-ScheduledTask", "Register-ScheduledTask"]
    lookups, registered = calls[0]["parameters"], calls[1]["parameters"]
    assert lookups == {"TaskName": "terminal_keeper", "TaskPath": "\\MT5Quant\\", "ErrorAction": "SilentlyContinue"}
    assert calls[0]["rest"] == [] and calls[1]["rest"] == []          # 没有约定之外的参数
    assert registered["TaskPath"] == "\\MT5Quant\\" and registered["TaskName"] == "terminal_keeper"
    action, trigger, settings, principal = (one(registered[key]) for key in ("Action", "Trigger", "Settings", "Principal"))
    wscript = os.path.join(os.environ["SystemRoot"], "System32", "wscript.exe")
    assert action["cim_class"] == "MSFT_TaskExecAction"
    assert action["Execute"] == wscript
    assert action["Arguments"] == EXPECTED_ACTION.format(deploy=deploy, terminal=terminal, data=data)
    assert action["WorkingDirectory"] == str(deploy)
    assert trigger["cim_class"] == "MSFT_TaskTimeTrigger" and trigger["Repetition"]["Interval"] == "PT1M"
    boundary = datetime.fromisoformat(trigger["StartBoundary"].replace("Z", "+00:00"))
    assert started + timedelta(seconds=20) < boundary < started + timedelta(minutes=3)   # 一分钟之后
    assert settings["cim_class"].startswith("MSFT_TaskSettings") and settings["MultipleInstances"] == "IgnoreNew"
    assert settings["ExecutionTimeLimit"] == "PT2M"
    assert principal["cim_class"].startswith("MSFT_TaskPrincipal") and principal["LogonType"] == "Interactive"
    assert principal["RunLevel"] == "Highest" and principal["UserId"].endswith("\\" + getpass.getuser())
    (line,) = parameters(done)
    assert line["mode"] == "install"
    check_reported_task(line, started, deploy, terminal, data)
    assert line["start_boundary"] == trigger["StartBoundary"]
    assert "STAND-IN GUARD OK (after)" in text(done)


@requires_powershell
def test_the_action_it_would_register_runs_the_keeper_through_the_launcher(tmp_path):
    """把打印出来的动作参数原样交给 cscript（任务交给 wscript 的是同一条命令行），末尾加看护脚本
    自己的 -DryRun：只报告，不启动。路径里有空格、括号和 &。启动器起的 PowerShell 不继承
    cscript 被重定向的标准输出，看不到看护脚本的那一行；能核对的是退出码和看护脚本自己的日志——
    启动器把看护脚本的退出码原样交出来：参数原样到达时是 0，什么都不写（-DryRun 的 would-start
    不是状态变化）；把终端换成不存在的文件时是 1，拒绝是一条状态变化，写进日志。看护脚本对这组
    参数的输出，另外直接运行一次核对。"""
    deploy, terminal, data, modules = sandbox(tmp_path)
    done, _, calls = run(tmp_path, modules, *located(deploy, terminal, data))
    assert done.returncode == 0 and calls == [], text(done)
    (line,) = parameters(done)
    log = data / "ops-evidence" / "terminal_keeper" / "terminal_keeper.log"
    chain = subprocess.run(f'cscript.exe {line["arguments"]} -DryRun', capture_output=True, timeout=120,
                           creationflags=NO_WINDOW)
    assert chain.returncode == 0, (chain.stdout + chain.stderr).decode("mbcs", errors="replace")
    assert not (data / "ops-evidence").exists()                     # would-start 不是状态变化：没有日志

    missing = tmp_path / "no such terminal" / "terminal64.exe"       # 对：名字对、文件不在
    changed = line["arguments"].replace(f'-TerminalPath "{terminal}"', f'-TerminalPath "{missing}"')
    assert changed != line["arguments"]
    chain = subprocess.run(f'cscript.exe {changed} -DryRun', capture_output=True, timeout=120,
                           creationflags=NO_WINDOW)
    assert chain.returncode == 1, (chain.stdout + chain.stderr).decode("mbcs", errors="replace")
    assert not missing.exists() and log.read_text(encoding="utf-8-sig").rstrip().endswith("refused reason=file_missing")

    rc, out, err = powershell(tmp_path, "& " + quote(deploy / "scripts" / "ops" / "terminal_keeper.ps1")
                              + " -TerminalPath " + quote(terminal) + " -LogPath " + quote(log) + " -DryRun\n")
    assert rc == 0, out + err
    assert out.startswith("KEEPER would-start other_instances=")      # 只报告，不启动；不输出路径
    assert str(terminal) not in out


@requires_powershell
def test_trailing_separators_in_the_given_paths_do_not_reach_the_task(tmp_path):
    deploy, terminal, data, modules = sandbox(tmp_path)
    done, _, calls = run(tmp_path, modules, f"-TerminalPath {quote(terminal)}",
                         f"-DeployDir {quote(str(deploy) + chr(92))}", f"-DataDir {quote(str(data) + '/')}")
    assert done.returncode == 0 and calls == [], text(done)
    (line,) = parameters(done)
    assert f'"{data}\\ops-evidence\\terminal_keeper\\terminal_keeper.log"' in line["arguments"]
    assert line["working_directory"] == str(deploy)


@requires_powershell
def test_install_refuses_to_overwrite_a_task_that_is_already_there(tmp_path):
    deploy, terminal, data, modules = sandbox(tmp_path)
    done, before, calls = run(tmp_path, modules, *located(deploy, terminal, data), "-Install", exists=True)
    assert done.returncode == 1 and "-Uninstall" in text(done)
    assert names(calls) == ["Get-ScheduledTask"] and entries(tmp_path) == before    # 查了，没有注册


@requires_powershell
def test_the_three_switches_cannot_be_combined(tmp_path):
    deploy, terminal, data, modules = sandbox(tmp_path)
    for switches in ("-Install -Uninstall", "-Status -Install", "-Status -Uninstall"):
        done, before, calls = run(tmp_path, modules, *located(deploy, terminal, data), *switches.split())
        assert done.returncode == 1 and "最多给一个" in text(done), switches
        assert calls == [] and entries(tmp_path) == before, switches


@requires_powershell
@pytest.mark.parametrize("break_it, expected", [
    pytest.param("no-terminal", ["-TerminalPath 没有给"], id="one-parameter-missing"),
    pytest.param("none", ["-TerminalPath 没有给", "-DeployDir 没有给", "-DataDir 没有给"], id="all-missing-listed-together"),
    pytest.param("relative", ["-TerminalPath 不是完整路径"], id="terminal-path-relative"),
    pytest.param("other-name", ["-TerminalPath 的文件名不是 terminal64.exe"], id="terminal-not-terminal64"),
    pytest.param("no-terminal-file", ["-TerminalPath 的文件不存在"], id="terminal-file-missing"),
    pytest.param("no-deploy", ["-DeployDir 不存在"], id="deploy-dir-missing"),
    pytest.param("no-data", ["-DataDir 不存在"], id="data-dir-missing"),
    pytest.param("no-keeper", ["scripts\\ops\\terminal_keeper.ps1"], id="keeper-script-missing"),
    pytest.param("no-launcher", ["scripts\\ops\\hidden_task.vbs"], id="launcher-missing"),
    pytest.param("root", ["根目录"], id="data-dir-is-a-drive-root"),
    pytest.param("percent", ["百分号"], id="percent-in-a-path"),
    # 定版前评审 S6：双引号到不了启动器（WSH 解析命令行时就把引号吃掉了，参数会变样），
    # 所以安装脚本在入口拒——引号还看得见的地方
    pytest.param("quote", ["-DeployDir 里有双引号"], id="double-quote-in-the-deploy-dir"),
    pytest.param("quote-terminal", ["-TerminalPath 里有双引号"], id="double-quote-in-the-terminal-path"),
])
def test_install_refuses_what_it_cannot_install_and_creates_nothing(tmp_path, break_it, expected):
    """带 -Install 跑：任何一项不对都拒绝——任务计划程序一次都没有被碰（连查询都没有），不建目录、
    不写文件；原因里点名是哪一项，缺的一次列全。"""
    deploy, terminal, data, modules = sandbox(tmp_path)
    flags = located(deploy, terminal, data)
    if break_it == "no-terminal":
        flags = flags[1:]
    elif break_it == "none":
        flags = []
    elif break_it == "relative":
        flags[0] = "-TerminalPath 'terminal64.exe'"
    elif break_it == "other-name":
        other = terminal.with_name("notepad.exe")
        other.write_bytes(b"")
        flags[0] = f"-TerminalPath {quote(other)}"
    elif break_it == "no-terminal-file":
        flags[0] = f"-TerminalPath {quote(terminal.parent / 'missing' / 'terminal64.exe')}"
    elif break_it == "no-deploy":
        flags[1] = f"-DeployDir {quote(tmp_path / 'no such deploy')}"
    elif break_it == "no-data":
        flags[2] = f"-DataDir {quote(tmp_path / 'no such data')}"
    elif break_it == "no-keeper":
        (deploy / "scripts" / "ops" / "terminal_keeper.ps1").unlink()
    elif break_it == "no-launcher":
        (deploy / "scripts" / "ops" / "hidden_task.vbs").unlink()
    elif break_it == "root":
        flags[2] = f"-DataDir {quote(tmp_path.anchor)}"
    elif break_it == "percent":
        (tmp_path / "100% deploy").mkdir()
        flags[1] = f"-DeployDir {quote(tmp_path / '100% deploy')}"
    elif break_it == "quote":
        flags[1] = f"-DeployDir {quote(str(tmp_path / 'a') + chr(34) + 'b')}"
    elif break_it == "quote-terminal":
        flags[0] = f"-TerminalPath {quote(str(terminal) + chr(34))}"
    done, before, calls = run(tmp_path, modules, *flags, "-Install")
    assert done.returncode == 1, text(done)
    assert calls == [] and entries(tmp_path) == before
    out = text(done)
    assert "INSTALLER-ERROR: 拒绝" in out
    for fragment in expected:
        assert fragment in out, fragment


WESTERN = (1252, 1252)
SPLIT = (1252, 437)


@requires_powershell
def test_paths_follow_the_system_code_pages_like_the_other_installer(tmp_path):
    """和 setup_scheduled_tasks.ps1 同一条规矩。两个代码页相同：这个代码页表示不了的字符拒绝，
    表示得了的照常；两个代码页不同：带非 ASCII 字符的路径一律拒绝。"""
    deploy, terminal, data, modules = sandbox(tmp_path, "d\xe9ploy")          # é：1252 表示得了
    done, before, calls = run(tmp_path, modules, *located(deploy, terminal, data), "-Install", code_pages=WESTERN)
    assert done.returncode == 0 and names(calls) == ["Get-ScheduledTask", "Register-ScheduledTask"], text(done)

    plain = tmp_path / "ascii deploy"
    shutil.copytree(deploy, plain)
    done, before, calls = run(tmp_path, modules, *located(plain, terminal, data), "-Install", code_pages=WESTERN)
    assert done.returncode == 0 and names(calls) == ["Get-ScheduledTask", "Register-ScheduledTask"], text(done)

    before = entries(tmp_path)
    done, _, calls = run(tmp_path, modules, *located(deploy, terminal, data), "-Install", code_pages=SPLIT)
    assert done.returncode == 1 and "OEM 437" in text(done)
    assert calls == [] and entries(tmp_path) == before

    chinese = tmp_path / "部署 tree"
    shutil.copytree(plain, chinese)
    before = entries(tmp_path)
    done, _, calls = run(tmp_path, modules, *located(chinese, terminal, data), "-Install", code_pages=WESTERN)
    assert done.returncode == 1 and "1252" in text(done)
    assert calls == [] and entries(tmp_path) == before


@requires_powershell
def test_uninstall_only_unregisters_the_task_and_leaves_the_log(tmp_path):
    """-Uninstall：先查，在才注销（替身记下 -Confirm:$false），不建、不删任何文件；没有安装时什么都
    不做，退出码 4。任务路径和名字可以指定；任务路径首尾各一个反斜杠。"""
    deploy, terminal, data, modules = sandbox(tmp_path)
    log = data / "ops-evidence" / "terminal_keeper" / "terminal_keeper.log"
    log.parent.mkdir(parents=True)
    log.write_text("2026-11-24T00:00:00.000Z\tstarted pid=1 other_instances=0\n", encoding="utf-8")
    kept = log.read_bytes()
    tasks_before = real_tasks(tmp_path)
    before = entries(tmp_path)
    done, _, calls = run(tmp_path, modules, "-Uninstall", *located(deploy, terminal, data))
    assert done.returncode == 4, text(done)
    assert names(calls) == ["Get-ScheduledTask"] and "没有安装" in text(done)

    done, before, calls = run(tmp_path, modules, "-Uninstall", *located(deploy, terminal, data), exists=True)
    assert done.returncode == 0, text(done)
    assert real_tasks(tmp_path) == tasks_before
    assert names(calls) == ["Get-ScheduledTask", "Unregister-ScheduledTask"]
    assert calls[1] == {"call": "Unregister-ScheduledTask",
                        "parameters": {"Confirm": False, "TaskName": "terminal_keeper", "TaskPath": "\\MT5Quant\\"},
                        "rest": []}
    assert str(log) in text(done) and log.read_bytes() == kept
    assert entries(tmp_path) == before

    done, _, calls = run(tmp_path, modules, "-Uninstall", "-TaskPath 'Ops'", "-TaskName 'keeper 2'", exists=True)
    assert done.returncode == 0, text(done)
    assert calls[1]["parameters"]["TaskPath"] == "\\Ops\\" and calls[1]["parameters"]["TaskName"] == "keeper 2"


@requires_powershell
def test_status_reads_and_changes_nothing(tmp_path):
    """-Status：只读。没有安装：state 是 not-installed，退出码 4。安装了：状态、上次运行、上次结果，
    时刻是 UTC。两种情况都只查询。"""
    deploy, terminal, data, modules = sandbox(tmp_path)
    before = entries(tmp_path)
    done, _, calls = run(tmp_path, modules, "-Status")
    assert done.returncode == 4, text(done)
    assert names(calls) == ["Get-ScheduledTask"]
    assert status(done) == [{"task_path": "\\MT5Quant\\", "task_name": "terminal_keeper", "state": "not-installed"}]
    assert state_lines(done) == []                                 # 没给 -DataDir：不看状态文件

    done, _, calls = run(tmp_path, modules, "-Status", *located(deploy, terminal, data))
    assert done.returncode == 4, text(done)
    assert state_lines(done) == [{"state_file": "missing"}]

    done, _, calls = run(tmp_path, modules, "-Status", *located(deploy, terminal, data), exists=True, last_result=267009)
    assert done.returncode == 0, text(done)
    assert names(calls) == ["Get-ScheduledTask", "Get-ScheduledTaskInfo"]
    assert calls[1]["parameters"] == {"TaskName": "terminal_keeper", "TaskPath": "\\MT5Quant\\"}
    (line,) = status(done)
    assert line == {"task_path": "\\MT5Quant\\", "task_name": "terminal_keeper", "state": "Ready",
                    "last_run_time": "2026-11-24T12:00:00Z", "last_task_result": 267009,
                    "next_run_time": "2026-11-24T12:01:00Z", "missed_runs": 0}
    assert entries(tmp_path) == before


@requires_powershell
def test_status_also_shows_the_keeper_s_latest_state_line_without_touching_it(tmp_path):
    """给了 -DataDir，-Status 多打印一行 KEEPER_STATE：看护脚本状态文件里最近的一条——装完当场就能看到
    看护有没有跑成。只读：文件原样。outcome、reason 只有长得像标识符的才照原样打印，其余写 other。"""
    deploy, terminal, data, modules = sandbox(tmp_path)
    path = data / "ops-evidence" / "terminal_keeper" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"ts": "2026-11-24T12:00:00.000Z", "outcome": "started", "reason": "",
                                "running": 0, "others": 0, "pid": 4321}), encoding="utf-8")
    kept = path.read_bytes()
    done, _, calls = run(tmp_path, modules, "-Status", *located(deploy, terminal, data), exists=True)
    assert done.returncode == 0, text(done)
    assert names(calls) == ["Get-ScheduledTask", "Get-ScheduledTaskInfo"] and path.read_bytes() == kept
    (line,) = state_lines(done)
    assert line == {"ts": "2026-11-24T12:00:00Z", "outcome": "started", "reason": None,
                    "age_s": line["age_s"]} and isinstance(line["age_s"], int) and line["age_s"] >= 0

    path.write_text(json.dumps({"ts": "2026-11-24T12:00:00.000Z", "outcome": "refused", "reason": str(terminal)}),
                    encoding="utf-8")
    done, _, calls = run(tmp_path, modules, "-Status", *located(deploy, terminal, data), exists=True)
    (line,) = state_lines(done)
    assert line["outcome"] == "refused" and line["reason"] == "other" and str(terminal) not in text(done)

    path.write_text("{not json", encoding="utf-8")
    done, _, calls = run(tmp_path, modules, "-Status", *located(deploy, terminal, data), exists=True)
    assert state_lines(done) == [{"state_file": "unreadable"}] and done.returncode == 0


@requires_powershell
@pytest.mark.parametrize("break_it", [
    pytest.param("off-the-path", id="sandbox-module-taken-off-the-path"),
    pytest.param("one-stand-in-not-exported", id="a-stand-in-not-exported"),
    pytest.param("new-not-exported", id="new-not-exported"),
    pytest.param("new-impersonated", id="a-plain-function-impersonating-new"),
])
def test_the_guard_stops_the_run_when_the_sandbox_is_not_what_it_must_be(tmp_path, break_it):
    """核对自己的变异验证。四种残缺的沙箱：驱动以 99 退出、说出是哪一项，安装脚本一步都没跑
    （没有它的输出，没有记录下来的调用）——失败，不是跳过。安装脚本的参数是 -Install -Uninstall：
    万一核对没拦住，它也因为两个开关互斥而拒绝，碰不到任务计划程序。"""
    if break_it == "off-the-path":
        deploy, terminal, data, modules = sandbox(tmp_path)
        shutil.rmtree(modules)                                     # the directory stays, the module is gone
        modules.mkdir()
    else:
        change = {
            "one-stand-in-not-exported": "Export-ModuleMember -Function ($news + @($standins | Where-Object { $_ -ne 'Start-ScheduledTask' }))",
            "new-not-exported": "Export-ModuleMember -Function $standins",
            "new-impersonated": "function New-ScheduledTaskAction { 'not a task object' }\n" + EXPORT_FUNCTIONS,
        }[break_it]
        deploy, terminal, data, modules = sandbox(tmp_path, module_text=STANDINS.replace(EXPORT_FUNCTIONS, change))
    expected = {"off-the-path": "cannot be imported", "one-stand-in-not-exported": "answers for Start-ScheduledTask",
                "new-not-exported": "answers for New-ScheduledTask", "new-impersonated": "New-ScheduledTaskAction"}
    done, before, calls = run(tmp_path, modules, *located(deploy, terminal, data), "-Install", "-Uninstall")
    out = text(done)
    assert done.returncode == 99, out
    assert f"STAND-IN GUARD FAILED (before): " in out and expected[break_it] in out, out
    for absent in ("KEEPER_", "INSTALLER-ERROR", "STAND-IN GUARD OK"):
        assert absent not in out, absent
    assert calls == [] and entries(tmp_path) == before
