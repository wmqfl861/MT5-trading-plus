<#
.SYNOPSIS
    阶段 F 演练启动（T45/T49 + T51/T52/T55，蓝图 §5 F 行与 §7）——两种互斥模式。

.DESCRIPTION
    -Mode PaperSoak（影子盘延长观察；语义与 T45 原版一致）：
        每配置一个 --paper 运行器（真终端行情 + PaperBroker 影子券商，
        绝不连真实下单通道），一个风控 --loop、一个对账 --loop（均
        --paper-state 聚合读全部 state-<magic>.json）。用于 D′ 之后的
        延长观察；**不产生经纪商成交滑点证据**，其证据不被 demo_check
        --accept 采信（验收按 R10/C-1 要求 mode=="Demo" 的运行窗与真实
        成交证据）——单独跑它不能通过阶段 F 验收。

    -Mode Demo（MT5 模拟盘，蓝图阶段 F 正式口径）：
        运行器不带 --paper——真实 demo 账户成交路径（下单经 MT5 网关，
        网关保险丝只放行 trade_mode=DEMO 的账户）；配置必须固定
        volume_lots=0.01 且无 risk_fraction——判定走 scripts/
        config_check.py 调 LiveConfig 真解析器按**实际加载值**比对
        （文本行匹配可被注释/多行字符串/表作用域欺骗，R9-I2）；
        启动前跑 **DEMO 保险丝与网关预检**（只读 dry 检查：连接终端、
        读账户状态核对 trade_mode=0、逐配置品种读一笔报价——绝不下单；
        多行 Python 源码一律写临时 .py 文件后按文件路径调用——PS5.1
        原生传参会剥引号损坏内联源码，R9-I1）。风控/对账直连终端
        （--gw-config），不用 --paper-state。

    无 -Mode 拒绝裸跑（-Stop 例外）——两种模式的成交路径、手数口径与
    验收边界完全不同，不允许缺省猜测。

    生命周期（T110，单实例）：实例登记在 <DataDir>/live/instances.json。
    固定部署的检测、停止复用 live_supervisor.py 的 verified_tree：完整
    SHA/目录/参数、PID 创建时间及全部父子关系核验。不可读 PID、存活孤儿
    worker、身份不符均拒绝，不当作缺失后补起。旧无固定身份登记的补缺和
    -Stop 已禁用，保留登记供维护窗口迁移。检测→启动→登记→run-manifest
    追加在 instances.lock 独占段内完成（FileShare.None，与 supervisor
    共用）。pids.txt 只是追加式启动历史，不参与存活检测。

    运行窗登记（R10/C-1）：<DataDir>/live/run-manifest.json——
    schema_version/mode/magics/account_hint 描述当前运行；runs **追加式**
    记录每次启动（含恢复重启，kind=start|restart，重启不清窗）。
    读取失败或 JSON 损坏 → fail-closed 拒绝启动。

    启动前检查：点差模型缓存存在、研究库缓存副本存在、合约快照存在、
    会话配置存在、磁盘剩余空间足够、配置目录非空且 magic 唯一；Demo
    模式另加：每配置实际加载 volume_lots=0.01 且无 risk_fraction、
    DEMO 保险丝与网关预检。任一不满足 → 不启动任何进程（exit 1）。

    启动顺序：先全部运行器（写出 expected-<magic>.json / 心跳 / paper
    状态），再风控（fail-closed：风控先于运行器启动会把心跳缺失写成
    halt.json），最后对账。

    检查（蓝图 §7）用 scripts/demo_check.py 两档口径：
        .venv/Scripts/python.exe scripts/demo_check.py --day 2026-11-10   # 日常
        .venv/Scripts/python.exe scripts/demo_check.py --summary          # 汇总
        .venv/Scripts/python.exe scripts/demo_check.py --accept           # 阶段 F 验收

