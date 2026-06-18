// BonsAI IDE — Electron main process.
// Self-contained: bundles the IDE backend, downloads KoboldCPP + models on
// first run, auto-picks CUDA or CPU, starts everything, opens a native window.

const { app, BrowserWindow, shell } = require("electron");
const { spawn, spawnSync } = require("child_process");
const http  = require("http");
const https = require("https");
const path  = require("path");
const os    = require("os");
const fs    = require("fs");

// ── paths ─────────────────────────────────────────────────────────────────────

const IS_WIN = process.platform === "win32";

// Detect Python executable — Windows may have 'python' or 'py', not 'python3'
function findPython() {
  for (const candidate of ["python3", "python", "py"]) {
    const r = spawnSync(candidate, ["--version"], { timeout: 3000 });
    if (r.status === 0) return candidate;
  }
  return null;
}
const PYTHON = findPython();

// Bundled IDE backend (inside the AppImage/exe under resources/backend/)
const BACKEND = app.isPackaged
  ? path.join(process.resourcesPath, "backend")
  : path.join(__dirname, "..");   // dev: repo root

// User data dir — models + KoboldCPP live here, persists between updates
const DATA       = path.join(os.homedir(), ".bonsai-ide");
const KCPP_DIR   = path.join(DATA, "koboldcpp");
const MODELS_DIR = path.join(DATA, "models");

const KCPP_BIN_CPU  = path.join(KCPP_DIR, IS_WIN ? "koboldcpp.exe"      : "koboldcpp-linux-x64");
const KCPP_BIN_CUDA = path.join(KCPP_DIR, IS_WIN ? "koboldcpp_cu12.exe" : "koboldcpp-linux-x64-cuda12");
const MODEL_MAIN    = path.join(MODELS_DIR, "Bonsai-8B-Q1_0.gguf");
const MODEL_FIM     = path.join(MODELS_DIR, "Qwen2.5-Coder-0.5B-Q8_0.gguf");

// Download URLs
const URLS = {
  kcpp_cpu:   IS_WIN
    ? "https://github.com/LostRuins/koboldcpp/releases/latest/download/koboldcpp.exe"
    : "https://github.com/LostRuins/koboldcpp/releases/latest/download/koboldcpp-linux-x64",
  kcpp_cuda:  IS_WIN
    ? "https://github.com/LostRuins/koboldcpp/releases/latest/download/koboldcpp_cu12.exe"
    : "https://github.com/LostRuins/koboldcpp/releases/latest/download/koboldcpp-linux-x64-cuda12",
  model_main: "https://huggingface.co/prism-ml/Bonsai-8B-gguf/resolve/main/Bonsai-8B-Q1_0.gguf",
  model_fim:  "https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF/resolve/main/qwen2.5-coder-0.5b-instruct-q8_0.gguf",
};

const IDE_URL = "http://127.0.0.1:3000";
let win = null;
const procs = [];

// ── ui helpers ────────────────────────────────────────────────────────────────

function ui(status, pct) {
  if (!win) return;
  if (status !== undefined)
    win.webContents.executeJavaScript(`window.setStatus(${JSON.stringify(status)})`).catch(() => {});
  if (pct !== undefined)
    win.webContents.executeJavaScript(`window.setProgress(${pct})`).catch(() => {});
}

function uiError(msg) {
  if (!win) return;
  win.webContents.executeJavaScript(`window.setError(${JSON.stringify(msg)})`).catch(() => {});
}

// ── checks ────────────────────────────────────────────────────────────────────

function hasCuda() {
  try {
    const r = spawnSync("nvidia-smi", ["--query-gpu=name", "--format=csv,noheader"],
                        { timeout: 3000 });
    return r.status === 0 && r.stdout && r.stdout.toString().trim().length > 0;
  } catch { return false; }
}

function ideUp() {
  return new Promise((resolve) => {
    const req = http.get(IDE_URL, (r) => { r.resume(); resolve(r.statusCode === 200); });
    req.on("error", () => resolve(false));
    req.setTimeout(1500, () => { req.destroy(); resolve(false); });
  });
}

async function waitForIDE(timeoutMs = 300000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await ideUp()) return true;
    await new Promise(r => setTimeout(r, 1500));
  }
  return false;
}

