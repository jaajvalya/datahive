@echo off
set RND=%~dp0
start "" /B pythonw "%RND%connector_watchdog.py" 2>nul
if errorlevel 1 start "" /B python "%RND%connector_watchdog.py"
timeout /t 1 /nobreak >nul
start "" "%RND%main.html"
