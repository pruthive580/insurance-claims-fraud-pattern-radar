#!/usr/bin/env bash
# Start Fraud Pattern Radar. Uses the local venv (deps already installed).
#
# AI-mode narratives (optional — the app runs fine without any of this):
#   * Local Ollama (preferred, private, free):
#       - ensure a model is pulled, e.g.  ollama pull llama3.2
#       - this script auto-starts `ollama serve` if it isn't running
#       - pick a model with:  FRAUD_OLLAMA_MODEL=qwen3:8b ./run.sh
#   * Anthropic: export ANTHROPIC_API_KEY  (used only if Ollama isn't available)
#   * Force a backend:  FRAUD_LLM=ollama|anthropic|offline ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

# Default Ollama model for the SIU narratives + AI edge layers.
# qwen3:8b gives higher-fidelity extraction & rule proposals (a bit slower than
# llama3.2). Override with e.g. FRAUD_OLLAMA_MODEL=llama3.2:latest ./run.sh
export FRAUD_OLLAMA_MODEL="${FRAUD_OLLAMA_MODEL:-qwen3:8b}"

# Auto-start Ollama if it's installed but not yet serving.
if command -v ollama >/dev/null 2>&1; then
  if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "Starting Ollama server…"
    ollama serve >/tmp/ollama.log 2>&1 &
    for _ in $(seq 1 20); do
      curl -s http://localhost:11434/api/tags >/dev/null 2>&1 && break
      sleep 0.5
    done
  fi
fi

exec .venv/bin/python -m uvicorn fraud_radar.api:app --host 0.0.0.0 --port 8000 "$@"