function apiUp(port) {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${port}/v1/models`, (r) => {
      r.resume(); resolve(r.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(2000, () => { req.destroy(); resolve(false); });
  });
}

async function waitForApi(port, label, timeoutMs = 300000) {
  const t0 = Date.now();
  let dots = 0;
  while (Date.now() - t0 < timeoutMs) {
    if (await apiUp(port)) return true;
    dots++;
    ui(`Waiting for ${label}${".".repeat((dots % 3) + 1)}`);
    await new Promise(r => setTimeout(r, 2000));
  }
  return false;
}

// ── download (resumable) ──────────────────────────────────────────────────────

function download(url, dest, label) {
  return new Promise((resolve, reject) => {
    const tmp = dest + ".part";
    const resume = fs.existsSync(tmp) ? fs.statSync(tmp).size : 0;
    const headers = resume ? { Range: `bytes=${resume}-` } : {};

    const doGet = (url) => {
      const mod = url.startsWith("https") ? https : http;
      mod.get(url, { headers }, (res) => {
        if (res.statusCode === 301 || res.statusCode === 302)
          return doGet(res.headers.location);
        if (res.statusCode !== 200 && res.statusCode !== 206)
          return reject(new Error(`HTTP ${res.statusCode}`));

        const total = resume + parseInt(res.headers["content-length"] || "0", 10);
        let got = resume;
        const out = fs.createWriteStream(tmp, { flags: resume ? "a" : "w" });

        res.on("data", (chunk) => {
          out.write(chunk);
          got += chunk.length;
          if (total > 0) {
            const pct = Math.round((got / total) * 100);
            const gb = (got / 1e9).toFixed(2);
            const total_gb = (total / 1e9).toFixed(2);
            ui(`Downloading ${label} — ${gb} / ${total_gb} GB`, pct);
          } else {
            ui(`Downloading ${label} — ${(got / 1e6).toFixed(1)} MB`);
          }
        });
        res.on("end",   () => { out.end(); fs.renameSync(tmp, dest); resolve(); });
        res.on("error", (e) => { out.destroy(); reject(e); });
      }).on("error", reject);
    };
    doGet(url);
  });
}

// ── first-run setup ───────────────────────────────────────────────────────────

async function setup() {
  fs.mkdirSync(KCPP_DIR,    { recursive: true });
  fs.mkdirSync(MODELS_DIR,  { recursive: true });

  const cuda    = hasCuda();
  const kcppBin = cuda ? KCPP_BIN_CUDA : KCPP_BIN_CPU;
  const kcppUrl = cuda ? URLS.kcpp_cuda : URLS.kcpp_cpu;

  // KoboldCPP binary
  if (!fs.existsSync(kcppBin)) {
    ui(`Downloading KoboldCPP (${cuda ? "CUDA ⚡" : "CPU"})…`, 0);
    await download(kcppUrl, kcppBin, "KoboldCPP");
    if (!IS_WIN) fs.chmodSync(kcppBin, 0o755);
  }

  // Bonsai-8B main model
  if (!fs.existsSync(MODEL_MAIN)) {
    ui("Downloading Bonsai-8B — 1.1 GB (one-time)…", 0);
    await download(URLS.model_main, MODEL_MAIN, "Bonsai-8B");
  }

  // Qwen2.5-Coder FIM completion model (optional — skip if previously failed)
  const skipFim = path.join(DATA, ".skip-fim");
  if (!fs.existsSync(MODEL_FIM) && !fs.existsSync(skipFim)) {
    ui("Downloading completion model — 0.5 GB…", 0);
    try {
      await download(URLS.model_fim, MODEL_FIM, "completion model");
    } catch {
      fs.writeFileSync(skipFim, "");  // don't retry next launch
    }
  }

  return { cuda, kcppBin };
}

// ── process launchers ─────────────────────────────────────────────────────────

function startKcpp(bin, model, port, cudaFlags) {
  const p = spawn(bin, [
    "--model", model,
    "--port",  String(port),
    "--host",  "127.0.0.1",
    "--contextsize",  "32768",
    "--quantkv",      "q8_0",
    "--threads",      "4",
    "--blasthreads",  "4",
    "--batchsize",    "512",
    "--multiuser",    "8",
    "--skiplauncher",
    ...cudaFlags,
  ], { stdio: "ignore" });
  procs.push(p);
  return p;
}

function startIDE(workspace) {
  if (!PYTHON) throw new Error("Python not found. Install Python 3 from https://python.org and make sure to tick 'Add to PATH'.");
  const server = path.join(BACKEND, "server.py");
  const logFile = path.join(DATA, "ide.log");
  const out = fs.openSync(logFile, "w");
  const p = spawn(PYTHON, [server, workspace || os.homedir(), "--port", "3000"],
                  { stdio: ["ignore", out, out] });
  p.on("exit", (code) => {
    if (code !== 0 && code !== null) {
      const log = fs.existsSync(logFile) ? fs.readFileSync(logFile, "utf8").slice(-400) : "";
      uiError(`IDE crashed (exit ${code}).\n\n${log}`);
    }
  });
  procs.push(p);
  return p;
}

// ── window ────────────────────────────────────────────────────────────────────

function createWindow() {
  win = new BrowserWindow({
    width: 1440, height: 900, title: "BonsAI IDE",
    backgroundColor: "#1e1e1e",
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  win.loadFile(path.join(__dirname, "loading.html"));
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(IDE_URL)) { shell.openExternal(url); return { action: "deny" }; }
    return { action: "allow" };
  });
  win.on("closed", () => { win = null; });
}

// ── boot ──────────────────────────────────────────────────────────────────────

async function boot() {
  createWindow();

  // If the IDE is already running (e.g. started via ~/bonsai), just load it
  if (await ideUp()) { win.loadURL(IDE_URL); return; }

  try {
    ui("Checking setup…", -1);
    const { cuda, kcppBin } = await setup();
    const cudaFlags = cuda ? ["--usecuda", "--gpulayers", "99", "--flashattention"] : [];

    // Bonsai-8B on :5001
    if (!await apiUp(5001)) {
      ui(`Starting Bonsai-8B (${cuda ? "CUDA ⚡" : "CPU"})…`, -1);
      startKcpp(kcppBin, MODEL_MAIN, 5001, cudaFlags);
      await waitForApi(5001, "Bonsai-8B");
    }

    // Completion model on :5002 (if downloaded)
    if (fs.existsSync(MODEL_FIM) && !await apiUp(5002)) {
      ui("Starting completion model…", -1);
      startKcpp(kcppBin, MODEL_FIM, 5002, cudaFlags);
      await waitForApi(5002, "completion model");
    }

    // IDE server
    ui("Starting IDE…", -1);
    startIDE(process.argv[2]);
    const ok = await waitForIDE();

    if (ok && win) win.loadURL(IDE_URL);
    else uiError("IDE failed to start. Make sure python3 is installed and try again.");

  } catch (err) {
    uiError(`Setup failed: ${err.message}`);
  }
}

// ── lifecycle ─────────────────────────────────────────────────────────────────

app.whenReady().then(boot);
app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) boot(); });
app.on("window-all-closed", () => {
  for (const p of procs) { try { p.kill(); } catch (_) {} }
  app.quit();
});
