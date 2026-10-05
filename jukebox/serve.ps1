# MusicMate launcher: keeps the server up and its logs bounded.
#
# Runs server.py in a loop, restarting it a few seconds after any exit, so a
# crash or an unhandled error costs seconds rather than a trip to the machine.
# Logs go to logs\server-*.log, rotated at each start and when they pass 5 MB,
# five kept. Registered as the Scheduled Task "MusicMate" (see register-task.ps1);
# run by hand it behaves the same way.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File serve.ps1 [-Port 80]  (default 80 since 3 Sep 2026)
#
# To stop it for good, create the file logs\STOP: the loop checks between runs,
# and a launcher started afterwards refuses while the file is there. Delete it
# to serve again. Killing python alone just gets it restarted, and killing the
# launcher only buys five minutes -- the task retries (see register-task.ps1).

param([int]$Port = 80)

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Logs = Join-Path $Here 'logs'
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$Stop = Join-Path $Logs 'STOP'
# STOP means stop, and keeps meaning it. This used to delete the file and carry
# on, which was harmless while the only way in was a logon; with the task now
# retrying every five minutes that would have undone the stop a few minutes
# later, for ever. Delete logs\STOP yourself to start serving again.
if (Test-Path $Stop) {
    "$(Get-Date -Format s) logs\STOP present; not starting" | Add-Content (Join-Path $Logs 'launcher.log')
    exit 0
}

function Rotate([string]$base) {
    # base.log -> base.1.log -> ... -> base.5.log, oldest dropped
    for ($i = 4; $i -ge 1; $i--) {
        $from = Join-Path $Logs "$base.$i.log"; $to = Join-Path $Logs "$base.$($i + 1).log"
        if (Test-Path $from) { Move-Item -Force $from $to }
    }
    $cur = Join-Path $Logs "$base.log"
    if (Test-Path $cur) { Move-Item -Force $cur (Join-Path $Logs "$base.1.log") }
}

# refuse to double up: another copy (or a hand-started server) already owns the port
$owner = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($owner) {
    "$(Get-Date -Format s) port $Port already in use by pid $($owner.OwningProcess); not starting" |
        Add-Content (Join-Path $Logs 'launcher.log')
    exit 0
}

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$attempt = 0
while (-not (Test-Path $Stop)) {
    Rotate 'server'
    $log = Join-Path $Logs 'server.log'
    $attempt++
    $t0 = Get-Date
    "$(Get-Date -Format s) starting server.py on port $Port (attempt $attempt)" | Add-Content (Join-Path $Logs 'launcher.log')
    $p = Start-Process -FilePath 'python' -ArgumentList 'server.py', '--port', "$Port" -WorkingDirectory $Here `
        -RedirectStandardOutput $log -RedirectStandardError (Join-Path $Logs 'server.err.log') -PassThru -WindowStyle Hidden -Wait
    $ran = ((Get-Date) - $t0).TotalSeconds
    "$(Get-Date -Format s) server exited with code $($p.ExitCode) after $([int]$ran)s" | Add-Content (Join-Path $Logs 'launcher.log')
    if (Test-Path $Stop) { break }
    # a fast crash loop should not spin the CPU; a healthy long run resets the count
    if ($ran -gt 600) { $attempt = 0 }
    $pause = if ($attempt -gt 5) { 60 } else { 5 }
    Start-Sleep -Seconds $pause
}
"$(Get-Date -Format s) launcher stopped" | Add-Content (Join-Path $Logs 'launcher.log')
