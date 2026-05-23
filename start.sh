#!/usr/bin/env bash
# start.sh — one-shot launcher for the brain (full stack)
#
# Default brings up everything: UI + voice (Deepgram/ElevenLabs) + auditory
# cortex (speaker enrollment, prosody) + DMN (idle thinking) + metacognition +
# motor cortex (tool use). Disable any of these with env-var overrides — see
# the CONFIG block below.
#
# Usage:
#   ./start.sh                   # full stack
#   FEATURES=minimal ./start.sh  # text-only UI, no voice or background loops
#   BRAIN_WIRING_FROZEN=true ./start.sh   # disable Hebbian weighted routing
#
# Ctrl+C shuts everything down cleanly.

set -euo pipefail

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[brain]${NC} $*"; }
ok()      { echo -e "${GREEN}[brain]${NC} $*"; }
warn()    { echo -e "${YELLOW}[brain]${NC} $*"; }
err()     { echo -e "${RED}[brain]${NC} $*" >&2; }

# ── CONFIG ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_PORT="${BRAIN_PORT:-8765}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
OLLAMA_MODELS=("qwen2.5:7b" "nomic-embed-text")

# FEATURES: which optional subsystems to launch.
#   full     — UI + voice + ears + DMN + metacognition + motor (default)
#   standard — UI + DMN + metacognition + motor (no voice)
#   minimal  — UI only (text in, text out)
#   custom   — honour individual BRAIN_* flags as-is (no flag injection)
FEATURES="${FEATURES:-full}"

# Optional motor cortex filesystem allowlist (colon-separated absolute paths).
# Defaults to the project directory so the entity can read/write its own code
# and second_brain. Remove or override to lock motor down.
BRAIN_MOTOR_PATHS="${BRAIN_MOTOR_PATHS:-$SCRIPT_DIR}"

# Inject feature flags based on FEATURES preset (unless caller chose 'custom')
BRAIN_ARGS=()
case "$FEATURES" in
    full)
        BRAIN_ARGS=(--ui --voice --ears --dmn --metacognition --motor)
        ;;
    standard)
        BRAIN_ARGS=(--ui --dmn --metacognition --motor)
        ;;
    minimal)
        BRAIN_ARGS=(--ui)
        ;;
    custom)
        # Caller passes args via BRAIN_EXTRA_ARGS
        if [[ -n "${BRAIN_EXTRA_ARGS:-}" ]]; then
            # shellcheck disable=SC2206
            BRAIN_ARGS=(${BRAIN_EXTRA_ARGS})
        else
            BRAIN_ARGS=(--ui)
        fi
        ;;
    *)
        err "Unknown FEATURES preset: '$FEATURES'. Use full | standard | minimal | custom."
        exit 1
        ;;
esac

export BRAIN_MOTOR_PATHS

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

# ── 5. Sanity-check API keys for the features we're starting ─────────────────
if [[ " ${BRAIN_ARGS[*]} " =~ " --voice " ]]; then
    if [[ -z "${DEEPGRAM_API_KEY:-}" ]]; then
        warn "DEEPGRAM_API_KEY is not set — voice input (STT) will be disabled at runtime."
    fi
    if [[ -z "${ELEVENLABS_API_KEY:-}" ]]; then
        warn "ELEVENLABS_API_KEY is not set — voice output (TTS) will be disabled at runtime."
    fi
fi

# ── 6. Start the brain ────────────────────────────────────────────────────────
info "Starting brain with: ${BRAIN_ARGS[*]}"
info "Motor allowlist: ${BRAIN_MOTOR_PATHS}"
cd "$SCRIPT_DIR"
uv run python -m brain.run "${BRAIN_ARGS[@]}" &
BRAIN_PID=$!

# ── 7. Wait for the UI server to be ready ────────────────────────────────────
info "Waiting for UI server on port ${BRAIN_PORT}..."
for i in $(seq 1 45); do
    if curl -sf "http://localhost:${BRAIN_PORT}/" &>/dev/null || \
       nc -z localhost "${BRAIN_PORT}" 2>/dev/null; then
        ok "UI server is ready."
        break
    fi
    if ! kill -0 "$BRAIN_PID" 2>/dev/null; then
        err "Brain process exited unexpectedly. Check the output above for errors."
        exit 1
    fi
    if [[ $i -eq 45 ]]; then
        warn "UI server didn't respond in time — opening browser anyway."
        break
    fi
    sleep 1
done

# ── 8. Open the browser ───────────────────────────────────────────────────────
info "Opening http://localhost:${BRAIN_PORT} in your browser..."
open "http://localhost:${BRAIN_PORT}" 2>/dev/null || \
    xdg-open "http://localhost:${BRAIN_PORT}" 2>/dev/null || \
    warn "Could not open browser automatically. Visit http://localhost:${BRAIN_PORT} manually."

ok "Brain is running (FEATURES=$FEATURES). Press Ctrl+C to stop everything."

# ── 9. Wait for brain process (keeps logs streaming to terminal) ──────────────
wait "$BRAIN_PID"