.STOP / 恢复（R8-I6 + R9-I5：绝不满组重跑）
    停止方式一（killSwitch，先停开仓再停进程；演练项"一键停机"即此）：
        New-Item -ItemType File data/live/killSwitch
        # 风控进程下一轮写 data/live/halt.json（一键停机，不自动平仓），
        # 各执行器开仓腿随即被拒；确认后按方式二结束进程。

    停止方式二（T110）：先禁用 supervisor 计划任务并设置 killSwitch，
    再带启动时完全相同的 -DeploymentSha / DataDir / ConfigDir / PythonExe /
    GatewayConfig / RiskConfig / SessionsConfig / SpreadModel / LogDir 调 -Stop。
    它调用 live_supervisor.py --stop，先核验全部角色，再子先父后停止，
    不清空登记；身份不确定则拒绝。完整命令见 runbook §6.2/§6.3。

    恢复：人工完成对账与停机原因核验后，只补严格证明整树已退出的角色。
    固定部署重用同一组路径与 SHA；旧 PID/basename 登记不能自动迁移。
    禁止清登记后满组重跑，禁止仅杀 venv launcher。

.PARAMETER Mode
    PaperSoak | Demo（必填；-Stop 时不需要）。
.PARAMETER Stop
    仅固定 Demo 部署：共用 supervisor 的完整树核验与停止；需要 killSwitch
    和完整固定部署路径参数，保留登记。不需要 -Mode。
.PARAMETER PrecheckOnly
    只做启动前检查 + 生命周期检测（含 Demo 模式的 DEMO 保险丝与网关
    预检 dry 检查），不启动任何进程、不写 pids.txt、不改登记文件。
.PARAMETER SkipGatewayCheck
    跳过 Demo 网关连通预检（不连终端），只保留保险丝源码语法/字节
    完整性检查（到达"等待网关"边界）。**必须与 -PrecheckOnly 同用**——
    不带 -PrecheckOnly 单独使用等于绕过 DEMO 保险丝的后门，直接拒绝
    启动（exit 1）。
.PARAMETER ConfigDir
    策略配置目录（每个 .toml 一个策略；缺省 data/demo-configs）。
.PARAMETER DataDir
    数据根（live 目录 = <DataDir>/live；缺省 data。实例登记、运行窗
    manifest 与 instances.lock 都在 <DataDir>/live 下——跨 LogDir 生效）。
.PARAMETER LogDir
    日志目录（缺省 data/demo；每次启动一个带时间戳的新日志文件，
    重启即新文件——demo_check 据此识别重启并要求上一份日志留有说明。
    pids.txt 在此目录，追加式历史日志）。
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
    [switch]$SkipGatewayCheck,
    [switch]$Stop,
    [string]$ConfigDir = "data/demo-configs",
    [string]$DataDir = "data",
    [string]$LogDir = "data/demo",
    [string]$RiskConfig = "config/risk.toml",
    [string]$SpreadModel = "data/costs/spread_model.json",
    [string]$ResearchCache = "data_research/costs/spread_model.json",
    [string]$PythonExe = ".venv/Scripts/python.exe",
    [string]$GatewayConfig = "",
    [string]$SessionsConfig = "config/sessions.toml",
    [string]$DeploymentSha = "",
    [double]$MinFreeGB = 5
)

