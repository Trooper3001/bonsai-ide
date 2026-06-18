# Shipping BonsAI as one app

Goal: a double-clickable app that "just works" and uses CUDA automatically at home,
CPU on the laptop. Here's the realistic architecture and the order to build it.

## The pieces and how they ship

```
BonsAI IDE (Electron)
├── frontend/            Monaco web IDE            → bundled in the app
├── main.js              spawns the stack, opens window → bundled
├── server.py            stdlib backend (file API, SSE, agent) → needs python3 OR PyInstaller-frozen
├── bonsai               control script (start/stop/status, CUDA auto-detect) → bundled
├── koboldcpp binary     model server                → ship the official PREBUILT binary
└── models/*.gguf        Bonsai-8B + Qwen2.5-Coder   → downloaded on first run (too big to bundle)
```

Why not one giant executable with everything? The two models are ~1.7 GB. Baking
them into an installer makes a 2 GB download that re-downloads on every update.
Every local-AI app (LM Studio, Ollama, Jan) ships a small app and fetches models
on first run. Do that.

## Acceleration (already done)

`~/kobald/_detect.sh` picks the backend at launch:
- NVIDIA GPU **and** a CUDA koboldcpp build present → `--usecuda --gpulayers 99 --flashattention`
- GPU but CPU-only build → CPU + a note to rebuild
- no GPU → `--usecpu`

So the **same app** uses CUDA at home and CPU on the laptop with no config. The one
prerequisite: a CUDA-capable koboldcpp on the home box — either rebuild
(`make LLAMA_CUBLAS=1` → `koboldcpp_cublas.so`) or drop in the official prebuilt
binary (it embeds all backends, so `_detect.sh`'s GPU branch just works).

## Build order

1. **✅ Unified launcher** — `bonsai start|stop|status|restart` (done) with CUDA auto-detect.
2. **✅ Electron shell** — `electron/` (done): spawns `bonsai start`, loads `:3000`.
3. **First-run model fetch** — on launch, if `models/*.gguf` missing, show a download
   screen that runs `download_model.sh` + the Qwen fetch with a progress bar.
4. **Bundle koboldcpp** — vendor the prebuilt binary per-platform; point `_detect.sh`/launch
   scripts at the bundled path instead of `~/kobald/koboldcpp`.
5. **Freeze python (optional)** — PyInstaller `server.py` so end users don't need python3.
6. **`npm run dist`** — electron-builder → AppImage / NSIS exe / dmg.

## Quickest path to "I can send this to a friend"

Steps 2 → 3 → 4 give a working installer. Step 5 (freezing python) is only needed
if the target machines don't already have python3. Step 1 and 2 are already done.
