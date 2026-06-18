/**
 * palette.js — VS Code-style Command Palette (Ctrl+Shift+P) and Quick Open (Ctrl+P).
 *
 * Modes are chosen by the first character of the input:
 *   ">"  → commands (registered via Palette.register)
 *   else → fuzzy file open (from /api/allfiles, cached)
 */
window.Palette = (() => {
  const commands = [];               // {id, label, hint, kbd, icon, run}
  let fileCache = null;              // string[] of workspace-relative paths
  let items = [];                   // currently shown {icon,label,hint,kbd,run,matchIdx}
  let sel = 0;

  let overlay, input, list;

  function init() {
    overlay = document.getElementById("palette-overlay");
    input   = document.getElementById("palette-input");
    list    = document.getElementById("palette-list");

    input.addEventListener("input", _refresh);
    input.addEventListener("keydown", _onKey);
    overlay.addEventListener("mousedown", e => { if (e.target === overlay) close(); });

    document.getElementById("command-center")?.addEventListener("click", () => open(""));
  }

  function register(cmd) { commands.push(cmd); }

  function open(prefix = "") {
    overlay.hidden = false;
    input.value = prefix;
    input.focus();
    if (prefix === "") _ensureFiles();
    _refresh();
  }
  function close() { overlay.hidden = true; input.value = ""; }
  function isOpen() { return !overlay.hidden; }

  async function _ensureFiles() {
    if (fileCache) return;
    try {
      const r = await fetch("/api/allfiles");
      fileCache = (await r.json()).files || [];
    } catch { fileCache = []; }
    if (isOpen() && !input.value.startsWith(">")) _refresh();
  }
  function invalidateFiles() { fileCache = null; }

  // ── fuzzy match: subsequence with contiguity bonus ──────────────────────────
  function fuzzy(query, text) {
    if (!query) return { score: 0, idx: [] };
    const q = query.toLowerCase(), t = text.toLowerCase();
    let qi = 0, idx = [], score = 0, streak = 0, last = -1;
    for (let ti = 0; ti < t.length && qi < q.length; ti++) {
      if (t[ti] === q[qi]) {
        idx.push(ti);
        streak = (last === ti - 1) ? streak + 1 : 1;
        score += streak * 2 + (ti === 0 || "/._-".includes(t[ti - 1]) ? 4 : 0);
        last = ti; qi++;
      }
    }
    if (qi < q.length) return null;          // not all chars matched
    score -= (t.length - idx.length) * 0.1;  // prefer shorter / denser
    return { score, idx };
  }

  function _refresh() {
    const raw = input.value;
    const cmdMode = raw.startsWith(">");
    const query = cmdMode ? raw.slice(1).trim() : raw.trim();
    sel = 0;
    items = cmdMode ? _matchCommands(query) : _matchFiles(query);
    _render(query);
  }

  function _matchCommands(query) {
    const out = [];
    for (const c of commands) {
      const m = query ? fuzzy(query, c.label) : { score: 0, idx: [] };
      if (m) out.push({ ...c, _score: m.score, _idx: m.idx });
    }
    out.sort((a, b) => b._score - a._score);
    return out.slice(0, 50);
  }

  function _matchFiles(query) {
    const files = fileCache || [];
    const out = [];
    for (const f of files) {
      const name = f.split("/").pop();
      const m = query ? (fuzzy(query, f) || fuzzy(query, name)) : { score: 0, idx: [] };
      if (m) {
        out.push({
          icon: window.Icons.forFile(name),
          label: name,
          hint: f.includes("/") ? f.slice(0, f.lastIndexOf("/")) : "",
          run: () => window.App.openFile(f),
          _score: m.score, _idx: [],
        });
      }
      if (out.length > 400) break;
    }
    out.sort((a, b) => b._score - a._score);
    return out.slice(0, 50);
  }

  function _render(query) {
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = `<div class="pal-empty">${
        (input.value.startsWith(">")) ? "No matching commands" :
        (fileCache ? "No matching files" : "Loading files…")}</div>`;
      return;
    }
    items.forEach((it, i) => {
      const el = document.createElement("div");
      el.className = "pal-item" + (i === sel ? " sel" : "");
      const label = it._idx && it._idx.length ? _hl(it.label, it._idx) : _esc(it.label);
      el.innerHTML =
        `<span class="pal-ico">${it.icon || "⌘"}</span>` +
        `<span class="pal-label">${label}` +
        (it.hint ? ` <span class="pal-hint">${_esc(it.hint)}</span>` : "") + `</span>` +
        (it.kbd ? `<span class="pal-kbd">${_esc(it.kbd)}</span>` : "");
      el.addEventListener("click", () => _run(i));
      el.addEventListener("mousemove", () => { if (sel !== i) { sel = i; _paint(); } });
      list.appendChild(el);
    });
  }
  function _paint() {
    [...list.children].forEach((el, i) => el.classList.toggle("sel", i === sel));
    list.children[sel]?.scrollIntoView({ block: "nearest" });
  }

  function _onKey(e) {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(sel + 1, items.length - 1); _paint(); }
    else if (e.key === "ArrowUp")   { e.preventDefault(); sel = Math.max(sel - 1, 0); _paint(); }
    else if (e.key === "Enter")     { e.preventDefault(); _run(sel); }
  }
  function _run(i) {
    const it = items[i];
    if (!it) return;
    close();
    try { it.run(); } catch (err) { console.error(err); }
  }

  function _hl(text, idx) {
    const set = new Set(idx); let out = "";
    for (let i = 0; i < text.length; i++)
      out += set.has(i) ? `<span class="pal-match">${_esc(text[i])}</span>` : _esc(text[i]);
    return out;
  }
  function _esc(s) { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

  return { init, register, open, close, isOpen, invalidateFiles };
})();
