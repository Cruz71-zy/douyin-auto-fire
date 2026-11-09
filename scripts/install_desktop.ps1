param([string]$Python = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$serviceScript = Join-Path $PSScriptRoot 'desktop_service.py'
$taskName = 'DouyinLocalDaily'
if (!$Python) { $Python = Join-Path $projectRoot '.venv\Scripts\pythonw.exe' }
if (!(Test-Path -LiteralPath $Python)) { throw 'Create the Python virtual environment first' }
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and !($existing.Actions.Arguments -match [regex]::Escape($serviceScript))) {
    throw 'An unrelated task uses this name; nothing changed'
}
$owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $Python -Argument ('"' + $serviceScript + '" --tick') -WorkingDirectory $projectRoot
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At '07:30'),
    (New-ScheduledTaskTrigger -AtLogOn -User $owner),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15))
)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'Local Douyin sender: 07:30, logon catch-up, SQLite duplicate prevention and private local state. Requires completed four-target setup.' -Force | Out-Null
$info = Get-ScheduledTaskInfo -TaskName $taskName
[pscustomobject]@{Task=$taskName;State=(Get-ScheduledTask -TaskName $taskName).State;NextRun=$info.NextRunTime.ToString('s')} | ConvertTo-Json -Compress
