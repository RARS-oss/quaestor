<#
.SYNOPSIS
  Registers the quaestor watchdog as a Windows scheduled task.

.DESCRIPTION
  Three triggers, each covering a different way the loop can silently disappear
  between now and the Sep 4 deadline:

    * every 5 minutes  — a crash, an OOM kill, a stray `wsl --shutdown`
    * at logon         — a reboot
    * daily at 16:15 local (09:15 ET), waking the machine — a laptop that slept
      through the open, which is the failure that would cost a whole trading day

  The task runs hidden and only while this user is logged on, so it needs no
  stored password and no elevation. The watchdog itself is idempotent: it starts
  the loop only when nothing is running, so overlapping triggers cannot produce
  two agents trading the same account.

  Remove with:  Unregister-ScheduledTask -TaskName quaestor-watchdog -Confirm:$false
#>
[CmdletBinding()]
param(
    [string]$TaskName = "quaestor-watchdog",
    [string]$Launcher = "C:\Users\Daniil\Desktop\alpaca-hack\quaestor\scripts\watchdog-silent.vbs",
    [string]$WakeAt   = "16:15"          # local time == 09:15 ET, 15 min before the open
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Launcher)) { throw "watchdog launcher not found: $Launcher" }

# Launched through wscript, not powershell.exe directly: Task Scheduler builds the
# console host before -WindowStyle Hidden can take effect, so running PowerShell
# straight from the task flashes a window on every run. wscript creates no console,
# and the .vbs starts PowerShell hidden from there.
$action = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument "//B //Nologo `"$Launcher`""

$triggers = @()

# Crash cover: repeat every 5 minutes, well past the contest deadline.
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 30)
$triggers += $repeat

# Reboot cover.
$triggers += New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

# Slept-through-the-open cover; -WakeToRun below makes this one wake the machine.
$triggers += New-ScheduledTaskTrigger -Daily -At $WakeAt

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "removed the previous $TaskName"
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings `
    -Description "Keeps the quaestor trading loop alive through the Alpaca contest week." | Out-Null

Write-Output "registered $TaskName"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
