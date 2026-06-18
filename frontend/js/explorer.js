/**
 * explorer.js — file-tree sidebar with Open Editors + collapsible sections.
 */
window.Explorer = (() => {
  let _openDirs = new Set();
  let _onOpen = () => {};
  let _treeEl = null;

  function init(onOpenFile) {
    _onOpen = onOpenFile;
    _treeEl = document.getElementById("file-tree");

    document.getElementById("new-file-btn")?.addEventListener("click", e => { e.stopPropagation(); _newFile(); });
    document.getElementById("refresh-tree-btn")?.addEventListener("click", e => { e.stopPropagation(); _reloadTree(); });
    document.getElementById("collapse-tree-btn")?.addEventListener("click", e => { e.stopPropagation(); _openDirs.clear(); _reloadTree(); });

    // collapsible explorer sections (OPEN EDITORS / WORKSPACE)
    document.querySelectorAll(".section-header").forEach(h => {
      h.addEventListener("click", () => h.closest(".explorer-section").classList.toggle("open"));
    });

    refresh();
  }

  async function refresh(dir = "") {
    const data = await (await fetch(`/api/files?path=${encodeURIComponent(dir)}`)).json();
    if (dir === "") { _treeEl.innerHTML = ""; _renderEntries(data.entries, _treeEl); }
  }

  function _renderEntries(entries, container) {
    for (const e of entries) {
      const item = document.createElement("div");
      item.className = `tree-item ${e.isDir ? "dir" : "file"}`;
      item.dataset.path = e.path;
      item.style.paddingLeft = `${_depth(e.path) * 10 + 6}px`;

      const arrow = document.createElement("span");
      arrow.className = "ti-arrow";
      arrow.textContent = e.isDir ? (_openDirs.has(e.path) ? "▾" : "▸") : "";

      const icon = document.createElement("span");
      icon.className = "ti-icon";
      icon.textContent = e.isDir ? window.Icons.forDir(_openDirs.has(e.path)) : window.Icons.forFile(e.name);

      const name = document.createElement("span");
      name.className = "ti-name";
      name.textContent = e.name;

      item.append(arrow, icon, name);
      container.appendChild(item);

      if (e.isDir) {
        const children = document.createElement("div");
        children.className = "tree-children";
        container.appendChild(children);
        if (_openDirs.has(e.path)) { item.classList.add("open"); children.classList.add("open"); _loadDir(e.path, children); }

        item.addEventListener("click", evt => {
          evt.stopPropagation();
          const open = item.classList.toggle("open");
          children.classList.toggle("open", open);
          arrow.textContent = open ? "▾" : "▸";
          icon.textContent = window.Icons.forDir(open);
          if (open) { _openDirs.add(e.path); _loadDir(e.path, children); } else { _openDirs.delete(e.path); }
        });
        item.addEventListener("contextmenu", evt => _ctxDir(evt, e.path));
      } else {
        item.addEventListener("click", evt => {
          evt.stopPropagation();
          _onOpen(e.path);
        });
        item.addEventListener("contextmenu", evt => _ctxFile(evt, e.path));
      }
    }
  }

  async function _loadDir(path, container) {
    container.innerHTML = "";
    const data = await (await fetch(`/api/files?path=${encodeURIComponent(path)}`)).json();
    _renderEntries(data.entries, container);
  }

  // ── context menus (use shared App.ctxMenu) ──────────────────────────────────
  function _ctxFile(evt, path) {
    evt.preventDefault();
    window.App.ctxMenu(evt, [
      { label: "Open", fn: () => _onOpen(path) },
      { label: "Rename…", fn: () => _rename(path) },
      { label: "Delete", fn: () => _delete(path) },
      { sep: true },
      { label: "🤖 Bonsai: Explain", fn: () => window.App.bonsaiOnPath(path, "explain") },
      { label: "Copy Path", fn: () => navigator.clipboard?.writeText(path) },
    ]);
  }
  function _ctxDir(evt, path) {
    evt.preventDefault();
    window.App.ctxMenu(evt, [
      { label: "New File…", fn: () => _newFileIn(path) },
      { label: "New Folder…", fn: () => _newFolder(path) },
      { sep: true },
      { label: "Rename…", fn: () => _rename(path) },
      { label: "Delete", fn: () => _delete(path) },
    ]);
  }

  // ── mutations ────────────────────────────────────────────────────────────
  async function _post(url, body) {
    await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    window.Palette?.invalidateFiles();
  }
  async function _newFile() {
    const name = prompt("New file name (path relative to workspace):");
    if (!name) return;
    await _post("/api/write", { path: name, content: "" });
    if (name.includes("/")) _openDirs.add(name.slice(0, name.lastIndexOf("/")));
    await _reloadTree(); _onOpen(name);
  }
  async function _newFileIn(dir) {
    const name = prompt("New file name:"); if (!name) return;
    const path = `${dir}/${name}`;
    await _post("/api/write", { path, content: "" });
    _openDirs.add(dir); await _reloadTree(); _onOpen(path);
  }
  async function _newFolder(dir) {
    const name = prompt("New folder name:"); if (!name) return;
    await _post("/api/mkdir", { path: `${dir}/${name}` });
    _openDirs.add(dir); await _reloadTree();
  }
  async function _rename(path) {
    const parts = path.split("/");
    const newName = prompt("Rename to:", parts[parts.length - 1]); if (!newName) return;
    parts[parts.length - 1] = newName;
    await _post("/api/rename", { from: path, to: parts.join("/") });
    await _reloadTree();
  }
  async function _delete(path) {
    if (!confirm(`Delete "${path}"? This cannot be undone.`)) return;
    await _post("/api/delete", { path });
    await _reloadTree();
  }

  async function _reloadTree() {
    const data = await (await fetch("/api/files?path=")).json();
    _treeEl.innerHTML = ""; _renderEntries(data.entries, _treeEl);
  }

  function setActive(path) {
    document.querySelectorAll(".tree-item.active").forEach(el => el.classList.remove("active"));
    const el = document.querySelector(`.tree-item[data-path="${CSS.escape(path)}"]`);
    if (el) el.classList.add("active");
  }

  function _depth(p) { return p ? p.split("/").length - 1 : 0; }

  return { init, refresh, setActive, newFilePrompt: _newFile };
})();
