@echo off
setlocal
chcp 65001 >nul
title Stop Codex Auto-Retry V2
cd /d "%~dp0"
set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
"%PYTHON_EXE%" "%~dp0codex_auto_retry_v2.py" --stop
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
