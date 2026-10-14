<#
.SYNOPSIS
  Keep the organism's WSL VM up independently of any cockpit, terminal or
  agent session: one Task-Scheduler-owned wsl.exe client that never exits.

.DESCRIPTION
  The WSL VM lives only while a wsl.exe client is attached. A daemon started
  by `ov` is not a client, so every soak or organism so far has died with
  whatever wsl.exe happened to be holding the VM -- an agent session
  (bt-2026-09-23-005910), a terminal, or an interactive-logon task torn down
  at a Windows sign-in (bt-2026-09-25-004828, 2.45 h). The keeper is the
  ONE client that does nothing but hold the VM.

  Principal, by evidence:
    * S4U ("run whether the user is logged on or not", no stored password)
      runs outside the interactive session, so sign-out, a session switch or
      a Hello re-sign-in cannot tear it down. Registering it needs an
      ELEVATED shell -- an unelevated attempt is refused ("Access is
      denied", measured 2026-10-13).
    * Interactive is the fallback when unelevated: it survives lock/unlock
      but not sign-out. The script says which one it installed.
  Either way the task restarts itself if its wsl.exe ever exits, starts at
  logon (and at boot under S4U), and has no execution time limit.

  The hold is the distro's own `sleep infinity` under `exec`, so the client
  costs one sleeping process and nothing else.

.EXAMPLE
  # once, from an ELEVATED PowerShell (recommended):
  .\install_wsl_keeper.ps1
  # unelevated (interactive fallback):
  .\install_wsl_keeper.ps1 -AllowInteractiveFallback
  .\install_wsl_keeper.ps1 -Status
  .\install_wsl_keeper.ps1 -Uninstall
#>
param(
    [string]$TaskName = "OV-WSLKeeper",
    [string]$Distro = "Ubuntu",
    [string]$User = "jarvis_svc",
    # Restart policy: how often, and how many times in a row, Task Scheduler
    # revives a keeper whose wsl.exe exited. Defaults mirror the scheduler's
    # own minimum interval; the count is effectively unbounded.
    [int]$RestartIntervalMinutes = 1,
    [int]$RestartCount = 9999,
    [switch]$AllowInteractiveFallback,
    [switch]$Status,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$wsl = Join-Path $env:WINDIR "System32\wsl.exe"
$conhost = Join-Path $env:WINDIR "System32\conhost.exe"

function Get-KeeperState {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { return "not installed" }
    $i = Get-ScheduledTaskInfo -TaskName $TaskName
    $uptime = (& $wsl -d $Distro -u $User -- cat /proc/uptime 2>$null) -split " " | Select-Object -First 1
    return "task=$($t.State) logon=$($t.Principal.LogonType) lastResult=$($i.LastTaskResult) vm_uptime_s=$uptime"
}

if ($Status) { Write-Output (Get-KeeperState); return }
if ($Uninstall) {
    # Unregistering ends the keeper's wsl.exe; the VM then lives only as long
    # as some other client does. Said, not hidden.
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed '$TaskName'. The VM now stops when its last other wsl.exe client exits."
    return
}

$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
            ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $elevated -and -not $AllowInteractiveFallback) {
    throw ("Not elevated. Re-run from an elevated PowerShell for the session-independent " +
           "(S4U) keeper, or pass -AllowInteractiveFallback for one that survives lock but not sign-out.")
}
$logonType = if ($elevated) { "S4U" } else { "Interactive" }

$action = New-ScheduledTaskAction -Execute $conhost `
    -Argument "--headless `"$wsl`" -d $Distro -u $User --exec /usr/bin/sleep infinity"
$triggers = @(New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME")
if ($elevated) { $triggers += New-ScheduledTaskTrigger -AtStartup }
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartInterval (New-TimeSpan -Minutes $RestartIntervalMinutes) -RestartCount $RestartCount `
    -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType $logonType -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

# Prove it holds: the task must be Running and the distro reachable.
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running") {
        Write-Output "Installed '$TaskName' ($logonType). $(Get-KeeperState)"
        if (-not $elevated) {
            Write-Output "NOTE: interactive keeper -- survives lock, not sign-out. Re-run elevated for S4U."
        }
        return
    }
}
$info = Get-ScheduledTaskInfo -TaskName $TaskName
throw "Keeper '$TaskName' did not reach Running (LastTaskResult=$($info.LastTaskResult))."
