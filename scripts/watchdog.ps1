<#
.SYNOPSIS
  Keeps the quaestor trading loop alive, unattended, for the rest of the contest.

.DESCRIPTION
  The loop is market-hours aware and sleeps to the next open by itself, so it
  only needs to exist continuously — the risk is not that it stops trading, but
  that its process quietly goes away and nobody notices until the deadline. It
  lives inside WSL, so a reboot, a `wsl --shutdown`, a laptop sleep or a crash
  all end it silently.

  Run every few minutes (see install-watchdog.ps1) this script:
    * starts the loop if no process is running;
    * during market hours, restarts it if the heartbeat has gone stale — a hung
      process is worse than a dead one, because a dead one gets replaced;
    * leaves it completely alone otherwise, including all night, when the loop
      is legitimately asleep and writes no heartbeat.

  Idempotent and safe to run at any time. Never places, cancels or touches an
  order; it only supervises the process.

.NOTES
  Log: runs/watchdog.log next to the repo.
#>
[CmdletBinding()]
param(
    [string]$RepoWindows = "C:\Users\Daniil\Desktop\alpaca-hack\quaestor",
    [string]$RepoWsl     = "/mnt/c/Users/Daniil/Desktop/alpaca-hack/quaestor",
    # ABSOLUTE, not ~ : this string is both the launch command and the pgrep
    # pattern, and a tilde inside single quotes is never expanded, so an anchored
    # match against it silently finds nothing and the watchdog concludes the loop
    # is dead while it is running happily.
    [string]$Venv        = "/home/daniil/hack/venv/bin/python",
    [int]$IntervalSec    = 300,
    [int]$StaleMinutes   = 15
)

$ErrorActionPreference = "Stop"
$logPath = Join-Path $RepoWindows "runs\watchdog.log"

function Write-Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    try { Add-Content -Path $logPath -Value $line -Encoding utf8 } catch { }
    Write-Output $line
}

# The loop's own process, matched on the interpreter path so the pattern can
# never match the shell that is doing the matching (that mistake once killed the
# loop, its shell, and then WSL itself).
function Get-LoopPid {
    $out = & wsl.exe -e bash -lc "pgrep -f '^$Venv' | head -1" 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $trimmed = ($out | Out-String).Trim()
    if ($trimmed -match '^\d+$') { return [int]$trimmed }
    return $null
}

function Start-Loop {
    # The trailing `sleep` is load-bearing, not politeness. `& disown` backgrounds
    # the whole chain and bash then exits immediately; WSL tears the session down
    # with it and the child dies before it ever execs. Holding the launching shell
    # open for a few seconds lets the loop become a process WSL will keep alive.
    $cmd = "cd $RepoWsl && mkdir -p runs && nohup env QUAESTOR_SEALED=1 PYTHONUNBUFFERED=1 $Venv -u -m quaestor loop --interval $IntervalSec >> runs/loop.log 2>&1 < /dev/null & disown; sleep 12"
    & wsl.exe -e bash -lc $cmd 2>$null | Out-Null
    return (Get-LoopPid)
}

# Eastern time drives every market decision here; the host is on Cyprus time.
function Get-EasternNow {
    $tz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    return [System.TimeZoneInfo]::ConvertTime([DateTimeOffset]::Now, $tz)
}

function Test-MarketHours {
    $et = Get-EasternNow
    if ($et.DayOfWeek -eq 'Saturday' -or $et.DayOfWeek -eq 'Sunday') { return $false }
    $minutes = $et.Hour * 60 + $et.Minute
    return ($minutes -ge (9 * 60 + 30)) -and ($minutes -lt (16 * 60))
}

# Age of runs/heartbeat.json in minutes, or $null when it cannot be read. The
# loop writes no heartbeat while it sleeps overnight, which is why staleness is
# only ever consulted during market hours.
function Get-HeartbeatAgeMinutes {
    $hb = Join-Path $RepoWindows "runs\heartbeat.json"
    if (-not (Test-Path $hb)) { return $null }
    try {
        $data = Get-Content $hb -Raw | ConvertFrom-Json
        $ts = [double]$data.ts
        $age = ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $ts) / 60.0
        return [math]::Round($age, 1)
    } catch { return $null }
}

$loopPid = Get-LoopPid

if (-not $loopPid) {
    Write-Log "loop not running -> starting"
    $new = Start-Loop
    if ($new) { Write-Log "started, pid=$new" } else { Write-Log "START FAILED - check runs/loop.log" }
    exit 0
}

if (-not (Test-MarketHours)) {
    Write-Log "alive pid=$loopPid (outside market hours, no heartbeat expected)"
    exit 0
}

$age = Get-HeartbeatAgeMinutes
if ($null -eq $age) {
    Write-Log "alive pid=$loopPid (heartbeat unreadable; leaving it alone)"
    exit 0
}

if ($age -gt $StaleMinutes) {
    Write-Log "HUNG: pid=$loopPid heartbeat ${age}min old during market hours -> restarting"
    & wsl.exe -e bash -lc "kill $loopPid" 2>$null | Out-Null
    Start-Sleep -Seconds 3
    $new = Start-Loop
    if ($new) { Write-Log "restarted, pid=$new" } else { Write-Log "RESTART FAILED - check runs/loop.log" }
} else {
    Write-Log "alive pid=$loopPid heartbeat ${age}min old"
}
