<#
.SYNOPSIS
    阶段 F 演练启动（T45/T49，蓝图 §5 F 行与 §7）——两种互斥模式。

.DESCRIPTION
    -Mode PaperSoak（影子盘延长观察；语义与 T45 原版一致）：
        每配置一个 --paper 运行器（真终端行情 + PaperBroker 影子券商，
        绝不连真实下单通道），一个风控 --loop、一个对账 --loop（均
        --paper-state 聚合读全部 state-<magic>.json）。用于 D′ 之后的
        延长观察；**不产生经纪商成交滑点证据**，单独跑它不能通过阶段 F
        验收（demo_check --accept 会如实判未验收）。

    -Mode Demo（MT5 模拟盘，蓝图阶段 F 正式口径）：
        运行器不带 --paper——真实 demo 账户成交路径（下单经 MT5 网关，
        网关保险丝只放行 trade_mode=DEMO 的账户）；配置必须固定
        volume_lots=0.01（出现 risk_fraction 或其他手数 → 拒绝启动）；
        启动前跑 **DEMO 保险丝与网关预检**（只读 dry 检查：连接终端、
        读账户状态核对 trade_mode=0、逐配置品种读一笔报价——绝不下单）。
        风控/对账直连终端（--gw-config），不用 --paper-state。

    无 -Mode 拒绝裸跑（列出两种模式说明后退出 1）——两种模式的成交路径、
    手数口径与验收边界完全不同，不允许缺省猜测。

    生命周期（R8-I6，单实例）：启动前读 <LogDir>/pids.txt（追加式带时间
    戳），对每个目标 magic 与角色（risk/reconcile）检测已有存活进程
    （PID 存在性；死 PID 不视为存活）。发现存活运行器 → 跳过该策略并
    告警，**绝不并行第二份**（每 magic 单写者：state-<magic>.json、意图
    与下单日志不允许两个进程同时写）；存活风控/对账同样跳过。pids.txt
    只追加不覆盖，保留可核对的完整进程清单。

    启动前检查：点差模型缓存存在、研究库缓存副本存在、合约快照存在、
    会话配置存在、磁盘剩余空间足够、配置目录非空且 magic 唯一；Demo
    模式另加：每配置固定 volume_lots=0.01 且无 risk_fraction、DEMO 保险
    丝与网关预检。任一不满足 → 不启动任何进程（exit 1）。

    启动顺序：先全部运行器（写出 expected-<magic>.json / 心跳 / paper
    状态），再风控（fail-closed：风控先于运行器启动会把心跳缺失写成
    halt.json），最后对账。

    检查（蓝图 §7）用 scripts/demo_check.py 两档口径：
        .venv/Scripts/python.exe scripts/demo_check.py --day 2026-11-10   # 日常
        .venv/Scripts/python.exe scripts/demo_check.py --summary          # 汇总
        .venv/Scripts/python.exe scripts/demo_check.py --accept           # 阶段 F 验收

.STOP / 恢复（R8-I6：绝不满组重跑）
    停止方式一（killSwitch，先停开仓再杀进程；演练项"一键停机"即此）：
        New-Item -ItemType File -Path data/live/killSwitch
        # 风控进程下一轮写 data/live/halt.json（一键停机，不自动平仓），
        # 各执行器开仓腿随即被拒；确认后按方式二结束进程。

    恢复（只移除停机信号，仅重启已确认退出的进程）：
        Remove-Item data/live/killSwitch, data/live/halt.json -ErrorAction SilentlyContinue
        # ① 确认哪些进程真的退了：Get-Content data/demo/pids.txt 逐行核对
        #    PID（Get-Process -Id <pid>）与对应日志尾部（"收到中断，退出"/
        #    Traceback = 真退出）；
        # ② 只重启已确认退出的角色——重跑本脚本即可：它按 pids.txt 检测
        #    存活实例并跳过，只补缺的运行器/风控/对账；
        # ③ 绝不"删掉 pids.txt 后全组重跑"——同 magic 并行第二份运行器会
        #    双写 state-<magic>.json/意图/下单日志（R8-I6）。

    停止方式二（taskkill，按 PID 清单直接结束）：
        Get-Content data/demo/pids.txt | ForEach-Object {
            taskkill /PID ($_ -split "`t")[0] /F }
        # （本脚本用 Start-Process 直启 python，PID 即 python 进程，无需 /T。
        #   taskkill /F 不留退出日志——计划内停止优先 killSwitch/Ctrl-C。）

