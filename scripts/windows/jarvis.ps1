<#
.SYNOPSIS
  Start, stop, inspect or follow JARVIS (unified_supervisor.py) on native Windows.

.DESCRIPTION
  The Body runs natively on Windows (audio, screen, desktop control; under
  Remote Desktop the session redirects speakers and microphone to the client).
  The supervisor brings up its siblings from the declarations in .env:
  J-Prime Engine (JARVIS_JPRIME_START_CMD) and Reactor-Core in WSL
  (JARVIS_REACTOR_START_CMD). O+V (`ov`) stays in WSL.

  `start` launches the supervisor DETACHED (Win32_Process.Create, no handles
  shared with this shell), so it outlives the terminal and an RDP disconnect,
  and writes its output to %LOCALAPPDATA%\JARVIS\logs\supervisor-<time>.log.
  The interpreter runs in UTF-8 mode (-X utf8): Windows' default code page
  cannot encode the emoji the kernel logs, in any of its file handlers.

  First run creates the venv (%LOCALAPPDATA%\JARVIS\venvs\body, or
  JARVIS_BODY_VENV) from backend\requirements-windows.txt with uv.

.EXAMPLE
  jarvis.ps1 start              # detached; returns once the kernel answers
  jarvis.ps1 status
  jarvis.ps1 logs               # follow the current log
  jarvis.ps1 stop
  jarvis.ps1 foreground         # run in this console (rich UI), Ctrl+C to stop
  jarvis.ps1 start -SupervisorArgs '--debug'
#>
param(
  [ValidateSet('start', 'stop', 'status', 'logs', 'foreground')] [string]$Action = 'status',
  # Empty = the full kernel, exactly as `python unified_supervisor.py` boots
  # it. A lighter boot is an explicit choice: -SupervisorArgs '--skip-docker --skip-gcp'.
  [string]$SupervisorArgs = '',
  [int]$WaitSeconds = 300
)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Venv = if ($env:JARVIS_BODY_VENV) { $env:JARVIS_BODY_VENV } else { Join-Path $env:LOCALAPPDATA 'JARVIS\venvs\body' }
$Py = Join-Path $Venv 'Scripts\python.exe'
$State = Join-Path $env:LOCALAPPDATA 'JARVIS\body'
$Logs = Join-Path $env:LOCALAPPDATA 'JARVIS\logs'
$PidFile = Join-Path $State 'supervisor.pid'
$LogPointer = Join-Path $State 'supervisor.logpath'
New-Item -ItemType Directory -Force $State, $Logs | Out-Null

$KernelLock = Join-Path $env:USERPROFILE '.jarvis\locks\kernel.lock'

function Get-KernelPid {
  # The kernel's own lock is the authority, so a kernel started any way
  # (this launcher, `python unified_supervisor.py`, an IDE) is found. The
  # launcher's pid file covers the seconds before the kernel takes its lock.
  $candidates = @()
  try { $candidates += [int]((Get-Content $KernelLock -Raw | ConvertFrom-Json).pid) } catch {}
  if (Test-Path $PidFile) { try { $candidates += [int](Get-Content $PidFile -Raw) } catch {} }
  foreach ($p in $candidates) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction SilentlyContinue
    if ($proc -and $proc.CommandLine -match 'unified_supervisor\.py') { return $p }
  }
  return $null
}

function Initialize-Venv {
  if (Test-Path $Py) { return }
  $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
  if (-not $uv) { $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
  "creating venv $Venv (first run)"
  & $uv venv --python 3.11 $Venv | Out-Null
  & $uv pip install --python $Py -r (Join-Path $Repo 'backend\requirements-windows.txt')
  if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }
}

function Invoke-Supervisor([string]$cliArgs) {
  # Short control commands (--status / --shutdown) in this console. The kernel
  # logs to stderr; Windows PowerShell turns each such line into an error
  # record, so stderr is merged and passed through as plain text.
  $ErrorActionPreference = 'Continue'
  $env:PYTHONUTF8 = '1'
  Push-Location $Repo
  try { & $Py -X utf8 -u unified_supervisor.py $cliArgs.Split(' ') --ui plain --no-ansi 2>&1 | ForEach-Object { "$_" } }
  finally { Pop-Location }
}

