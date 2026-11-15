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
echo  [1/4] Closing Island if it is running (a running island breaks the install)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like python* -and $_.CommandLine -match island2\.py|island_watchdog } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
echo  [2/4] Installing packages (one time, 1-3 min)...
python -m pip install --upgrade --quiet -r requirements.txt
python check_qt.py
if errorlevel 1 goto fixqt
:qtok
echo  [3/4] Adding Island to Windows startup...
python island2.py --install-startup
echo  [4/4] Starting Island...
start "" pythonw "%~dp0island2.py"
echo.
echo  Done. Look at the top of your screen.
echo  Hover it, click it, right-click it. Drag it to a side edge for the side island.
timeout /t 8 >nul
exit /b 0
:fixqt
echo.
echo  Qt WebEngine is broken - reinstalling PySide6 clean (big download, 2-5 min)...
python -m pip install --force-reinstall --no-cache-dir --quiet PySide6 PySide6_Essentials PySide6_Addons shiboken6
python check_qt.py
if not errorlevel 1 goto qtok
echo.
echo  Still missing. Your antivirus (Bitdefender, Defender...) probably quarantined it.
echo  Restore Qt6WebEngineCore.dll / QtWebEngineProcess.exe from its quarantine, add an exception
echo  for the PySide6 folder printed above, then run setup.bat again.
pause
exit /b 1
