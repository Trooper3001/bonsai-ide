// BonsAI IDE — Electron main process.
// Boots the whole stack via the `bonsai` control script (auto CPU/CUDA), waits
// for the IDE to answer on :3000, then loads it in a native window. Stops the
// stack on quit (only if this app started it).
const { app, BrowserWindow, shell } = require("electron");
const { spawn, execFile } = require("child_process");
const http = require("http");
const path = require("path");
const os = require("os");
const fs = require("fs");

const IDE_URL = "http://127.0.0.1:3000";
// the `bonsai` control script lives in $HOME by default; override with BONSAI_BIN
const BONSAI = process.env.BONSAI_BIN || path.join(os.homedir(), "bonsai");

let win = null;
let weStartedIt = false;

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
    await new Promise((r) => setTimeout(r, 1500));
  }
  return false;
}

function createWindow() {
  win = new BrowserWindow({
    width: 1440, height: 900, title: "BonsAI IDE",
    backgroundColor: "#1e1e1e",
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  win.loadFile(path.join(__dirname, "loading.html"));
  // open target=_blank / external links in the real browser, not a new window
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(IDE_URL)) { shell.openExternal(url); return { action: "deny" }; }
    return { action: "allow" };
  });
  win.on("closed", () => { win = null; });
}

async function boot() {
  createWindow();
  const alreadyRunning = await ideUp();
  if (!alreadyRunning) {
    if (!fs.existsSync(BONSAI)) {
      win.webContents.executeJavaScript(
        `document.body.innerHTML = '<pre style="color:#f48771;padding:24px">Could not find the bonsai launcher at ${BONSAI}\\nSet BONSAI_BIN or run \\'bonsai start\\' manually.</pre>'`
      );
      return;
    }
    weStartedIt = true;
    spawn("bash", [BONSAI, "start"], { detached: false, stdio: "ignore" });
  }
  const ok = await waitForIDE();
  if (ok && win) win.loadURL(IDE_URL);
  else if (win) win.webContents.executeJavaScript(
    `document.body.innerHTML = '<pre style="color:#f48771;padding:24px">IDE did not come up in time. Check: bonsai status</pre>'`
  );
}

app.whenReady().then(boot);

app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) boot(); });

app.on("window-all-closed", () => {
  // tear the stack down only if we were the ones who started it
  if (weStartedIt) { try { execFile("bash", [BONSAI, "stop"]); } catch (_) {} }
  app.quit();
});