.PARAMETER Mode
    PaperSoak | Demo（必填；无缺省，裸跑拒绝）。
.PARAMETER PrecheckOnly
    只做启动前检查 + 生命周期检测（含 Demo 模式的 DEMO 保险丝与网关
    预检 dry 检查），不启动任何进程、不写 pids.txt。
.PARAMETER ConfigDir
    策略配置目录（每个 .toml 一个策略；缺省 data/demo-configs）。
.PARAMETER DataDir
    数据根（live 目录 = <DataDir>/live；缺省 data）。
.PARAMETER LogDir
    日志目录（缺省 data/demo；每次启动一个带时间戳的新日志文件，
    重启即新文件——demo_check 据此识别重启并要求上一份日志留有说明。
    pids.txt 在此目录，追加式）。
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
    [string]$Mode = "",
    [switch]$PrecheckOnly,
    [string]$ConfigDir = "data/demo-configs",
    [string]$DataDir = "data",
    [string]$LogDir = "data/demo",
    [string]$RiskConfig = "config/risk.toml",
    [string]$SpreadModel = "data/costs/spread_model.json",
    [string]$ResearchCache = "data_research/costs/spread_model.json",
    [string]$PythonExe = ".venv/Scripts/python.exe",
    [double]$MinFreeGB = 5
)

# ---- 模式校验（先于一切副作用；R8-I4：拒绝裸跑） ----
if ([string]::IsNullOrWhiteSpace($Mode)) {
    Write-Host "× 缺少 -Mode（拒绝裸跑）：两种演练模式的成交路径、手数口径与验收边界不同，必须显式选择。" -ForegroundColor Red
    Write-Host ""
    Write-Host "  -Mode PaperSoak   影子盘延长观察（语义同原 T45 影子盘）：--paper 影子券商，"
    Write-Host "                     绝不连真实下单通道；不产生经纪商滑点证据，不能单独完成阶段 F 验收。"
    Write-Host "  -Mode Demo        MT5 模拟盘（蓝图阶段 F 正式口径）：真实 demo 账户成交路径，"
    Write-Host "                     固定 volume_lots=0.01（禁 risk_fraction）；启动前跑 DEMO 保险丝与网关预检。"
    Write-Host ""
    Write-Host "示例：powershell -ExecutionPolicy Bypass -File scripts/demo_run.ps1 -Mode Demo"
    exit 1
}
if ($Mode -ne "PaperSoak" -and $Mode -ne "Demo") {
    Write-Host ("× 未知 -Mode '{0}'（只接受 PaperSoak / Demo）。" -f $Mode) -ForegroundColor Red
    Write-Host "  -Mode PaperSoak   影子盘延长观察（--paper 影子券商，不连真实下单通道）。"
    Write-Host "  -Mode Demo        MT5 模拟盘（真实 demo 账户成交路径，固定 0.01 手）。"
    exit 1
}

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

function Test-PidAlive([int]$procId) {
    # PID 存在性（R8-I6 生命周期）：死 PID 不视为存活——PID 复用属尽力
    # 检测边界，恢复操作仍须按日志尾部人工确认。
    if ($procId -le 0) { return $false }
    return $null -ne (Get-Process -Id $procId -ErrorAction SilentlyContinue)
}

Write-Host ("== 演练启动前检查（阶段 F，蓝图 §7；模式 = {0}） ==" -f $Mode)

