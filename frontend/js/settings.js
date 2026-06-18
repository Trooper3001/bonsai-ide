/**
 * settings.js — Settings panel (gear in the activity bar).
 * Persists to localStorage and applies live to Monaco and the Bonsai defaults.
 */
window.Settings = (() => {
  const KEY = "bonsai-ide-settings";

  const SCHEMA = [
    { group: "Editor" },
    { id: "fontSize",   label: "Font Size",   type: "number", def: 13, min: 8, max: 32 },
    { id: "tabSize",    label: "Tab Size",    type: "number", def: 4,  min: 1, max: 8 },
    { id: "wordWrap",   label: "Word Wrap",   type: "select", def: "off", opts: ["off", "on", "bounded"] },
    { id: "lineNumbers",label: "Line Numbers",type: "select", def: "on", opts: ["on", "off", "relative"] },
    { id: "renderWhitespace", label: "Render Whitespace", type: "select", def: "selection", opts: ["none","selection","all"] },
    { id: "minimap",    label: "Minimap",     type: "toggle", def: true },
    { id: "fontLigatures", label: "Font Ligatures", type: "toggle", def: true },

    { group: "Bonsai AI" },
    { id: "think",      label: "Thinking on by default", type: "toggle", def: true,
      desc: "Reason before answering — better quality, slower." },
    { id: "allowWrites",label: "Agent may edit & run by default", type: "toggle", def: true },
    { id: "temperature",label: "Chat temperature", type: "number", def: 0.3, min: 0, max: 1, step: 0.1 },

    { group: "Inline Completion" },
    { id: "autocomplete", label: "Inline autocomplete", type: "toggle", def: true,
      desc: "Ghost-text suggestions as you type (Qwen2.5-Coder FIM). Alt+\\ triggers manually." },
    { id: "autocompleteDelay", label: "Trigger delay (ms)", type: "number", def: 350,
      min: 0, max: 2000, step: 50,
      desc: "How long to pause typing before requesting a suggestion." },
    { id: "autocompleteTokens", label: "Max suggestion length", type: "number", def: 80,
      min: 16, max: 256, step: 8 },
  ];

  let values = {};

  function _defaults() {
    const d = {}; SCHEMA.forEach(s => { if (s.id) d[s.id] = s.def; }); return d;
  }
  function load() {
    values = _defaults();
    try { Object.assign(values, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch {}
  }
  function get(id) { return values[id]; }
  function _save() { try { localStorage.setItem(KEY, JSON.stringify(values)); } catch {} }

  function init() {
    load();
    _render();
    _applyEditor();
    _applyBonsaiDefaults();
  }

  function _render() {
    const body = document.getElementById("settings-body");
    if (!body) return;
    body.innerHTML = "";
    for (const s of SCHEMA) {
      if (s.group) {
        const g = document.createElement("div");
        g.className = "set-group"; g.textContent = s.group;
        body.appendChild(g);
        continue;
      }
      const item = document.createElement("div");
      item.className = "set-item" + (s.type === "toggle" ? " set-row" : "");
      const lab = document.createElement("label");
      lab.textContent = s.label; lab.htmlFor = `set-${s.id}`;

      let ctrl;
      if (s.type === "toggle") {
        ctrl = document.createElement("input");
        ctrl.type = "checkbox"; ctrl.checked = !!values[s.id];
        ctrl.addEventListener("change", () => _set(s.id, ctrl.checked));
      } else if (s.type === "select") {
        ctrl = document.createElement("select");
        s.opts.forEach(o => {
          const opt = document.createElement("option"); opt.value = o; opt.textContent = o;
          if (o === values[s.id]) opt.selected = true; ctrl.appendChild(opt);
        });
        ctrl.addEventListener("change", () => _set(s.id, ctrl.value));
      } else {
        ctrl = document.createElement("input");
        ctrl.type = "number"; ctrl.value = values[s.id];
        if (s.min != null) ctrl.min = s.min; if (s.max != null) ctrl.max = s.max;
        if (s.step) ctrl.step = s.step;
        ctrl.addEventListener("change", () => _set(s.id, s.step ? parseFloat(ctrl.value) : parseInt(ctrl.value, 10)));
      }
      ctrl.id = `set-${s.id}`;

      if (s.type === "toggle") { item.appendChild(lab); item.appendChild(ctrl); }
      else {
        item.appendChild(lab); item.appendChild(ctrl);
      }
      if (s.desc) {
        const d = document.createElement("div"); d.className = "set-desc"; d.textContent = s.desc;
        item.appendChild(d);
      }
      body.appendChild(item);
    }
  }

  function _set(id, val) {
    values[id] = val; _save();
    _applyEditor();
    _applyBonsaiDefaults();
  }

  function _applyEditor() {
    window.App?.applyEditorOptions?.({
      fontSize: values.fontSize,
      tabSize: values.tabSize,
      wordWrap: values.wordWrap,
      lineNumbers: values.lineNumbers,
      renderWhitespace: values.renderWhitespace,
      minimap: { enabled: !!values.minimap },
      fontLigatures: !!values.fontLigatures,
    });
  }

  // sync the think / allow-writes checkboxes with saved defaults (only on load)
  function _applyBonsaiDefaults() {
    const think = document.getElementById("bonsai-think");
    const aw = document.getElementById("agent-allow-writes");
    if (think && think.dataset.userset !== "1") think.checked = !!values.think;
    if (aw && aw.dataset.userset !== "1") aw.checked = !!values.allowWrites;
  }

  return { init, get };
})();
