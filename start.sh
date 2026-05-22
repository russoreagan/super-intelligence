#!/usr/bin/env bash
# start.sh — one-shot launcher for the brain + UI
# Usage: ./start.sh
# Ctrl+C shuts everything down cleanly.

set -euo pipefail

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[brain]${NC} $*"; }
ok()      { echo -e "${GREEN}[brain]${NC} $*"; }
warn()    { echo -e "${YELLOW}[brain]${NC} $*"; }
err()     { echo -e "${RED}[brain]${NC} $*" >&2; }

# ── config ────────────────────────────────────────────────────────────────────
BRAIN_PORT=8765
OLLAMA_PORT=11434
OLLAMA_MODELS=("qwen2.5:7b" "nomic-embed-text")
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Track background PIDs for clean shutdown
OLLAMA_STARTED=false
BRAIN_PID=""

cleanup() {
    echo ""
    info "Shutting down..."
    if [[ -n "$BRAIN_PID" ]]; then
        kill "$BRAIN_PID" 2>/dev/null && info "Brain stopped." || true
    fi
    if [[ "$OLLAMA_STARTED" == "true" ]]; then
        pkill -f "ollama serve" 2>/dev/null && info "Ollama stopped." || true
    fi
    exit 0
}
trap cleanup INT TERM

# ── 1. Check ollama is installed ──────────────────────────────────────────────
if ! command -v ollama &>/dev/null; then
    err "Ollama is not installed."
    err "Install it from https://ollama.com or run: brew install ollama"
    exit 1
fi

# ── 2. Start ollama serve if not already running ──────────────────────────────
if curl -sf "http://localhost:${OLLAMA_PORT}/api/tags" &>/dev/null; then
    ok "Ollama is already running."
else
    info "Starting Ollama in the background..."
    ollama serve &>/tmp/ollama.log &
    OLLAMA_STARTED=true

    info "Waiting for Ollama to be ready..."
    for i in $(seq 1 30); do
        if curl -sf "http://localhost:${OLLAMA_PORT}/api/tags" &>/dev/null; then
            ok "Ollama is ready."
            break
        fi
        if [[ $i -eq 30 ]]; then
            err "Ollama did not start in time. Check /tmp/ollama.log for details."
            exit 1
        fi
        sleep 1
    done
fi

# ── 3. Pull any missing models ────────────────────────────────────────────────
INSTALLED=$(ollama list 2>/dev/null || true)
for model in "${OLLAMA_MODELS[@]}"; do
    # ollama list shows "name:tag" — match loosely on the base name
    base="${model%%:*}"
    if echo "$INSTALLED" | grep -q "^${base}"; then
        ok "Model ${model} is already available."
    else
        info "Pulling model ${model} (this may take a few minutes on first run)..."
        ollama pull "$model"
        ok "Model ${model} ready."
    fi
done

# ── 4. Check uv is available ──────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    err "uv is not installed. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# ── 5. Start the brain with UI ────────────────────────────────────────────────
info "Starting brain with UI on port ${BRAIN_PORT}..."
cd "$SCRIPT_DIR"
uv run python -m brain.run --ui &
BRAIN_PID=$!

# ── 6. Wait for the UI server to be ready ────────────────────────────────────
info "Waiting for UI server to be ready..."
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${BRAIN_PORT}/" &>/dev/null || \
       nc -z localhost "${BRAIN_PORT}" 2>/dev/null; then
        ok "UI server is ready."
        break
    fi
    if ! kill -0 "$BRAIN_PID" 2>/dev/null; then
        err "Brain process exited unexpectedly. Check the output above for errors."
        exit 1
    fi
    if [[ $i -eq 30 ]]; then
        warn "UI server didn't respond in time — opening browser anyway."
        break
    fi
    sleep 1
done

# ── 7. Open the browser ───────────────────────────────────────────────────────
info "Opening http://localhost:${BRAIN_PORT} in your browser..."
open "http://localhost:${BRAIN_PORT}" 2>/dev/null || \
    xdg-open "http://localhost:${BRAIN_PORT}" 2>/dev/null || \
    warn "Could not open browser automatically. Visit http://localhost:${BRAIN_PORT} manually."

ok "Brain is running. Press Ctrl+C to stop everything."

# ── 8. Wait for brain process (keeps logs streaming to terminal) ──────────────
wait "$BRAIN_PID"
