@echo off
setlocal
chcp 65001 >nul
title Codex Auto-Retry V2
cd /d "%~dp0"
set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
"%PYTHON_EXE%" "%~dp0codex_auto_retry_v2.py" --check
if errorlevel 1 goto check_failed
echo Starting V2. Fixed cooldown: 2 seconds. Log: logs\auto-retry.log
echo A running original or V2 instance will prevent duplicate startup.
"%PYTHON_EXE%" -u "%~dp0codex_auto_retry_v2.py"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Program exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
:check_failed
echo ERROR: Preflight failed. Review the message above.
echo Install dependencies: "%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
pause
exit /b 1
