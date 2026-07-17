@echo off
REM ═══════════════════════════════════════════════════════
REM  daybrief — Windows Setup Script
REM  Double-click setup.bat OR run in Command Prompt / PowerShell
REM ═══════════════════════════════════════════════════════

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ═══ daybrief — Windows Setup ═══
echo Project path: %CD%
echo.

REM ── 1. Check Python ──────────────────────────────────
echo [1/7] Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found.
    echo  Download Python 3.11+ from https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during install.
    pause & exit /b 1
)
for /f "tokens=*" %%i in ('python --version') do echo  Found: %%i

REM ── 2. Create virtual environment ────────────────────
echo.
echo [2/7] Creating virtual environment...
if exist ".venv" (
    echo  .venv already exists — skipping creation
) else (
    python -m venv .venv
    echo  Created .venv
)

REM ── 3. Activate venv and install packages ─────────────
echo.
echo [3/7] Installing Python dependencies (includes PyTorch — allow a few GB)...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt
if errorlevel 1 (
    echo  ERROR: pip install failed. Check your internet connection.
    pause & exit /b 1
)
echo  All packages installed.

REM ── 4. Create personal config files from the examples ─
echo.
echo [4/7] Creating personal config files...
if not exist "config\settings.yaml" (
    copy "config\settings.example.yaml" "config\settings.yaml" >nul
    echo  Created config\settings.yaml — edit it with your outputs/credentials.
) else (
    echo  config\settings.yaml already exists — leaving it untouched.
)
if not exist "config\watchlist.yaml" (
    copy "config\watchlist.example.yaml" "config\watchlist.yaml" >nul
    echo  Created config\watchlist.yaml — edit it with your topics.
) else (
    echo  config\watchlist.yaml already exists — leaving it untouched.
)
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo  Created .env — add secrets there, never in the YAML files.
)

REM ── 5. Initialise SQLite database ─────────────────────
echo.
echo [5/7] Initialising database...
python -c "from agent.db import init_db; init_db(); print('  Database ready: db/news.sqlite')"

REM ── 6. Download sentence-transformer model ────────────
echo.
echo [6/7] Downloading sentence-transformer model (~90 MB, one-time)...
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2'); print('  Model ready.')"

REM ── 7. Check Ollama ───────────────────────────────────
echo.
echo [7/7] Checking Ollama...
ollama --version >nul 2>&1
if errorlevel 1 (
    echo  WARNING: Ollama not found or not in PATH.
    echo  ─────────────────────────────────────────────────────
    echo  Install Ollama for Windows from: https://ollama.com/download
    echo  After installing, open a new terminal and run:
    echo      ollama pull llama3.2
    echo  ─────────────────────────────────────────────────────
    echo  OR: Switch to an OpenAI-compatible API in config\settings.yaml:
    echo      llm.provider: "openai"
) else (
    echo  Ollama found. Pulling llama3.2 model...
    echo  (This downloads ~2 GB on first run — go make a coffee)
    ollama pull llama3.2
    echo  Model ready.
)

REM ── Done ──────────────────────────────────────────────
echo.
echo ════════════════════════════════════════════════════
echo  Setup complete!
echo ════════════════════════════════════════════════════
echo.
echo  NEXT STEPS:
echo.
echo  1. Edit config\settings.yaml
echo       - Enable the outputs you want (Obsidian path, email, Telegram)
echo.
echo  2. Edit config\watchlist.yaml
echo       - Add your topics, companies, and regions
echo.
echo  3. Put any secrets (SMTP password, API keys) in .env
echo.
echo  4. Run the pipeline once:
echo       run.bat
echo.
echo  5. To run every morning, schedule run.bat with Task Scheduler
echo       (see docs\scheduling.md for the one-line command)
echo.
pause