# 1) Python 解释器
$PythonExeAbs = Resolve-RepoPath $PythonExe
if (-not (Test-Path $PythonExeAbs)) {
    Write-Host "  [!]  $PythonExe 不存在，退回 PATH 里的 python" -ForegroundColor Yellow
    $PythonExeAbs = "python"
}
Assert-Ok ($PythonExeAbs -eq "python" -or (Test-Path $PythonExeAbs)) "Python 解释器可用：$PythonExeAbs"

# 2) 点差模型缓存（点差闸门 + 成本口径；缺文件在 PaperSoak 只告警、在
#    Demo 实盘路径拒绝启动——全链路演练要求重建后的模型在场）
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

# 9) Demo 模式专项：固定 0.01 手（蓝图阶段 F"0.01 手连续 2 周"）——
#    出现 risk_fraction 或 volume_lots ≠ 0.01 → 拒绝启动
if ($Mode -eq "Demo") {
    foreach ($c in $configs) {
        $riskFrac = @(Select-String -Path $c.FullName -Pattern '^\s*risk_fraction\s*=')
        if ($riskFrac.Count -gt 0) {
            Assert-Ok $false "$($c.Name) 使用 risk_fraction（按风险定手数）——Demo 模式必须固定 volume_lots=0.01"
            continue
        }
        $lots = @(Select-String -Path $c.FullName -Pattern '^\s*volume_lots\s*=\s*0\.01\s*$')
        Assert-Ok ($lots.Count -eq 1) "$($c.Name) 固定 volume_lots=0.01（Demo 手数上限）"
    }
}

if ($failures.Count -gt 0) {
    Write-Host ""
    Write-Host ("启动前检查未通过（" + $failures.Count + " 项），未启动任何进程。") -ForegroundColor Red
    exit 1
}

# ---- 生命周期检测（R8-I6：单实例；绝不并行第二份） ----
$LogDirAbs = Resolve-RepoPath $LogDir
New-Item -ItemType Directory -Force -Path $LogDirAbs | Out-Null
$pidsFile = Join-Path $LogDirAbs "pids.txt"
$LiveDirAbs = Join-Path $DataDirAbs "live"
$PaperStateAbs = Join-Path $LiveDirAbs "paper"

# 读现有 PID 清单（追加式；行格式 pid<tab>kind<tab>name<tab>log[<tab>mode<tab>utc]，
# 旧 4 列格式同样兼容），按 PID 存在性判定存活实例
$aliveRunners = @{}   # magic -> pid（该 magic 已有存活运行器 → 跳过）
$aliveRoles = @{}     # risk/reconcile -> pid（已有存活进程 → 跳过）
if (Test-Path $pidsFile) {
    foreach ($line in Get-Content $pidsFile) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $cols = $line -split "`t"
        if ($cols.Count -lt 3) { continue }
        $procId = 0
        if (-not [int]::TryParse($cols[0], [ref]$procId)) { continue }
        if (-not (Test-PidAlive $procId)) { continue }   # 死 PID：不挡启动
        $kind = $cols[1].Trim()
        $name = $cols[2].Trim()
        if ($kind -eq "runner") {
            $m = 0
            if ([int]::TryParse($name, [ref]$m)) { $aliveRunners[$m] = $procId }
        } elseif ($kind -eq "risk" -or $kind -eq "reconcile") {
            $aliveRoles[$kind] = $procId
        }
    }
}

