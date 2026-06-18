/**
 * app.js — BonsAI IDE core.
 * Monaco bootstrap, tabs + open editors, breadcrumbs, rich status bar,
 * command palette / settings wiring, and the activity bar.
 */
window.App = (() => {
  let _monaco = null, _editor = null;
  const _tabs = new Map();          // path → { model, dirty, viewState }
  let _active = null;

  const LANG_NAMES = {
    javascript: "JavaScript", typescript: "TypeScript", python: "Python",
    html: "HTML", css: "CSS", json: "JSON", markdown: "Markdown", shell: "Shell Script",
    rust: "Rust", go: "Go", cpp: "C++", c: "C", yaml: "YAML", toml: "TOML",
    xml: "XML", sql: "SQL", ruby: "Ruby", java: "Java", kotlin: "Kotlin",
    swift: "Swift", plaintext: "Plain Text",
  };

  // ── bootstrap ──────────────────────────────────────────────────────────────
  require(["vs/editor/editor.main"], monaco => {
    _monaco = monaco;
    monaco.editor.defineTheme("bonsai-dark", {
      base: "vs-dark", inherit: true, rules: [],
      colors: {
        "editor.background": "#1e1e1e",
        "editorLineNumber.foreground": "#5a5a5a",
        "editorLineNumber.activeForeground": "#c6c6c6",
        "editor.selectionBackground": "#264f78",
        "editorIndentGuide.background1": "#404040",
      },
    });

    _editor = monaco.editor.create(document.getElementById("editor-container"), {
      theme: "bonsai-dark", automaticLayout: true,
      fontSize: 13, fontFamily: "'Cascadia Code','Fira Code','Courier New',monospace",
      fontLigatures: true, minimap: { enabled: true },
      scrollBeyondLastLine: false, lineNumbers: "on",
      renderWhitespace: "selection", tabSize: 4, insertSpaces: true,
      smoothScrolling: true, cursorBlinking: "smooth",
      inlineSuggest: { enabled: true },
      quickSuggestions: { other: true, comments: false, strings: false },
      bracketPairColorization: { enabled: true },
    });

    Bonsai.Completions.register(monaco, _editor);

    _editor.onDidChangeCursorPosition(e => {
      document.getElementById("status-pos").textContent = `Ln ${e.position.lineNumber}, Col ${e.position.column}`;
    });
    _editor.onDidChangeModelContent(() => {
      if (_active && _tabs.has(_active)) {
        const t = _tabs.get(_active);
        if (!t.dirty) { t.dirty = true; _renderTabs(); _renderOpenEditors(); }
      }
    });

    // editor keybindings
    _editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, _save);
    _editor.addCommand(monaco.KeyMod.Alt | monaco.KeyCode.Backslash, _triggerComplete);
    _editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyP, () => Palette.open(""));
    _editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyMod.Shift | monaco.KeyCode.KeyP, () => Palette.open(">"));
    _editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Backquote, () => Terminal.togglePanel());
    _editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyMod.Shift | monaco.KeyCode.KeyB, () => _showPanel("bonsai"));
    _editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyW, () => { if (_active) _closeTab(_active); });
    _editor.addCommand(monaco.KeyCode.F5, () => _runActiveFile());

    // modules
    Explorer.init(_openFile);
    Terminal.init();
    Bonsai.Status.start();
    Bonsai.Chat.init();
    Bonsai.Agent.init();
    Palette.init();
    Settings.init();

    _registerCommands();
    _wireActivityBar();
    _wireToolbar();
    _wireBonsaiTabs();
    _wireWelcome();
    _wireGlobalKeys();
    _wireRunPanel();
    _wireFolderPicker();

    document.getElementById("search-query")?.addEventListener("input", _debounce(_searchInFiles, 400));
    window.addEventListener("resize", () => _editor.layout());
    _loadWorkspaceRoot();
  });

  // ── workspace ────────────────────────────────────────────────────────────
  async function _loadWorkspaceRoot() {
    try {
      const d = await (await fetch("/api/workspace")).json();
      const name = d.workspace.split("/").filter(Boolean).pop() || "/";
      document.title = `BonsAI IDE — ${name}`;
      document.getElementById("cc-label").textContent = name;
      document.getElementById("ws-name").textContent = name.toUpperCase();
    } catch {}
  }

  // ── open folder (workspace picker) ─────────────────────────────────────────
  let _fpPath = null;   // folder currently being browsed
  async function _browseFolder(path) {
    try {
      const url = "/api/browse" + (path ? `?path=${encodeURIComponent(path)}` : "");
      const d = await (await fetch(url)).json();
      _fpPath = d.path;
      document.getElementById("fp-path").textContent = d.path;
      document.getElementById("fp-current").textContent = `Will open: ${d.path}`;
      const list = document.getElementById("fp-list");
      list.innerHTML = "";
      if (d.parent) {
        const up = document.createElement("div");
        up.className = "fp-item fp-up";
        up.innerHTML = `<span class="fp-ico">↰</span><span>..</span>`;
        up.addEventListener("click", () => _browseFolder(d.parent));
        list.appendChild(up);
      }
      if (!d.dirs.length) {
        const e = document.createElement("div");
        e.className = "fp-empty"; e.textContent = "No sub-folders.";
        list.appendChild(e);
      }
      for (const dir of d.dirs) {
        const el = document.createElement("div");
        el.className = "fp-item";
        el.innerHTML = `<span class="fp-ico">${window.Icons.forDir(false)}</span><span>${_esc(dir.name)}</span>`;
        el.addEventListener("click", () => _browseFolder(dir.path));
        list.appendChild(el);
      }
    } catch (e) { _toast(`Browse failed: ${e.message}`); }
  }
  function _openFolderPicker() {
    document.getElementById("folder-overlay").hidden = false;
    _browseFolder(null);   // start at home
  }
  function _closeFolderPicker() {
    document.getElementById("folder-overlay").hidden = true;
  }
  async function _confirmOpenFolder() {
    if (!_fpPath) return;
    try {
      const r = await fetch("/api/workspace", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: _fpPath }),
      });
      if (!r.ok) throw new Error(await r.text());
      location.reload();   // re-root: simplest correct reset of tree/tabs/terminal
    } catch (e) { _toast(`Could not open folder: ${e.message}`); }
  }
  function _wireFolderPicker() {
    document.getElementById("open-folder-btn")?.addEventListener("click", _openFolderPicker);
    document.getElementById("fp-close")?.addEventListener("click", _closeFolderPicker);
    document.getElementById("fp-open")?.addEventListener("click", _confirmOpenFolder);
    document.getElementById("folder-overlay")?.addEventListener("click", e => {
      if (e.target.id === "folder-overlay") _closeFolderPicker();
    });
  }

  // ── file open / activate / close ───────────────────────────────────────────
  async function _openFile(path) {
    if (_tabs.has(path)) { _activateTab(path); return; }
    try {
      const r = await fetch(`/api/read?path=${encodeURIComponent(path)}`);
      if (!r.ok) throw new Error(await r.text());
      const { content } = await r.json();
      const model = _monaco.editor.createModel(content, _detectLanguage(path),
        _monaco.Uri.parse(`file:///${encodeURI(path)}`));
      _tabs.set(path, { model, dirty: false, viewState: null });
      _activateTab(path);
      document.getElementById("welcome")?.classList.add("hidden");
    } catch (e) { _toast(`Could not open ${path}: ${e.message}`); }
  }

  function _activateTab(path) {
    if (_active && _tabs.has(_active)) _tabs.get(_active).viewState = _editor.saveViewState();
    _active = path;
    const tab = _tabs.get(path);
    _editor.setModel(tab.model);
    if (tab.viewState) _editor.restoreViewState(tab.viewState);
    _editor.focus();
    _renderTabs(); _renderOpenEditors(); _renderBreadcrumbs(path); _updateStatusBar(path);
    Explorer.setActive(path);
  }

  function _closeTab(path, e) {
    e?.stopPropagation();
    const tab = _tabs.get(path);
    if (tab?.dirty && !confirm(`"${_base(path)}" has unsaved changes. Close anyway?`)) return;
    tab?.model.dispose();
    _tabs.delete(path);
    if (_active === path) {
      const keys = [..._tabs.keys()];
      if (keys.length) _activateTab(keys[keys.length - 1]);
      else {
        _active = null; _editor.setModel(null);
        document.getElementById("welcome")?.classList.remove("hidden");
        document.getElementById("breadcrumbs").innerHTML = "";
        document.getElementById("status-file").textContent = "No file";
      }
    }
    _renderTabs(); _renderOpenEditors();
  }

  // ── tabs ─────────────────────────────────────────────────────────────────
  function _renderTabs() {
    const bar = document.getElementById("tabs-bar");
    bar.innerHTML = "";
    for (const [path, tab] of _tabs) {
      const t = document.createElement("div");
      t.className = `tab ${path === _active ? "active" : ""} ${tab.dirty ? "dirty" : ""}`;
      t.title = path;
      t.innerHTML = `<span class="tab-ico">${window.Icons.forFile(_base(path))}</span>` +
                    `<span class="tab-name">${_esc(_base(path))}</span>`;
      const close = document.createElement("button");
      close.className = "tab-close"; close.title = "Close";
      if (!tab.dirty) close.textContent = "×";
      close.addEventListener("click", e => _closeTab(path, e));
      t.appendChild(close);
      t.addEventListener("click", () => _activateTab(path));
      t.addEventListener("mousedown", e => { if (e.button === 1) _closeTab(path, e); }); // middle-click
      t.addEventListener("contextmenu", e => { e.preventDefault(); _ctxTab(e, path); });
      bar.appendChild(t);
      if (path === _active) t.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  function _ctxTab(evt, path) {
    _ctxMenu(evt, [
      { label: "Close", kbd: "Ctrl W", fn: () => _closeTab(path) },
      { label: "Close Others", fn: () => [..._tabs.keys()].filter(p => p !== path).forEach(p => _closeTab(p)) },
      { label: "Close All", fn: () => [..._tabs.keys()].forEach(p => _closeTab(p)) },
      { sep: true },
      { label: "Copy Path", fn: () => navigator.clipboard?.writeText(path) },
      { label: "Reveal in Explorer", fn: () => { _showPanel("explorer"); Explorer.setActive(path); } },
    ]);
  }

  // ── open editors list ───────────────────────────────────────────────────────
  function _renderOpenEditors() {
    const list = document.getElementById("open-editors-list");
    const count = document.getElementById("oe-count");
    if (!list) return;
    list.innerHTML = "";
    count.textContent = _tabs.size || "";
    for (const [path, tab] of _tabs) {
      const el = document.createElement("div");
      el.className = `oe-item ${path === _active ? "active" : ""} ${tab.dirty ? "dirty" : ""}`;
      const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
      el.innerHTML = `<button class="oe-close" title="Close"></button>` +
                     `<span class="ti-icon">${window.Icons.forFile(_base(path))}</span>` +
                     `<span class="oe-name">${_esc(_base(path))}</span>` +
                     (dir ? `<span class="oe-dir">${_esc(dir)}</span>` : "");
      el.addEventListener("click", () => _activateTab(path));
      el.querySelector(".oe-close").addEventListener("click", e => _closeTab(path, e));
      list.appendChild(el);
    }
  }

  // ── breadcrumbs ────────────────────────────────────────────────────────────
  function _renderBreadcrumbs(path) {
    const bc = document.getElementById("breadcrumbs");
    bc.innerHTML = "";
    const parts = path.split("/").filter(Boolean);
    parts.forEach((seg, i) => {
      if (i) { const s = document.createElement("span"); s.className = "bc-sep"; s.textContent = "›"; bc.appendChild(s); }
      const el = document.createElement("span");
      el.className = "bc-seg" + (i === parts.length - 1 ? " file" : "");
      const icon = i === parts.length - 1 ? window.Icons.forFile(seg) : window.Icons.forDir(false);
      el.innerHTML = `<span>${icon}</span><span>${_esc(seg)}</span>`;
      bc.appendChild(el);
    });
    // Run button — shown only when the language registry knows how to run this file
    const spacer = document.createElement("span"); spacer.className = "bc-spacer"; bc.appendChild(spacer);
    const runBtn = document.createElement("button");
    runBtn.className = "bc-run"; runBtn.textContent = "▶ Run"; runBtn.style.display = "none";
    runBtn.title = "Run this file (F5)";
    runBtn.addEventListener("click", _runActiveFile);
    bc.appendChild(runBtn);
    fetch(`/api/runinfo?path=${encodeURIComponent(path)}`)
      .then(r => r.json())
      .then(info => { if (info.runnable) { runBtn.style.display = ""; runBtn.textContent = `▶ Run ${info.language}`; } })
      .catch(() => {});
  }

  // ── run the active file via the language registry ────────────────────────────
  async function _runActiveFile() {
    if (!_active) { _showPanel("run"); return; }
    await _save();                                    // run the latest saved content
    Terminal.togglePanel(true);
    document.querySelector('.btm-tab[data-btab="bonsai-out"]')?.click();
    const out = document.getElementById("bonsai-output-log");
    out.innerHTML = `<div class="run-line run-cmd">▶ running ${_esc(_base(_active))} …</div>`;
    try {
      const r = await fetch("/api/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: _active }),
      });
      _renderRunOutput(out, await r.json());
    } catch (e) {
      out.innerHTML = `<div class="run-line run-err">✗ ${_esc(e.message)}</div>`;
    }
  }

  function _renderRunOutput(out, j) {
    let h = "";
    if (j.error) {
      h = `<div class="run-line run-err">✗ ${_esc(j.error)}</div>`;
    } else {
      h += `<div class="run-line run-cmd">$ ${_esc(j.cmd || "")}</div>`;
      if (j.stdout) h += `<pre class="run-out">${_esc(j.stdout)}</pre>`;
      if (j.stderr) h += `<pre class="run-out run-stderr">${_esc(j.stderr)}</pre>`;
      h += `<div class="run-line ${j.code === 0 ? "run-ok" : "run-err"}">exit ${j.code}</div>`;
    }
    out.innerHTML = h;
    out.scrollTop = out.scrollHeight;
  }

  // ── status bar ─────────────────────────────────────────────────────────────
  function _updateStatusBar(path) {
    const model = _tabs.get(path)?.model;
    document.getElementById("status-file").textContent = _base(path);
    if (!model) return;
    const langId = model.getLanguageId();
    document.getElementById("sb-lang").textContent = LANG_NAMES[langId] || langId;
    document.getElementById("sb-eol").textContent = model.getEOL() === "\n" ? "LF" : "CRLF";
    const o = model.getOptions();
    document.getElementById("sb-indent").textContent =
      (o.insertSpaces ? "Spaces: " : "Tab Size: ") + o.tabSize;
  }

  // ── save ───────────────────────────────────────────────────────────────────
  async function _save() {
    if (!_active) return;
    const tab = _tabs.get(_active); if (!tab) return;
    await fetch("/api/write", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: _active, content: tab.model.getValue() }),
    });
    tab.dirty = false; _renderTabs(); _renderOpenEditors();
  }

  function _triggerComplete() { _editor.trigger("bonsai", "editor.action.inlineSuggest.trigger", {}); }

  // ── settings → editor ────────────────────────────────────────────────────
  function applyEditorOptions(opts) {
    if (!_editor) return;
    _editor.updateOptions(opts);
    if (opts.tabSize != null) _tabs.forEach(t => t.model.updateOptions({ tabSize: opts.tabSize }));
    if (_active) _updateStatusBar(_active);
  }

  // ── command registry ─────────────────────────────────────────────────────
  function _registerCommands() {
    const C = [
      { id: "newFile", label: "File: New File", icon: "⊕", kbd: "", run: () => Explorer.newFilePrompt() },
      { id: "openFolder", label: "File: Open Folder…", icon: "🗁", run: () => _openFolderPicker() },
      { id: "save", label: "File: Save", icon: "💾", kbd: "Ctrl S", run: _save },
      { id: "closeTab", label: "View: Close Editor", kbd: "Ctrl W", run: () => _active && _closeTab(_active) },
      { id: "closeAll", label: "View: Close All Editors", run: () => [..._tabs.keys()].forEach(p => _closeTab(p)) },
      { id: "quickOpen", label: "Go to File…", icon: "📄", kbd: "Ctrl P", run: () => Palette.open("") },
      { id: "explorer", label: "View: Show Explorer", icon: "🗂", run: () => _showPanel("explorer") },
      { id: "search", label: "View: Show Search", icon: "🔍", run: () => { _showPanel("search"); document.getElementById("search-query").focus(); } },
      { id: "openBonsai", label: "Bonsai: Open Chat", icon: "💬", kbd: "Ctrl ⇧ B", run: () => { _showPanel("bonsai"); document.querySelector('.bmode[data-mode="chat"]').click(); document.getElementById("bonsai-input").focus(); } },
      { id: "openAgent", label: "Bonsai: Open Agent", icon: "🤖", run: () => { _showPanel("bonsai"); document.querySelector('.bmode[data-mode="agent"]').click(); document.getElementById("agent-input").focus(); } },
      { id: "settings", label: "Preferences: Open Settings", icon: "⚙️", run: () => _showPanel("settings") },
      { id: "terminal", label: "Terminal: Toggle", icon: "⊳", kbd: "Ctrl `", run: () => Terminal.togglePanel() },
      { id: "explainFile", label: "Bonsai: Explain Current File", icon: "💡", run: () => _active && App.bonsaiOnPath(_active, "explain") },
      { id: "wrap", label: "View: Toggle Word Wrap", run: () => { const w = _editor.getOption(_monaco.editor.EditorOption.wordWrap); applyEditorOptions({ wordWrap: w === "on" ? "off" : "on" }); } },
      { id: "format", label: "Format Document", run: () => _editor.getAction("editor.action.formatDocument")?.run() },
      { id: "fold", label: "Fold All", run: () => _editor.getAction("editor.foldAll")?.run() },
      { id: "unfold", label: "Unfold All", run: () => _editor.getAction("editor.unfoldAll")?.run() },
    ];
    C.forEach(c => Palette.register(c));
  }

  // ── activity bar / panels ──────────────────────────────────────────────────
  function _wireActivityBar() {
    document.querySelectorAll(".ab-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        const panel = btn.dataset.panel;
        const wasActive = btn.classList.contains("active");
        const hidden = document.body.classList.contains("sidebar-hidden");
        if (wasActive && !hidden) { document.body.classList.add("sidebar-hidden"); _editor?.layout(); return; }
        document.body.classList.remove("sidebar-hidden");
        _showPanel(panel);
      });
    });
  }
  function _showPanel(panel) {
    document.body.classList.remove("sidebar-hidden");
    document.querySelectorAll(".ab-btn").forEach(b => b.classList.toggle("active", b.dataset.panel === panel));
    document.querySelectorAll(".side-panel").forEach(p => p.classList.toggle("active", p.id === `panel-${panel}`));
    _editor?.layout();
    _syncToolbar();
  }

  // ── top toolbar ────────────────────────────────────────────────────────────
  function _wireToolbar() {
    const actions = {
      explorer: () => _showPanel("explorer"),
      search:   () => { _showPanel("search"); document.getElementById("search-query")?.focus(); },
      chat:     () => { _showPanel("bonsai"); document.querySelector('.bmode[data-mode="chat"]')?.click();
                        document.getElementById("bonsai-input")?.focus(); },
      agent:    () => { _showPanel("bonsai"); document.querySelector('.bmode[data-mode="agent"]')?.click();
                        document.getElementById("agent-input")?.focus(); },
      run:      () => { if (_active) _runActiveFile(); else _showPanel("run"); },
      settings: () => _showPanel("settings"),
      terminal: () => Terminal.togglePanel(),
      palette:  () => Palette.open(">"),
    };
    document.querySelectorAll(".tbar-btn").forEach(btn => {
      btn.addEventListener("click", () => { actions[btn.dataset.tbar]?.(); _syncToolbar(); });
    });
    // keep highlights honest no matter how a panel was opened (shortcut, palette, activity bar)
    setInterval(_syncToolbar, 600);
  }

  // Reflect current view state in the toolbar's active highlights.
  function _syncToolbar() {
    const hidden = document.body.classList.contains("sidebar-hidden");
    const panel  = hidden ? null : document.querySelector(".ab-btn.active")?.dataset.panel;
    const mode   = document.querySelector(".bmode.active")?.dataset.mode;
    const term   = document.body.classList.contains("panel-open");
    const on = {
      explorer: panel === "explorer",
      search:   panel === "search",
      chat:     panel === "bonsai" && mode === "chat",
      agent:    panel === "bonsai" && mode === "agent",
      run:      panel === "run",
      settings: panel === "settings",
      terminal: term,
    };
    document.querySelectorAll(".tbar-btn").forEach(b => b.classList.toggle("active", !!on[b.dataset.tbar]));
  }

  function _wireBonsaiTabs() {
    document.querySelectorAll(".bmode").forEach(btn => {
      btn.addEventListener("click", () => {
        const mode = btn.dataset.mode;
        document.querySelectorAll(".bmode").forEach(b => b.classList.toggle("active", b === btn));
        document.querySelectorAll(".bonsai-mode").forEach(m => m.classList.toggle("active", m.id === `bonsai-mode-${mode}`));
        _syncToolbar();
      });
    });
    document.getElementById("bonsai-clear")?.addEventListener("click", () => { Bonsai.Chat.clear(); Bonsai.Agent.clear(); });
    // remember manual think/allow-writes overrides so Settings doesn't stomp them
    document.getElementById("bonsai-think")?.addEventListener("change", e => e.target.dataset.userset = "1");
    document.getElementById("agent-allow-writes")?.addEventListener("change", e => e.target.dataset.userset = "1");
  }

  function _wireWelcome() {
    document.querySelectorAll(".wl[data-cmd]").forEach(a => {
      a.addEventListener("click", () => {
        const cmd = a.dataset.cmd;
        if (cmd === "quickOpen") Palette.open("");
        else if (cmd === "openFolder") _openFolderPicker();
        else if (cmd === "newFile") Explorer.newFilePrompt();
        else if (cmd === "openAgent") { _showPanel("bonsai"); document.querySelector('.bmode[data-mode="agent"]').click(); }
      });
    });
  }

  function _wireRunPanel() {
    document.getElementById("run-open-terminal")?.addEventListener("click", () => Terminal.focus());
    document.getElementById("run-new-terminal")?.addEventListener("click", () => Terminal.focus());
  }

  function _wireGlobalKeys() {
    document.addEventListener("keydown", e => {
      const mod = e.ctrlKey || e.metaKey;
      if (e.key === "Escape" && !document.getElementById("folder-overlay").hidden) {
        e.preventDefault(); _closeFolderPicker(); return;
      }
      if (mod && e.shiftKey && (e.key === "o" || e.key === "O")) { e.preventDefault(); _openFolderPicker(); return; }
      if (mod && e.shiftKey && (e.key === "P" || e.key === "p")) { e.preventDefault(); Palette.open(">"); }
      else if (mod && (e.key === "p")) { e.preventDefault(); Palette.open(""); }
      else if (mod && e.key === "`") { e.preventDefault(); Terminal.togglePanel(); }
      else if (mod && e.shiftKey && (e.key === "e" || e.key === "E")) { e.preventDefault(); _showPanel("explorer"); }
      else if (mod && e.shiftKey && (e.key === "f" || e.key === "F")) { e.preventDefault(); _showPanel("search"); document.getElementById("search-query").focus(); }
      else if (mod && e.shiftKey && (e.key === "b" || e.key === "B")) { e.preventDefault(); _showPanel("bonsai"); }
      else if (mod && e.key === "b" && !e.shiftKey) { e.preventDefault(); document.body.classList.toggle("sidebar-hidden"); _editor?.layout(); }
    });
  }

  // ── search in files ────────────────────────────────────────────────────────
  async function _searchInFiles() {
    const q = document.getElementById("search-query")?.value.trim();
    const out = document.getElementById("search-results");
    if (!q || !out) { if (out) out.innerHTML = ""; return; }
    out.innerHTML = "<div style='color:#858585;font-size:12px;padding:4px'>Searching…</div>";
    try {
      const d = await (await fetch("/api/terminal", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cmd: `grep -rnI --exclude-dir={.git,node_modules,__pycache__} "${q.replace(/"/g, '\\"')}" . | head -60` }),
      })).json();
      const lines = (d.stdout || "").trim().split("\n").filter(Boolean);
      out.innerHTML = lines.length ? "" : "<div style='color:#858585;font-size:12px;padding:4px'>No results.</div>";
      for (const line of lines) {
        const m = line.match(/^\.?\/?(.+?):(\d+):(.*)/); if (!m) continue;
        const [, file, ln, text] = m;
        const el = document.createElement("div");
        el.className = "sr-item";
        el.innerHTML = `<div class="sr-path">${_esc(file)}:${ln}</div>` +
          `<div class="sr-match">${_esc(text.trim().slice(0, 90))}</div>`;
        el.addEventListener("click", () => _openFile(file));
        out.appendChild(el);
      }
    } catch (e) { out.innerHTML = `<div style='color:#f48771;padding:4px'>${_esc(e.message)}</div>`; }
  }

  // ── bonsai action from explorer/command ────────────────────────────────────
  async function bonsaiOnPath(path, action) {
    await _openFile(path);
    const tab = _tabs.get(path); if (!tab) return;
    const content = tab.model.getValue().slice(0, 2500);
    const prompts = { explain: `Explain this file (${_base(path)}):\n\`\`\`\n${content}\n\`\`\`` };
    if (prompts[action]) {
      _showPanel("bonsai");
      document.querySelector('.bmode[data-mode="chat"]').click();
      document.getElementById("bonsai-input").value = prompts[action];
      Bonsai.Chat.send();
    }
  }

  // ── helpers ────────────────────────────────────────────────────────────────
  function _detectLanguage(path) {
    const ext = path.split(".").pop().toLowerCase();
    const map = { js:"javascript",mjs:"javascript",jsx:"javascript",ts:"typescript",tsx:"typescript",
      py:"python",html:"html",htm:"html",css:"css",scss:"scss",json:"json",md:"markdown",sh:"shell",
      bash:"shell",rs:"rust",go:"go",cpp:"cpp",cc:"cpp",c:"c",h:"c",hpp:"cpp",yaml:"yaml",yml:"yaml",
      toml:"ini",ini:"ini",xml:"xml",svg:"xml",sql:"sql",rb:"ruby",java:"java",kt:"kotlin",swift:"swift",
      php:"php",dockerfile:"dockerfile" };
    return map[ext] || "plaintext";
  }
  function _base(p) { return p.split("/").pop(); }
  function _esc(s) { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
  function _debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
  function _toast(msg) { console.warn(msg); /* lightweight */ }

  function _ctxMenu(evt, items) {
    document.querySelectorAll(".ctx-menu").forEach(m => m.remove());
    const menu = document.createElement("div"); menu.className = "ctx-menu";
    items.forEach(it => {
      if (it.sep) { const s = document.createElement("div"); s.className = "ctx-sep"; menu.appendChild(s); return; }
      const el = document.createElement("div"); el.className = "ctx-item";
      el.innerHTML = `<span>${it.label}</span>` + (it.kbd ? `<span class="ctx-kbd">${it.kbd}</span>` : "");
      el.addEventListener("click", () => { menu.remove(); it.fn(); });
      menu.appendChild(el);
    });
    menu.style.left = `${Math.min(evt.clientX, innerWidth - 200)}px`;
    menu.style.top = `${evt.clientY}px`;
    document.body.appendChild(menu);
    setTimeout(() => document.addEventListener("click", () => menu.remove(), { once: true }), 0);
  }

  return {
    openFile: _openFile, save: _save, bonsaiOnPath, applyEditorOptions,
    showPanel: _showPanel, ctxMenu: _ctxMenu,
    get editor() { return _editor; },
  };
})();
