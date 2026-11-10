@echo off
setlocal
chcp 65001 >nul
title Codex Auto-Retry V2 Tests
cd /d "%~dp0"

set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

"%PYTHON_EXE%" -m pytest -q tests
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Test exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
