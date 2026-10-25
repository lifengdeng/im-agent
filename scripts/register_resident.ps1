param([Parameter(Mandatory=$true)][string]$RuntimeRoot,
      [Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$Entry,
      [switch]$StartNow)
$ErrorActionPreference = 'Stop'
$taskName = 'ImAgent-Offline-Resident'
if ([IO.Path]::GetFileName($Python) -ne 'pythonw.exe' -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Windowless Python required for resident task' }
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$old = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($old -and $old.Description -ne 'im-agent-offline-resident-v3') { throw 'Unrelated scheduled task preserved' }
$action = New-ScheduledTaskAction -Execute $Python -Argument ('-I -B -X utf8 "' + $Entry + '" run --runtime-root "' + $RuntimeRoot + '"')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Description 'im-agent-offline-resident-v3' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
$saved = Get-ScheduledTask -TaskName $taskName
if ($saved.Principal.LogonType -ne 'Interactive' -or $saved.Principal.RunLevel -ne 'Limited') { throw 'Unexpected task principal' }
if ($StartNow) { Start-ScheduledTask -TaskName $taskName }
Write-Output 'offline-current-user-task-registered'