# ---- Demo 模式专项：DEMO 保险丝 + 网关预检（只读 dry 检查，绝不下单） ----
if ($Mode -eq "Demo") {
    Write-Host ""
    Write-Host "== Demo 专项预检：DEMO 保险丝 + 网关连通（只读，不下单） =="
    $symbols = @()
    foreach ($c in $configs) {
        $s = (Select-String -Path $c.FullName -Pattern '^\s*symbol\s*=\s*"([^"]+)"').Matches
        if ($s.Count -gt 0) { $symbols += $s[0].Groups[1].Value }
    }
    $symbols = @($symbols | Select-Object -Unique)
    $demoFusePy = @'
import sys
from pathlib import Path
sys.path.insert(0, "src")  # cwd = 仓库根（脚本已 Set-Location）
from mt5gw import GatewayConfig, MT5Gateway, load
try:
    from mt5gw.models import ACCOUNT_TRADE_MODE_DEMO
except ImportError:                                    # 常量兜底：0=DEMO
    ACCOUNT_TRADE_MODE_DEMO = 0
cfg_path = next((p for p in ("config.json", "gateway.json") if Path(p).exists()), None)
cfg = load(cfg_path) if cfg_path else GatewayConfig()
symbols = [s for s in sys.argv[1:] if s]
try:
    with MT5Gateway(cfg) as gw:
        st = gw.account.state()          # 读不到 → 异常 → fail-closed 拒启动
        if st.trade_mode != ACCOUNT_TRADE_MODE_DEMO:
            print(f"FAIL 账户 {st.login} trade_mode={st.trade_mode} 非模拟盘"
                  "（0=DEMO）——Demo 模式只允许模拟账户，拒绝启动")
            sys.exit(1)
        print(f"OK 网关连通；账户 {st.login}（trade_mode={st.trade_mode} 模拟盘，"
              f"{st.currency} 余额 {st.balance} 服务器 {st.server}）")
        for s in symbols:
            q = gw.market.quote(s)       # 只读行情，绝不下单
            if q is None or q.bid <= 0 or q.ask < q.bid:
                print(f"FAIL {s} 报价不可用（bid={getattr(q, 'bid', None)}）")
                sys.exit(1)
            print(f"OK {s} 报价 bid={q.bid} ask={q.ask}")
except SystemExit:
    raise
except Exception as e:                   # GatewayError/连接失败 → fail-closed
    print(f"FAIL 网关预检未通过：{type(e).__name__}: {e}")
    sys.exit(1)
'@
    & $PythonExeAbs -c $demoFusePy @symbols
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [X]  DEMO 保险丝/网关预检未通过——不启动任何进程。" -ForegroundColor Red
        exit 1
    }
}

if ($PrecheckOnly) {
    Write-Host ""
    Write-Host "== -PrecheckOnly：检查完成，不启动任何进程 =="
    $skipped = @($aliveRunners.Keys | ForEach-Object { "runner/$_" }) + @($aliveRoles.Keys)
    if ($skipped.Count -gt 0) {
        Write-Host ("存活实例（若此时启动将被跳过）：" + ($skipped -join "、")) -ForegroundColor Yellow
    } else {
        Write-Host "未发现存活实例（各 magic/角色当前无运行中的进程）。"
    }
    exit 0
}

# ---- 启动 ----
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$manifest = @()

function Append-PidLine([int]$procId, [string]$kind, [string]$name, [string]$log) {
    # pids.txt 追加式（R8-I6）：pid<tab>kind<tab>name<tab>log<tab>mode<tab>utc。
    # 首列仍是 pid——runbook 的 taskkill 单行命令兼容；不再覆盖旧清单。
    $line = "{0}`t{1}`t{2}`t{3}`t{4}`t{5}" -f $procId, $kind, $name, $log,
        $Mode, (Get-Date).ToUniversalTime().ToString("o")
    Add-Content -Encoding UTF8 -Path $pidsFile -Value $line
}

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
    Append-PidLine $p.Id $kind $name $log
    return $p.Id
}

Write-Host ""
if ($Mode -eq "Demo") {
    Write-Host "== 启动模拟盘进程（Demo：真实 demo 账户成交路径，固定 0.01 手） =="
} else {
    Write-Host "== 启动模拟盘进程（PaperSoak：--paper 影子券商，绝不连真实下单通道） =="
}

