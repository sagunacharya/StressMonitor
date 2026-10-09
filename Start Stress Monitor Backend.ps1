$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root 'backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$app = Join-Path $backend 'app.py'
$pidFile = Join-Path $root '.stress_monitor_backend.pid'
$stdoutLog = Join-Path $root '.stress_monitor_backend.log'
$stderrLog = Join-Path $root '.stress_monitor_backend_error.log'

if (-not (Test-Path -LiteralPath $python)) { throw "Virtual-environment Python not found: $python" }
if (-not (Test-Path -LiteralPath $app)) { throw "Backend app not found: $app" }

# Quote app.py because repository paths may contain spaces. Start-Process
# returns the Python process ID used by Stop Stress Monitor.bat.
$argumentList = '"' + $app + '"'
$process = Start-Process -FilePath $python -ArgumentList $argumentList `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
[System.IO.File]::WriteAllText($pidFile, "$($process.Id)`r`n")
Write-Output "Started Stress Monitor backend (PID $($process.Id))."
