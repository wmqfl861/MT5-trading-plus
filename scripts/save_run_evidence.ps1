<#
.SYNOPSIS
    T180：部署启动时留存锚点证据副本（manifest/launcher/instances）。

.DESCRIPTION
    把 demo_run.ps1 本次启动产生的 run-manifest.json、本次运行清单
    （launcher 盘存）与最终 instances.json 原字节复制到
    <LiveDir>\evidence\run-<RunIndex>\{manifest,launcher,instances}.json，
    并在输出里逐个打印 sha256（供 phase-f-log 部署记录引用）。

    只做复制与打印，不改任何原件；任一复制失败打印 [X] 并以退出码 1
    结束（进程此时已由调用方启动，失败含义是"本次启动不能作正式窗口
    锚点"，不是"启动失败"）。

.PARAMETER LiveDir
    <DataDir>\live 目录（evidence/ 建在其下）。
.PARAMETER RunIndex
    本次启动在 run-manifest runs 里的零基序号。
.PARAMETER Manifest
    run-manifest.json 完整路径。
.PARAMETER Launcher
    本次运行清单（demo-run-*.json）完整路径。
.PARAMETER Instances
    instances.json 完整路径。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$LiveDir,
    [Parameter(Mandatory = $true)][int]$RunIndex,
    [Parameter(Mandatory = $true)][string]$Manifest,
    [Parameter(Mandatory = $true)][string]$Launcher,
    [Parameter(Mandatory = $true)][string]$Instances
)

$ErrorActionPreference = "Stop"

$items = @(
    @{ name = "manifest";  src = $Manifest },
    @{ name = "launcher";  src = $Launcher },
    @{ name = "instances"; src = $Instances }
)

$evidenceDir = Join-Path $LiveDir ("evidence\run-" + $RunIndex)
New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null

$failed = $false
foreach ($item in $items) {
    $dst = Join-Path $evidenceDir ($item.name + ".json")
    try {
        if (-not (Test-Path -LiteralPath $item.src -PathType Leaf)) {
            throw "source missing: $($item.src)"
        }
        # 原字节复制（含 BOM/换行原样），证据以字节为准。
        [IO.File]::Copy($item.src, $dst, $true)
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $dst).Hash.ToLowerInvariant()
        Write-Output ("evidence run-{0}/{1}.json sha256={2}" -f $RunIndex, $item.name, $hash)
    } catch {
        $failed = $true
        Write-Output ("[X] evidence run-{0}/{1}.json retention failed: {2}" -f $RunIndex, $item.name, $_.Exception.Message)
    }
}

if ($failed) {
    Write-Output "anchor evidence incomplete: this start cannot anchor a formal window; processes may already be running - investigate and redeploy."
    exit 1
}
exit 0
