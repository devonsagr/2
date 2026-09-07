@echo off
cd /d "%~dp0"
where pyw >nul 2>&1
if errorlevel 1 (
  start "" /min pythonw.exe "%~dp0tw_monitor.py" --server
) else (
  start "" /min pyw.exe "%~dp0tw_monitor.py" --server
)
timeout /t 1 /nobreak >nul
start "" "http://127.0.0.1:17997/"
