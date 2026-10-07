<#
.SYNOPSIS
  Run a WSL script whose lifetime is owned by Task Scheduler, not by any
  terminal, agent or interop caller.

.DESCRIPTION
  The WSL VM stays up only while a wsl.exe client is attached. A job started
  from an interactive session -- or from a process inside WSL that is about
  to exit -- dies with the VM when that session's last client goes. A
  Scheduled Task's wsl.exe belongs to the Task Scheduler service instead, so
  the job survives whoever started it. This is the one place that mechanism
  lives; start_detached_soak.ps1 and the training handoff both use it.

  Refuses to start a second instance of a running task. After starting, it
  waits for -ReadyPattern to appear in WSL's process list, so a job whose
  pre-flight fails is reported instead of leaving a task that ran nothing.

.EXAMPLE
  .\start_detached_wsl.ps1 -TaskName OV-TrainingHandoff `
      -Script /home/jarvis_svc/jarvis/scripts/train/run_training_handoff.sh `
      -TimeLimitSeconds 43200 -ReadyPattern "training_handoff run"
#>
param(
    [Parameter(Mandatory)] [string]$TaskName,
    [Parameter(Mandatory)] [string]$Script,
    [string]$Arguments = "",
    [int]$TimeLimitSeconds = 86400,
    [string]$ReadyPattern = "",
    [int]$ReadySeconds = 90,
    [string]$FailureLogCommand = "",
    [string]$Distro = "Ubuntu",
    [string]$User = "jarvis_svc"
)
$ErrorActionPreference = "Stop"
$wsl = Join-Path $env:WINDIR "System32\wsl.exe"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq "Running") {
    throw "Task '$TaskName' is already running."
}

# conhost --headless: no console window to close by accident.
$action = New-ScheduledTaskAction -Execute (Join-Path $env:WINDIR "System32\conhost.exe") `
    -Argument "--headless `"$wsl`" -d $Distro -u $User -- bash $Script $Arguments"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds $TimeLimitSeconds) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Settings $settings `
    -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

if (-not $ReadyPattern) { Write-Output "Started '$TaskName'."; return }
$deadline = (Get-Date).AddSeconds($ReadySeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    if ((Get-ScheduledTask -TaskName $TaskName).State -ne "Running") {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        $log = if ($FailureLogCommand) { & $wsl -d $Distro -u $User -- bash -c $FailureLogCommand } else { "" }
        throw "Task '$TaskName' ended during start (LastTaskResult=$($info.LastTaskResult)).`n$log"
    }
    $pid_ = & $wsl -d $Distro -u $User -- pgrep -f $ReadyPattern
    if ($pid_) { Write-Output "Started '$TaskName': pid $($pid_ -join ',')."; return }
}
throw "Task '$TaskName' is running but '$ReadyPattern' did not appear within ${ReadySeconds}s."
