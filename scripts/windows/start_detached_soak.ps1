<#
.SYNOPSIS
  Start (or gracefully stop) an O+V soak whose lifetime is owned by Task
  Scheduler, not by any terminal or agent session.

.DESCRIPTION
  The WSL VM stays up only while a wsl.exe client is attached. Soak
  bt-2026-09-23-005910 was held by a wsl.exe inside an interactive session;
  when that session closed the VM stopped under the soak, with no shutdown
  sequence and nothing recorded. A Scheduled Task's wsl.exe belongs to the
  Task Scheduler service instead, so it survives the session that started it.

  -Stop sends SIGTERM to the supervisor INSIDE WSL, so the harness writes its
  own ending. Never Stop-ScheduledTask a running soak: killing the wsl.exe
  stops the VM, which is the very death this exists to prevent.

.EXAMPLE
  .\start_detached_soak.ps1                      # 6 h Sentinel soak
  .\start_detached_soak.ps1 -MaxWallSeconds 7200
  .\start_detached_soak.ps1 -Stop
#>
param(
    [int]$MaxWallSeconds = 21600,
    [string]$Distro = "Ubuntu",
    [string]$User = "jarvis_svc",
    [string]$Script = "/home/jarvis_svc/jarvis/scripts/soak/launch_supervised_soak.sh",
    [string]$TaskName = "OV-Soak",
    [switch]$Stop
)
$ErrorActionPreference = "Stop"
$wsl = Join-Path $env:WINDIR "System32\wsl.exe"

if ($Stop) {
    & $wsl -d $Distro -u $User -- pkill -TERM -f "battle_test.terminal_supervisor"
    Write-Output "SIGTERM sent; the task ends when the harness has written its summary."
    return
}

# The detached-task mechanism lives in start_detached_wsl.ps1 (shared with
# the training handoff); this script only supplies the soak's specifics.
& (Join-Path $PSScriptRoot "start_detached_wsl.ps1") -TaskName $TaskName -Script $Script `
    -Arguments "$MaxWallSeconds" -TimeLimitSeconds ($MaxWallSeconds + 3600) `
    -ReadyPattern "scripts/ouroboros_battle_test.py --production-soak" `
    -FailureLogCommand 'tail -20 "$(ls -t ~/soak_logs/soak-*.log | head -1)"' `
    -Distro $Distro -User $User
