@echo off
rem Wallac instrument-microservice autostart (Phase C, doc 98).
rem Installed into 'lambda's Startup folder, so it runs natively AS lambda at
rem logon (with autologon enabled) -- no CreateProcessAsUser needed at boot.
rem 1) launch the OEM GUI (drives the ARCnet instrument connect),
rem 2) wait for it to connect, 3) run the agent in a relaunch (watchdog) loop.

set PY=C:\Users\lambda\AppData\Local\Programs\Python\Python38-32\python.exe
set PYW=C:\Users\lambda\AppData\Local\Programs\Python\Python38-32\pythonw.exe

cd /d "C:\Program Files\Wallac1420\Program"
start "" MlrMgr.exe

rem give MlrMgr time to connect the instrument (~45s); ping as a portable sleep
ping -n 46 127.0.0.1 >nul

rem lid_watcher: auto-Ignore the false LID-OPEN-ERROR modal (faulty lid sensor)
start "" "%PYW%" "C:\install\lid_watcher.py"

:agent
start /wait "" "%PY%" "C:\install\agent.py"
ping -n 6 127.0.0.1 >nul
goto agent
