> **留档说明（第二十二轮 §2.3-5 / D102）**：本文是 2026-11-19 僵局恢复时 codex
> （gpt-6.2-astra/max）产出、用户授权主控执行的恢复决策执行单的留档副本。原件在
> `mt5-external-handoff-20261119/recovery-decision.md`（本机 handoff 目录，不入库），
> sha256 `ad521a0e853c9d3288a9a5ac310fdde1bafd9f5fe3b411b39e4222054dbdfd55`。
> 脱敏核验（2026-11-20）：repo hygiene 五类模式零命中、无本机绝对路径、无 7 位以上
> 数字串——正文与原件逐字节一致，仅添加本说明段。执行结果见 phase-f-log 11-19 节；
> 生产包装器留档见 docs/ops/ops-patches/2026-11-19/。

# MT5 Demo 恢复决策 · 2026-11-19 · D64 主控执行单

授权范围为两个 Demo magic、同一终端和固定候选 `cd6fc4a`。下列命令供主控在 Windows PowerShell 中分段执行，每项核验完成再进入下一项。外部审核只做了文件、任务、操作系统进程的只读检查和临时目录内的空载启动试验。没有连接交易终端或执行恢复。时间均为 UTC。

## 1. 恢复 risk、reconcile 和 supervisor

**决策。** 立即保护现有敞口。正常补缺用 §6.2 和完整部署身份。现有机器的只读核验已拒绝，走下文的受控迁移分支。本次维护内使一个每分钟监督任务生效，正式起算前完成两项演练。保留固定树和它的原版身份校验器。

**依据。** `bbf021f` 只追加了日志。11-17 06:25–06:29 的原始告警是 `process script/arguments mismatch`，没有记录哪一个参数。固定树的 `scripts/live_supervisor.py` 的 `command` 和 `scripts/demo_run.ps1` 给 risk 的都是 `--config`，给 runner 的都是 `--risk-config`。传给 supervisor 自身的是 `--risk-config`。11-19 14:05 的本机复核中，四条登记 argv 全部匹配。以下是当时的操作系统身份，实施前重新读取。

| 登记角色 | 只读实测 |
| --- | --- |
| 902501 | 启动器 8820、Python worker 9008 参数和创建时间吻合；另有子进程 13224 `conhost.exe` |
| 902610 | 启动器 17832、Python worker 17860 参数和创建时间吻合；另有子进程 17428 `conhost.exe` |
| risk | 原启动器 10348 已退出。同号现为 11-18 05:43:23 启动的 `ctfmon.exe`；退出 marker 的 14060 是旧 Python worker |
| reconcile | 原启动器 17736 的业务树已不存在；退出 marker 的 4080 是旧 Python worker |

`verified_tree` 校验每个后代；两个控制台宿主都会使它拒绝，risk 的 PID 复用会独立造成拒绝。不能用今天的进程清单反推 11-17 06:25 当时一定是哪一个后代。标准任务 `\MT5Quant\live_supervisor` 缺失，根目录另有已禁用的 `\MT5Quant-supervisor`，上次结果为 2。

**执行与验证。**

**1a　固定参数、保全、保护。** 从主工作树根执行。`$e` 供主控保存非公开原件及本次任务输出；不要把配置凭据、账号或机器路径写入公开日志。

~~~powershell
$ErrorActionPreference = 'Stop'
$m = (Resolve-Path .).Path
$d = (Resolve-Path ..\demo-deploy-cd6fc4a).Path
$sha = 'cd6fc4aaac7d3886be0fbdfa4f437345b6b1b8c5'
$py = Join-Path $m '.venv\Scripts\python.exe'
$data = Join-Path $m 'data'
$live = Join-Path $data 'live'
$e = Join-Path $data ('ops-evidence\recovery-' + [datetime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))
New-Item -ItemType Directory -Path $e | Out-Null
if ((git -C $d rev-parse HEAD) -ne $sha) { throw 'SHA mismatch' }
git -C $d symbolic-ref -q HEAD
if ($LASTEXITCODE -ne 1) { throw 'deployment must be detached' }
$dirty = git -C $d status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'deployment must be clean' }
$a = @{
  Mode='Demo'; DeploymentSha=$sha; DataDir=$data
  ConfigDir=(Join-Path $data 'demo-configs-consolidated'); PythonExe=$py
  GatewayConfig=(Join-Path $m 'gateway.json'); RiskConfig=(Join-Path $d 'config\risk.toml')
  SessionsConfig=(Join-Path $d 'config\sessions.toml'); SpreadModel=(Join-Path $data 'costs\spread_model.json')
  ResearchCache=(Join-Path $m 'data_research\costs\spread_model.json'); LogDir=(Join-Path $data 'demo')
}
$sv = @('-u',"$d\scripts\live_supervisor.py",'--directory',$d,'--sha',$sha,
  '--data-dir',$data,'--config-dir',$a.ConfigDir,'--python',$py,'--gateway-config',$a.GatewayConfig,
  '--risk-config',$a.RiskConfig,'--sessions-config',$a.SessionsConfig,
  '--spread-model',$a.SpreadModel,'--log-dir',$a.LogDir)
