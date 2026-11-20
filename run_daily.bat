@echo off
:: ponytail: wrapper for Task Scheduler — retries once on failure, no visible window flicker
:: Official source of truth. %~dp0 = this repo's folder, so paths stay portable.
ping -n 1 8.8.8.8 >nul 2>&1
if errorlevel 1 (
    echo Waiting for network...
    timeout /t 30 /nobreak >nul
)
C:\Python314\python.exe "%~dp0src\watcher.py"
if errorlevel 1 (
    echo Watcher failed, retrying in 60s...
    timeout /t 60 /nobreak >nul
    C:\Python314\python.exe "%~dp0src\watcher.py"
)
C:\Python314\python.exe "%~dp0src\publish.py"
