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

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq "Running") {
    throw "Task '$TaskName' is already running a soak. Use -Stop first."
}

# conhost --headless: no console window to close by accident.
$action = New-ScheduledTaskAction -Execute (Join-Path $env:WINDIR "System32\conhost.exe") `
    -Argument "--headless `"$wsl`" -d $Distro -u $User -- bash $Script $MaxWallSeconds"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds ($MaxWallSeconds + 3600)) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Settings $settings `
    -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

# A task whose pre-flight fails just ends; say so instead of leaving a
# registered task that silently ran nothing.
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    if ((Get-ScheduledTask -TaskName $TaskName).State -ne "Running") {
        $log = & $wsl -d $Distro -u $User -- bash -c 'tail -20 "$(ls -t ~/soak_logs/soak-*.log | head -1)"'
        throw "Task '$TaskName' ended during boot (LastTaskResult=$($info.LastTaskResult)).`n$log"
    }
    $pid_ = & $wsl -d $Distro -u $User -- pgrep -f "scripts/ouroboros_battle_test.py --production-soak"
    if ($pid_) {
        Write-Output "Started '$TaskName': daemon pid $($pid_ -join ',') (max wall ${MaxWallSeconds}s). Log: ~/soak_logs in WSL."
        return
    }
}
throw "Task '$TaskName' is running but no daemon appeared within 90s; check ~/soak_logs."