$markers = 'instances.json','run-manifest.json','exit-risk.json','exit-reconcile.json',
  'disconnect-exit-risk.json','disconnect-exit-reconcile.json','pause-902610.json','pause-902501.json',
  'heartbeat-risk.json','heartbeat-reconcile.json','supervisor-state.json','halt.json','halt-risk-veto.json'
foreach ($name in $markers) {
  $src = Join-Path $live $name
  if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination $e }
}
Get-ChildItem -File -LiteralPath $e | Get-FileHash -Algorithm SHA256 |
  Export-Csv -NoTypeInformation -LiteralPath "$e\original-hashes.csv"
Get-ScheduledTask | Where-Object { $_.TaskPath -eq '\MT5Quant\' -or $_.TaskName -match 'supervisor|deploy-consolidated' } |
  Select-Object TaskPath,TaskName,State | Tee-Object -FilePath "$e\tasks.txt"
$reg = (Get-Content -Raw -Encoding UTF8 "$live\instances.json" | ConvertFrom-Json).instances
Get-CimInstance Win32_Process | Where-Object {
  $_.ProcessId -in $reg.pid -or $_.ParentProcessId -in $reg.pid
} | Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine |
  Format-List | Out-File "$e\processes.txt"
$ks = Join-Path $live 'killSwitch'
if (-not (Test-Path -LiteralPath $ks)) {
  New-Item -ItemType File -Path $ks | Out-Null
  'created by this maintenance' | Set-Content -LiteralPath "$e\killSwitch-owner.txt"
}
& $py @sv --inspect
$inspectRC = $LASTEXITCODE
~~~

主控在专用终端 GUI 确认连接的是预期 Demo，逐 magic 记录实际持仓、挂单、券商侧 SL/TP；特别核实 902501 的 0.01 手。`killSwitch` 阻止新开仓，不会平掉已有仓位。采集原 alerts、各角色 stdout/stderr、平台 Journal 相应时段和调度分支的三日日检原件，保留原始时间。`halt.lock` 是互斥锁；保护状态看 `halt.json` 等实际信号。任务和身份变动要重核。

**1b　选择补缺或迁移。** 若 `--inspect` 返回 0、`refused=false`，且 `alive` 恰有两个登记 runner，使用 1d 的一次性任务调用 §6.2，保留现有登记、暂停、持仓、journal 和 expected。输出应显示跳过两个原 runner，仅启动 risk（10 秒）及 reconcile（30 秒）。若四个均活，核验推进情况即可。若出现未解释的另一组身份，用清单查明。出现 1a 实测的 rc=2 时，不执行自动补缺、普通 supervisor tick 或 `--stop`。以下是这台机器的迁移路径。

1. 留住 `killSwitch` 和 902610 暂停。先按 §2 在正确的 Demo 终端打开全局 Algo Trading，并取得 `trade_allowed=true`、`tradeapi_disabled=false` 的交互界面/既有遥测证据。全局开关关闭时连平仓也可能收到 10027，所以不要在开关关闭时写平仓指令。开关确认后，趁可成交时段通过仍活着的两个 runner 执行一次有编号的退出；在已有未完成指令时先核清它。以下只写文件，出场仍经已部署的网关。核对两个相同 id 的 `commands\flatten.ack-902610`、`flatten.ack-902501` 或 `drill-archive\<id>\` 中的原件。GUI 复核整个账户所有 magic 都为零持仓、零挂单，journal 没有未裁定的 pending/sent/unknown。若有部分成交、外部退出待仲裁或关市，保持保护，按 runbook §5 处理。旧 risk 已死时原样留住指令和 ACK，待旧数据目录上的 risk 恢复后自动归档。记录实际成交费用；回滚不重建旧仓。

~~~powershell
@'
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
from mt5live.commands import write_flatten
print(write_flatten(sys.argv[2], reason="recovery-2026-11-19", now=datetime.now(timezone.utc)).id)
'@ | & $py - $d $live
if ($LASTEXITCODE -ne 0) { throw 'flatten command not confirmed' }
~~~

2. 由 D64 做**一次性旧树退役**。先导出/停用针对这份 DataDir 的所有拉起任务，确认没有正在执行的部署或 supervisor。分别核验每个 runner 的启动器、worker、系统控制台宿主三者的完整映像路径、创建时间和直接父子关系。以固定 supervisor 的 `command`、`_identity` 核验两个 Python 进程，worker PID 还须等于该 magic 新鲜心跳的 PID。控制台宿主须为本机微软签名的 `%SystemRoot%\System32\conhost.exe`，且没有子进程。全系统扫描确认没有相同业务参数的孤儿/重复进程；确认 risk、reconcile 两个旧 worker 确已退出。任何额外或不可读身份均中止。**只对这两棵已核实的树发正常中断**，可用下列控制台操作。这个分支供 D64 人工迁移，不改变 supervisor 对 PID 复用和未知后代的一般拒绝规则。

   将下面的 Python 段保存为 `$e\interrupt-console.py`。在两个 ACK、GUI 空仓核验、上述身份核验、例行任务停用后，分别执行 `& $py "$e\interrupt-console.py" $d $live 902501`、`& $py "$e\interrupt-console.py" $d $live 902610`。每次检查退出码，再确认这一个 runner 的 worker 和 launcher 自行退出。该段向专属控制台发送与 Ctrl+C 相同的事件，不结束系统进程。

~~~python
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

d, live = map(Path, sys.argv[1:3])
magic = int(sys.argv[3])
if magic not in (902501, 902610) or not (live / "killSwitch").is_file():
    raise SystemExit("wrong magic or maintenance protection absent")
sys.path.insert(0, str(d / "scripts"))
import live_supervisor as s

def require(ok, reason):
    if not ok:
        raise SystemExit(reason)

def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))

m = live.parent.parent
cfg = s.Deployment(str(d), "cd6fc4aaac7d3886be0fbdfa4f437345b6b1b8c5",
                   str(live.parent), str(live.parent / "demo-configs-consolidated"),
                   sys.executable, str(m / "gateway.json"), str(d / "config/risk.toml"),
                   str(d / "config/sessions.toml"), str(live.parent / "costs/spread_model.json"),
                   str(live.parent / "demo"))
wp = s.WindowsProcesses()
with s.registry_lock(str(live / "instances.lock")):
    s.verify_deployment(cfg)
    entries = [e for e in read(live / "instances.json")["instances"]
               if e.get("kind") == "runner" and e.get("magic") == magic]
    require(len(entries) == 1, "ambiguous registration")
    e = entries[0]
    args = s.command(cfg, s.components(cfg)[str(magic)])
    require(e["mode"] == "Demo" and e["deployment_sha"] == cfg.sha
            and s.canonical(e["deployment_dir"]) == s.canonical(d)
            and s.canonical(e["data_dir"]) == s.canonical(live.parent)
            and s._argv_matches(e["argv"], args), "registry differs")
    table = wp.snapshot()
    root = table[e["pid"]]
    hb = read(live / f"heartbeat-{magic}.json")
    worker = table[hb["pid"]]
    require(hb["deployment_sha"] == cfg.sha and worker["parent_pid"] == root["pid"],
            "heartbeat/worker differs")
    s._identity(root, e, args)
    s._identity(worker, {**e, "started_utc": root["started_utc"]}, args)
    require(s.canonical(root["executable"]) == s.canonical(cfg.python)
            and s.canonical(worker["executable"]) == s.canonical(sys._base_executable),
            "Python image differs")
    matches = {p["pid"] for p in table.values() if s._argv_matches(p["argv"], args, worker=True)}
    require(matches == {root["pid"], worker["pid"]}, "unexpected Python participants")
    siblings = [p for p in table.values() if p["parent_pid"] == root["pid"] and p["pid"] != worker["pid"]]
    require(len(siblings) == 1, "unexpected child count")
    host = siblings[0]
    require(s.canonical(host["executable"]) == s.canonical(Path(os.environ["SystemRoot"]) / "System32/conhost.exe")
            and s.parse_iso(host["started_utc"]) >= s.parse_iso(root["started_utc"]),
            "unexpected console host")
    require(not any(p["parent_pid"] in {host["pid"], worker["pid"]} for p in table.values()),
            "unexpected descendants")
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.FreeConsole()
    require(k.AttachConsole(root["pid"]), "cannot attach to the dedicated console")
    try:
        pids = (wintypes.DWORD * 16)()
        n = k.GetConsoleProcessList(pids, len(pids))
        require(n == 3 and set(pids[:n]) == {root["pid"], worker["pid"], os.getpid()},
                "console is shared or membership has changed")
        require(k.SetConsoleCtrlHandler(None, True), "cannot protect signalling process")
        require(k.GenerateConsoleCtrlEvent(0, 0), "interrupt not delivered")
        time.sleep(0.5)
    finally:
        k.FreeConsole()
~~~

   从独立 PowerShell 用 `Get-AuthenticodeSignature -LiteralPath "$env:SystemRoot\System32\conhost.exe"` 核实签名。中断没有成功、或任一业务进程仍在时保持保护并记录，禁止改登记。两棵树都确认消失后，再查整个系统的脚本路径、DataDir、完整命令行和创建时间，包含已换 PID 的孤儿。原 risk PID 所指的 `ctfmon.exe` 保留原状。核验完成才按 11-04 的迁移先例将 `instances.json` 原字节归档，保留 SHA256 和迁移单；在部署/监督任务停用、无并发写手的维护段用 `Move-Item -LiteralPath "$live\instances.json" -Destination "$e\instances-retired.json"`。其余旧状态留在原位。此时原版 `& $py @sv --inspect` 必须返回 0 且 `alive={}`。出现未登记 worker 就中止。

**1c　修正运维启动方式。** 在固定树外准备一份可审查的 §6.2 启动器。业务代码、进程参数、网关预检、完整树校验、T180 留存均取固定树原版。本机空载实测，原 `Start-Process -WindowStyle Hidden` 产生 conhost 子进程；专用隐藏任务宿主内用 `-NoNewWindow`，得到 venv launcher → Python worker，stdout/stderr 均成功重定向。分别用隐藏控制台宿主和无窗口宿主测过。正式采用前，主控用同一计划任务主体跑空载程序，核对任务宿主退出后 worker 仍工作、退出码及两路输出可读。下段生成外部候选，核对 diff 后采用。若部署政策要求改动合入版本才能上线，或空载/实际身份核验失败，保留停机，交运维工具评审。

~~~powershell
$stock = Join-Path $d 'scripts\demo_run.ps1'
$entry = Join-Path $e 'demo_run_ops.ps1'
$t = [IO.File]::ReadAllText($stock)
$rootLine = '$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path'
$changes = [ordered]@{
  $rootLine = ('$RepoRoot = (Resolve-Path -LiteralPath ' + "'" + $d.Replace("'","''") + "'" + ').Path')
  'Join-Path $PSScriptRoot "config_check.py"' = 'Join-Path $RepoRoot "scripts\config_check.py"'
  'Join-Path $PSScriptRoot "save_run_evidence.ps1"' = 'Join-Path $RepoRoot "scripts\save_run_evidence.ps1"'
  '-WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru' = '-WorkingDirectory $RepoRoot -NoNewWindow -PassThru'
}
foreach ($old in $changes.Keys) {
  if ([regex]::Matches($t,[regex]::Escape($old)).Count -ne 1) { throw "unexpected source: $old" }
  $t = $t.Replace($old,$changes[$old])
}
# BOM 使本机 Windows PowerShell 5.1 正确读取原脚本中的中文。
[IO.File]::WriteAllText($entry,$t,[Text.UTF8Encoding]::new($true))
git diff --no-index -- $stock $entry
Get-FileHash -Algorithm SHA256 -LiteralPath $stock,$entry
~~~

**1d　用计划任务部署。** 两个分支共用这一段。用刚审核的外部启动器解决实际启动形态；若使用未经修改的固定树启动器，则须先在相同任务宿主证明其形态能过原版 `--inspect`。禁止直接在 agent shell 中启动业务。部署前 GUI 核实 Demo、检查 `allow_live_trading=false`、VENV/配置齐全、风控 500 美元和两份配置 hash；部署器会自行跑规定的 Demo 网关预检，须见 `trade_mode=0`。不要传 `-SkipGatewayCheck`。每次尝试分配新文件名和任务名，避免覆盖前次日志。注册的任务不带自动触发器，只手动启动一次。

~~~powershell
$attempt = Join-Path $e ('start-' + [datetime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))
New-Item -ItemType Directory -Path $attempt | Out-Null
$a | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath "$attempt\parameters.json"
$entry | Set-Content -Encoding UTF8 -LiteralPath "$attempt\entry.txt"
@'
$ErrorActionPreference = 'Stop'
$o = Get-Content -Raw -Encoding UTF8 -LiteralPath "$PSScriptRoot\parameters.json" | ConvertFrom-Json
$p = @{}
foreach ($v in $o.PSObject.Properties) { $p[$v.Name] = $v.Value }
$target = (Get-Content -Raw -Encoding UTF8 -LiteralPath "$PSScriptRoot\entry.txt").Trim()
& $target @p *> "$PSScriptRoot\deploy.log"
exit $LASTEXITCODE
'@ | Set-Content -Encoding UTF8 -LiteralPath "$attempt\start.ps1"
$acct = [Security.Principal.WindowsIdentity]::GetCurrent().Name
# 在已核实与专用终端相同的交互用户下注册。
$pr = New-ScheduledTaskPrincipal -UserId $acct -LogonType Interactive -RunLevel Highest
$act = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $d -Argument (
  '-NoProfile -NonInteractive -WindowStyle Hidden -File "' + "$attempt\start.ps1" + '"')
$one = 'recover-' + [datetime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
Register-ScheduledTask -TaskPath '\MT5Quant\' -TaskName $one -Action $act -Principal $pr |
  Out-Null
Start-ScheduledTask -TaskPath '\MT5Quant\' -TaskName $one
~~~

主控轮询 `Get-ScheduledTaskInfo -TaskPath '\MT5Quant\' -TaskName $one` 和 `Get-Content -Tail 30 "$attempt\deploy.log"`。须检查本轮实际运行时刻、任务完成码 0、四条完整进程链、父链属于计划任务、原版 `--inspect` 返回四个角色、四个完成心跳均带本 SHA/正确 worker PID 且持续增加、相应日志推进。每次都按**本次**登记认 PID。迁移分支应在保留的旧 DataDir 中先恢复保护，确认旧 flatten 的归档、有两个真实 ACK、reconcile 连续两轮 `reconcile_clean=true`。这次恢复只能作维护记录。

**1e　安装每分钟 supervisor。** 旧的根目录任务留在 Disabled，若标准任务已存在则先导出 XML 并核对，不重复注册。正式重部署前禁用并等当前一轮结束，F-8 记录写好后再启用。原版 `--inspect` 绿后执行。

~~~powershell
$q = ($sv | ForEach-Object { '"' + $_ + '"' }) -join ' '
$sa = New-ScheduledTaskAction -Execute $py -Argument $q -WorkingDirectory $d
$tr = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$st = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskPath '\MT5Quant\' -TaskName live_supervisor -Action $sa -Trigger $tr -Settings $st -Principal $pr
~~~

连续检查 `Get-ScheduledTaskInfo -TaskPath '\MT5Quant\' -TaskName live_supervisor`、`supervisor-state.json`、四个心跳及各自日志 mtime。要求连续健康 ≥300 秒，相邻检查间隔 ≤90 秒，对账干净、没有在途意图，无新增 `SUPERVISOR_REFUSED`/`SUPERVISOR_EVIDENCE`。“任务存在”不足以证明守护生效。维护期间若保留 killSwitch，其对应风险 halt 可预期；只在归属及原因核清、敞口与风险通过核对后退出维护，先移本次的 killSwitch，再按 owner/reason 处置本次 `risk/kill_switch` halt。Supervisor 自有 halt 等 token 匹配后由它自动解除。其他 halt、风险 veto 和人工暂停各自单独裁定。

**回滚。** 新增任务先 `Disable-ScheduledTask -TaskPath '\MT5Quant\' -TaskName live_supervisor`，等当次结束。保留保护和取证。若启动半成功，先重读登记并 `--inspect`，保持 killSwitch；身份完整时用 `& $py @sv --stop` 核验整树退出。只有全死且无孤儿时才可迁移原件。保留旧任务禁用状态、旧仓平仓记录、journal、expected、consumed 和新旧证据，不用旧 PID 清单恢复机器。

## 2. Algo Trading 与解除 902610 暂停

**决策与依据。** 11-17 12:30、12:45、13:00 三个 M15 bar 被拒，`pause-902610.json` 记录 13:00:05Z、`streak=3`、`last_retcode=10027`。它只挡该策略的开仓。902501 的已有持仓不能证明终端现在允许新单。官方把 10027 定义为客户端禁止自动交易。已安装的 MetaTrader5 Python 5.0.6231 有 `terminal_info()`，没有设置该开关的公开函数。专用终端映像对应数据目录的 `common.ini` 上次落盘有 `[Experts] Enabled=0`、`Api=0`；平台 Journal 有 automated trading disabled 记录。INI 的值只作旁证。

**步骤。** 保持 902610 暂停，在 Demo 账户、正确终端路径和窗口均确认后，用交互桌面或 Windows UI Automation 操作工具栏 Algo Trading，先读状态再点一次；也可在“工具 → 选项 → EA 交易”设置。检查“允许算法交易”已勾选、“禁用通过外部 Python API 的自动交易”未勾选，保留切换账户、配置文件时的自动禁用保护。终端全局开关会作用于所有 EA；核实是专用终端。若主控不在交互桌面，交给能看到该 Demo 窗口的操作者。禁止盲发切换快捷键。向主控回报 GUI 状态和 Journal 的新时间；若原有合规遥测已提供，可交叉验证 `connected=True`、`trade_allowed=True`、`tradeapi_disabled=False`。D82 不开新的 Python/MT5 查询或测试单。

平台支持 `terminal64.exe /config:<绝对路径>` 读取基于 `common.ini` 的自定义启动配置，官方列出 `[Experts] Enabled` 与 `AllowLiveTrading` 等 EA 项。文件在该次运行中只读。仅作受控维护备选：停止全部交易客户端、正常退出这一终端、留存原配置/ACL/哈希，在用户私有目录做副本，保留 Demo 身份与保护项，再用相同 Windows 用户和相同 terminal64 启动，例如 `Start-Process -FilePath $terminalExe -ArgumentList ('/config:"' + $privateIni + '"')`。核验只有该实例、预期 Demo、GUI 和 Journal；外部 Python 权限也要单独核验。不要热改在用 INI，或凭未文档化的 `Api` 值推断运行状态。这个 INI 的 `AllowLiveTrading` 和项目网关的 `allow_live_trading` 是两层完全不同的设置，后者始终为 false。

**解暂停与验证。** 位次为：终端允许 → risk/reconcile 已健康且对账一致 → 查 journal、真实敞口/挂单、独立 `reconcile-block-902610.json`、全局保护 → 在项目同一文件锁内归档这个特定拒单暂停。取证时核对当前原件仍是 11-17 那份；若内容被更新，重新判定。下面用于保留运行目录的恢复。若做全平后干净重部署，该暂停随退役状态归档，等价的验证须在新部署允许开仓前完成。

~~~powershell
@'
import hashlib, json, os, sys
from pathlib import Path
d, live, ev = map(Path, sys.argv[1:4])
sys.path.insert(0, str(d / "src"))
from mt5live.fileio import file_lock
p = live / "pause-902610.json"
dest = ev / "pause-902610.released.json"
with file_lock(str(p) + ".lock"):
    raw = p.read_bytes()
    old = (ev / "pause-902610.json").read_bytes()
    x = json.loads(raw)
    if raw != old or x.get("magic") != 902610 or x.get("paused") is not True or x.get("reason") != "order_rejected_streak" or x.get("last_retcode") != 10027:
        raise SystemExit("pause changed: keep it")
    if dest.exists() or (live / "reconcile-block-902610.json").exists():
        raise SystemExit("inspect current protection")
    os.rename(p, dest)   # 同卷 Windows rename；目标已存在会失败
    print(hashlib.sha256(raw).hexdigest())
'@ | & $py - $d $live $e
if ($LASTEXITCODE -ne 0) { throw 'pause release stopped' }
~~~

跟踪随后 M15 的正常决策；等首个自然、应交易信号，查其订单、成交/确定拒绝、归因和对账，确认无新 10027 和重新暂停。没有信号就如实记录“待自然信号验证”。902501 单独核对，绝不清它的 pause、其他保护或 journal 来替 902610 放行。

**回滚。** 出现 10027、配置/账户有变或对账不符，立即保留或重设 902610 的人工开仓暂停，在 `pause-902610.json.lock` 内操作，保留任何新写入的保护；若无法区分先建 killSwitch。归档的旧暂停只在已核对、目标为空时原样恢复。若恢复修改了终端设置，停开仓并还原已记录的 GUI 设置，或退出自定义配置的终端，以原配置再启动。

## 3. 窗口裁定与新日期

**决策。** 截至本次核验，正式有效交易日 **0/10**。主控在 `docs/STATUS.md` 和 `docs/ops/phase-f-log.md` 追加裁定，保留调度原文。11-17、18、19 的 D1/D2/D3 是调度标签。第十九轮曾计划 D1=11-17；第二十一轮 §1 取代其时间表，要求烧机后第一个完整交易日、原拟 11-18。主会话本地 `28505a9` 还记载 06:07 首部署、10:44 四进程死亡和重新计时，日检所见的 11:55 是另一批启动器。第二次烧机又出现 pause 和两个非演练退出，资格没有成立。即使采纳日检的 D1，按第十八轮 §5 仍须判失败。

**依据与步骤。** 两份 disconnect marker 的段首分别为 20:48:30/40，实测连续失败 901.297/900.453 秒，随后 `exit-risk` 为 21:03:31Z、`exit-reconcile` 为 21:03:41Z，均 rc=78。11-19 日检根据平台日志将整段勘误为 20:48→23:26，9,480 秒。T141 正常退出、T201 保住两个 runner；监督/对账证据和 902610 样本无法回填。按第十八轮 §1 第 5 项和 D92，长断线 rc=78 可同 SHA 重试；前提是启动载体问题已按 §1 收敛。若查出必须修改交易业务代码，停止此路径，另行定版、影子盘、部署。真正发生两次连续同类环境失败时停报外审，不把同一事故的两份 marker 或三次日检算成三次失败。

**验证与回滚。** 11-19 的 12:00 UTC 截止已过；晚上 21:55–23:00 可维护，距当日 24:00 不满 12 小时。建议 11-20 可交易时段完成旧仓清理，**11-22 周日 09:00 UTC（休市）**作拟定全量部署时刻，12:00 前写完新记录。贯穿周日换日窗和重开，核验真实可交易时段的 M15/H1 决策及 902501 首个合格 H1 的 0.01 手持仓。周末无报价的小时不能替代这项证据。满足烧机、演练及 D1 试跑后，最早候选 D1=11-23，D10=12-08，11-26/27 计 holiday；按现行日报节奏预计 12-09 21:57 后正式检查。Windows 更新暂停目前至 12-05，主控在新锚点之前把 D85 的防重启安排延至最终检查结束并记录（A5 后恢复既定设置）。错过截止、烧机或试跑失败就重选合格锚点，按实际 UTC 与日历重新计算。

## 4. 正式重部署、演练与交付

**决策与步骤。**

1. 正式部署前先用 1b 的方法建立 killSwitch、用真实 ACK/归档和 GUI/新鲜对账证明全账户平仓清挂；如果在已归零的维护状态，先复核现状。禁用 supervisor，等在途 tick 结束。用 `& $py @sv --inspect`（应为 0）和 `& $py @sv --stop` 停整树，核实四角色和孤儿均消失。若身份红，按第 1 节拒绝分支处置。正式窗口采用第 21 轮 §2 的干净数据状态。
2. 按第 21 轮 §2 第 4 步在**停止所有写手后**归档原 live 内容。保留原地的 `run-manifest.json`、`ops-restarts.jsonl`（如有）及整个 `evidence\`；后两者成对保留。先做文件清单/SHA256，移到同卷 `data\ops-evidence\<本次唯一目录>\`，再逐一核对。旧 journal、expected、state、pause、halt、killSwitch、alerts 和退出标记均留有完整历史原件。任何未决风险/成交先闭环，归档不能替代仲裁。确认新运行区既没有陈旧保护，也没有窗外策略的状态或纸面仓位。
3. 用 1d 从已核实的空登记启动四个角色。部署输出保存 T180 三份原件 SHA256。**先写 F-8，再启用 supervisor**。F-8 选行内恰好一个完整 40 位 SHA、一个以 Z 结尾的真实 UTC 完成时刻（在启动清单和留存成功后记录，不能早于 `launcher.json.started_utc`），以及本次 `instances.json` 四个启动器 PID 逐字照录。解释器 PID 在选行外另列；两份策略配置 hash、风控文件 hash 和 500 美元限额另列。两份策略配置须分别匹配 `d983a242…a497b` / `e0498f28…3f06f`，风控 `e8db707b…d3e05a`（完整值见 11-17 phase-f-log）。三项数据任务 Disabled；日报/周报指向实际数据、可用 venv 和固定入口，用最近任务结果与产物验证。任务如需重装，在维护内修正外部载体/路径，安装参数 `-Mode Demo` 并省略 `-IncludeDataTasks`。原版安装器从自身所在根推导 `.venv` 和输出目录，应先核对这些路径。
4. 启用唯一标准任务。首轮 `--inspect`、心跳、日志和对账健康；连续健康至少 5 分钟。做 A：仅结束**当前已核验的 risk 树**，让 supervisor 写自有 halt、拉回 risk，然后连续健康 ≥5 分钟正常自解。做 B：仅结束 runner-902610，1–2 分钟内自动回来，902501 PID 和敞口不变。两次分别归档新旧身份、时间、原因、heartbeat 和 stdout/stderr。单角色演练使用原版 `registry_lock` + `_registered_trees` + `terminate_verified_tree`；例如将下段保存为 `$e\drill-one.py`，分别调用 `& $py "$e\drill-one.py" risk @sv` 与 `& $py "$e\drill-one.py" 902610 @sv`，每次等该次演练验完。此处只用于新锚点后、烧机阶段的计划内演练。调度器维持每分钟运行，锁冲突或停机保护存在时先查清原因。

~~~python
import sys
from pathlib import Path
key = sys.argv[1]
if key not in ("risk", "902610"):
    raise SystemExit("unsupported drill")
# @sv starts with -u, script, followed by flag/value pairs.
opts = dict(zip(sys.argv[4::2], sys.argv[5::2], strict=True))
sys.path.insert(0, str(Path(opts["--directory"]) / "scripts"))
import live_supervisor as s
cfg = s.Deployment(**{n: opts["--" + n.replace("_", "-")] for n in s.Deployment.__dataclass_fields__})
p = s.WindowsProcesses()
live = Path(cfg.data_dir) / "live"
with s.registry_lock(str(live / "instances.lock")):
    s.verify_deployment(cfg)
    if any((live / name).exists() for name in ("killSwitch", "halt.json", "halt-risk-veto.json")):
        raise SystemExit("check active protection")
    roles, entries, trees = s._registered_trees(cfg, p)
    if not all(trees.values()) or key not in entries:
        raise SystemExit("all four roles must be present")
    s.terminate_verified_tree(entries[key], p, s.command(cfg, roles[key]))
~~~

5. 核对 `ops-restarts.jsonl` 六字段、实际物理行号与 `evidence\instances\index.jsonl` 的 `seq`、组件、时间、前后副本哈希一一对应，且无新增 `SUPERVISOR_EVIDENCE`/`REFUSED`。烧机到当日 24:00，≥12 小时含换日，四个 .err 空，无非演练退出、halt/pause、未恢复降级和不可归因交易；两条策略按各自周期决策。任何一项失败，记录失败、恢复保护，重新烧机。
6. 第一个合格交易日 01:00 UTC 后，用 **main** 版本做离线证据链试跑。将本次完整证据复制到 `$off`，内含 `data\live`、各角色日志 `data\demo`、`data\reports\live`、`config\sessions.toml`、部署记录 `phase-f-log.md` 与 `calendar-source.md`（从完成日至检查日逐自然日，含周末及两假日原因）。`$first/$last` 取本次 F-8 的实际起止行。

~~~powershell
& $py "$m\scripts\formal_descriptor.py" --root $off --deploy-log phase-f-log.md --deploy-lines $first $last --calendar calendar-source.md --drill-magic 902603
if ($LASTEXITCODE -ne 0) { throw 'descriptor invalid' }
& $py "$m\scripts\demo_check.py" --accept --formal-window "$off\formal.json" --data-dir "$off\data" --log-dir "$off\data\demo" --reports-dir "$off\data\reports" --sessions-config "$off\config\sessions.toml"
$trial = $LASTEXITCODE
~~~

   试跑只接受 rc=1 且失败项全是天数、演练或交易样本不足，运行/重启绑定、身份、存活、当前状态、日志告警必须通过。rc=2 是材料无效；证据有缺口按 §3.8 处置。第十个已结束交易日且相应日报到齐后再运行，目标 rc=3，随后交独立 A5 审核。D2–D6 每日最多一项演练；重连只在 T205 v2 已独立审核后拦 60 秒，否则使用新窗口内自然断线证据。本次修复不人为断线。若做了 T205，核对规则和兜底任务均已移除。

**验证与回滚。** 每个交易日由 D64 至少检查 phase-f-log、alerts、心跳新鲜度、当日任务结果和两策略的保护状态，接收 13:15 的只读日检并为每条“需要处理”写处置时刻。主控负责运行/值守，调度维持只读。发生新退出或超过降级限额即做当日裁定；rc=75 先查根因，连续同类环境失败两次交下轮评估 VPS。写入部署、演练、烧机、试跑与重试记录后更新 STATUS 并汇报；公开记录脱敏。无须等待原主会话上线或用户再次授权。

## 总体执行顺序

裁定旧尝试作废并保全原件 → killSwitch/核对 902501 实际敞口 → 在保护下于正确 Demo 终端开启全局及 Python 交易权限 → 原版 inspect 分支 → 红分支先实平、认证退役与迁移，绿分支直接补缺 → 定版树业务恢复、风险与对账验证 → 单一 supervisor → 经核验处理 902610 暂停 → 合格维护时段归零、全量部署及 F-8 → 演练 A/B → ≥12 小时烧机 → 新 D1 试跑 → 连续值守和 A5。

## 依据

- 主工作树 `docs/STATUS.md`、第十八轮 §1/§3.7–3.9/§5、第十九轮 §1/§2.5、第二十一轮 §1–4、runbook §5/§6.2–6.3、phase-f-log 的 11-16/17 原记录及更正。日检在相邻 `sched-ops-log/docs/ops/sched-checks/`，依次为 `5681d3a`、`01fc6ce`、`eddd178`。事故与退役步骤的脚本、登记、marker 均取自本机实际部署。
- MetaQuotes [Python 接口目录](https://www.mql5.com/en/docs/python_metatrader5)、[terminal_info 字段](https://www.mql5.com/en/docs/python_metatrader5/mt5terminalinfo_py)、[10027](https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes)、[平台及外部 Python 权限](https://www.metatrader5.com/en/terminal/help/startworking/settings)、[启动参数与配置文件](https://www.metatrader5.com/en/terminal/help/start_advanced/start)。
