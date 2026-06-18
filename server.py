#!/usr/bin/env python3
"""
BonsAI IDE — Python3 stdlib backend.
File API + terminal + Bonsai proxy. Zero external deps.

Usage: python3 server.py [workspace] [--port PORT] [--kcpp URL]
"""
import argparse
import http.server
import json
import mimetypes
import os
import pathlib
import select
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

from bonsai_engine import BonsaiEngine

FRONTEND_DIR = pathlib.Path(__file__).parent / "frontend"
DEFAULT_PORT  = 3000
DEFAULT_KCPP  = "http://127.0.0.1:5001"
DEFAULT_COMPLETE = "http://127.0.0.1:5002"   # small Qwen2.5-Coder FIM model

# ── path safety ────────────────────────────────────────────────────────────────

def safe_resolve(workspace: pathlib.Path, user_path: str) -> pathlib.Path | None:
    """Return absolute path inside workspace, or None on traversal attempt."""
    try:
        target = (workspace / user_path.lstrip("/\\")).resolve()
        target.relative_to(workspace.resolve())   # raises ValueError if outside
        return target
    except Exception:
        return None

# ── handler ────────────────────────────────────────────────────────────────────

class IDEHandler(http.server.BaseHTTPRequestHandler):
    workspace: pathlib.Path = pathlib.Path.home()
    kcpp_url:  str           = DEFAULT_KCPP
    engine: BonsaiEngine     = None    # set in main()
    _lock = threading.Lock()

    def log_message(self, fmt, *args):          # suppress default logging
        pass

    # ── server-sent events ──────────────────────────────────────────────────────

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type",      "text/event-stream")
        self.send_header("Cache-Control",     "no-cache")
        self.send_header("Connection",        "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

    def _client_gone(self) -> bool:
        """Detect a disconnected SSE client. Writes to a dead socket are buffered
        and don't raise promptly, so we peek: if the socket is readable and recv
        sees EOF (b''), the peer closed. This is what lets us abort the model
        server-side the instant the browser tab closes or Stop is clicked."""
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            if r:
                if not self.connection.recv(1, socket.MSG_PEEK):
                    return True
        except Exception:
            return True
        return False

    def _sse_send(self, obj) -> bool:
        """Write one SSE event. Returns False if the client disconnected."""
        if self._client_gone():
            return False
        try:
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _start_disconnect_watchdog(self, genkey: str):
        """Poll the client connection in a side thread. On disconnect: (1) set the
        `disconnected` flag so the agent loop stops starting new steps, and (2)
        repeatedly abort the active generation (a single abort can miss it between
        steps). Returns (stop_event, disconnected_event)."""
        stop = threading.Event()
        disconnected = threading.Event()
        engine = self.engine

        def watch():
            while not stop.wait(1.0):
                if self._client_gone():
                    disconnected.set()
                    while not stop.wait(0.5):
                        engine._abort(genkey)
                    return
        threading.Thread(target=watch, daemon=True).start()
        return stop, disconnected

    # helpers ──────────────────────────────────────────────────────────────────

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def err(self, msg, status=400):
        self.send_json({"error": msg}, status)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def qs(self) -> dict:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    # routing ──────────────────────────────────────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/api/"):
            self._api_get(p)
        else:
            self._static(p)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/api/"):
            self._api_post(p)
        else:
            self.err("not found", 404)

    # static files ─────────────────────────────────────────────────────────────

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        fp = FRONTEND_DIR / path.lstrip("/")
        if not fp.is_file():
            self.send_response(404); self.end_headers(); return
        data = fp.read_bytes()
        mime, _ = mimetypes.guess_type(str(fp))
        self.send_response(200)
        self.send_header("Content-Type",   mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # GET /api/* ───────────────────────────────────────────────────────────────

    def _api_get(self, path: str):
        q = self.qs()
        p = q.get("path", [""])[0]

        if path == "/api/workspace":
            self.send_json({"workspace": str(self.workspace)})

        elif path == "/api/files":
            target = safe_resolve(self.workspace, p or "")
            if target is None or not target.is_dir():
                return self.err("invalid path")
            entries = []
            try:
                for item in sorted(target.iterdir(),
                                   key=lambda x: (x.is_file(), x.name.lower())):
                    stat = item.stat()
                    entries.append({
                        "name":  item.name,
                        "path":  str(item.relative_to(self.workspace)),
                        "isDir": item.is_dir(),
                        "size":  stat.st_size if item.is_file() else 0,
                        "mtime": stat.st_mtime,
                    })
            except PermissionError:
                pass
            self.send_json({"entries": entries, "cwd": p or ""})

        elif path == "/api/read":
            target = safe_resolve(self.workspace, p)
            if target is None or not target.is_file():
                return self.err("invalid path")
            try:
                self.send_json({"content": target.read_text(errors="replace"), "path": p})
            except Exception as e:
                self.err(str(e))

        elif path == "/api/allfiles":
            # flat workspace-relative file list for quick-open (Ctrl+P)
            skip = {".git", "__pycache__", "node_modules", ".cache", "logs",
                    "venv", ".venv", "dist", "build", "target", ".idea", ".vscode"}
            noise = {".o", ".a", ".so", ".dll", ".exe", ".pyc", ".bin", ".gguf",
                     ".obj", ".lib", ".class", ".lock", ".png", ".jpg", ".jpeg",
                     ".gif", ".webp", ".ico"}
            out, cap = [], 4000
            for root, dirs, files in os.walk(self.workspace):
                dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
                for f in files:
                    if f.startswith(".") and f not in (".gitignore", ".env.example"):
                        continue
                    if os.path.splitext(f)[1].lower() in noise:
                        continue
                    rel = os.path.relpath(os.path.join(root, f), self.workspace)
                    out.append(rel)
                    if len(out) >= cap:
                        break
                if len(out) >= cap:
                    break
            out.sort()
            self.send_json({"files": out, "truncated": len(out) >= cap})

        elif path == "/api/browse":
            # directory browser for the "Open Folder" picker. Unlike /api/files this
            # is NOT sandboxed to the workspace — it lets the user choose any folder
            # on the machine to open as a new workspace root. Lists directories only.
            try:
                base = pathlib.Path(p).expanduser() if p else pathlib.Path.home()
                base = base.resolve()
            except Exception:
                base = pathlib.Path.home()
            if not base.is_dir():
                base = pathlib.Path.home()
            dirs = []
            try:
                for item in sorted(base.iterdir(), key=lambda x: x.name.lower()):
                    if not item.is_dir():
                        continue
                    try:
                        if not os.access(item, os.R_OK):
                            continue
                    except OSError:
                        continue
                    dirs.append({"name": item.name, "path": str(item)})
            except PermissionError:
                pass
            parent = str(base.parent) if base.parent != base else None
            self.send_json({"path": str(base), "parent": parent, "dirs": dirs})

        elif path == "/api/runinfo":
            # is this file runnable, and what language? (drives the Run button)
            from bonsai_engine import LANGS
            ext = ("." + p.rsplit(".", 1)[-1]) if "." in p else ""
            lang = LANGS.get(ext.lower())
            self.send_json({"runnable": bool(lang),
                            "language": lang["name"] if lang else None})

        elif path == "/api/bonsai/status":
            # report both servers: the main 8B (chat/agent) and the small FIM
            # completion model. `complete` lets the UI show whether real FIM
            # autocomplete is active vs. the 8B continuation fallback.
            complete_ok = self.engine.completion_status() if self.engine else False
            try:
                req = urllib.request.Request(
                    f"{self.kcpp_url}/v1/models",
                    headers={"User-Agent": "bonsai-ide/1.0"},
                )
                with urllib.request.urlopen(req, timeout=2) as r:
                    data = json.loads(r.read())
                self.send_json({"running": True, "complete": complete_ok,
                                "models": data.get("data", [])})
            except Exception:
                self.send_json({"running": False, "complete": complete_ok})

        else:
            self.err("unknown endpoint", 404)

    # POST /api/* ──────────────────────────────────────────────────────────────

    def _api_post(self, path: str):
        b = self.body()

        if path == "/api/workspace":
            # re-root the workspace to a new absolute folder (the "Open Folder" action).
            try:
                new_ws = pathlib.Path(b.get("path", "")).expanduser().resolve()
            except Exception:
                return self.err("invalid path")
            if not new_ws.is_dir():
                return self.err("not a directory")
            with self._lock:
                IDEHandler.workspace = new_ws
                if self.engine is not None:
                    self.engine.workspace = new_ws
            self.send_json({"ok": True, "workspace": str(new_ws)})

        elif path == "/api/write":
            fp = safe_resolve(self.workspace, b.get("path", ""))
            if fp is None: return self.err("invalid path")
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(b.get("content", ""), encoding="utf-8")
            self.send_json({"ok": True})

        elif path == "/api/mkdir":
            dp = safe_resolve(self.workspace, b.get("path", ""))
            if dp is None: return self.err("invalid path")
            dp.mkdir(parents=True, exist_ok=True)
            self.send_json({"ok": True})

        elif path == "/api/delete":
            fp = safe_resolve(self.workspace, b.get("path", ""))
            if fp is None or not fp.exists(): return self.err("invalid path")
            shutil.rmtree(fp) if fp.is_dir() else fp.unlink()
            self.send_json({"ok": True})

        elif path == "/api/rename":
            src = safe_resolve(self.workspace, b.get("from", ""))
            dst = safe_resolve(self.workspace, b.get("to", ""))
            if src is None or dst is None: return self.err("invalid path")
            src.rename(dst)
            self.send_json({"ok": True})

        elif path == "/api/terminal":
            cmd  = b.get("cmd",  "")
            cwd_ = b.get("cwd",  str(self.workspace))
            if not pathlib.Path(cwd_).exists():
                cwd_ = str(self.workspace)
            try:
                r = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True,
                    timeout=30, cwd=cwd_,
                )
                self.send_json({"stdout": r.stdout, "stderr": r.stderr, "code": r.returncode})
            except subprocess.TimeoutExpired:
                self.send_json({"stdout": "", "stderr": "timed out", "code": -1})
            except Exception as e:
                self.send_json({"stdout": "", "stderr": str(e), "code": -1})

        elif path == "/api/run":
            # run the given source file via the language registry (Run button)
            rel = b.get("path", "")
            result = self.engine.run_file(rel, timeout=b.get("timeout", 30))
            self.send_json(result)

        elif path == "/api/bonsai/complete":
            # inline code completion — plain continuation, no-think, blocking JSON
            text = self.engine.complete(
                prefix=b.get("prefix", ""),
                suffix=b.get("suffix", ""),
                language=b.get("language", ""),
                max_tokens=b.get("max_tokens", 64),
            )
            self.send_json({"text": text})

        elif path == "/api/bonsai/chat":
            # plain chat — token-streamed over SSE (thinking optional)
            self._sse_start()
            genkey = self.engine._new_genkey()
            stop_watch, _ = self._start_disconnect_watchdog(genkey)
            gen = self.engine.chat_stream(
                b.get("messages", []),
                max_tokens=b.get("max_tokens"),       # None → think-aware default
                temperature=b.get("temperature", 0.3),
                think=b.get("think", True),
                project_aware=b.get("project_aware", True),
                genkey=genkey)
            full = []
            try:
                for delta in gen:
                    full.append(delta)
                    if not self._sse_send({"type": "token", "text": delta}):
                        return
                self._sse_send({"type": "done", "content": "".join(full)})
            finally:
                stop_watch.set(); gen.close()

        elif path == "/api/bonsai/agent":
            # agentic loop — event-streamed over SSE (thinking optional)
            self._sse_start()
            genkey = self.engine._new_genkey()
            stop_watch, disconnected = self._start_disconnect_watchdog(genkey)
            gen = self.engine.agent_run(
                task=b.get("task", ""),
                allow_writes=b.get("allow_writes", True),
                max_steps=b.get("max_steps", 8),
                context=b.get("context"),
                think=b.get("think", True),
                staged=b.get("staged", True),
                impl_strategy=b.get("impl_strategy", "oneshot"),
                research=b.get("research", True),
                genkey=genkey,
                alive=lambda: not disconnected.is_set())
            try:
                for event in gen:
                    if not self._sse_send(event):
                        return
            except Exception as e:
                # surface agent crashes instead of silently ending the stream
                import traceback
                tb = traceback.format_exc()
                sys.stderr.write(f"[agent crash] {tb}\n"); sys.stderr.flush()
                self._sse_send({"type": "error", "msg": f"agent crashed: {e}"})
            finally:
                stop_watch.set(); gen.close()        # stop watchdog + cascade abort

        else:
            self.err("unknown endpoint", 404)


# ── entry point ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="BonsAI IDE server")
    ap.add_argument("workspace", nargs="?", default=str(pathlib.Path.home()),
                    help="Root workspace directory (default: $HOME)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--kcpp", default=DEFAULT_KCPP, help="KoboldCPP base URL (8B chat/agent)")
    ap.add_argument("--complete-url", default=DEFAULT_COMPLETE,
                    help="KoboldCPP base URL for the small FIM completion model")
    args = ap.parse_args()

    ws = pathlib.Path(args.workspace).resolve()
    if not ws.is_dir():
        print(f"Error: workspace '{ws}' is not a directory", file=sys.stderr)
        sys.exit(1)

    IDEHandler.workspace = ws
    IDEHandler.kcpp_url  = args.kcpp.rstrip("/")
    IDEHandler.engine    = BonsaiEngine(args.kcpp.rstrip("/"), ws,
                                        complete_url=args.complete_url.rstrip("/"))

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), IDEHandler)
    print(f"BonsAI IDE  →  http://127.0.0.1:{args.port}")
    print(f"Workspace   →  {ws}")
    print(f"Bonsai API  →  {args.kcpp}")
    print(f"Completion  →  {args.complete_url}")
    print("Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