switch ($Action) {
  'status' {
    $p = Get-KernelPid
    if (-not $p) { "JARVIS is not running"; exit 3 }
    "JARVIS kernel pid $p  log: $(Get-Content $LogPointer -ErrorAction SilentlyContinue)"
    Invoke-Supervisor '--status' | Where-Object { $_ -match '^\s+(State|PID|Uptime|Mode|Port|Tier):|RUNNING|NOT' }
    exit 0
  }
  'logs' {
    $log = Get-Content $LogPointer -ErrorAction SilentlyContinue
    if (-not $log -or -not (Test-Path $log)) { "no supervisor log recorded"; exit 3 }
    Get-Content $log -Tail 40 -Wait
  }
  'stop' {
    $p = Get-KernelPid
    if (-not $p) { "JARVIS is not running"; exit 0 }
    "asking kernel pid $p to shut down..."
    Invoke-Supervisor '--shutdown' | Out-Null
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline -and (Get-Process -Id $p -ErrorAction SilentlyContinue)) { Start-Sleep 1 }
    if (Get-Process -Id $p -ErrorAction SilentlyContinue) {
      "graceful shutdown timed out; stopping pid $p"
      Stop-Process -Id $p -Force
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    "stopped. J-Prime and Reactor keep running (stop them with their own launchers)."
    exit 0
  }
  'foreground' {
    Initialize-Venv
    $env:PYTHONUTF8 = '1'
    Push-Location $Repo
    try { & $Py -X utf8 unified_supervisor.py $SupervisorArgs.Split(' ', [StringSplitOptions]::RemoveEmptyEntries) }
    finally { Pop-Location }
  }
  'start' {
    $p = Get-KernelPid
    if ($p) { "JARVIS is already running (pid $p)"; exit 0 }
    Initialize-Venv
    $log = Join-Path $Logs ("supervisor-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Set-Content -Path $LogPointer -Value $log
    $cmd = "cmd.exe /d /c `"`"$Py`" -X utf8 -u unified_supervisor.py $SupervisorArgs --ui plain --no-ansi > `"$log`" 2>&1`""
    # Win32_Process.Create, not Start-Process: the kernel must share NO handles
    # with this shell (see prime\scripts\engine\jprime_engine.ps1).
    $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
      CommandLine = $cmd; CurrentDirectory = $Repo; ProcessStartupInformation = $startup }
    if ($r.ReturnValue -ne 0) { "Win32_Process.Create failed rc=$($r.ReturnValue)"; exit 1 }
    $shellPid = [int]$r.ProcessId
    # The venv's python.exe is a launcher that re-spawns the base interpreter;
    # the kernel is the deepest python under the cmd shell.
    $kernel = $null
    $deadline = (Get-Date).AddSeconds(30)
    while (-not $kernel -and (Get-Date) -lt $deadline) {
      Start-Sleep -Milliseconds 500
      $pys = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'unified_supervisor\.py' })
      $tree = @($shellPid)
      do {
        $next = @($pys | Where-Object { $tree -contains $_.ParentProcessId -and $tree -notcontains $_.ProcessId })
        $tree += $next.ProcessId
      } while ($next.Count -gt 0)
      $kernel = $pys | Where-Object { $tree -contains $_.ProcessId -and -not ($pys.ParentProcessId -contains $_.ProcessId) } |
        Select-Object -First 1
    }
    if (-not $kernel) { "kernel process did not appear; see $log"; exit 1 }
    Set-Content -Path $PidFile -Value $kernel.ProcessId
    "JARVIS starting (kernel pid $($kernel.ProcessId)); log: $log"
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline) {
      if (-not (Get-Process -Id $kernel.ProcessId -ErrorAction SilentlyContinue)) { "kernel exited during boot; see $log"; exit 1 }
      if (Select-String -Path $log -Pattern 'PHASE_END\] \[finalizing' -Quiet -ErrorAction SilentlyContinue) {
        $port = (Select-String -Path $log -Pattern 'Backend.*port (\d+)|selected port (\d+)' -ErrorAction SilentlyContinue | Select-Object -Last 1)
        "JARVIS is online. $(if ($port) { $port.Line.Trim() })"
        exit 0
      }
      Start-Sleep 2
    }
    "still booting after ${WaitSeconds}s (pid $($kernel.ProcessId)); follow with: jarvis.ps1 logs"
    exit 0
  }
}
