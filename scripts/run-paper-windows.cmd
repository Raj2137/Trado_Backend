@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Missing Windows virtual environment. Follow docs\windows-paper-setup.md.
    exit /b 1
)
if not exist ".env" (
    echo Missing .env. Configure the existing Neon database first.
    exit /b 1
)
if not exist "logs" mkdir "logs"
if not exist "logs" exit /b 1
rem Paper-only even if .env contains settings intended for a future live host.
set "PAPER_TRADING=true"
set "LIVE_TRADING_ENABLED=false"
set "LIVE_ENTRIES_ENABLED=false"
set "LLM_ADVISOR_ENABLED=false"
set "REQUIRE_PERSISTENT_DATABASE=true"
set "PYTHONUNBUFFERED=1"
rem Keep one previous log instead of allowing unlimited log growth.
for %%F in ("logs\paper-scheduler.log") do if exist "%%~F" if %%~zF GTR 10485760 move /y "%%~F" "logs\paper-scheduler.previous.log" >nul
echo [%date% %time%] Starting paper cycle>>"logs\paper-scheduler.log"
".venv\Scripts\python.exe" -m app.run_cycle --trade-mode paper >>"logs\paper-scheduler.log" 2>&1
set "TRADO_EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] Finished paper cycle, exit=%TRADO_EXIT_CODE%>>"logs\paper-scheduler.log"
exit /b %TRADO_EXIT_CODE%
