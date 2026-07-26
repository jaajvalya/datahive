# Installs connector_watchdog.py to run hidden at Windows logon (current user).
$ErrorActionPreference = "Stop"
$Rnd = Split-Path -Parent $MyInvocation.MyCommand.Path
$Watchdog = Join-Path $Rnd "connector_watchdog.py"
$Pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $Pythonw) {
  $Pythonw = (Get-Command python -ErrorAction Stop).Source -replace "python.exe", "pythonw.exe"
  if (-not (Test-Path $Pythonw)) { $Pythonw = (Get-Command python -ErrorAction Stop).Source }
}
$Startup = [Environment]::GetFolderPath("Startup")
$Shortcut = Join-Path $Startup "DataHive UI Watchdog.lnk"
$Wsh = New-Object -ComObject WScript.Shell
$Link = $Wsh.CreateShortcut($Shortcut)
$Link.TargetPath = $Pythonw
$Link.Arguments = "`"$Watchdog`""
$Link.WorkingDirectory = $Rnd
$Link.WindowStyle = 7
$Link.Description = "Starts DataHive connector API when main.html is open"
$Link.Save()
Write-Host "Installed:" $Shortcut
Write-Host "Starting watchdog now..."
Start-Process -FilePath $Pythonw -ArgumentList "`"$Watchdog`"" -WorkingDirectory $Rnd -WindowStyle Hidden
