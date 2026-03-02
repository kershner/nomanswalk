@echo off
echo Waiting for network connectivity...

:WAIT_FOR_NETWORK
ping -n 1 8.8.8.8 >nul 2>&1
if errorlevel 1 (
    echo Network not available, retrying in 5 seconds...
    timeout /t 5 /nobreak >nul
    goto WAIT_FOR_NETWORK
)

echo Network is available!
echo Starting No Man's Walk...

venv\Scripts\python start_no_mans_walk.py --mode twitch