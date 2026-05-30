# Ollama Optimizer

Optimize Ollama configuration based on system hardware analysis.

## When to Use

Use this skill when the user asks to optimize Ollama, configure Ollama, speed up Ollama, fix Ollama running slow, set up a local LLM, tune inference speed, reduce memory usage, or select models that fit their GPU/RAM. The skill analyzes hardware (GPU, VRAM, RAM, CPU) and produces tailored recommendations.

Do not use for LM Studio, llama.cpp, vLLM, or hosted-API LLM providers (OpenAI, Anthropic) — those use different runtimes and tuning surfaces.

## Repo Sync Before Edits (mandatory)

Before creating/updating/deleting files in an existing repository, sync the current branch with remote:

```bash
branch="$(git rev-parse --abbrev-ref HEAD)"
git fetch origin
git pull --rebase origin "$branch"
```

If the working tree is not clean, stash first, sync, then restore:

```bash
git stash push -u -m "pre-sync"
branch="$(git rev-parse --abbrev-ref HEAD)"
git fetch origin && git pull --rebase origin "$branch"
git stash pop
```

If `origin` is missing, pull is unavailable, or rebase/stash conflicts occur, stop and ask the user before continuing.

## Workflow

### Phase 1: System Detection

Run the detection script to gather hardware information:

```bash
python3 scripts/detect_system.py
```

Parse the JSON output to identify:
- OS and version
- CPU model and core count
- Total RAM / unified memory
- GPU type, VRAM, and driver version
- Current Ollama installation and environment variables

### Phase 2: Analyze and Recommend

Based on detected hardware, determine the optimization profile:

**Hardware Tier Classification:**

| Tier | Criteria | Max Model | Key Optimizations |
|------|----------|-----------|-------------------|
| CPU-only | No GPU detected | 3B | num_thread tuning, Q4_K_M quant |
| Low VRAM | <6GB VRAM | 3B | Flash attention, KV cache q4_0 |
| Entry | 6-8GB VRAM | 8B | Flash attention, KV cache q8_0 |
| Prosumer | 10-12GB VRAM | 14B | Flash attention, full offload |
| Workstation | 16-24GB VRAM | 32B | Standard config, Q5_K_M option |
| High-end | 48GB+ VRAM | 70B+ | Multiple models, Q5/Q6 quants |

**Apple Silicon Special Case:**
- Unified memory = shared CPU/GPU RAM
- 8GB Mac → treat as 6GB VRAM tier
- 16GB Mac → treat as 12GB VRAM tier
- 32GB+ Mac → treat as workstation tier

### Phase 3: Generate Optimization Plan

Create a structured optimization guide with these sections:

#### 1. System Overview
Present detected hardware specs and highlight constraints (e.g., "8GB unified memory limits to 7B models").

#### 2. Dependency Assessment
List what's needed based on the platform:
- macOS: Ollama only (Metal automatic)
- Linux NVIDIA: Ollama + NVIDIA driver 450+
- Linux AMD: Ollama + ROCm 5.0+
- Windows: Ollama + NVIDIA driver 452+

#### 3. Configuration Recommendations

**Essential environment variables:**
```bash
# Always recommended
export OLLAMA_FLASH_ATTENTION=1

# Memory-constrained systems (<12GB)
export OLLAMA_KV_CACHE_TYPE=q8_0  # or q4_0 for severe constraints
```
