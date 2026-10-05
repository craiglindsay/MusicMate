# Register (or refresh) the Scheduled Task that starts MusicMate at logon and
# keeps watch over it.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File register-task.ps1
#
# Runs serve.ps1 hidden when this user logs on, restarts it if it fails, never
# times out (the default for a scheduled task is three days), and starts it on
# battery too. This account is not an administrator, so the trigger is logon
# rather than boot: the server comes back when you sign in. From an elevated
# PowerShell the same task can be made to start at boot instead:
#
#   $t = New-ScheduledTaskTrigger -AtStartup
#   Set-ScheduledTask -TaskName MusicMate -Trigger $t -User "$env:USERDOMAIN\$env:USERNAME" -Password (Read-Host 'password')
#
# (a task that runs with nobody logged on needs stored credentials, hence the password).

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Serve = Join-Path $Here 'serve.ps1'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Serve`"" `
    -WorkingDirectory $Here

# Two triggers. Logon starts the server when you sign in. The second one is a
# watchdog: it tries to start the task every five minutes for ever, and
# MultipleInstances IgnoreNew means a launcher that is already running is left
# alone, so the attempt costs nothing. RestartCount only covers a task that
# FAILS, and the launcher has three times been killed outright instead --
# once by a Ctrl-C-class exit, once by Windows running short of memory, once
# unexplained -- and each time nothing brought it back until the next logon
# (9, 12 and 20 Sep 2026). A repetition trigger is the one recovery that does
# not need an administrator.
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$watch = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
# "for ever" is an EMPTY duration in the task schema; [TimeSpan]::MaxValue is
# rejected as out of range (P99999999DT23H59M59S).
$watch.Repetition.Duration = ''
$watch.Repetition.StopAtDurationEnd = $false

$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'MusicMate' -Action $action -Trigger @($logon, $watch) -Settings $settings `
    -Description 'MusicMate jukebox server (serve.ps1 keeps server.py running; retried every 5 min if it dies)' -Force | Out-Null
Get-ScheduledTask -TaskName 'MusicMate' | Select-Object TaskName, State
(Get-ScheduledTask -TaskName 'MusicMate').Triggers |
    Select-Object @{n = 'Trigger'; e = { $_.CimClass.CimClassName } }, @{n = 'Every'; e = { $_.Repetition.Interval } }
