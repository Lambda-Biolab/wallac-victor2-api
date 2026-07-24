@echo off
rem Wallac instrument-microservice autostart (Phase C, doc 98).
rem Installed into 'lambda's Startup folder, so it runs natively AS lambda at
rem logon (with autologon enabled) -- no CreateProcessAsUser needed at boot.
rem 1) launch the OEM GUI (drives the ARCnet instrument connect),
rem 2) wait for it to connect, 3) run the agent in a relaunch (watchdog) loop.
rem
rem Issue #31: ensure C:\ProgramData\Wallac\ exists before the agent starts
rem so the (restricted-ACL) bearer-token file at
rem C:\ProgramData\Wallac\agent_token.txt can be written by deploy tooling.
rem The directory is created with default inheritance; restrict the ACL via
rem `icacls C:\ProgramData\Wallac /inheritance:r /grant:r "SYSTEM:(OI)(CI)F"
rem  "Administrators:(OI)(CI)F" "lambda:(OI)(CI)R"` after the first deploy.

set PY=C:\Users\lambda\AppData\Local\Programs\Python\Python38-32\python.exe
set PYW=C:\Users\lambda\AppData\Local\Programs\Python\Python38-32\pythonw.exe

if not exist "C:\ProgramData\Wallac" mkdir "C:\ProgramData\Wallac" >nul 2>&1

cd /d "C:\Program Files\Wallac\Wallac1420\Program"
start "" MlrMgr.exe

rem give MlrMgr time to connect the instrument (~45s); ping as a portable sleep
ping -n 46 127.0.0.1 >nul

rem lid_watcher: auto-Ignore the false LID-OPEN-ERROR modal (faulty lid sensor)
start "" "%PYW%" "C:\install\lid_watcher.py"

:agent
start /wait "" "%PY%" "C:\install\agent.py"
ping -n 6 127.0.0.1 >nul
goto agent