# 1) 每配置一个运行器（先启动：写出意图/心跳/paper 状态）；
#    该 magic 已有存活运行器 → 跳过（R8-I6：绝不并行第二份）
$runnerPids = @()
foreach ($c in $configs) {
    $magic = [int]((Select-String -Path $c.FullName -Pattern '^\s*magic\s*=\s*(\d+)').Matches[0].Groups[1].Value)
    if ($aliveRunners.ContainsKey($magic)) {
        Write-Host ("跳过策略 {0}（magic {1} 已有存活运行器 PID {2}——绝不并行第二份；" -f $c.Name, $magic, $aliveRunners[$magic]) -ForegroundColor Yellow
        Write-Host "       如确认旧进程已死，先核对旧日志尾部再清理 pids.txt 中该行）" -ForegroundColor Yellow
        continue
    }
    $runnerArgs = @(
        "scripts/live_runner.py",
        "--config", $c.FullName,
        "--loop",
        "--risk-config", $RiskConfigAbs,
        "--spread-model", $SpreadModelAbs
    )
    if ($Mode -eq "PaperSoak") {
        $runnerArgs += "--paper"
    }
    $runnerPids += Start-DemoProcess "runner" "$magic" $runnerArgs
}

# 运行器先落盘：意图 expected-<magic>.json、心跳、（PaperSoak）paper/state-<magic>.json
if ($runnerPids.Count -gt 0) {
    Write-Host "等待运行器落盘（8 秒）……"
    Start-Sleep -Seconds 8
}

# 2) 一个风控 --loop（PaperSoak：--paper-state 聚合读全部 state-<magic>.json，
#    不连终端；Demo：直连真实 demo 终端只读账户）；已有存活风控 → 跳过
if ($aliveRoles.ContainsKey("risk")) {
    Write-Host ("跳过风控进程（已有存活 PID {0}——绝不并行第二份）" -f $aliveRoles["risk"]) -ForegroundColor Yellow
} else {
    $riskArgs = @(
        "scripts/risk_process.py",
        "--config", $RiskConfigAbs,
        "--data-dir", $DataDirAbs,
        "--loop", "--interval", "10"
    )
    if ($Mode -eq "PaperSoak") { $riskArgs += @("--paper-state", $PaperStateAbs) }
    Start-DemoProcess "risk" "account" $riskArgs | Out-Null
    Start-Sleep -Seconds 2
}

# 3) 一个对账 --loop（PaperSoak：--paper-state 同上；Demo：直连终端）；
#    已有存活对账 → 跳过
if ($aliveRoles.ContainsKey("reconcile")) {
    Write-Host ("跳过对账进程（已有存活 PID {0}——绝不并行第二份）" -f $aliveRoles["reconcile"]) -ForegroundColor Yellow
} else {
    $reconArgs = @(
        "scripts/live_reconcile.py",
        "--data-dir", $DataDirAbs,
        "--loop", "--interval", "30"
    )
    if ($Mode -eq "PaperSoak") { $reconArgs += @("--paper-state", $PaperStateAbs) }
    Start-DemoProcess "reconcile" "all" $reconArgs | Out-Null
}

# ---- 清单 ----
$manifestFile = Join-Path $LogDirAbs ("demo-run-{0}-{1}.json" -f $Mode, $stamp)
@{ started_utc = (Get-Date).ToUniversalTime().ToString("o")
   mode = $Mode
   repo_root = $RepoRoot
   config_dir = $ConfigDirAbs
   paper_state_dir = $PaperStateAbs
   skipped_alive_runners = @($aliveRunners.Keys | ForEach-Object { [int]$_ })
   processes = $manifest } | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $manifestFile

Write-Host ""
Write-Host ("模式 {0}：本次新启动 {1} 个进程（PID 清单（追加式）：{2}；运行清单：{3}）" -f $Mode, $manifest.Count, $pidsFile, $manifestFile)
Write-Host "停止（killSwitch）：New-Item -ItemType File data/live/killSwitch   （恢复：只移除停机信号，重启仅针对已确认退出的进程——见脚本头）"
Write-Host "停止（taskkill）  ：Get-Content $pidsFile | ForEach-Object { taskkill /PID (`$_ -split `"`t`")[0] /F }"
Write-Host "日常检查        ：$PythonExeAbs scripts/demo_check.py --day YYYY-MM-DD   或 --summary"
Write-Host "阶段 F 验收     ：$PythonExeAbs scripts/demo_check.py --accept   （严格：无样本/未覆盖=未验收）"
