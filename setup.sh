#!/usr/bin/env bash
# setup.sh — First-time setup for daybrief (Linux / macOS)
# Run: bash setup.sh

set -e
echo "═══ daybrief — Setup ═══"

# 1. Python virtual environment
echo "→ Creating Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
echo "→ Installing Python dependencies (this includes PyTorch — allow a few GB)..."
pip install --upgrade pip
pip install -r requirements.txt

# 3. Create personal config files from the examples (if not present)
echo "→ Creating personal config files..."
if [ ! -f config/settings.yaml ]; then
    cp config/settings.example.yaml config/settings.yaml
    echo "  Created config/settings.yaml — edit it with your outputs/credentials."
else
    echo "  config/settings.yaml already exists — leaving it untouched."
fi
if [ ! -f config/watchlist.yaml ]; then
    cp config/watchlist.example.yaml config/watchlist.yaml
    echo "  Created config/watchlist.yaml — edit it with your topics."
else
    echo "  config/watchlist.yaml already exists — leaving it untouched."
fi
if [ ! -f .env ]; then
    cp .env.example .env
    echo "  Created .env — add secrets there (never in the YAML files)."
fi

# 4. Initialise database
echo "→ Initialising SQLite database..."
python3 -c "from agent.db import init_db; init_db(); print('  DB initialised.')"

# 5. Download sentence-transformer model (used for embeddings + KeyBERT)
echo "→ Pre-downloading sentence-transformer model (all-MiniLM-L6-v2, ~90 MB)..."
python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2'); print('  Model ready.')"

# 6. Check Ollama
echo ""
echo "→ Checking Ollama..."
if command -v ollama &>/dev/null; then
    echo "  Ollama found. Pulling llama3.2 model (~2 GB on first run)..."
    ollama pull llama3.2
else
    echo "  ⚠️  Ollama not found. Install from https://ollama.com and run: ollama pull llama3.2"
    echo "     OR set llm.provider to 'openai' in config/settings.yaml (works with any OpenAI-compatible API)"
fi

echo ""
echo "═══ Setup complete ═══"
echo ""
echo "Next steps:"
echo "  1. Edit config/settings.yaml — enable the outputs you want (Obsidian path, email, Telegram)"
echo "  2. Edit config/watchlist.yaml — add your topics, companies, and regions"
echo "  3. Put any secrets (SMTP password, API keys) in .env"
echo "  4. Run once:  source .venv/bin/activate && python3 main.py"
echo ""
echo "  To run every morning, schedule it with cron (see docs/scheduling.md):"
echo "  30 7 * * * cd $(pwd) && .venv/bin/python3 main.py >> logs/cron.log 2>&1"
