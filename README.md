# BonsAI IDE

A lightweight, privacy-first web IDE with a built-in local AI coding assistant.
No cloud. No API keys. Everything runs on your machine — **including on potato hardware**.

**Stack:** Monaco editor · Python 3 stdlib backend · [KoboldCPP](https://github.com/LostRuins/koboldcpp) local inference

---

## What this project is really about

This is an experiment in **how far a tiny 1-bit model can be pushed as a coding agent**.

The AI behind the agent is **[Bonsai-8B](https://prismml.com/news/bonsai-8b)** by [PrismML](https://prismml.com) — the first commercially viable 1-bit LLM, trained from scratch with 1-bit weights across every layer (embeddings, attention, MLP, LM head). At ~1.1 GB it scores 70.5 avg on benchmarks, beating Llama 3.1 8B while being 14× smaller. Speed varies by hardware — ~5–10 tok/s on CPU, ~80+ tok/s on an RTX 3060, ~370 tok/s on an RTX 4090. Most people would write a 1-bit model off as too weak to be useful for coding. The goal here was to prove otherwise by engineering around its limitations:

- **Staged pipeline** (analyze → research → plan → execute) so the model never has to do too many things at once
- **10-tool editing toolkit** with surgical tools (`replace_lines`, `replace_function`, `rename_symbol`, `append_file`) so the model rarely needs to rewrite a whole file
- **Automatic syntax checking** after every file write, with the exact broken line fed back so it can fix it with one `replace_lines` call
- **System-enforced testing** — the agent can't declare success until its code actually runs clean
- **Disconnect watchdog** + abort on client close so zombie generations don't pile up
- **FIM completion** offloaded to a separate tiny Qwen2.5-Coder-0.5B model so the 8B isn't taxed by every keystroke

The 1-bit model is the real ceiling — it can build working multi-file Python projects, refactor across files, and use web search to look up docs it doesn't know. It struggles with complex logic bugs and occasionally needs a nudge. Everything in the engine is built to get the most out of it.

If you swap in a Q4 or Q8 7B+ model the agent becomes noticeably more capable with no other changes.

---

## Features

- **Editor** — Monaco (VS Code engine): syntax highlighting, tabs, minimap, Ctrl+P quick-open, in-files search, Ctrl+S save
- **AI Chat** — streaming Q&A with the current file or selection as context; optional reasoning display
- **AI Agent** — give it a task; it reads, writes, runs files, and auto-tests its own work step by step
- **Inline completion** — ghost-text suggestions as you type (FIM model, debounced, Alt+\ to trigger manually)
- **Terminal** — run shell commands without leaving the browser (Ctrl+\`)
- **Run button** — execute Python, JavaScript, Shell, C/C++, Java, Go, Ruby, and more (F5)
- **Open Folder** — switch the workspace root from inside the IDE (🗁 button or Ctrl+Shift+O)
- **Think toggle** — enable/disable model reasoning (better answers vs. faster responses)

---

## Requirements

- **Python 3** (standard library only — nothing to pip-install)
- **[KoboldCPP](https://github.com/LostRuins/koboldcpp)** serving two models:
  | Model | Port | Purpose |
  |---|---|---|
  | [Bonsai-8B](https://huggingface.co/prism-ml/Bonsai-8B-gguf) (PrismML 1-bit LLM) | 5001 | Chat and agent |
  | Qwen2.5-Coder-0.5B-Q8 | 5002 | Inline FIM completion *(optional)* |

If the completion model (`:5002`) is not running, completions fall back to the 8B model.
If the 8B model (`:5001`) is not running, the editor still works — AI features just show "offline".

> **Windows note:** The agent and terminal use bash syntax (the model is trained on it). The app auto-detects Git Bash or WSL and uses it if present. Without either, commands fall back to `cmd.exe` and the agent may struggle. Install [Git for Windows](https://git-scm.com/download/win) (includes Git Bash) for the best experience.

---

## Quick start

### 1. Get KoboldCPP

Download the prebuilt binary from the [KoboldCPP releases](https://github.com/LostRuins/koboldcpp/releases).
Pick the CUDA build if you have an NVIDIA GPU, otherwise the CPU build.

### 2. Get the models

Download these GGUF files (e.g. from Hugging Face) into a `models/` folder:

- `Bonsai-8B-Q1_0.gguf` — PrismML 1-bit LLM (~1.1 GB) — [download](https://huggingface.co/prism-ml/Bonsai-8B-gguf)
- `Qwen2.5-Coder-0.5B-Q8_0.gguf` — FIM completion model (~0.5 GB, optional)

### 3. Start KoboldCPP

```bash
# Chat / agent model on port 5001
./koboldcpp Bonsai-8B-Q1_0.gguf --port 5001 --contextsize 32768 --quantkv q8_0

# Inline completion model on port 5002 (optional)
./koboldcpp Qwen2.5-Coder-0.5B-Q8_0.gguf --port 5002 --contextsize 4096
```

Add `--usecuda --gpulayers 99 --flashattention` if you have a CUDA GPU.

### 4. Start the IDE

```bash
python3 server.py [/path/to/workspace]
```

Open **http://127.0.0.1:3000** in your browser.

---

## One-command launcher (`~/bonsai`)

The companion `bonsai` script starts both models and the IDE with CUDA/CPU auto-detection:

```bash
~/bonsai start [workspace_dir]   # → http://127.0.0.1:3000
~/bonsai status                  # check what's running
~/bonsai stop                    # stop everything
```

Environment knobs:

| Variable | Effect |
|---|---|
| `BONSAI_FORCE_CPU=1` | Use CPU even if a GPU is present |
| `BONSAI_NO_COMPLETION=1` | Skip the FIM model (saves ~0.6 GB RAM) |
| `GPU_LAYERS=N` | Layers to offload to CUDA (default: 99 = all) |

---

## Desktop app (Electron)

An optional Electron wrapper opens the IDE as a native desktop window:

```bash
cd electron
npm install
npm start
```

Build a distributable with `npm run dist` → AppImage (Linux), exe (Windows), or dmg (macOS).
See `PACKAGING.md` for the full packaging roadmap.

---

## Server options

```
python3 server.py [workspace] [--port PORT] [--kcpp URL] [--complete-url URL]
```

| Option | Default | Purpose |
|---|---|---|
| `workspace` | `$HOME` | Root directory exposed to the IDE |
| `--port` | 3000 | IDE server port |
| `--kcpp` | http://127.0.0.1:5001 | KoboldCPP URL for the 8B model |
| `--complete-url` | http://127.0.0.1:5002 | KoboldCPP URL for the FIM model |

---

## Project layout

```
server.py          HTTP + SSE server (stdlib only) — file API, terminal, AI proxy
bonsai_engine.py   Model interaction: chat, agent loop, FIM completion, tool execution
frontend/          Monaco web IDE
  index.html
  js/app.js        Shell, keyboard shortcuts, layout
  js/bonsai.js     Chat, agent, inline completion client
  js/explorer.js   File tree
  js/terminal.js   Terminal panel
  js/settings.js   Settings panel
  css/style.css
electron/          Optional native desktop wrapper
start.sh           Background/foreground IDE process manager
PACKAGING.md       Roadmap for building a distributable installer
```

---

## How the AI works (brief)

The agent uses a hand-crafted tool-call loop over KoboldCPP's raw `/v1/completions` endpoint
(not the `tools=` param, which forces grammar-constrained sampling and is ~6× slower).
Each agent run goes through four phases: **Analyze → Research → Plan → Execute**,
with automatic syntax checking and test running after every file write.

Inline completion uses Qwen2.5-Coder's fill-in-the-middle (FIM) format so suggestions
respect code after the cursor, not just what came before.
