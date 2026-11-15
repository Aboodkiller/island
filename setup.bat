@echo off
title Island setup
cd /d "%~dp0"
echo.
where python >nul 2>&1
if errorlevel 1 (
  echo  Python is not installed. Get Python 3.12+ from python.org and TICK "Add python.exe to PATH".
  start "" https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
echo  [1/3] Installing packages (one time, 1-3 min)...
python -m pip install --upgrade --quiet -r requirements.txt
echo  [2/3] Adding Island to Windows startup...
python island2.py --install-startup
echo  [3/3] Starting Island...
start "" pythonw "%~dp0island2.py"
echo.
echo  Done. Look at the top of your screen.
echo  Hover it, click it, right-click it. Drag it to a side edge for the side island.
timeout /t 8 >nul
