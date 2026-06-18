/**
 * icons.js — file-type glyphs shared by the explorer, tabs, open-editors and
 * the command palette. Emoji keep it dependency-free; the Dark+ theme carries
 * the VS Code feel.
 */
window.Icons = (() => {
  const BY_EXT = {
    js: "📜", mjs: "📜", cjs: "📜", jsx: "📜",
    ts: "📘", tsx: "📘",
    py: "🐍", rb: "💎", go: "🐹", rs: "🦀",
    c: "🔧", h: "🔧", cpp: "🔧", cc: "🔧", hpp: "🔧",
    java: "☕", kt: "🟪", swift: "🐦", php: "🐘",
    html: "🌐", htm: "🌐", xml: "📰", svg: "🖼",
    css: "🎨", scss: "🎨", less: "🎨",
    json: "🗂", yaml: "🗂", yml: "🗂", toml: "🗂", ini: "🗂", cfg: "🗂",
    md: "📝", txt: "📄", rst: "📄", csv: "📊",
    sh: "⚙️", bash: "⚙️", zsh: "⚙️", bat: "⚙️", ps1: "⚙️",
    png: "🖼", jpg: "🖼", jpeg: "🖼", gif: "🖼", webp: "🖼", ico: "🖼",
    gguf: "🧠", bin: "📦", zip: "📦", tar: "📦", gz: "📦",
    lock: "🔒", env: "🔒", pem: "🔒", key: "🔒",
    sql: "🗄", db: "🗄",
    gbnf: "🔤", kcpps: "🗂", kcppt: "🗂",
  };
  const BY_NAME = {
    "dockerfile": "🐳", "makefile": "🛠", "readme.md": "📖",
    ".gitignore": "🙈", "license": "📜", "package.json": "📦",
    "requirements.txt": "📦",
  };

  function forFile(name) {
    const lower = (name || "").toLowerCase();
    if (BY_NAME[lower]) return BY_NAME[lower];
    const ext = lower.includes(".") ? lower.split(".").pop() : "";
    return BY_EXT[ext] || "📄";
  }
  function forDir(open) { return open ? "📂" : "📁"; }

  return { forFile, forDir };
})();
