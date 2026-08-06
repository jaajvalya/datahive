@echo off
set RND=%~dp0
REM Open UI over HTTP — file:// often blocks fetch to localhost ("Failed to fetch").
start "" /B pythonw "%RND%connector_watchdog.py" 2>nul
if errorlevel 1 start "" /B python "%RND%connector_watchdog.py"
timeout /t 2 /nobreak >nul
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Method POST http://127.0.0.1:5056/ensure | Out-Null } catch {}"
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:5055/"
