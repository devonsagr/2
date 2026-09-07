@echo off
cd /d "%~dp0"
if exist "%~dp0release\ClashNodeMonitor.exe" (
  start "" "%~dp0release\ClashNodeMonitor.exe" --server --open-browser
  exit /b 0
)
where pyw >nul 2>&1
if errorlevel 1 (
  start "" /min pythonw.exe "%~dp0clash_node_monitor.py" --server --open-browser
) else (
  start "" /min pyw.exe "%~dp0clash_node_monitor.py" --server --open-browser
)
