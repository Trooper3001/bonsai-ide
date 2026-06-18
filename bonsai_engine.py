#!/usr/bin/env python3
"""
bonsai_engine.py — Bonsai-8B (Qwen3) interaction layer for BonsAI IDE.

Encapsulates everything we learned probing the model:
  • It's Qwen3 architecture with native <tool_call> tool support.
  • It's a reasoning model; on this CPU (~4 tok/s) thinking is too slow for
    interactive use, so we force NO-THINK by pre-filling an empty <think></think>
    block in the assistant turn.
  • FIM infill is NOT trained in → code completion uses plain continuation.
  • Native /v1/chat/completions + `tools` gives clean structured tool_calls
    with thinking already suppressed → that path drives the agent.

Three capabilities:
  chat_stream()  — plain Q&A, no-think, token-streamed
  complete()     — inline code completion, plain continuation
  agent_run()    — tool-using agent loop over a sandboxed workspace, event-streamed
"""
import json
import pathlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

# On Windows 'python3' may not exist — use the current interpreter as fallback
_PY = "python3" if shutil.which("python3") else sys.executable

IM_START = "<|im_start|>"
IM_END   = "<|im_end|>"
# Pre-filled empty think block → model skips reasoning and answers directly.
NOTHINK_PREFILL = "<think>\n\n</think>\n\n"

# Qwen2.5-Coder fill-in-middle tokens — used by the small dedicated completion
# model (NOT Bonsai-8B, which has no FIM). The model fills the gap between
# prefix and suffix, so completions respect what comes *after* the cursor too.
FIM_PREFIX = "<|fim_prefix|>"
FIM_SUFFIX = "<|fim_suffix|>"
FIM_MIDDLE = "<|fim_middle|>"
FIM_STOPS  = ["<|endoftext|>", "<|fim_pad|>", FIM_PREFIX, FIM_SUFFIX, FIM_MIDDLE,
              "<|file_sep|>", "<|repo_name|>", IM_END, IM_START]

# ── language registry ──────────────────────────────────────────────────────────
# One entry per language; adding support later is a one-liner. Templates use
# {file} (the filename) and {exe} (output binary). Three run styles:
#   "run"        interpreted: run argv directly
#   "compile" + "run_class"  compile then `java <ClassName>`   (Java)
#   "compile" + "run_exe"    compile to {exe} then run it      (C/C++/Rust/Go-build)
# "check" (optional) is a fast syntax-only command. Python is special-cased in
# _lang_check for in-process, line-precise errors.
LANGS = {
    ".py":   {"name": "Python",     "run": [_PY, "{file}"]},
    ".js":   {"name": "JavaScript", "run": ["node", "{file}"],     "check": ["node", "--check", "{file}"]},
    ".mjs":  {"name": "JavaScript", "run": ["node", "{file}"],     "check": ["node", "--check", "{file}"]},
    ".sh":   {"name": "Shell",      "run": ["bash", "{file}"],     "check": ["bash", "-n", "{file}"]},
    ".rb":   {"name": "Ruby",       "run": ["ruby", "{file}"],     "check": ["ruby", "-c", "{file}"]},
    ".php":  {"name": "PHP",        "run": ["php", "{file}"],      "check": ["php", "-l", "{file}"]},
    ".pl":   {"name": "Perl",       "run": ["perl", "{file}"],     "check": ["perl", "-c", "{file}"]},
    ".lua":  {"name": "Lua",        "run": ["lua", "{file}"]},
    ".go":   {"name": "Go",         "run": ["go", "run", "{file}"]},
    ".ts":   {"name": "TypeScript", "run": ["npx", "-y", "tsx", "{file}"]},
    ".java": {"name": "Java",       "compile": ["javac", "{file}"], "run_class": True},
    ".c":    {"name": "C",          "compile": ["cc", "{file}", "-o", "{exe}"],  "run_exe": True},
    ".cpp":  {"name": "C++",        "compile": ["c++", "{file}", "-o", "{exe}"], "run_exe": True},
    ".rs":   {"name": "Rust",       "compile": ["rustc", "{file}", "-o", "{exe}"], "run_exe": True},
}


# ── path sandbox ─────────────────────────────────────────────────────────────────

def safe_resolve(workspace: pathlib.Path, user_path: str):
    """Resolve user_path inside workspace; None if it escapes. Strips leading
    slashes because the model often emits absolute-looking paths like /foo.txt."""
    try:
        target = (workspace / (user_path or "").lstrip("/\\")).resolve()
        target.relative_to(workspace.resolve())
        return target
    except Exception:
        return None


# ── engine ───────────────────────────────────────────────────────────────────────

