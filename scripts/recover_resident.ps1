param([Parameter(Mandatory=$true)][string]$RuntimeRoot,
      [Parameter(Mandatory=$true)][string]$Entry,
      [Parameter(Mandatory=$true)][int]$ExpectedPid)
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName 'ImAgent-Offline-Resident'
if ($task.Description -ne 'im-agent-offline-resident-v3' -or $task.Principal.LogonType -ne 'Interactive' -or $task.Actions.Execute -notmatch 'pythonw?\.exe$') { throw 'Scheduled task ownership mismatch' }
$process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $ExpectedPid)
if (!$process -or !$process.CommandLine.Contains($Entry) -or !$process.CommandLine.Contains($RuntimeRoot) -or !$process.CommandLine.Contains(' run ')) { throw 'Supervisor process identity mismatch' }
Stop-Process -Id $ExpectedPid -Force
$deadline = (Get-Date).AddSeconds(10)
while ((Get-ScheduledTask -TaskName 'ImAgent-Offline-Resident').State -eq 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
Start-ScheduledTask -TaskName 'ImAgent-Offline-Resident'
Write-Output 'owned-idle-supervisor-recovery-requested'
