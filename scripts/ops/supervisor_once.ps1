# Runs the pinned deployment's supervisor once, as the scheduled task live_supervisor2 does.
#
# The task's action is wscript.exe //B //Nologo scripts\ops\hidden_task.vbs, which runs
# this script hidden and hands its exit code back (the launcher says why it exists and what
# its own exit codes 2 to 5 mean). The parameters are named after the options of
# live_supervisor.py, one for one, and go through unchanged.
#
# Log: the interpreter's stdout and stderr are appended, as the bytes it wrote, to
# <DataDir>\ops-evidence\supervisor\supervisor.log -- a "start" line, the output (stdout,
# then stderr), an "exit" line for each run. Nothing is decoded and encoded again on the
# way. Piping a native command with 2>&1 under ErrorActionPreference = Stop turns its
# first stderr line into a terminating error in PowerShell 5.1, and the exit code would be
# this script's instead of the interpreter's -- and a crash traceback is exactly what goes
# to stderr. The file is in the encoding the interpreter itself uses for redirected output
# on this machine (the system ANSI code page unless the user's environment says otherwise);
# this script's own lines use that code page too. Over 5 MB the log becomes
# supervisor.log.1; one generation is kept.
#
# Exit code: the supervisor's own. 3 when this script could not run it; the reason is in
# the log, because under the launcher nobody sees this script's error stream.
#
# The interpreter is an explicit parameter and is not looked up. It currently lives in the
# agent workspace; D112 requires production to leave the workspace before the formal
# window starts. That move is not made here.
#
# Every parameter except -Sha has to be an absolute path. live_supervisor.py refuses a
# relative one (verify_deployment: unsafe path); refusing it here first keeps the log where
# the absolute -DataDir says and nothing is started.

param(
    [string]$Directory = "",
    [string]$Sha = "",
    [string]$DataDir = "",
    [string]$ConfigDir = "",
    [string]$Python = "",
    [string]$GatewayConfig = "",
    [string]$RiskConfig = "",
    [string]$SessionsConfig = "",
    [string]$SpreadModel = "",
    [string]$LogDir = ""
)

$ErrorActionPreference = "Stop"

# The options of live_supervisor.py, in the order Deployment declares its fields. They are
# not declared Mandatory: asked for a missing one under a hidden window, PowerShell would
# prompt and the task would hang until its time limit.
$options = [ordered]@{
    "directory"       = $Directory
    "sha"             = $Sha
    "data-dir"        = $DataDir
    "config-dir"      = $ConfigDir
    "python"          = $Python
    "gateway-config"  = $GatewayConfig
    "risk-config"     = $RiskConfig
    "sessions-config" = $SessionsConfig
    "spread-model"    = $SpreadModel
    "log-dir"         = $LogDir
}
$limit = 5MB
$log = ""
# Only an absolute -DataDir says where the log is: a relative one would be relative to whatever
# the current directory is at the moment, and that changes at Set-Location below. It is refused
# below like every other relative path, and nothing is logged for it.
if ($DataDir -and [System.IO.Path]::IsPathRooted($DataDir)) { $log = Join-Path $DataDir "ops-evidence\supervisor\supervisor.log" }

function Append-Bytes([string]$path, [byte[]]$bytes) {
    $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write)
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
}
function Append-Text([string]$text) {
    Append-Bytes $log ([System.Text.Encoding]::Default.GetBytes($text))
}
function Stamp() { return "[{0:yyyy-MM-ddTHH:mm:ssZ}]" -f [DateTime]::UtcNow }
function Refuse([string]$reason) {
    # Under the launcher nobody sees this script's error stream: the log gets the reason
    # whenever the data directory says where the log is.
    $line = "$(Stamp) supervisor_once.ps1: $reason"
    if ($log) {
        try {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null
            Append-Text ($line + "`r`n")
        } catch { }
    }
    $host.UI.WriteErrorLine($line)
    exit 3
}
function Append-File([string]$path) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return }
    # Read/Write sharing: a process the interpreter started may have inherited the handle.
    $source = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
                                     [System.IO.FileShare]::ReadWrite)
    $buffer = New-Object System.IO.MemoryStream
    try { $source.CopyTo($buffer) } finally { $source.Dispose() }
    $bytes = $buffer.ToArray()
    $buffer.Dispose()
    Append-Bytes $log $bytes
    if ($bytes.Length -gt 0 -and $bytes[$bytes.Length - 1] -ne 10) { Append-Bytes $log ([byte[]](13, 10)) }
}

