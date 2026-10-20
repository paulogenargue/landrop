# Starts landrop at login on Windows by dropping a shortcut into your Startup
# folder (no admin rights needed). Run from the repo root in PowerShell:
#   powershell -ExecutionPolicy Bypass -File autostart\install-windows.ps1
# To undo, delete landrop.lnk from the folder printed below.
$dir = (Resolve-Path "$PSScriptRoot\..").Path
$pythonw = Join-Path $dir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "No virtualenv at $dir\.venv; create it first (see README)." }
$startup = [Environment]::GetFolderPath("Startup")
$link = (New-Object -ComObject WScript.Shell).CreateShortcut("$startup\landrop.lnk")
$link.TargetPath = $pythonw
$link.Arguments = "`"$dir\server.py`" --log-file `"$dir\landrop.log`""
$link.WorkingDirectory = $dir
$link.Save()
Write-Host "Created $startup\landrop.lnk; landrop will start at every login."
# Start it now, while you're here: Windows Firewall asks once per executable,
# and this is the pythonw.exe that will run at login. Allow it on Private networks.
Start-Process "$startup\landrop.lnk"
Write-Host "Started landrop. If a firewall prompt appears, allow Private networks only."
