@echo off
REM ═══════════════════════════════════════════════════════
REM  daybrief — Run the pipeline once, right now
REM  (Schedule daily runs with Task Scheduler — see docs/scheduling.md)
REM ═══════════════════════════════════════════════════════

cd /d "%~dp0"

echo.
echo ═══ daybrief — Pipeline Run ═══
echo Starting at %DATE% %TIME%
echo.

REM Activate virtual environment
if not exist ".venv\Scripts\activate.bat" (
    echo ERROR: Virtual environment not found. Run setup.bat first.
    pause & exit /b 1
)
call .venv\Scripts\activate.bat

REM Run the pipeline
python main.py

if errorlevel 1 (
    echo.
    echo ─────────────────────────────────────────────────
    echo  Pipeline finished with errors. Check logs\agent.log
    echo ─────────────────────────────────────────────────
) else (
    echo.
    echo ─────────────────────────────────────────────────
    echo  Pipeline complete!
    echo  Check your configured outputs for today's digest
    echo  Check logs\agent.log for the full run log
    echo ─────────────────────────────────────────────────
)

echo.
pause