# ---- 模式/开关校验（先于一切副作用；R8-I4 拒绝裸跑；T51 防后门） ----
if ($Stop) {
    if ($PrecheckOnly) {
        Write-Host "× -Stop 与 -PrecheckOnly 互斥：停止就是停止，不是检查。" -ForegroundColor Red
        exit 1
    }
    if ($SkipGatewayCheck) {
        Write-Host "× -Stop 与 -SkipGatewayCheck 互斥：停止流程不涉及网关预检。" -ForegroundColor Red
        exit 1
    }
} else {
    if ([string]::IsNullOrWhiteSpace($Mode)) {
        Write-Host "× 缺少 -Mode（拒绝裸跑）：两种演练模式的成交路径、手数口径与验收边界不同，必须显式选择。" -ForegroundColor Red
        Write-Host ""
        Write-Host "  -Mode PaperSoak   影子盘延长观察（语义同原 T45 影子盘）：--paper 影子券商，"
        Write-Host "                     绝不连真实下单通道；不产生经纪商滑点证据，不能单独完成阶段 F 验收。"
        Write-Host "  -Mode Demo        MT5 模拟盘（蓝图阶段 F 正式口径）：真实 demo 账户成交路径，"
        Write-Host "                     固定 volume_lots=0.01（禁 risk_fraction）；启动前跑 DEMO 保险丝与网关预检。"
        Write-Host ""
        Write-Host "示例：powershell -ExecutionPolicy Bypass -File scripts/demo_run.ps1 -Mode Demo"
        Write-Host "停止：固定部署参数与 -Stop（先禁用 supervisor 并设置 killSwitch，见 runbook §6.3）"
        exit 1
    }
    if ($Mode -ne "PaperSoak" -and $Mode -ne "Demo") {
        Write-Host ("× 未知 -Mode '{0}'（只接受 PaperSoak / Demo）。" -f $Mode) -ForegroundColor Red
        Write-Host "  -Mode PaperSoak   影子盘延长观察（--paper 影子券商，不连真实下单通道）。"
        Write-Host "  -Mode Demo        MT5 模拟盘（真实 demo 账户成交路径，固定 0.01 手）。"
        exit 1
    }
    if ($SkipGatewayCheck -and -not $PrecheckOnly) {
        # T51：跳过网关预检只允许用于"不启动任何进程"的检查路径——
        # 否则等于给绕过 DEMO 保险丝留后门。
        Write-Host "× -SkipGatewayCheck requires -PrecheckOnly：跳过网关预检只允许配合 -PrecheckOnly（只检查、不启动进程）。" -ForegroundColor Red
        Write-Host "  实际启动（不带 -PrecheckOnly）必须完整执行 DEMO 保险丝与网关预检——这是 Demo 模式的强制边界。" -ForegroundColor Red
        exit 1
    }
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

$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:PyFileExit = 0

function Invoke-VerifiedLifecycle([string]$action) {
    if ($DeploymentSha -notmatch '^[0-9a-f]{40}$') {
        throw "verified-tree-required: fixed DeploymentSha and explicit paths are required; legacy PID-only stop/replacement is disabled"
    }
    $lifecyclePython = Resolve-RepoPath $PythonExe
    if (-not (Test-Path -LiteralPath $lifecyclePython -PathType Leaf)) {
        throw "verified-tree-required: explicit PythonExe is missing"
    }
    $gatewayPath = $GatewayConfig
    if ([string]::IsNullOrWhiteSpace($gatewayPath)) {
        $gatewayPath = "config.json"
        if (-not (Test-Path (Resolve-RepoPath $gatewayPath)) -and (Test-Path (Resolve-RepoPath "gateway.json"))) {
            $gatewayPath = "gateway.json"
        }
    }
    $lifecycleArgs = @(
        "-u", (Join-Path $RepoRoot "scripts/live_supervisor.py"),
        "--directory", $RepoRoot, "--sha", $DeploymentSha,
        "--data-dir", (Resolve-RepoPath $DataDir),
        "--config-dir", (Resolve-RepoPath $ConfigDir),
        "--python", $lifecyclePython,
        "--gateway-config", (Resolve-RepoPath $gatewayPath),
        "--risk-config", (Resolve-RepoPath $RiskConfig),
        "--sessions-config", (Resolve-RepoPath $SessionsConfig),
        "--spread-model", (Resolve-RepoPath $SpreadModel),
        "--log-dir", (Resolve-RepoPath $LogDir), ("--" + $action)
    )
    $raw = & $lifecyclePython @lifecycleArgs
    $code = $LASTEXITCODE
    try { $result = ($raw -join "`n") | ConvertFrom-Json } catch { $result = $null }
    if ($code -ne 0 -or $null -eq $result -or $result.refused) {
        throw ("verified-tree-required: " + ($raw -join "`n"))
    }
    return $result
}

function Write-InstancesFile([string]$path, [array]$entries) {
    # 登记表整文件重写（调用方都在 instances.lock 临界区内；死项已被
    # 剔除）。UTF-8 无 BOM，便于 demo_check 等工具直接 json.load。
    $obj = [ordered]@{ schema_version = 1; instances = @($entries) }
    [IO.File]::WriteAllText($path, ($obj | ConvertTo-Json -Depth 5) + "`n", $script:Utf8NoBom)
}

function Invoke-PythonFile([string]$source, [string[]]$pyArgs) {
    # T51/R9-I1：多行 Python 源码绝不作内联参数传给原生进程（PS5.1
    # 原生传参剥双引号，源码必坏）；写 $env:TEMP 随机名 .py（UTF-8 无
    # BOM）后按文件路径调用；try/finally 清理含失败路径。stdout 原样
    # 透传（赋值调用时即捕获），退出码存 $script:PyFileExit。
    $f = Join-Path ([IO.Path]::GetTempPath()) ("mt5demo-" + [guid]::NewGuid().ToString("N") + ".py")
    [IO.File]::WriteAllText($f, $source, $script:Utf8NoBom)
    try {
        & $script:PythonExeAbs $f @pyArgs
        $script:PyFileExit = $LASTEXITCODE
    } catch {
        $script:PyFileExit = 1
    } finally {
        Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
    }
}

function Get-UtcStamp {
    return (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ",
        [Globalization.CultureInfo]::InvariantCulture)
}

# ---- Stop shares the supervisor's full-tree identity and termination path. ----
if ($Stop) {
    try {
        $result = Invoke-VerifiedLifecycle "stop"
        Write-Host ("Verified stopped roles: " + ($result.stopped -join ","))
        Write-Host "instances.json retained for audit and verified restart."
        exit 0
    } catch {
        Write-Host ([string]$_) -ForegroundColor Red
        exit 1
    }
}

Write-Host ("== 演练启动前检查（阶段 F，蓝图 §7；模式 = {0}） ==" -f $Mode)

# 1) Python 解释器
$PythonExeAbs = Resolve-RepoPath $PythonExe
if (-not (Test-Path $PythonExeAbs)) {
    Write-Host "  [!]  $PythonExe 不存在，退回 PATH 里的 python" -ForegroundColor Yellow
    $PythonExeAbs = "python"
}
Assert-Ok ($PythonExeAbs -eq "python" -or (Test-Path $PythonExeAbs)) "Python 解释器可用：$PythonExeAbs"

# One effective gateway configuration for precheck AND every child.
if ([string]::IsNullOrWhiteSpace($GatewayConfig)) {
    $GatewayConfig = "config.json"
    if (-not (Test-Path (Resolve-RepoPath $GatewayConfig)) -and (Test-Path (Resolve-RepoPath "gateway.json"))) {
        $GatewayConfig = "gateway.json"
    }
}
$GatewayConfigAbs = Resolve-RepoPath $GatewayConfig
$env:PYTHONUNBUFFERED = "1"
if (-not [string]::IsNullOrWhiteSpace($DeploymentSha)) {
    if ($Mode -ne "Demo" -or $DeploymentSha -notmatch '^[0-9a-f]{40}$') {
        throw "DeploymentSha requires Demo and a full fixed SHA"
    }
    $actualSha = (& git -C $RepoRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $actualSha -ne $DeploymentSha) { throw "deployment SHA mismatch" }
    & git -C $RepoRoot symbolic-ref -q HEAD | Out-Null
    if ($LASTEXITCODE -ne 1) { throw "deployment must be detached at fixed SHA" }
    $dirty = & git -C $RepoRoot status --porcelain --untracked-files=all
    if ($LASTEXITCODE -ne 0 -or $dirty) { throw "deployment must be clean and isolated" }
    if ($PythonExeAbs -eq "python") { throw "deployment requires explicit PythonExe" }
    $env:MT5_DEPLOYMENT_SHA = $DeploymentSha
} else {
    $env:MT5_DEPLOYMENT_SHA = ""
}
if ($Mode -eq "Demo" -and -not $SkipGatewayCheck) {
    Assert-Ok (Test-Path $GatewayConfigAbs) "Explicit gateway configuration: $GatewayConfigAbs"
}

# 2) 点差模型缓存（点差闸门 + 成本口径；缺文件在 PaperSoak 只告警、在
#    Demo 实盘路径拒绝启动——全链路演练要求重建后的模型在场）
$SpreadModelAbs = Resolve-RepoPath $SpreadModel
Assert-Ok (Test-Path $SpreadModelAbs) "点差模型缓存存在：$SpreadModel"

# 3) 研究库缓存副本（T27 研究库隔离；演练期间的成本口径应与研究库一致）
$ResearchCacheAbs = Resolve-RepoPath $ResearchCache
Assert-Ok (Test-Path $ResearchCacheAbs) "研究库缓存存在：$ResearchCache"

# 4) 合约快照（PaperBroker 盈亏换算和定手数必需）
$SpecsAbs = Resolve-RepoPath (Join-Path $DataDir "specs/latest.json")
Assert-Ok (Test-Path $SpecsAbs) "合约快照存在：$SpecsAbs"

# 5) 会话配置（换日禁入窗唯一来源）
$SessionsAbs = Resolve-RepoPath $SessionsConfig
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

# 9) Demo 模式专项：固定 0.01 手（T52/R9-I2）——判定走 scripts/
#    config_check.py 的 LiveConfig 真解析器按实际加载值比对（单行 JSON、
#    ASCII-only）；Select-String 不再承担判定（仅第 8 步 magic 提取保留）。
if ($Mode -eq "Demo") {
    $configCheckPy = Join-Path $PSScriptRoot "config_check.py"
    Assert-Ok (Test-Path $configCheckPy) "配置解析器存在：scripts/config_check.py"
    foreach ($c in $configs) {
        $raw = $null
        $info = $null
        $rc = 1
        try {
            $raw = & $PythonExeAbs $configCheckPy $c.FullName
            $rc = $LASTEXITCODE
        } catch { $rc = 1 }
        try { $info = ($raw | Select-Object -First 1) | ConvertFrom-Json } catch { $info = $null }
        if ($rc -ne 0 -or $null -eq $info -or -not $info.ok) {
            $ec = "?"
            if ($null -ne $info -and $info.error_code) { $ec = $info.error_code }
            Assert-Ok $false "$($c.Name) 配置解析失败（config_check error_code=$ec；输出：$raw）——fail-closed 拒绝"
            continue
        }
        if ($info.has_risk_fraction) {
            Assert-Ok $false "$($c.Name) 使用 risk_fraction（按风险定手数）——Demo 模式必须固定 volume_lots=0.01"
            continue
        }
        Assert-Ok ($info.volume_lots -eq 0.01) "$($c.Name) 固定 volume_lots=0.01（实际加载值：$($info.volume_lots)；Demo 手数上限）"
    }
}

if ($failures.Count -gt 0) {
    Write-Host ""
    Write-Host ("启动前检查未通过（" + $failures.Count + " 项），未启动任何进程。") -ForegroundColor Red
    exit 1
}

# ---- 临界区（T55/R9-I5 + C-1）：instances.lock 独占 ----
# 检测→预检→启动→登记→run-manifest 追加在同一段临界区内完成：并发启动
# 的第二个调用在取锁处立即失败（绝不并行第二份）；run-manifest 的追加
# 与实例登记同段，防互相覆盖。句柄存 $script: 变量防 GC 提前释放。
$LogDirAbs = Resolve-RepoPath $LogDir
New-Item -ItemType Directory -Force -Path $LogDirAbs | Out-Null
$LiveDirAbs = Join-Path $DataDirAbs "live"
New-Item -ItemType Directory -Force -Path $LiveDirAbs | Out-Null
$PaperStateAbs = Join-Path $LiveDirAbs "paper"
$pidsFile = Join-Path $LogDirAbs "pids.txt"
$InstanceLockPath = Join-Path $LiveDirAbs "instances.lock"
$InstanceRegistryFile = Join-Path $LiveDirAbs "instances.json"
$RunManifestFile = Join-Path $LiveDirAbs "run-manifest.json"
$accountHint = "paper"
if ($Mode -eq "Demo") { $accountHint = "demo" }

try {
    $script:InstancesLockHandle = [IO.File]::Open($InstanceLockPath,
        [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
} catch {
    Write-Host "× instances.lock 已被独占——另一个启动/停止流程正在进行（绝不并行第二份）。本次立即失败。" -ForegroundColor Red
    exit 1
}

try {
    # C-1：run-manifest 读失败 / JSON 损坏 / schema 不认识 → fail-closed。
    # 验收运行窗（runs 中当前 mode 最早 started_utc → now）不可核验时
    # 不允许启动——损坏的 manifest 会让 --accept 的窗口判定失真。
    if (Test-Path $RunManifestFile) {
        $manifestOk = $false
        try {
            $mf = Get-Content -Raw -Encoding UTF8 $RunManifestFile | ConvertFrom-Json
            if ($null -ne $mf -and $mf.schema_version -eq 1 -and $null -ne $mf.runs) { $manifestOk = $true }
        } catch { }
        if (-not $manifestOk) {
            Write-Host "× run-manifest.json 读失败或 JSON 损坏（schema_version 非 1 / 缺 runs 同样拒绝）——fail-closed：运行窗不可核验，拒绝启动。人工核验、修复或归档该文件后再来。" -ForegroundColor Red
            exit 1
        }
    }

    # Fixed deployments share the supervisor's complete process-tree verifier.
    # Legacy entries cannot prove missing workers; never drop them and fill gaps.
    $aliveRunners = @{}
    $aliveRoles = @{}
    $script:RegistryKept = @()
    $entries = @()
    if (Test-Path $InstanceRegistryFile) {
        try {
            $parsed = Get-Content -Raw -Encoding UTF8 $InstanceRegistryFile | ConvertFrom-Json
            if ($null -eq $parsed -or $parsed.schema_version -ne 1 -or $null -eq $parsed.instances) {
                throw "invalid schema"
            }
            $entries = @($parsed.instances | Where-Object { $null -ne $_ })
        } catch {
            Write-Host "instances.json invalid: refuse startup" -ForegroundColor Red
            exit 1
        }
    }
    foreach ($e in $entries) {
        if ([string]$e.mode -ne $Mode) {
            Write-Host ("mode conflict: kind={0} magic={1} PID={2}" -f $e.kind, $e.magic, $e.pid) -ForegroundColor Red
            exit 1
        }
    }
    if ([string]::IsNullOrWhiteSpace($DeploymentSha)) {
        if ($entries.Count -gt 0) {
            Write-Host "verified-tree-required: legacy entries cannot authorize stop/replacement; retain registry and migrate verified identity" -ForegroundColor Red
            exit 1
        }
    } else {
        try { $inspection = Invoke-VerifiedLifecycle "inspect" } catch {
            Write-Host ([string]$_) -ForegroundColor Red
            exit 1
        }
        foreach ($e in $entries) {
            $key = [string]$e.kind
            if ($key -eq "runner") { $key = [string]$e.magic }
            $alive = $inspection.alive.PSObject.Properties[$key]
            if ($null -eq $alive) { continue }  # verifier proved the WHOLE tree absent
            $script:RegistryKept += $e
            if ([string]$e.kind -eq "runner") {
                $aliveRunners[[int]$e.magic] = [int]$alive.Value
            } else {
                $aliveRoles[[string]$e.kind] = [int]$alive.Value
            }
        }
    }

    # ---- Demo 专项：DEMO 保险丝 + 网关预检（T51：源码走临时 .py 文件） ----
    if ($Mode -eq "Demo") {
        Write-Host ""
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
cfg = load(sys.argv[1])
if cfg.allow_live_trading is not False:
    raise SystemExit("FAIL Demo configuration must keep allow_live_trading=false")
symbols = [s for s in sys.argv[2:] if s]
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
        # T60：行尾归一（跨检出确定）。core.autocrlf=true 的全新克隆/CI 检出
        # 为 CRLF 时，here-string 值保留 \r\n，后续字节拷贝路径（WriteAllText
        # 写保险丝临时 .py 与 Invoke-PythonFile 写 $fuseCheckPy 临时 .py）会
        # 原样落 CRLF，与测试侧（read_text 通用换行恒 LF）sha 不等——赋值后
        # 立即归一为 LF（单一钉点，两条写盘路径全覆盖）。
        $demoFusePy = $demoFusePy -replace "`r`n", "`n"
        $fuseCheckPy = @'
import ast, hashlib, sys
data = open(sys.argv[1], 'rb').read()
ast.parse(data.decode('utf-8'))
print('sha256=' + hashlib.sha256(data).hexdigest())
'@
        $fuseCheckPy = $fuseCheckPy -replace "`r`n", "`n"
        if ($SkipGatewayCheck) {
            # 只做保险丝源码语法/字节完整性检查（不连终端、不执行保险丝）
            Write-Host "== Demo 专项预检（-SkipGatewayCheck）：保险丝源码语法/字节检查，不连终端 =="
            $fuseFile = Join-Path ([IO.Path]::GetTempPath()) ("mt5demo-" + [guid]::NewGuid().ToString("N") + ".py")
            [IO.File]::WriteAllText($fuseFile, $demoFusePy, $script:Utf8NoBom)
            try {
                $hashLine = Invoke-PythonFile $fuseCheckPy @($fuseFile)
                if ($script:PyFileExit -ne 0 -or [string]::IsNullOrWhiteSpace([string]$hashLine)) {
                    Write-Host "  [X]  DEMO 保险丝源码语法/字节完整性检查未通过——不启动任何进程。" -ForegroundColor Red
                    exit 1
                }
                Write-Host ("  [OK] DEMO 保险丝源码语法检查通过（{0}，与脚本内原文逐字节一致）" -f (@($hashLine) | Select-Object -First 1))
                Write-Host "  [!]  网关连通检查已跳过（gateway skipped，-SkipGatewayCheck）：到达等待网关边界、未跨越——实际启动必须完整预检（账户为模拟盘 + 逐品种报价）。" -ForegroundColor Yellow
            } finally {
                Remove-Item -LiteralPath $fuseFile -Force -ErrorAction SilentlyContinue
            }
        } else {
            Write-Host "== Demo 专项预检：DEMO 保险丝 + 网关连通（只读，不下单） =="
            Invoke-PythonFile $demoFusePy (@($GatewayConfigAbs) + $symbols)
            if ($script:PyFileExit -ne 0) {
                Write-Host "  [X]  DEMO 保险丝/网关预检未通过——不启动任何进程。" -ForegroundColor Red
                exit 1
            }
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
        # pids.txt 追加式历史日志（R8-I6/T55：不参与存活检测）：pid<tab>
        # kind<tab>name<tab>log<tab>mode<tab>utc。
        $line = "{0}`t{1}`t{2}`t{3}`t{4}`t{5}" -f $procId, $kind, $name, $log,
            $Mode, (Get-Date).ToUniversalTime().ToString("o")
        Add-Content -Encoding UTF8 -Path $pidsFile -Value $line
    }

    function Start-DemoProcess([string]$kind, [string]$name, $magicVal, [string[]]$argList) {
        $argList = @("-u") + $argList
        $log = Join-Path $LogDirAbs ("{0}-{1}-{2}.log" -f $kind, $name, $stamp)
        $errLog = "$log.err"
        Write-Host ("启动 {0} {1} -> {2}" -f $kind, $name, (Split-Path $log -Leaf))
        $quotedArgs = @($argList | ForEach-Object {
            if ($_.Contains('"')) { throw "Unsupported quote in process argument" }
            '"' + $_.TrimEnd('\') + '"'
        })
        $p = Start-Process -FilePath $PythonExeAbs -ArgumentList $quotedArgs `
            -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $log -RedirectStandardError $errLog
        $registeredStart = (Get-Date).ToUniversalTime().ToString("o")
        try { $registeredStart = $p.StartTime.ToUniversalTime().ToString("o") } catch { }
        $script:manifest += [ordered]@{
            kind = $kind; name = $name; pid = $p.Id
            log = $log; err_log = $errLog
            started_utc = (Get-Date).ToUniversalTime().ToString("o")
            command = ($PythonExeAbs + " " + ($argList -join " "))
        }
        Append-PidLine $p.Id $kind $name $log
        # T55 实例登记（instances.json，锁内）：存活三要素据此核验
        $script:RegistryKept += [ordered]@{
            pid = $p.Id; kind = $kind; magic = $magicVal; mode = $Mode
            started_utc = $registeredStart
            log = $log; account_hint = $accountHint
            argv = @($PythonExeAbs) + $argList
            data_dir = $DataDirAbs; deployment_dir = $RepoRoot
            deployment_sha = $DeploymentSha; gateway_config = $GatewayConfigAbs
        }
        Write-InstancesFile $InstanceRegistryFile $script:RegistryKept
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
            Write-Host "       如确认旧进程已死，先核对旧日志尾部再人工清理 instances.json 该条目）" -ForegroundColor Yellow
            continue
        }
        $runnerArgs = @(
            (Join-Path $RepoRoot "scripts/live_runner.py"),
            "--data-dir", $DataDirAbs,
            "--loop",
            "--config", $c.FullName,
            "--gateway-config", $GatewayConfigAbs,
            "--risk-config", $RiskConfigAbs,
            "--sessions-config", $SessionsAbs,
            "--spread-model", $SpreadModelAbs
        )
        if ($Mode -eq "PaperSoak") {
            $runnerArgs += "--paper"
        }
        $runnerPids += Start-DemoProcess "runner" "$magic" $magic $runnerArgs
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
            (Join-Path $RepoRoot "scripts/risk_process.py"),
            "--data-dir", $DataDirAbs,
            "--loop",
            "--gw-config", $GatewayConfigAbs,
            "--interval", "10.0",
            "--config", $RiskConfigAbs,
            "--sessions-config", $SessionsAbs
        )
        if ($Mode -eq "PaperSoak") { $riskArgs += @("--paper-state", $PaperStateAbs) }
        Start-DemoProcess "risk" "account" $null $riskArgs | Out-Null
        Start-Sleep -Seconds 2
    }

    # 3) 一个对账 --loop（PaperSoak：--paper-state 同上；Demo：直连终端）；
    #    已有存活对账 → 跳过
    if ($aliveRoles.ContainsKey("reconcile")) {
        Write-Host ("跳过对账进程（已有存活 PID {0}——绝不并行第二份）" -f $aliveRoles["reconcile"]) -ForegroundColor Yellow
    } else {
        $reconArgs = @(
            (Join-Path $RepoRoot "scripts/live_reconcile.py"),
            "--data-dir", $DataDirAbs,
            "--loop",
            "--gw-config", $GatewayConfigAbs,
            "--interval", "30.0"
        )
        if ($Mode -eq "PaperSoak") { $reconArgs += @("--paper-state", $PaperStateAbs) }
        Start-DemoProcess "reconcile" "all" $null $reconArgs | Out-Null
    }

    # ---- C-1 run-manifest：锁临界区内追加（与实例登记同段，绝不覆盖 runs） ----
    # 本次有被跳过的存活实例 = 恢复重启（kind=restart）；全新拉起 = start。
    # 重建窗：runs 中当前 mode 最早 started_utc → now（重启不清窗，B 组
    # demo_check --accept 据此判定运行窗）。
    $runKind = "start"
    if ($aliveRunners.Count -gt 0 -or $aliveRoles.Count -gt 0) { $runKind = "restart" }
    $runs = @()
    if (Test-Path $RunManifestFile) {
        $existing = Get-Content -Raw -Encoding UTF8 $RunManifestFile | ConvertFrom-Json
        $runs = @($existing.runs | Where-Object { $null -ne $_ })
    }
    $runs += [ordered]@{ started_utc = Get-UtcStamp; log_dir = $LogDirAbs; kind = $runKind; mode = $Mode }
    $manifestObj = [ordered]@{
        schema_version = 1
        mode = $Mode
        magics = @($magics.Keys | Sort-Object | ForEach-Object { [int]$_ })
        runs = @($runs)
        account_hint = $accountHint
    }
    [IO.File]::WriteAllText($RunManifestFile, ($manifestObj | ConvertTo-Json -Depth 6) + "`n", $script:Utf8NoBom)

    # ---- 本次运行清单（LogDir 内，运维浏览用） ----
    $manifestFile = Join-Path $LogDirAbs ("demo-run-{0}-{1}.json" -f $Mode, $stamp)
    @{ started_utc = (Get-Date).ToUniversalTime().ToString("o")
       mode = $Mode
       repo_root = $RepoRoot
       config_dir = $ConfigDirAbs
       paper_state_dir = $PaperStateAbs
       skipped_alive_runners = @($aliveRunners.Keys | ForEach-Object { [int]$_ })
       processes = $manifest } | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $manifestFile

    Write-Host ""
    Write-Host ("模式 {0}：本次新启动 {1} 个进程（登记表：{2}；PID 历史日志（追加式）：{3}；运行清单：{4}；运行窗：{5}）" -f $Mode, $manifest.Count, $InstanceRegistryFile, $pidsFile, $manifestFile, $RunManifestFile)
    Write-Host "停止（killSwitch）：New-Item -ItemType File data/live/killSwitch   （先停开仓；恢复=只移除停机信号，重启仅针对已确认退出的进程）"
    Write-Host "停止（-Stop）      ：复用同一完整固定部署参数；先禁用 supervisor 并设置 killSwitch，见 runbook §6.3（整树核验，保留登记）"
    Write-Host "日常检查        ：$PythonExeAbs scripts/demo_check.py --day YYYY-MM-DD   或 --summary"
    Write-Host "阶段 F 验收     ：$PythonExeAbs scripts/demo_check.py --accept   （严格：无样本/未覆盖=未验收）"
} finally {
    if ($null -ne $script:InstancesLockHandle) {
        $script:InstancesLockHandle.Close()
        $script:InstancesLockHandle = $null
    }
}
