/**
 * terminal.js — lightweight terminal panel.
 * Sends commands to /api/terminal and displays output.
 * History, CWD tracking, Ctrl+C stub.
 */

window.Terminal = (() => {
  let _cwd     = "";           // relative to workspace
  let _history = [];
  let _hIdx    = -1;

  let _outputEl  = null;
  let _inputEl   = null;
  let _cwdEl     = null;
  let _workspace = "/";

  // ── init ───────────────────────────────────────────────────────────────────

  async function init() {
    _outputEl = document.getElementById("terminal-output");
    _inputEl  = document.getElementById("terminal-input");
    _cwdEl    = document.getElementById("terminal-cwd");

    // get workspace root for display
    try {
      const r = await fetch("/api/workspace");
      const d = await r.json();
      _workspace = d.workspace;
    } catch {}
    _cwdEl.textContent = _shortCwd();

    _inputEl.addEventListener("keydown", _onKey);

    // bottom-panel tab switching
    document.querySelectorAll(".btm-tab").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".btm-tab").forEach(b => b.classList.remove("active"));
        document.querySelectorAll(".btab-content").forEach(c => c.classList.remove("active"));
        btn.classList.add("active");
        document.getElementById(`btab-${btn.dataset.btab}`)?.classList.add("active");
      });
    });

    document.getElementById("panel-close").addEventListener("click", () => togglePanel(false));
    document.getElementById("panel-maximize")?.addEventListener("click", () => {
      const panel = document.getElementById("bottom-panel");
      const max = panel.classList.toggle("maximized");
      document.getElementById("panel-maximize").textContent = max ? "▾" : "▴";
      window.dispatchEvent(new Event("resize"));
    });

    _print("BonsAI IDE terminal. Type commands and press Enter.", "info");
    _print(`Workspace: ${_workspace}`, "info");
  }

  // ── keyboard ───────────────────────────────────────────────────────────────

  function _onKey(evt) {
    if (evt.key === "Enter") {
      const cmd = _inputEl.value;
      _inputEl.value = "";
      if (cmd.trim()) {
        _history.unshift(cmd);
        if (_history.length > 100) _history.pop();
        _hIdx = -1;
        run(cmd);
      }
    } else if (evt.key === "ArrowUp") {
      _hIdx = Math.min(_hIdx + 1, _history.length - 1);
      _inputEl.value = _history[_hIdx] ?? "";
    } else if (evt.key === "ArrowDown") {
      _hIdx = Math.max(_hIdx - 1, -1);
      _inputEl.value = _hIdx === -1 ? "" : (_history[_hIdx] ?? "");
    }
  }

  // ── run command ────────────────────────────────────────────────────────────

  async function run(cmd) {
    _print(`${_shortCwd()} $ ${cmd}`, "cmd");

    // handle `cd` locally (the shell subprocess can't persist cwd between calls)
    const cdMatch = cmd.trim().match(/^cd\s+(.+)$/);
    if (cdMatch) {
      const target = cdMatch[1].trim();
      let next;
      if (target === "~" || target === "") {
        next = "";
      } else if (target.startsWith("/")) {
        // absolute — store as-is (backend handles cwd as absolute path)
        next = target;
      } else {
        next = _cwd ? `${_cwd}/${target}` : target;
      }
      // verify via files API
      const r = await fetch(`/api/files?path=${encodeURIComponent(next)}`);
      if (r.ok) {
        _cwd = next;
        _cwdEl.textContent = _shortCwd();
      } else {
        _print(`cd: ${target}: No such directory`, "err");
      }
      return;
    }

    try {
      const absCwd = _cwd.startsWith("/") ? _cwd : `${_workspace}/${_cwd}`;
      const r = await fetch("/api/terminal", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ cmd, cwd: absCwd }),
      });
      const d = await r.json();
      if (d.stdout) _print(d.stdout.replace(/\n$/, ""));
      if (d.stderr) _print(d.stderr.replace(/\n$/, ""), "err");
      if (d.code !== 0 && !d.stderr) _print(`exit ${d.code}`, "err");
    } catch (e) {
      _print(`Error: ${e.message}`, "err");
    }
    _outputEl.scrollTop = _outputEl.scrollHeight;
  }

  // ── helpers ────────────────────────────────────────────────────────────────

  function _print(text, cls = "") {
    const line = document.createElement("div");
    line.className = `t-line ${cls}`;
    line.textContent = text;
    _outputEl.appendChild(line);
    _outputEl.scrollTop = _outputEl.scrollHeight;
  }

  function _shortCwd() {
    if (!_cwd) return "~";
    // if absolute path under workspace, show relative
    if (_cwd.startsWith(_workspace)) {
      const rel = _cwd.slice(_workspace.length).replace(/^\//, "");
      return `~/${rel}`;
    }
    return _cwd;
  }

  function focus() {
    togglePanel(true);
    _inputEl?.focus();
  }

  function togglePanel(open) {
    const panel = document.getElementById("bottom-panel");
    if (open === undefined) open = panel.classList.contains("collapsed");
    panel.classList.toggle("collapsed", !open);
    document.body.classList.toggle("panel-open",  open);
    document.body.classList.toggle("panel-closed", !open);
    // trigger Monaco layout recalculation
    window.dispatchEvent(new Event("resize"));
    if (open) _inputEl?.focus();
  }

  return { init, run, focus, togglePanel };
})();