class BonsaiEngine:
    def __init__(self, kcpp_url: str, workspace: pathlib.Path, complete_url: str = ""):
        self.kcpp = kcpp_url.rstrip("/")
        # Dedicated small FIM model for inline completion. Empty → completion
        # falls back to plain continuation on the main (8B) model.
        self.complete_url = (complete_url or "").rstrip("/")
        self.workspace = workspace.resolve()

    # ---- low level HTTP --------------------------------------------------------

    def _post(self, endpoint: str, payload: dict, timeout=300, base=None):
        req = urllib.request.Request(
            f"{base or self.kcpp}{endpoint}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        return urllib.request.urlopen(req, timeout=timeout)

    def _post_json(self, endpoint: str, payload: dict, timeout=300, base=None) -> dict:
        with self._post(endpoint, payload, timeout, base=base) as r:
            return json.loads(r.read())

    def completion_status(self) -> bool:
        """True if the dedicated FIM completion model is reachable."""
        if not self.complete_url:
            return False
        try:
            req = urllib.request.Request(f"{self.complete_url}/v1/models",
                                         headers={"User-Agent": "bonsai-ide/1.0"})
            with urllib.request.urlopen(req, timeout=2):
                return True
        except Exception:
            return False

    def _abort(self, genkey: str):
        """Tell KoboldCPP to stop the generation tagged with `genkey`. Critical:
        without this, a disconnected client leaves the model grinding server-side
        (zombie generations pile up and choke every later request)."""
        if not genkey:
            return
        try:
            self._post_json("/api/extra/abort", {"genkey": genkey}, timeout=5)
        except Exception:
            pass

    @staticmethod
    def _new_genkey() -> str:
        import uuid
        return "bonsai_" + uuid.uuid4().hex[:12]

    @staticmethod
    def _strip_think(text: str) -> str:
        """Remove a leading/!embedded <think>…</think> reasoning block."""
        import re
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    # ---- prompt rendering ------------------------------------------------------

    def render_qwen3(self, messages: list, think=True) -> str:
        """Render chat messages into a Qwen3 prompt string. We render manually
        (rather than trusting KoboldCPP's chat endpoint) so we control reasoning:
        think=True lets the model emit its own <think>…</think> (better answers);
        think=False pre-fills an empty block so it skips straight to the answer
        (faster — useful on weak hardware)."""
        parts = []
        for m in messages:
            role = m["role"]
            content = m.get("content", "") or ""
            parts.append(f"{IM_START}{role}\n{content}{IM_END}\n")
        parts.append(f"{IM_START}assistant\n")
        if not think:
            parts.append(NOTHINK_PREFILL)
        return "".join(parts)

    # ---- plain chat (streaming) ------------------------------------------------

    def chat_stream(self, messages: list, max_tokens=None, temperature=0.3,
                    think=True, genkey=None, project_aware=False):
        """Yield text chunks for a chat completion. With think=True the stream
        includes a <think>…</think> block the frontend renders collapsibly.
        project_aware=True prepends a system message describing the workspace so the
        user can reason about the whole project (not just the open file) in chat."""
        if project_aware and not (messages and messages[0].get("role") == "system"):
            sys_msg = ("You are BonsAI, a coding assistant. You're helping with the project in this "
                       "workspace; use this structure to reason about it. Files (relative to root):\n"
                       + self._workspace_tree() +
                       "\n\nWhen the user asks about the project, reference these files. To inspect a "
                       "file's contents, ask the user to open it (its text is added to the chat).")
            messages = [{"role": "system", "content": sys_msg}] + list(messages)
        prompt = self.render_qwen3(messages, think=think)
        if max_tokens is None:
            max_tokens = 1280 if think else 512   # thinking needs room to finish
        payload = {
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.9,
            "rep_pen": 1.08,
            "stop": [IM_END, IM_START],
            "stream": True,
        }
        yield from self._stream_completion(payload, genkey=genkey or self._new_genkey())

    def _stream_completion(self, payload: dict, genkey: str = None):
        """Consume KoboldCPP SSE stream from /v1/completions, yield text deltas.
        If the consumer stops early (client disconnect → generator closed), abort
        the server-side generation so the model doesn't keep running."""
        payload = dict(payload)
        if genkey:
            payload["genkey"] = genkey
        try:
            # generous timeout: prefill of the big agent prompt on a slow CPU can take
            # minutes; a too-tight socket timeout crashed the run mid-generation.
            with self._post("/v1/completions", payload, timeout=900) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = obj.get("choices", [{}])[0].get("text", "")
                        if delta:
                            yield delta
                    except json.JSONDecodeError:
                        continue
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # never crash the agent on a slow/failed generation — degrade gracefully
            yield f"\n[Bonsai generation interrupted: {e}]"
        except GeneratorExit:
            # client went away mid-stream — stop the model server-side
            self._abort(genkey)
            raise

    # ---- inline completion -----------------------------------------------------

    def complete(self, prefix: str, suffix: str = "", language="", max_tokens=80) -> str:
        """Inline completion. Prefer the dedicated Qwen2.5-Coder FIM model (it
        respects the `suffix` after the cursor); if it's unreachable, fall back to
        plain continuation on Bonsai-8B (which has no FIM)."""
        if self.complete_url:
            text = self._complete_fim(prefix, suffix, max_tokens)
            if text is not None:          # None == FIM model unreachable → fall back
                return text
        return self._complete_continuation(prefix, max_tokens)

    def _complete_fim(self, prefix: str, suffix: str, max_tokens: int):
        """Fill the gap between prefix and suffix with Qwen2.5-Coder FIM. Returns
        the completion string, or None if the FIM model can't be reached (so the
        caller can fall back). Context is bounded for low latency."""
        prefix = "\n".join(prefix.splitlines()[-80:])[-4000:]
        suffix = "\n".join(suffix.splitlines()[:40])[:1500]
        prompt = f"{FIM_PREFIX}{prefix}{FIM_SUFFIX}{suffix}{FIM_MIDDLE}"
        payload = {
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": 0.15,
            "top_p": 0.9,
            "rep_pen": 1.0,
            "stop": FIM_STOPS,
            "stream": False,
        }
        try:
            data = self._post_json("/v1/completions", payload,
                                    timeout=20, base=self.complete_url)
            return data.get("choices", [{}])[0].get("text", "")
        except Exception:
            return None

    def _complete_continuation(self, prefix: str, max_tokens: int) -> str:
        """Fallback path: plain continuation on the main model. No FIM, so the
        suffix is ignored; we just continue the last ~60 lines of `prefix`."""
        prompt = "\n".join(prefix.splitlines()[-60:])
        payload = {
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "top_p": 0.9,
            "rep_pen": 1.05,
            "stop": ["\n\n", "\n\n\n", IM_END, "<|endoftext|>"],
            "stream": False,
        }
        try:
            data = self._post_json("/v1/completions", payload, timeout=60)
            return data.get("choices", [{}])[0].get("text", "")
        except Exception:
            return ""

    # ============================================================================
    #  AGENT
    # ============================================================================

    AGENT_SYSTEM = (
        "You are BonsAI, a coding agent in a sandboxed workspace. Accomplish the task using "
        "ONLY the tools listed below — never invent a tool name. Rules:\n"
        "- Keep reasoning SHORT, then ACT: every step must end with exactly one <tool_call> "
        "block, unless the task is finished.\n"
        "- Paths are RELATIVE to the workspace root; use exactly the paths in the file tree.\n"
        "- File edits — pick the RIGHT tool:\n"
        "    • new file / full rewrite → write_file\n"
        "    • ADD code (functions/methods) to an existing file → append_file (keeps what's there)\n"
        "    • RENAME a function/variable used in 1+ files → rename_symbol (does all files at once)\n"
        "    • change a function's signature or body → replace_function (give its name + full new code)\n"
        "    • change/fix specific line(s) → replace_lines (the syntax error gives the line number)\n"
        "    • replace a specific snippet → edit_file (read_file first so `find` is exact)\n"
        "  NEVER write_file over a file whose existing content you must keep — it deletes the rest.\n"
        "- For anything shell-like (running code/tests, git, grep, building) use run_command. "
        "PREFER running a file: `python3 test_store.py` or `python3 main.py`. Only use inline "
        "`python3 -c '...'` for a trivial one-liner, and then use SINGLE quotes inside it — never "
        "nested double quotes (they break the shell). run_command output is NOT file content; never "
        "put a shell command into a file with write_file/replace_lines.\n"
        "- After writing code, VERIFY it by running its file with run_command before declaring "
        "success. If a step fails, change your APPROACH — do not repeat the same failing call.\n"
        "- ALWAYS test your work, even if the task didn't ask: for a project, also write a "
        "test_<name>.py that imports the code and checks it, and run it. Your output is "
        "auto-tested before completion, so it must actually run.\n"
        "- RESEARCH when unsure: if the task uses a library, API, format, or syntax you're not "
        "certain about, web_search for it, then web_fetch a docs page to read it BEFORE coding. "
        "Don't guess at an API.\n"
        "- Call one tool per step. When (and only when) the task is fully done and verified, "
        "stop calling tools and reply with a one- or two-line summary."
    )

    # Few-shot trajectory — the 1-bit model imitates this pattern closely, which
    # fixes its most common mistakes: it writes a REAL .py file (not raw text, not
    # the test snippet), uses a filename (not "."), and verifies with `python3 -c`.
    AGENT_EXAMPLE = (
        "# Example of a good run\n"
        "user: Create greet.py with a function hello(name) that returns 'Hi, <name>!', and check hello('Sam').\n"
        "assistant:\n"
        "<tool_call>\n"
        '{"name": "write_file", "arguments": {"path": "greet.py", "content": "def hello(name):\\n    return f\\"Hi, {name}!\\""}}\n'
        "</tool_call>\n"
        "tool: Wrote 41 bytes to greet.py.\n"
        "assistant:\n"
        "<tool_call>\n"
        '{"name": "run_command", "arguments": {"command": "python3 -c \\"from greet import hello; print(hello(\'Sam\'))\\""}}\n'
        "</tool_call>\n"
        "tool: (exit 0)\nHi, Sam!\n"
        "assistant: Done — greet.py defines hello(name); hello('Sam') returns 'Hi, Sam!'."
    )

    SKIP_DIRS  = {".git", "__pycache__", "node_modules", ".cache", "logs",
                  "venv", ".venv", "dist", "build", "target", ".idea", ".vscode"}
    NOISE_EXT  = {".o", ".a", ".so", ".dll", ".exe", ".bin", ".gguf", ".pyc",
                  ".obj", ".lib", ".dylib", ".class", ".lock"}

    def _visible(self, p) -> bool:
        return (not p.name.startswith(".")
                and p.name not in self.SKIP_DIRS
                and p.suffix.lower() not in self.NOISE_EXT)

    def _workspace_tree(self, per_dir=10, max_lines=70) -> str:
        """Compact, noise-filtered two-level listing so the agent uses correct
        relative paths instead of guessing (each wrong guess ≈ one ~100s step).
        Top-level entries are ALWAYS shown first so they can't be starved by a
        single large subdirectory; each subdir is summarised, not dumped."""
        try:
            tops = sorted([p for p in self.workspace.iterdir() if self._visible(p)],
                          key=lambda x: (x.is_file(), x.name.lower()))
        except Exception:
            return "(unreadable)"

        lines = []
        for top in tops:
            if top.is_file():
                lines.append(top.name); continue
            # directory: show a handful of children, summarise the rest
            try:
                kids = sorted([p for p in top.iterdir() if self._visible(p)],
                              key=lambda x: (x.is_file(), x.name.lower()))
            except Exception:
                kids = []
            lines.append(f"{top.name}/")
            for k in kids[:per_dir]:
                lines.append(f"  {top.name}/{k.name}" + ("/" if k.is_dir() else ""))
            if len(kids) > per_dir:
                lines.append(f"  … +{len(kids) - per_dir} more in {top.name}/")
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"… (+{len(lines) - max_lines} more entries)"]
        return "\n".join(lines) or "(empty)"

    def _tool_schemas(self, allow_writes: bool):
        tools = [
            {"type": "function", "function": {
                "name": "list_dir",
                "description": "List files and folders in a directory (relative to workspace root).",
                "parameters": {"type": "object",
                    "properties": {"path": {"type": "string", "description": "Directory path, '' or '.' for root"}},
                    "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "read_file",
                "description": "Read a text file's contents.",
                "parameters": {"type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "search",
                "description": "Search the workspace for a string or regex. Returns matching file:line:text.",
                "parameters": {"type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]}}},
            {"type": "function", "function": {
                "name": "web_search",
                "description": "Search the WEB (DuckDuckGo) for docs, API usage, or examples when you "
                               "are unsure how something works. Returns top results (title, url, snippet). "
                               "Follow up with web_fetch to read a result.",
                "parameters": {"type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]}}},
            {"type": "function", "function": {
                "name": "web_fetch",
                "description": "Fetch a web page (e.g. a docs URL from web_search) and return its "
                               "readable text. Use to read documentation before coding.",
                "parameters": {"type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"]}}},
        ]
        if allow_writes:
            tools += [
                {"type": "function", "function": {
                    "name": "write_file",
                    "description": "Create or overwrite a file with the given content.",
                    "parameters": {"type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"]}}},
                {"type": "function", "function": {
                    "name": "edit_file",
                    "description": "Replace the first exact occurrence of `find` with `replace` in a file.",
                    "parameters": {"type": "object",
                        "properties": {"path": {"type": "string"},
                                       "find": {"type": "string"},
                                       "replace": {"type": "string"}},
                        "required": ["path", "find", "replace"]}}},
                {"type": "function", "function": {
                    "name": "replace_lines",
                    "description": "Replace lines `start`..`end` (1-based, inclusive) of a file with "
                                   "`content`. BEST way to fix a syntax error: the error gives the "
                                   "line number, so just send the corrected line(s) — no need to "
                                   "retype the whole file or match exact text.",
                    "parameters": {"type": "object",
                        "properties": {"path": {"type": "string"},
                                       "start": {"type": "integer", "description": "first line to replace (1-based)"},
                                       "end": {"type": "integer", "description": "last line to replace (inclusive); = start for one line"},
                                       "content": {"type": "string", "description": "replacement text ('' deletes the lines)"}},
                        "required": ["path", "start", "end", "content"]}}},
                {"type": "function", "function": {
                    "name": "append_file",
                    "description": "Append `content` to the end of a file (creates it if missing). The "
                                   "SAFE way to ADD functions/methods/code to an existing file — it "
                                   "keeps everything already there. Prefer this over write_file when "
                                   "adding to a file, since write_file would erase the existing code.",
                    "parameters": {"type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"]}}},
                {"type": "function", "function": {
                    "name": "replace_function",
                    "description": "Replace an entire function or method (or class) BY NAME with new "
                                   "code — the def line through its whole indented body. Use this to "
                                   "change a function's signature or body (e.g. add a parameter); you "
                                   "don't compute line numbers and it never leaves old lines behind.",
                    "parameters": {"type": "object",
                        "properties": {"path": {"type": "string"},
                                       "name": {"type": "string", "description": "function/class name to replace"},
                                       "content": {"type": "string", "description": "the full new definition"}},
                        "required": ["path", "name", "content"]}}},
                {"type": "function", "function": {
                    "name": "rename_symbol",
                    "description": "Rename an identifier (function/class/variable) across ALL files in "
                                   "the workspace at once. The correct tool for renaming something used "
                                   "in more than one file — it keeps every file consistent in one call, "
                                   "so you don't edit each file by hand. Whole-word matches only.",
                    "parameters": {"type": "object",
                        "properties": {"old": {"type": "string", "description": "current name"},
                                       "new": {"type": "string", "description": "new name"}},
                        "required": ["old", "new"]}}},
                {"type": "function", "function": {
                    "name": "run_command",
                    "description": "Run a shell command in the workspace and return its output.",
                    "parameters": {"type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"]}}},
            ]
        return tools

    # ---- tool implementations --------------------------------------------------

    @staticmethod
    def lang_for(target):
        """Language registry entry for a path, or None."""
        return LANGS.get(target.suffix.lower())

    def _lang_check(self, target, rel: str) -> str:
        """Syntax-check a source file, returning a message suffix. Python is checked
        in-process for an EXACT line + a ready replace_lines() fix. Other languages
        use their `check` command (node --check, bash -n, ruby -c, …) when present;
        a missing toolchain is silently skipped so it never blocks. '' = no check."""
        lang = self.lang_for(target)
        if not lang:
            return ""
        if target.suffix.lower() == ".py":
            try:
                compile(target.read_text(errors="replace"), str(target), "exec")
                return " Syntax OK."
            except SyntaxError as e:
                line = e.lineno or "?"
                bad = (e.text or "").rstrip("\n").strip()
                return (f" SYNTAX ERROR at line {line}: {bad!r} — {e.msg}. "
                        f"Fix ONLY that line: replace_lines(path='{rel}', start={line}, end={line}, "
                        f"content='<corrected line>'). Do NOT retype the whole file.")
            except Exception as e:
                return f" WARNING: could not compile ({e})."
        cmd = lang.get("check")
        if not cmd:
            return ""
        import subprocess
        try:
            argv = [a.format(file=target.name) for a in cmd]
            r = subprocess.run(argv, cwd=str(self.workspace),
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                return " Syntax OK."
            lines = (r.stderr or r.stdout).strip().splitlines()
            # pick the actual error line, not the toolchain version footer
            tail = next((l.strip() for l in lines
                         if "error" in l.lower() and "node.js v" not in l.lower()),
                        (lines[0].strip() if lines else "check failed"))[:180]
            return (f" SYNTAX ERROR ({lang['name']}): {tail}. Fix the reported line with "
                    f"replace_lines — don't retype the whole file.")
        except FileNotFoundError:
            return ""          # toolchain not installed — don't block the agent
        except Exception:
            return ""

    def run_file(self, rel_path: str, timeout: int = 30) -> dict:
        """Run a source file using the language registry. Returns
        {cmd, stdout, stderr, code} or {error}. Handles interpreted langs directly,
        and compiles+runs Java / C / C++ / Rust. Sandboxed to the workspace."""
        import subprocess, os
        target = safe_resolve(self.workspace, rel_path)
        if target is None or not target.is_file():
            return {"error": f"file '{rel_path}' not found"}
        lang = self.lang_for(target)
        if not lang:
            return {"error": f"no run support for {target.suffix or 'this'} files yet "
                             f"(add it to LANGS)"}
        cwd, fname, stem = str(self.workspace), target.name, target.stem

        def _run(argv, tmout=timeout):
            r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=tmout)
            return r

        try:
            if "run" in lang:
                argv = [a.format(file=fname) for a in lang["run"]]
                r = _run(argv)
                return {"cmd": " ".join(argv), "stdout": r.stdout, "stderr": r.stderr, "code": r.returncode}

            if lang.get("run_class"):                       # Java: javac then java <Class>
                comp = [a.format(file=fname) for a in lang["compile"]]
                rc = _run(comp, 60)
                if rc.returncode != 0:
                    return {"cmd": " ".join(comp), "stdout": rc.stdout, "stderr": rc.stderr, "code": rc.returncode}
                rr = _run(["java", stem])
                return {"cmd": f"{' '.join(comp)} && java {stem}",
                        "stdout": rr.stdout, "stderr": rr.stderr, "code": rr.returncode}

            if lang.get("run_exe"):                         # C/C++/Rust: compile to ./ then run
                exe = ".bonsai_run.out"
                comp = [a.format(file=fname, exe=exe) for a in lang["compile"]]
                rc = _run(comp, 60)
                if rc.returncode != 0:
                    return {"cmd": " ".join(comp), "stdout": rc.stdout, "stderr": rc.stderr, "code": rc.returncode}
                rr = _run([f"./{exe}"])
                try: os.unlink(os.path.join(cwd, exe))
                except OSError: pass
                return {"cmd": f"{' '.join(comp)} && ./{exe}",
                        "stdout": rr.stdout, "stderr": rr.stderr, "code": rr.returncode}

            return {"error": "no run method defined for this language"}
        except subprocess.TimeoutExpired:
            return {"error": f"timed out ({timeout}s)"}
        except FileNotFoundError as e:
            return {"error": f"toolchain not installed: {e.filename or e}"}
        except Exception as e:
            return {"error": str(e)}

    # ---- web research (stdlib, no API key) -------------------------------------

    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

    def _web_search(self, query: str, n: int = 5) -> str:
        """DuckDuckGo HTML search → top results as 'N. title\\n   url\\n   snippet'.
        POST + browser UA (the GET form serves a JS-challenge page to scripts)."""
        import urllib.parse, urllib.request, re, html
        if not query.strip():
            return "Error: empty query."
        data = urllib.parse.urlencode({"q": query}).encode()
        req = urllib.request.Request("https://html.duckduckgo.com/html/", data=data,
                                     headers={"User-Agent": self._UA})
        page = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        links = re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
        out = []
        for i, (href, title) in enumerate(links[:n]):
            title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
            href = html.unescape(href)
            m = re.search(r"uddg=([^&]+)", href)           # unwrap DDG redirect
            if m:
                href = urllib.parse.unquote(m.group(1))
            snip = html.unescape(re.sub(r"<[^>]+>", "", snippets[i])).strip() if i < len(snippets) else ""
            out.append(f"{i + 1}. {title}\n   {href}\n   {snip[:200]}")
        return "\n".join(out) or "(no results)"

    def _web_fetch(self, url: str, max_chars: int = 3500) -> str:
        """Fetch a page and return its readable text (scripts/styles/tags stripped)."""
        import urllib.request, re, html
        if not url.startswith(("http://", "https://")):
            return "Error: url must start with http:// or https://"
        req = urllib.request.Request(url, headers={"User-Agent": self._UA})
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        raw = re.sub(r"(?is)<(script|style|head|nav|footer).*?</\1>", " ", raw)
        text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", re.sub(r" *\n *", "\n", text)).strip()
        return text[:max_chars] + ("\n…[truncated]" if len(text) > max_chars else "")

    def _exec_tool(self, name: str, args: dict, allow_writes: bool) -> str:
        try:
            if name == "list_dir":
                target = safe_resolve(self.workspace, args.get("path", ""))
                if target is None or not target.is_dir():
                    return f"Error: '{args.get('path')}' is not a directory."
                items = []
                for it in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
                    items.append(f"{'[dir] ' if it.is_dir() else '      '}{it.name}")
                return "\n".join(items) or "(empty directory)"

            if name == "read_file":
                target = safe_resolve(self.workspace, args.get("path", ""))
                if target is None or not target.is_file():
                    return f"Error: file '{args.get('path')}' not found."
                text = target.read_text(errors="replace")
                if len(text) > 8000:
                    text = text[:8000] + "\n…[truncated — use search to find specific lines]"
                # Prefix line numbers so the model can target replace_lines correctly
                # and see exact indentation. (These numbers are an annotation — the
                # real file does NOT contain them.)
                numbered = "\n".join(f"{i}: {ln}" for i, ln in enumerate(text.split("\n"), 1))
                return ("# (line numbers shown for reference; not part of the file)\n" + numbered)

            if name == "search":
                q = args.get("query", "")
                if not q:
                    return "Error: empty query."
                # Try grep first (Linux/Mac/WSL); fall back to pure-Python search on Windows
                try:
                    r = subprocess.run(
                        ["grep", "-rn", "--exclude-dir=.git", "-I", q, "."],
                        cwd=self.workspace, capture_output=True, text=True, timeout=20,
                    )
                    out = r.stdout.strip()
                    lines = out.splitlines()[:40]
                    return "\n".join(lines) or "(no matches)"
                except FileNotFoundError:
                    import re as _re
                    results, pat = [], _re.compile(_re.escape(q), _re.IGNORECASE)
                    skip = self.SKIP_DIRS | {".git"}
                    for p in sorted(self.workspace.rglob("*")):
                        if not p.is_file(): continue
                        if any(part in skip for part in p.relative_to(self.workspace).parts): continue
                        try:
                            for i, ln in enumerate(p.read_text(errors="replace").splitlines(), 1):
                                if pat.search(ln):
                                    results.append(f"{p.relative_to(self.workspace)}:{i}:{ln.strip()[:120]}")
                                    if len(results) >= 40: break
                        except Exception: continue
                        if len(results) >= 40: break
                    return "\n".join(results) or "(no matches)"

            if name == "web_search":
                try:
                    return self._web_search(args.get("query", ""))
                except Exception as e:
                    return f"Error: web search failed ({e}). Check connectivity or try a simpler query."

            if name == "web_fetch":
                try:
                    return self._web_fetch(args.get("url", ""))
                except Exception as e:
                    return f"Error: could not fetch ({e})."

            if not allow_writes and name in ("write_file", "edit_file", "replace_lines",
                                             "append_file", "rename_symbol", "replace_function",
                                             "run_command"):
                return "Error: read-only mode is on; this action is blocked."

            if name == "replace_function":
                import re
                rel = (args.get("path", "") or "").strip()
                target = safe_resolve(self.workspace, rel)
                if target is None or not target.is_file():
                    return f"Error: file '{rel}' not found."
                fname = (args.get("name", "") or "").strip()
                new_code = self._unescape_if_needed(args.get("content", ""))
                if not fname or not new_code.strip():
                    return "Error: need both `name` and `content`."
                text = target.read_text(errors="replace")
                trailing_nl = text.endswith("\n")
                lines = text.split("\n")
                if trailing_nl:
                    lines = lines[:-1]
                def_re = re.compile(rf"^(\s*)(?:def|class)\s+{re.escape(fname)}\b")
                start = indent = None
                for i, ln in enumerate(lines):
                    m = def_re.match(ln)
                    if m:
                        start, indent = i, len(m.group(1)); break
                if start is None:
                    return (f"Error: no function/class named '{fname}' in {rel}. "
                            f"read_file to check the name.")
                # body = following lines indented MORE than the def; stop at first
                # non-blank line indented <= def. Track last body line so trailing
                # blank lines and the next definition are preserved.
                last = start
                for j in range(start + 1, len(lines)):
                    ln = lines[j]
                    if ln.strip() == "":
                        continue
                    if (len(ln) - len(ln.lstrip())) <= indent:
                        break
                    last = j
                new_lines = new_code.rstrip("\n").split("\n")
                # Re-indent the replacement so its def sits at the SAME indentation as
                # the original. The model routinely supplies a column-0 `def` for a
                # METHOD → without this the class structure breaks. Shift the whole
                # block by the delta between the original and the replacement's def.
                if new_lines:
                    cur = len(new_lines[0]) - len(new_lines[0].lstrip())
                    delta = indent - cur
                    if delta > 0:
                        pad = " " * delta
                        new_lines = [pad + l if l.strip() else l for l in new_lines]
                    elif delta < 0:
                        cut = -delta
                        new_lines = [l[cut:] if l[:cut].strip() == "" else l for l in new_lines]
                result = lines[:start] + new_lines + lines[last + 1:]
                target.write_text("\n".join(result) + ("\n" if trailing_nl else ""))
                return f"Replaced {fname} in {rel}." + self._lang_check(target, rel)

            if name == "rename_symbol":
                import re
                old = (args.get("old", "") or "").strip()
                new = (args.get("new", "") or "").strip()
                if not old or not new:
                    return "Error: need both `old` and `new` names."
                if not re.fullmatch(r"[A-Za-z_]\w*", new):
                    return f"Error: '{new}' is not a valid identifier."
                pat = re.compile(r"\b" + re.escape(old) + r"\b")
                changed, total = [], 0
                for p in sorted(self.workspace.rglob("*")):
                    if not (p.is_file() and p.suffix.lower() in LANGS and self._visible(p)):
                        continue
                    if any(part in self.SKIP_DIRS or part.startswith(".")
                           for part in p.relative_to(self.workspace).parts):
                        continue
                    txt = p.read_text(errors="replace")
                    n = len(pat.findall(txt))
                    if n:
                        p.write_text(pat.sub(new, txt))
                        changed.append(f"{p.relative_to(self.workspace)} ({n})")
                        total += n
                if not changed:
                    return f"'{old}' not found in any file — nothing renamed."
                return f"Renamed '{old}' → '{new}': {total} occurrence(s) across {len(changed)} file(s): {', '.join(changed)}."

            if name == "write_file":
                rel = (args.get("path", "") or "").strip()
                target = safe_resolve(self.workspace, rel)
                if target is None:
                    return "Error: invalid path."
                if rel in ("", ".", "/", "./") or target == self.workspace or target.is_dir():
                    return ("Error: `path` must be a file name (e.g. 'mathutils.py' or "
                            "'src/util.py'), not a directory. Retry with a real filename.")
                target.parent.mkdir(parents=True, exist_ok=True)
                content = self._unescape_if_needed(args.get("content", ""))
                target.write_text(content)
                # Immediate Python feedback so the model gets a tight correction loop,
                # and on error it's told the exact line + to fix just that line.
                return f"Wrote {len(content)} bytes to {rel}." + self._lang_check(target, rel)

            if name == "replace_lines":
                rel = (args.get("path", "") or "").strip()
                target = safe_resolve(self.workspace, rel)
                if target is None or not target.is_file():
                    return f"Error: file '{rel}' not found. Use write_file to create it."
                try:
                    start = int(args.get("start")); end = int(args.get("end", start))
                except (TypeError, ValueError):
                    return "Error: `start` and `end` must be integers (1-based line numbers)."
                text = target.read_text(errors="replace")
                trailing_nl = text.endswith("\n")
                lines = text.split("\n")
                if trailing_nl:
                    lines = lines[:-1]            # drop the empty element from the final \n
                n = len(lines)
                # replace_lines must target EXISTING lines (1..n). Appending past the
                # end is what append_file is for — allowing it here silently created
                # broken code. Require a valid in-range span and a clear error otherwise.
                if start < 1 or start > n:
                    return (f"Error: line {start} doesn't exist — {rel} has {n} lines (valid "
                            f"1..{n}). read_file to see the numbered lines, or use append_file "
                            f"to add at the end.")
                if end < start or end > n:
                    return (f"Error: end line {end} is invalid (must be {start}..{n}). "
                            f"For one line, set end = start.")
                repl = self._unescape_if_needed(args.get("content", "") or "")
                repl_lines = repl.split("\n") if repl != "" else []
                new_lines = lines[:start - 1] + repl_lines + lines[end:]
                target.write_text("\n".join(new_lines) + ("\n" if trailing_nl else ""))
                chk = self._lang_check(target, rel)
                # Auto-fix dropped indentation: the model often gives a single
                # corrected line WITHOUT its leading whitespace → IndentationError.
                # If re-indenting it to match the replaced line clears the error,
                # adopt that (safe: only kept if it compiles clean).
                if ("SYNTAX ERROR" in chk and start == end and repl_lines):
                    orig = lines[start - 1]
                    orig_indent = orig[:len(orig) - len(orig.lstrip())]
                    first = repl_lines[0]
                    if orig_indent and not (first[:1] in (" ", "\t")):
                        fixed = [orig_indent + l if l.strip() else l for l in repl_lines]
                        cand = lines[:start - 1] + fixed + lines[end:]
                        target.write_text("\n".join(cand) + ("\n" if trailing_nl else ""))
                        chk2 = self._lang_check(target, rel)
                        if "SYNTAX ERROR" not in chk2:
                            return f"Replaced line {start} of {rel} (auto-indented to match)." + chk2
                        target.write_text("\n".join(new_lines) + ("\n" if trailing_nl else ""))
                return (f"Replaced lines {start}..{end} of {rel} with {len(repl_lines)} line(s)." + chk)

            if name == "append_file":
                rel = (args.get("path", "") or "").strip()
                target = safe_resolve(self.workspace, rel)
                if target is None or rel in ("", ".", "/", "./") or target.is_dir():
                    return "Error: `path` must be a file name."
                target.parent.mkdir(parents=True, exist_ok=True)
                addition = self._unescape_if_needed(args.get("content", ""))
                existed = target.is_file()
                old = target.read_text(errors="replace") if existed else ""
                # Dedup: refuse to append code that's already in the file. This is the
                # main guard against the model's over-execution (it kept re-appending
                # duplicate loops/imports after the work was already done).
                if addition.strip() and addition.strip() in old:
                    return (f"That code is already in {rel} — nothing appended (it's already "
                            f"there). This part of the task is done; move on or finish.")
                # Refuse to append a def/class that ALREADY exists — that creates a
                # duplicate definition. The model means to CHANGE it, so steer it to
                # replace_lines (it has the line numbers from read_file).
                import re as _re
                for _m in _re.finditer(r"^\s*(?:def|class)\s+(\w+)", addition, _re.M):
                    nm = _m.group(1)
                    if _re.search(rf"^\s*(?:def|class)\s+{_re.escape(nm)}\b", old, _re.M):
                        return (f"Error: '{nm}' is already defined in {rel}. append_file would make a "
                                f"DUPLICATE definition. To CHANGE it, read_file to get its line "
                                f"number, then replace_lines those lines with the new version.")
                # ensure a clean separation so appended defs don't glue onto the last line
                sep = "" if (not old or old.endswith("\n\n")) else ("\n" if old.endswith("\n") else "\n\n")
                target.write_text(old + sep + addition)
                verb = "Appended to" if existed else "Created"
                return f"{verb} {rel} (+{len(addition)} bytes)." + self._lang_check(target, rel)

            if name == "edit_file":
                target = safe_resolve(self.workspace, args.get("path", ""))
                if target is None or not target.is_file():
                    return f"Error: file '{args.get('path')}' not found."
                text = target.read_text(errors="replace")
                find = args.get("find", "")
                replace = args.get("replace", "")
                if not find:
                    return "Error: `find` is empty."
                if find in text:
                    target.write_text(text.replace(find, replace, 1))
                    return f"Edited {args.get('path')} (replaced 1 occurrence)."
                # Fuzzy fallback: whitespace-INSENSITIVE match. Join every non-space
                # char of `find` with \s* so e.g. "def add(a,b):" matches the file's
                # "def add(a, b):". The 1-bit model rarely reproduces spacing exactly,
                # and this bridges intra-token gaps the old token match missed.
                import re
                core = [c for c in find if not c.isspace()]
                if core:
                    pat = r"\s*".join(re.escape(c) for c in core)
                    m = re.search(pat, text)
                    if m:
                        target.write_text(text[:m.start()] + replace + text[m.end():])
                        return (f"Edited {args.get('path')} (whitespace-insensitive match). "
                                f"Verify with read_file.")
                return ("Error: `find` not found in the file. To ADD code to a file, use "
                        "append_file (keeps existing content). To change code, read_file "
                        "then copy the exact text — do NOT write_file the whole file, it "
                        "would delete everything else.")

            if name == "run_command":
                cmd = args.get("command", "")
                r = subprocess.run(
                    cmd, shell=True, cwd=self.workspace,
                    capture_output=True, text=True, timeout=30,
                )
                out = (r.stdout + r.stderr).strip()
                if len(out) > 4000:
                    out = out[:4000] + "\n…[truncated]"
                return f"(exit {r.returncode})\n{out}" if out else f"(exit {r.returncode}, no output)"

            return f"Error: unknown tool '{name}'."
        except subprocess.TimeoutExpired:
            return "Error: command timed out (30s limit)."
        except Exception as e:
            return f"Error: {e}"

    # ---- agent prompt rendering ------------------------------------------------

    def _render_tools_block(self, allow_writes: bool) -> str:
        """Render the tool signatures exactly the way the model's Qwen3 chat
        template does — but WITHOUT going through KoboldCPP's `tools` param,
        which forces grammar-constrained sampling (~6× slower on this CPU)."""
        lines = ["# Tools",
                 "You may call functions. Signatures are inside <tools></tools>:",
                 "<tools>"]
        for t in self._tool_schemas(allow_writes):
            lines.append(json.dumps(t["function"], separators=(",", ":")))
        lines += ["</tools>",
                  "",
                  "To call a function, output ONLY a block (no other text):",
                  "<tool_call>",
                  '{"name": "<fn>", "arguments": {<args>}}',
                  "</tool_call>"]
        return "\n".join(lines)

    def _build_agent_prompt(self, messages: list) -> str:
        """messages: list of {role, content}. Render to a Qwen3 prompt with the
        no-think prefill on the trailing assistant turn."""
        parts = []
        for m in messages:
            parts.append(f"{IM_START}{m['role']}\n{m['content']}{IM_END}\n")
        parts.append(f"{IM_START}assistant\n{NOTHINK_PREFILL}")
        return "".join(parts)

    def _parse_tool_call(self, text: str):
        """Extract a {"name","arguments"} tool call from model output. Robust to:
        nested JSON (write_file content), missing/auto-closed </tool_call>, raw
        newlines inside string values, ```json fences, and trailing prose. The
        1-bit model is sloppy, so we try several candidate spans in priority order."""
        import re
        candidates = []
        # 1) inside <tool_call> … (closing tag optional — we stop-token on it)
        m = re.search(r"<tool_call>\s*(.*?)(?:</tool_call>|$)", text, re.DOTALL)
        if m:
            candidates.append(m.group(1))
        # 2) any ```json … ``` fenced block
        for fm in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
            candidates.append(fm.group(1))
        # 3) whatever's left after dropping the reasoning block
        candidates.append(self._strip_think(text))
        for c in candidates:
            obj = self._extract_json_object(c)
            if isinstance(obj, dict) and "name" in obj:
                return obj
        return None

    @staticmethod
    def _extract_json_object(s: str):
        """Return the first brace-balanced JSON object in `s` (string-aware), or
        None. Tries each '{' so leading junk doesn't defeat it."""
        start = s.find("{")
        while start != -1:
            depth = 0; in_str = False; esc = False
            for i in range(start, len(s)):
                ch = s[i]
                if in_str:
                    if esc:            esc = False
                    elif ch == "\\":   esc = True
                    elif ch == '"':    in_str = False
                else:
                    if ch == '"':      in_str = True
                    elif ch == "{":    depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            obj = BonsaiEngine._loads_lenient(s[start:i + 1])
                            if obj is not None:
                                return obj
                            break       # malformed — advance to next '{'
            start = s.find("{", start + 1)
        return None

    @staticmethod
    def _loads_lenient(blob: str):
        """json.loads, but tolerate raw newlines/tabs inside string values — small
        models frequently emit them unescaped in file `content`."""
        try:
            return json.loads(blob)
        except Exception:
            pass
        out, in_str, esc = [], False, False
        for ch in blob:
            if in_str:
                if esc:            out.append(ch); esc = False; continue
                if ch == "\\":     out.append(ch); esc = True;  continue
                if ch == '"':      in_str = False; out.append(ch); continue
                if ch == "\n":     out.append("\\n"); continue
                if ch == "\r":     out.append("\\r"); continue
                if ch == "\t":     out.append("\\t"); continue
                out.append(ch)
            else:
                if ch == '"':      in_str = True
                out.append(ch)
        try:
            return json.loads("".join(out))
        except Exception:
            return None

    # ---- always-on project verification ----------------------------------------

    def _auto_verify(self):
        """Exercise the workspace's Python and report (passed, report). Tries the
        project's own tests first (test_*.py / *_test.py, via pytest then a direct
        fallback); if there are none it syntax-checks and imports every module so
        broken code can't pass silently. Called automatically when the agent tries
        to finish — testing happens whether or not the task asked for it."""
        import subprocess, pathlib
        def rel_parts_ok(p):
            parts = p.relative_to(self.workspace).parts
            return not any(part in self.SKIP_DIRS or part.startswith(".") for part in parts)

        # 1) syntax-check EVERY registered-language source file (Python is line-precise;
        #    others use their check command). Catches breakage in any language.
        all_src = sorted(p for p in self.workspace.rglob("*")
                         if p.is_file() and p.suffix.lower() in LANGS and rel_parts_ok(p))
        if not all_src:
            return True, "no source files to test."
        for p in all_src:
            rel = str(p.relative_to(self.workspace))
            chk = self._lang_check(p, rel)
            if "SYNTAX ERROR" in chk or "WARNING" in chk:
                return False, f"{rel}:{chk.strip()}"

        # Python gets the deeper checks (import / tests / run-entry); non-Python
        # projects pass on a clean syntax check.
        pys = [p for p in all_src if p.suffix.lower() == ".py"]
        if not pys:
            langs = sorted({LANGS[p.suffix.lower()]["name"] for p in all_src})
            return True, f"{len(all_src)} source file(s) syntax-OK ({', '.join(langs)})."
        rels = [str(p.relative_to(self.workspace)) for p in pys]

        # 2) run tests if the project has any
        tests = [r for r in rels if pathlib.Path(r).name.startswith("test_")
                 or r.endswith("_test.py")]
        if tests:
            try:
                r = subprocess.run([_PY, "-m", "pytest", "-q"], cwd=self.workspace,
                                   capture_output=True, text=True, timeout=120)
                out = (r.stdout + r.stderr)
                if "No module named pytest" not in out and r.returncode != 5:
                    tail = out.strip().splitlines()[-12:]
                    return (r.returncode == 0), "pytest:\n" + "\n".join(tail)
            except Exception:
                pass
            # pytest unavailable → run each test file directly
            fails = []
            for t in tests:
                try:
                    rr = subprocess.run([_PY, t], cwd=self.workspace,
                                        capture_output=True, text=True, timeout=60)
                    if rr.returncode != 0:
                        fails.append(f"{t}: {(rr.stdout + rr.stderr).strip()[-400:]}")
                except Exception as e:
                    fails.append(f"{t}: {e}")
            if fails:
                return False, "test failures:\n" + "\n".join(fails)
            return True, f"{len(tests)} test file(s) ran clean."

        # 3) no tests → import every module to surface import/runtime errors
        fails = []
        for rel in rels:
            mod = rel[:-3].replace("/", ".")
            try:
                rr = subprocess.run(
                    [_PY, "-c", f"import importlib; importlib.import_module('{mod}')"],
                    cwd=self.workspace, capture_output=True, text=True, timeout=30)
                if rr.returncode != 0:
                    last = (rr.stderr.strip().splitlines() or ["error"])[-1]
                    fails.append(f"{rel}: {last[:160]}")
            except Exception as e:
                fails.append(f"{rel}: {e}")
        if fails:
            return False, "import errors:\n" + "\n".join(fails)

        # 3b) no tests, imports clean → actually RUN an entry point if there's a safe
        #     one, so "passed" means the program runs, not just imports. Skip files
        #     that read stdin (they'd hang) — the timeout is a backstop anyway.
        entry = next((r for r in rels if r in ("main.py", "app.py", "run.py")), None)
        if entry:
            try:
                src = (self.workspace / entry).read_text(errors="replace")
                if "input(" not in src:
                    rr = subprocess.run([_PY, entry], cwd=self.workspace,
                                        capture_output=True, text=True, timeout=20)
                    out = (rr.stdout + rr.stderr).strip()
                    if rr.returncode != 0:
                        return False, f"{entry} runs with errors:\n{out[-500:]}"
                    return True, f"ran {entry} OK (exit 0):\n{out[-500:] or '(no output)'}"
            except subprocess.TimeoutExpired:
                return True, f"{len(rels)} module(s) import cleanly; {entry} ran (timed out at 20s)."
            except Exception:
                pass
        return True, f"{len(rels)} module(s) import cleanly (no test files present)."

    # ---- agent loop (streaming event generator) --------------------------------

    def agent_run(self, task: str, allow_writes=True, max_steps=8, context=None,
                  think=True, genkey=None, alive=None, staged=True, impl_strategy="oneshot",
                  research=True):
        """Dispatch the agent. `staged=True` runs the focused multi-phase pipeline
        (analyze → plan → per-step implement/check/fix); `staged=False` runs the flat
        ReAct loop. `impl_strategy` controls how each file is implemented:
        'oneshot' (write the whole file at once), 'spec' (signatures-first micro-step
        then implement), 'skeleton' (stub bodies then fill each fn via replace_function).
        Yields dict events the server turns into SSE."""
        genkey = genkey or self._new_genkey()
        alive = alive or (lambda: True)
        if staged:
            yield from self._agent_run_staged(task, allow_writes, max_steps, context,
                                              think, genkey, alive, impl_strategy, research)
        else:
            yield from self._agent_run_react(task, allow_writes, max_steps, context,
                                             think, genkey, alive)

    def _agent_base_system(self, allow_writes, context) -> str:
        """Shared system preamble: role + tools + worked example + workspace tree."""
        sp = (f"{self.AGENT_SYSTEM}\n\n{self._render_tools_block(allow_writes)}\n\n"
              f"{self.AGENT_EXAMPLE}\n\n"
              f"# Workspace files (paths relative to root)\n{self._workspace_tree()}")
        if context:
            sp += f"\n\n# Open in editor now\n{context[:1000]}"
        return sp

    def _agent_run_react(self, task: str, allow_writes=True, max_steps=8, context=None,
                         think=True, genkey=None, alive=None):
        """Flat ReAct loop (fallback). One model instance does everything: reason,
        pick a tool, repeat. Robust but overloads a weak model on bigger tasks."""
        genkey = genkey or self._new_genkey()
        alive = alive or (lambda: True)
        sys_prompt = (
            f"{self.AGENT_SYSTEM}\n\n"
            f"{self._render_tools_block(allow_writes)}\n\n"
            f"{self.AGENT_EXAMPLE}\n\n"
            f"# Workspace files (paths relative to root)\n{self._workspace_tree()}"
        )
        if context:
            sys_prompt += f"\n\n# Open in editor now\n{context[:1000]}"

        # Conversation rendered as raw Qwen3 turns (we manage tool turns by hand).
        convo = [f"{IM_START}system\n{sys_prompt}{IM_END}\n",
                 f"{IM_START}user\n{task}{IM_END}\n"]

        call_counts = {}        # signature -> times seen (catch repeated identical calls)
        fail_streak = 0         # consecutive failing tool results (catch error loops)
        auto_verify_count = 0   # times the always-on auto-test forced a fix

        for step in range(1, max_steps + 1):
            if not alive():        # client gone — don't start another prefill
                return
            yield {"type": "status", "step": step, "msg": f"Working — step {step}/{max_steps}"}

            prefill = "" if think else NOTHINK_PREFILL
            prompt = "".join(convo) + f"{IM_START}assistant\n{prefill}"
            payload = {
                "prompt": prompt,
                # reasoning needs room to think AND emit the tool call / answer
                "max_tokens": 1024 if think else 512,
                # low temp → the weak 1-bit model emits more consistent, on-format
                # tool calls and drifts less
                "temperature": 0.15,
                "top_p": 0.9,
                "rep_pen": 1.07,
                "stop": ["</tool_call>", IM_END, IM_START],
                "stream": True,
            }

            acc = ""
            try:
                for delta in self._stream_completion(payload, genkey=genkey):
                    acc += delta
                    yield {"type": "token", "text": delta, "step": step}
                    if not alive():       # stop accumulating once client is gone
                        return
            except urllib.error.URLError as e:
                yield {"type": "error", "msg": f"Bonsai unreachable: {e}"}
                return

            tc = self._parse_tool_call(acc)

            # Salvage: thinking ran to the token cap without ever emitting an action
            # (open <think> with no tool call). Force the model out of reasoning and
            # let it produce just the tool call — otherwise the step is wasted and the
            # agent looks like it "did nothing". This is the main reliability fix.
            if tc is None and "<think>" in acc and "</think>" not in acc and alive():
                yield {"type": "status", "step": step, "msg": f"Step {step}: deciding action…"}
                cont_prompt = (prompt + acc + "\n</think>\n\n")
                cont_payload = dict(payload)
                cont_payload["prompt"] = cont_prompt
                cont_payload["max_tokens"] = 320
                cont = ""
                try:
                    for delta in self._stream_completion(cont_payload, genkey=genkey):
                        cont += delta
                        yield {"type": "token", "text": delta, "step": step}
                        if not alive():
                            return
                except urllib.error.URLError:
                    pass
                acc = acc + "\n</think>\n\n" + cont
                tc = self._parse_tool_call(acc)
            if tc is None:
                # no tool call → the model thinks it's done. Drop any <think> block.
                answer = self._strip_think(acc.split("<tool_call>")[0]).strip()

                # ── ALWAYS auto-test before accepting "done" ───────────────────
                # Verification is system-enforced, not left to the model: run the
                # project's tests if any exist, else syntax-check + import every
                # module. On failure, feed it back and force a fix (bounded), so
                # the agent never declares success on broken code.
                if allow_writes and auto_verify_count < 2 and alive():
                    yield {"type": "status", "step": step, "msg": "Auto-testing the project…"}
                    passed, report = self._auto_verify()
                    yield {"type": "tool_result", "name": "auto_test", "result": report, "step": step}
                    if not passed and step < max_steps:
                        auto_verify_count += 1
                        convo.append(f"{IM_START}assistant\n{answer or '(attempting to finish)'}{IM_END}\n")
                        convo.append(f"{IM_START}user\n<tool_response>\nAutomatic tests FAILED — you are "
                                     f"NOT done. Fix the code, then finish.\n{report}\n</tool_response>{IM_END}\n")
                        continue
                    answer = (answer + f"\n\n[auto-test] {report}").strip()

                yield {"type": "answer", "content": answer or "(done)"}
                yield {"type": "done", "steps": step}
                return

            name = tc.get("name", "")
            args = tc.get("arguments", {}) or {}
            if not isinstance(args, dict):
                args = {}
            yield {"type": "tool_call", "name": name, "args": args, "step": step}

            result = self._exec_tool(name, args, allow_writes)
            yield {"type": "tool_result", "name": name, "result": result, "step": step}

            # ── loop / dead-end detection ──────────────────────────────────────
            # The 1-bit model often repeats a failing edit verbatim or thrashes on
            # the same error. Detect that and steer it toward a different action;
            # bail out (rather than burn every step) if it won't change course.
            failed = result.startswith("Error") or result.startswith("(exit 1") \
                     or "Error:" in result[:20] or "Traceback" in result
            fail_streak = fail_streak + 1 if failed else 0
            sig = f"{name}|{json.dumps(args, sort_keys=True)}"
            call_counts[sig] = call_counts.get(sig, 0) + 1

            nudge = ""
            if call_counts[sig] >= 2:
                nudge = ("\n\n[You already made this exact call and it failed. Do something "
                         "DIFFERENT: to ADD code use append_file; to change a line use "
                         "replace_lines; read_file first to see exact text. Do NOT write_file a "
                         "file that already has content you must keep — it deletes the rest.]")
            elif fail_streak >= 2:
                nudge = ("\n\n[Two failures in a row. read_file to see the current exact contents, "
                         "then: append_file to add new code, or replace_lines to change a line.]")

            if call_counts[sig] >= 3 or fail_streak >= 4:
                yield {"type": "answer", "content":
                       ("I got stuck repeating an action that keeps failing (last error: "
                        + result.splitlines()[0][:160] + "). Stopping so I don't loop. "
                        "Try rephrasing the task or breaking it into smaller steps.")}
                yield {"type": "done", "steps": step, "stuck": True}
                return

            # Record the assistant turn as the model's ACTUAL output (its real
            # <tool_call> for Bonsai, or its code block for a chat coder like Qwen) —
            # recording a synthetic format the model doesn't produce makes ChatML
            # models emit EMPTY turns after a few rounds. Results use the `tool` role
            # (proper ChatML — avoids the consecutive-user-turn confusion too).
            result = result + nudge
            recorded = self._strip_think(acc).strip()
            if not recorded:   # safety: synthesized from a bridge with empty acc
                recorded = f'<tool_call>\n{{"name": "{name}", "arguments": {json.dumps(args)}}}\n</tool_call>'
            convo.append(f"{IM_START}assistant\n{recorded}{IM_END}\n")
            self._add_user(convo, f"<tool_response>\n{result}\n</tool_response>")

        yield {"type": "answer",
               "content": "Reached the step limit. Try a narrower task or raise max steps."}
        yield {"type": "done", "steps": max_steps, "truncated": True}

    # ============================================================================
    #  STAGED AGENT  —  focused single-job phases, gentler on a weak model
    # ============================================================================

    def _oneshot(self, system: str, user: str, max_tokens: int, genkey, alive,
                 think=False):
        """One focused, tool-free model instance. Streams tokens, returns the text
        (reasoning stripped). Used for the analyze and plan phases."""
        prompt = (f"{IM_START}system\n{system}{IM_END}\n"
                  f"{IM_START}user\n{user}{IM_END}\n{IM_START}assistant\n"
                  + ("" if think else NOTHINK_PREFILL))
        payload = {"prompt": prompt, "max_tokens": max_tokens, "temperature": 0.15,
                   "top_p": 0.9, "rep_pen": 1.07, "stop": [IM_END, IM_START], "stream": True}
        acc = ""
        for delta in self._stream_completion(payload, genkey=genkey):
            acc += delta
            yield {"type": "token", "text": delta}
            if not alive():
                break
        return self._strip_think(acc).strip()

    @staticmethod
    def _parse_plan(text: str):
        """Pull an ordered step list out of a numbered/bulleted plan; fall back to
        one step if the model didn't format it."""
        import re
        steps = []
        for line in text.splitlines():
            m = re.match(r"\s*(?:\d+[.)]|[-*])\s+(.*\S)", line)
            if m:
                steps.append(m.group(1).strip())
        if not steps:
            steps = [(text.strip()[:200] or "Implement the task.")]
        return steps[:8]

    @staticmethod
    def _add_user(convo, text):
        """Append a user turn, MERGING into the previous one if it's also a user turn.
        Chat models (Qwen) need strict user/assistant alternation — consecutive
        user turns (or a stray tool turn) make them emit empty turns after a few rounds."""
        tail = f"{IM_END}\n"
        if convo and convo[-1].startswith(f"{IM_START}user\n") and convo[-1].endswith(tail):
            convo[-1] = convo[-1][:-len(tail)] + "\n\n" + text + tail
        else:
            convo.append(f"{IM_START}user\n{text}{tail}")

    _LANG_TAGS = {"python", "py", "python3", "javascript", "js", "json", "bash", "sh",
                  "shell", "java", "c", "cpp", "c++", "go", "golang", "ruby", "rb",
                  "rust", "rs", "typescript", "ts", "lua", "perl", "php", "html", "css"}

    @staticmethod
    def _unescape_if_needed(content: str) -> str:
        """Clean file content of chat-model markdown/escaping artifacts: (1) un-escape an
        over-escaped one-line file (\\\\n → real newline; Qwen does this); (2) strip
        surrounding ``` fences; (3) drop a stray leading bare language-tag line ('python').
        Keeps write_file robust to the several ways Qwen emits code."""
        if not content:
            return content
        if "\n" not in content and ("\\n" in content or "\\t" in content):
            content = (content.replace("\\n", "\n").replace("\\t", "\t")
                              .replace('\\"', '"').replace("\\'", "'"))
        c = content.strip("\n")
        if c.lstrip().startswith("```"):
            c = c.lstrip()
            c = c.split("\n", 1)[1] if "\n" in c else ""
        if c.rstrip().endswith("```"):
            c = c.rstrip()[:-3].rstrip("\n")
        first, sep, rest = c.partition("\n")
        if sep and first.strip().strip("`").lower() in BonsaiEngine._LANG_TAGS:
            c = rest
        return c if c.strip() else content

    @staticmethod
    def _extract_code_block(text: str):
        """Return the code inside the first ``` fenced block, or None. Chat coding
        models (e.g. Qwen2.5-Coder) often output a markdown code block instead of a
        write_file tool call — we capture it and save it."""
        import re
        m = re.search(r"```[a-zA-Z0-9_+-]*\n(.*?)```", text, re.S)
        return m.group(1).rstrip("\n") if m else None

    def _run_tool_phase(self, convo, allow_writes, think, genkey, alive,
                        budget, max_calls, step_label, default_file=None):
        """Run a SHORT focused tool loop on `convo` (one plan step or one fix).
        Yields events, mutates `convo`, and returns the remaining global budget.
        Stops when the model stops calling tools, the per-phase cap is hit, or the
        budget runs out. `default_file`: if the model writes a code block instead of
        calling a tool, save it to this file (bridges markdown-style models)."""
        calls = 0
        forced = False
        while budget > 0 and calls < max_calls and alive():
            prefill = "" if think else NOTHINK_PREFILL
            prompt = "".join(convo) + f"{IM_START}assistant\n{prefill}"
            payload = {"prompt": prompt, "max_tokens": 1024 if think else 512,
                       "temperature": 0.15, "top_p": 0.9, "rep_pen": 1.07,
                       "stop": ["</tool_call>", IM_END, IM_START], "stream": True}
            acc = ""
            for delta in self._stream_completion(payload, genkey=genkey):
                acc += delta
                yield {"type": "token", "text": delta, "label": step_label}
                if not alive():
                    return budget

            tc = self._parse_tool_call(acc)
            if tc is None and "<think>" in acc and "</think>" not in acc and alive():
                cont = dict(payload)
                cont["prompt"] = prompt + acc + "\n</think>\n\n"
                cont["max_tokens"] = 320
                more = ""
                for delta in self._stream_completion(cont, genkey=genkey):
                    more += delta
                    yield {"type": "token", "text": delta, "label": step_label}
                    if not alive():
                        return budget
                acc = acc + "\n</think>\n\n" + more
                tc = self._parse_tool_call(acc)

            # Markdown fallback: model wrote a code block instead of a write_file call
            # (Qwen2.5-Coder does this). Save it to the step's file.
            if tc is None and default_file and allow_writes:
                code = self._extract_code_block(acc)
                if code:
                    tc = {"name": "write_file", "arguments": {"path": default_file, "content": code}}

            if tc is None:
                # The model narrated without acting. If it has done NOTHING this phase,
                # force one tool call (a step that just talks accomplishes nothing — this
                # was the cause of "step 1 created no file"). Otherwise accept it's done.
                convo.append(f"{IM_START}assistant\n{self._strip_think(acc).strip()}{IM_END}\n")
                if calls == 0 and not forced and budget > 0 and alive():
                    forced = True
                    self._add_user(convo, "You described it but didn't save anything. Now WRITE the "
                                 "code: either a write_file <tool_call>, or output the file's full "
                                 "code in one ```python ...``` block (it will be saved).")
                    continue
                return budget

            name = tc.get("name", "")
            args = tc.get("arguments", {}) or {}
            if not isinstance(args, dict):
                args = {}
            yield {"type": "tool_call", "name": name, "args": args}
            result = self._exec_tool(name, args, allow_writes)
            yield {"type": "tool_result", "name": name, "result": result}
            convo.append(f"{IM_START}assistant\n<tool_call>\n"
                         f'{{"name": "{name}", "arguments": {json.dumps(args)}}}\n'
                         f"</tool_call>{IM_END}\n")
            convo.append(f"{IM_START}user\n<tool_response>\n{result}\n</tool_response>{IM_END}\n")
            budget -= 1
            calls += 1
        return budget

    def _numbered_context(self, report: str) -> str:
        """Return line-numbered contents of the relevant source files so the fixer can
        call replace_lines on the exact line. Includes files named in the report PLUS
        the project's NON-test source files (a test failure names the test, but the bug
        is usually in the source it imports — the fixer needs to see that, numbered)."""
        import re, pathlib
        rels = []
        for rel in re.findall(r"[\w./-]+\.[A-Za-z0-9]+", report):
            rels.append(rel)
        for p in sorted(self.workspace.rglob("*")):
            if p.is_file() and p.suffix.lower() in LANGS:
                name = p.name
                if not (name.startswith("test_") or name.endswith("_test.py")):
                    rels.append(str(p.relative_to(self.workspace)))
        seen, out = set(), []
        for rel in rels:
            if rel in seen:
                continue
            seen.add(rel)
            t = safe_resolve(self.workspace, rel)
            if t is None or not t.is_file() or t.suffix.lower() not in LANGS:
                continue
            lines = t.read_text(errors="replace").split("\n")[:80]
            body = "\n".join(f"{n}: {ln}" for n, ln in enumerate(lines, 1))
            out.append(f"Current {rel} (line-numbered):\n{body}")
            if len(out) >= 4:        # cap context size
                break
        return "\n\n".join(out)

    @staticmethod
    def _step_filename(pstep: str):
        """Best-effort: the file name a plan step is about."""
        import re
        m = re.search(r"([\w/]+\.[A-Za-z0-9]+)", pstep or "")
        return m.group(1) if m else None

    def _skeleton_targets(self, hint):
        """(rel_path, [function/method names]) of the file to fill — the hinted file,
        else the most-recently-modified source file. Names are parsed deterministically."""
        import re
        cand = None
        if hint:
            t = safe_resolve(self.workspace, hint)
            if t and t.is_file():
                cand = t
        if cand is None:
            srcs = [p for p in self.workspace.rglob("*")
                    if p.is_file() and p.suffix.lower() in LANGS and not p.name.startswith("test_")]
            cand = max(srcs, key=lambda p: p.stat().st_mtime) if srcs else None
        if cand is None:
            return None, []
        names = re.findall(r"^\s*def\s+(\w+)", cand.read_text(errors="replace"), re.M)
        return str(cand.relative_to(self.workspace)), names

    def _implement_step(self, convo, i, pstep, task, strategy,
                        allow_writes, think, genkey, alive, budget):
        """Implement ONE plan step using `strategy`. Yields events, mutates convo,
        returns remaining budget. Three ways to narrow the model's cognitive load:
          oneshot  — write the whole file in one go (default).
          spec     — a micro-step writes signatures+1-liners, then implement from that.
          skeleton — write stub bodies, then fill each function via replace_function.
          auto     — skeleton for a class-heavy file, oneshot otherwise.
        """
        if strategy == "auto":
            import re as _re
            strategy = "skeleton" if _re.search(r"\bclass\b", pstep or "", _re.I) else "oneshot"

        # ── ONESHOT ─────────────────────────────────────────────────────────────
        if strategy not in ("spec", "skeleton"):
            self._add_user(convo, f"Do plan step {i} now: {pstep}\n"
                         f"Do ONLY this step — no extra files/functions/features. If it creates a "
                         f"file, WRITE it first (write_file/append_file) before running anything. "
                         f"One tool per message. Say 'step done' when finished.")
            # Don't bridge a code block to a file on a RUN/TEST step — the model may
            # emit the shell command in a block and we'd overwrite the file with it.
            import re as _re
            df = None if _re.match(r"\s*(run|execute|verify|test\b)", pstep or "", _re.I) \
                 else self._step_filename(pstep)
            return (yield from self._run_tool_phase(convo, allow_writes, think, genkey, alive,
                                                    budget, max_calls=3, step_label=f"impl {i}",
                                                    default_file=df))

        # ── SPEC ────────────────────────────────────────────────────────────────
        if strategy == "spec":
            yield {"type": "status", "msg": f"📐 Spec for step {i}…"}
            spec = yield from self._oneshot(
                system="You are the SPEC writer. For the ONE file in this step, list each "
                       "function/class SIGNATURE it needs and a one-line note (plain text, after a "
                       "dash) on what each does. SIGNATURES ONLY — no bodies, no docstrings, no "
                       "triple-quotes, no code fences, no extra functions. Terse.",
                user=f"Overall task:\n{task}\n\nThis step (one file): {pstep}",
                max_tokens=240, genkey=genkey, alive=alive, think=think)
            yield {"type": "tool_result", "name": f"spec {i}", "result": spec or "(none)"}
            self._add_user(convo, f"Do plan step {i}: {pstep}\nImplement EXACTLY this spec — "
                         f"write the COMPLETE file with real bodies in ONE write_file. Nothing beyond "
                         f"the spec. One tool per message.\n\nSPEC:\n{spec}")
            return (yield from self._run_tool_phase(convo, allow_writes, think, genkey, alive,
                                                    budget, max_calls=3, step_label=f"impl {i}",
                                                    default_file=self._step_filename(pstep)))

        # ── SKELETON ────────────────────────────────────────────────────────────
        yield {"type": "status", "msg": f"🦴 Skeleton for step {i}…"}
        self._add_user(convo, f"Do plan step {i}: {pstep}\nFIRST, write_file the file as a "
                     f"SKELETON: every function/class it needs with correct signatures, but each "
                     f"body is just `raise NotImplementedError`. No real logic yet. One tool.")
        budget = yield from self._run_tool_phase(convo, allow_writes, think, genkey, alive,
                                                 budget, max_calls=2, step_label=f"skel {i}")
        fpath, fnames = self._skeleton_targets(self._step_filename(pstep))
        if not fpath or not fnames:
            return budget
        for fn in fnames:                     # progressively replace each stub with real code
            if budget <= 0 or not alive():
                break
            yield {"type": "status", "msg": f"✍️ Filling {fn}() in {fpath}…"}
            self._add_user(convo, f"Now implement `{fn}` in {fpath}: ONE replace_function "
                         f"call (name='{fn}') replacing its NotImplementedError stub with the real "
                         f"code. Do NOT run or test anything — other methods are still stubs; just "
                         f"fill this one and stop.\n{self._numbered_context(fpath)}")
            budget = yield from self._run_tool_phase(convo, allow_writes, think, genkey, alive,
                                                     budget, max_calls=1, step_label=f"fill {fn}")
        return budget

    def _agent_run_staged(self, task, allow_writes, max_steps, context,
                          think, genkey, alive, impl_strategy="oneshot", research=True):
        """Multi-phase pipeline. Each phase is a separate, minimal-scope model
        instance so the 1-bit model never has to analyse + plan + code + test at
        once:  ANALYZE → PLAN → for each step (IMPLEMENT → CHECK → FIX) → final CHECK.
        CHECK is the deterministic system auto-test, not a flaky model self-review."""
        base_sys = self._agent_base_system(allow_writes, context)
        budget = max(max_steps, 4)

        # ── PHASE 1 · ANALYZE ────────────────────────────────────────────────
        yield {"type": "status", "msg": "🔍 Analyzing the task…"}
        analysis = yield from self._oneshot(
            system="You are the ANALYST. In 2–3 SHORT bullet points (one line each), state what "
                   "the user wants, which files are involved, and the key requirements. Be terse. "
                   "Do NOT write code.",
            user=task, max_tokens=160, genkey=genkey, alive=alive, think=think)
        yield {"type": "tool_result", "name": "analysis", "result": analysis or "(none)"}
        if not alive():
            return

        # ── PHASE 1.5 · RESEARCH (system-enforced — the model won't reliably look
        #     things up itself, so we do it and put the docs in front of it) ──────
        if research and alive():
            yield {"type": "status", "msg": "🔎 Deciding what to research…"}
            q = yield from self._oneshot(
                system="You are the RESEARCHER. Output ONE SPECIFIC web-search query for the exact "
                       "library + feature this task needs docs for — name the module AND the specific "
                       "function/API. Bad: 'python'. Good: 'python argparse add_subparsers example'. "
                       "If the task is trivial standard Python (basic functions, printing, math) and "
                       "needs no docs, output exactly: none",
                user=f"Task: {task}\n\nAnalysis:\n{analysis}",
                max_tokens=40, genkey=genkey, alive=alive, think=False)
            q = (q or "").strip().strip('"`').splitlines()[0][:120]
            # reject a uselessly-vague query (single common word like "python")
            if q.lower() in ("python", "python3", "code", "programming", "script"):
                q = "none"
            if q and q.lower() != "none" and len(q) > 3 and alive():
                yield {"type": "status", "msg": f"🔎 Researching: {q}"}
                try:
                    results = self._web_search(q, n=4)
                    yield {"type": "tool_result", "name": "web_search", "result": results}
                    import re
                    urls = re.findall(r"https?://\S+", results)
                    # prefer official/clean documentation over forum pages (SO etc. come
                    # back as mostly nav boilerplate; docs sites fetch cleanly)
                    docs = [u for u in urls if re.search(r"docs\.|/doc|readthedocs|developer\.|\.org/.*\b(library|reference|guide|tutorial)", u)]
                    url = (docs or urls or [None])[0]
                    if url:
                        page = self._web_fetch(url, max_chars=2200)
                        yield {"type": "tool_result", "name": "web_fetch",
                               "result": f"{url}\n{page[:400]}…"}
                        base_sys += (f"\n\n# Reference docs for this task (from {url}) — use this, "
                                     f"do NOT guess the API:\n{page}")
                except Exception as e:
                    yield {"type": "tool_result", "name": "web_search", "result": f"(research failed: {e})"}
            else:
                yield {"type": "tool_result", "name": "research", "result": "no external research needed"}

        # ── PHASE 2 · PLAN ───────────────────────────────────────────────────
        yield {"type": "status", "msg": "🧭 Planning the steps…"}
        plan_text = yield from self._oneshot(
            system="You are the PLANNER. Output a numbered plan with ONE step PER FILE the task "
                   "needs (a small task is 2–4 steps; a bigger multi-file project may be more — "
                   "but never pad with redundant steps). Plan ONLY what the task explicitly asks — "
                   "do NOT invent extra functions, loops, demo prints, or features. Each step writes "
                   "ONE WHOLE file COMPLETE (all the functions/classes that file needs at once). "
                   "ONE file = ONE step; NEVER a second step touching the same file, and NEVER a "
                   "step per function/method. The last step runs/tests it. No code, just the plan.",
            user=f"Task:\n{task}\n\nAnalysis:\n{analysis}", max_tokens=300,
            genkey=genkey, alive=alive, think=think)
        steps = self._parse_plan(plan_text)
        yield {"type": "tool_result", "name": "plan",
               "result": "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))}
        if not alive():
            return

        # ── PHASE 3 · EXECUTE each step (IMPLEMENT → CHECK → FIX) ─────────────
        convo = [f"{IM_START}system\n{base_sys}{IM_END}\n",
                 f"{IM_START}user\nTask: {task}\n\nApproved plan:\n"
                 + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) + f"{IM_END}\n"]

        for i, pstep in enumerate(steps, 1):
            if not alive() or budget <= 0:
                break
            yield {"type": "status", "msg": f"🔨 Implementing {i}/{len(steps)}: {pstep[:70]}"}
            budget = yield from self._implement_step(
                convo, i, pstep, task, impl_strategy,
                allow_writes, think, genkey, alive, budget)

            # CHECK (deterministic) — always, even though no one asked
            if allow_writes:
                yield {"type": "status", "msg": f"🧪 Checking after step {i}…"}
                passed, report = self._auto_verify()
                yield {"type": "tool_result",
                       "name": "check" if passed else "check ✗", "result": report}
                # FIX (focused) — only when the check failed and budget remains
                if not passed and budget > 0 and alive():
                    yield {"type": "status", "msg": f"🔧 Fixing step {i}…"}
                    # Give the fixer the NUMBERED file so replace_lines targets the
                    # exact line — the single biggest lever for fix success.
                    numbered = self._numbered_context(report)
                    self._add_user(convo, f"<tool_response>\nThe automatic check FAILED:\n"
                                 f"{report}\n{numbered}\nFix it now. For a syntax error, use "
                                 f"replace_lines on the reported line only — do not retype the "
                                 f"whole file.")
                    budget = yield from self._run_tool_phase(
                        convo, allow_writes, think, genkey, alive, budget,
                        max_calls=3, step_label=f"fix {i}")
                    passed, report = self._auto_verify()
                    yield {"type": "tool_result",
                           "name": "recheck" if passed else "recheck ✗", "result": report}

        # ── FINAL CHECK + summary ────────────────────────────────────────────
        if not alive():
            return
        passed, report = (self._auto_verify() if allow_writes else (True, "read-only run"))
        verdict = "✅ all checks passed" if passed else "⚠️ checks still failing"
        yield {"type": "answer",
               "content": f"Finished the plan ({len(steps)} steps). {verdict}.\n\n[final test] {report}"}
        yield {"type": "done", "steps": max(0, max_steps - budget), "staged": True}