foreach ($name in @($options.Keys)) {
    $value = $options[$name]
    if ([string]::IsNullOrWhiteSpace($value)) { Refuse "-$name was not given" }
    if ($value.Contains('"')) { Refuse "-$name contains a double quote" }
    if ($value.EndsWith("\")) { Refuse "-$name ends with a backslash" }
    if ($name -ne "sha" -and -not [System.IO.Path]::IsPathRooted($value)) { Refuse "-$name is not an absolute path" }
}
$supervisor = Join-Path $Directory "scripts\live_supervisor.py"
if (-not (Test-Path -LiteralPath $Directory -PathType Container)) { Refuse "deployment directory not found: $Directory" }
if (-not (Test-Path -LiteralPath $supervisor -PathType Leaf)) { Refuse "supervisor script not found: $supervisor" }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { Refuse "interpreter not found: $Python" }
try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null
    Set-Location -LiteralPath $Directory       # the task's working directory is not inherited through wscript
} catch {
    Refuse ("cannot use the log directory or the deployment directory: " + $_.Exception.Message)
}
if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt $limit) {
    Move-Item -LiteralPath $log -Destination "$log.1" -Force
}

$argumentList = @("-u", $supervisor)
foreach ($name in @($options.Keys)) { $argumentList += @("--$name", $options[$name]) }
$commandLine = ($argumentList | ForEach-Object { '"' + $_ + '"' }) -join " "
# A new pair of files for every run: a leftover process may still hold the previous pair.
$stamp = "{0:yyyyMMdd'T'HHmmssfff'Z'}-{1}" -f [DateTime]::UtcNow, $PID
$stdoutFile = Join-Path (Split-Path -Parent $log) "supervisor.$stamp.stdout.tmp"
$stderrFile = Join-Path (Split-Path -Parent $log) "supervisor.$stamp.stderr.tmp"

$started = [DateTime]::UtcNow
Append-Text ("$(Stamp) start`r`n")
try {
    # Not Start-Process -Wait: it waits for the whole process tree, and a role that the
    # supervisor restarts stays up for days. The process handle is waited for instead.
    $process = Start-Process -FilePath $Python -ArgumentList $commandLine -WorkingDirectory $Directory `
        -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile -NoNewWindow -PassThru
} catch {
    Refuse ("cannot run " + $Python + ": " + $_.Exception.Message)
}
# Held from here on: asked for only after the process has exited, ExitCode comes back empty.
$exitCode = $null
try {
    $null = $process.Handle
    $process.WaitForExit()
    $exitCode = $process.ExitCode
} catch { }
$seconds = ([DateTime]::UtcNow - $started).TotalSeconds

foreach ($file in @($stdoutFile, $stderrFile)) {
    try { Append-File $file }
    catch { Append-Text ("$(Stamp) supervisor_once.ps1: could not read {0}: {1}`r`n" -f (Split-Path -Leaf $file), $_.Exception.Message) }
    try { Remove-Item -LiteralPath $file -ErrorAction Stop } catch { }
}
if ($null -eq $exitCode) {
    Append-Text ("$(Stamp) supervisor_once.ps1: the exit code could not be read (the process ended before its handle was held)`r`n")
    $exitCode = 3
}
Append-Text ("$(Stamp) exit {0} ({1:N1} s)`r`n" -f $exitCode, $seconds)
exit $exitCode
