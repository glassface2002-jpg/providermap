@echo off
REM Convenience wrapper so you don't have to activate the venv every time.
REM   run.bat test
REM   run.bat scrape --dry-run --limit 50
REM   run.bat export
setlocal
cd /d "%~dp0"

REM UTF-8 console. Without this, provider names with accents raise
REM UnicodeEncodeError against the default cp1252 code page.
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Virtual environment not found. Run setup.ps1 first:
    echo     powershell -ExecutionPolicy Bypass -File setup.ps1
    echo.
    exit /b 1
)

".venv\Scripts\python.exe" run.py %*
exit /b %ERRORLEVEL%
