# BonsAI IDE — Desktop client (Electron)

A thin native shell over the web IDE. It runs `~/bonsai start` (which auto-picks
CUDA or CPU), waits for the IDE on `:3000`, then loads it in a window. Closing the
window stops the stack (only if the app started it).

## Run it (dev)

```bash
cd ~/projects/bonsai_dev/electron
npm install          # downloads Electron (~150 MB, one time)
npm start            # opens the BonsAI IDE window
```

Requires the `bonsai` launcher in `$HOME` (or set `BONSAI_BIN=/path/to/bonsai`).

## Build a distributable

```bash
npm run dist         # electron-builder → AppImage (Linux) / exe (Win) / dmg (Mac)
# output in dist/
```

## What gets bundled vs. fetched

The Electron app is small. The heavy pieces are **not** baked into the binary:

| Piece | Size | Strategy |
|---|---|---|
| Electron + frontend + main.js | ~150 MB | bundled by electron-builder |
| Python backend (`server.py`) | tiny | needs `python3` on the machine (stdlib only), or freeze with PyInstaller |
| KoboldCPP | ~50 MB | ship the official prebuilt binary (has CUDA+CPU+Vulkan) instead of the source build |
| Models (Bonsai-8B 1.1 GB + Qwen 0.5 GB) | ~1.7 GB | **download on first run** (`~/kobald/download_model.sh`) — do NOT bake into the installer |

See `../PACKAGING.md` for the full single-installer plan.
