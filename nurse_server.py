# -*- coding: utf-8 -*-
# nurse_server.py — 既存スクリプトをそのまま使うWebラッパ
# 全工程（assessment/diagnosis/record/careplan）を非同期・中止可に統一
from __future__ import annotations
import os, sys, json, subprocess, platform, threading, webbrowser, time
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

APP_DIR = Path(__file__).resolve().parent
os.chdir(APP_DIR)

FILES = {
    "ASSESS_RESULT": "assessment_result.txt",
    "DIAG_RESULT":   "diagnosis_result.txt",
    "DIAG_JSON":     "diagnosis_candidates.json",
    "RECORD_RESULT": "record_result.txt",
    "PLAN_RESULT":   "careplan_result.txt",
    "NANDA_XLSX":    "nanda_db.xlsx",
}

def _cmd_for(script_py: str, exe_name: str) -> list[str]:
    try:
        if getattr(sys, "frozen", False):
            return [str(Path(sys.executable).with_name(exe_name))]
    except Exception:
        pass
    return [sys.executable, "-X", "utf8", script_py]

ENV_BASE = {
    "AI_PROVIDER": "ollama",
    "AI_MODEL": os.environ.get("AI_MODEL","qwen2.5:7b-instruct"),
    "OLLAMA_HOST": os.environ.get("OLLAMA_HOST","http://127.0.0.1:11434"),
    "OPENAI_API_KEY": "",
    "AI_LOG_DISABLE": "1",
    "PYTHONIOENCODING":"utf-8",
    "PYTHONUTF8":"1",
}

class RunnerState(dict):
    def __init__(self): super().__init__(proc=None, started_at=None, done=False, rc=None, stdout="", stderr="", stdin_text="")

RUN: dict[str, RunnerState] = {
    "assessment": RunnerState(),
    "diagnosis":  RunnerState(),
    "record":     RunnerState(),
    "careplan":   RunnerState(),
}

def start_async(key: str, script_py: str, exe_name: str, stdin_text: str = "") -> bool:
    st = RUN[key]
    if st["proc"] and st["proc"].poll() is None:
        return False
    st.update({"done": False, "rc": None, "stdout": "", "stderr": "", "started_at": time.time(), "stdin_text": stdin_text})
    cmd = _cmd_for(script_py, exe_name)
    env = os.environ.copy(); env.update(ENV_BASE)
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="ignore", env=env
    )
    st["proc"] = proc

    def _runner():
        out, err = proc.communicate(input=stdin_text)
        st["stdout"], st["stderr"], st["rc"], st["done"] = (out or ""), (err or ""), proc.returncode, True

    threading.Thread(target=_runner, daemon=True).start()
    return True

def kill_run(key: str):
    st = RUN[key]; p = st.get("proc")
    if p and p.poll() is None:
        try:
            p.terminate(); time.sleep(0.2)
            if p.poll() is None: p.kill()
        except Exception:
            pass
    st.update({"proc": None, "done": False})

class Handler(SimpleHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")

    def translate_path(self, path):
        p = super().translate_path(path)
        if path in ("/","/index","/ui"):
            return str(APP_DIR / "nurse_ui.html")
        return p

    def _ok_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200); self._cors()
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _ok_bytes(self, data: bytes, ctype="application/octet-stream"):
        self.send_response(200); self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def _err(self, code, msg=""):
        body = (msg or "").encode("utf-8")
        self.send_response(code); self._cors()
        self.send_header("Content-Type","text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        if self.path.startswith("/files/"):
            name = unquote(self.path.split("/files/",1)[1])
            fp = APP_DIR / name
            if not fp.exists(): return self._err(404, f"not found: {name}")
            return self._ok_bytes(fp.read_bytes(), "text/plain; charset=utf-8")

        if self.path == "/nanda.xlsx":
            fp = APP_DIR / FILES["NANDA_XLSX"]
            if not fp.exists(): return self._err(404, "nanda_db.xlsx が見つかりません")
            return self._ok_bytes(fp.read_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        if self.path.startswith("/status/"):
            key = self.path.split("/status/",1)[1]
            if key not in RUN: return self._err(404, "unknown job")
            st = RUN[key]; running = bool(st["proc"] and st["proc"].poll() is None)
            res_map = {
                "assessment": FILES["ASSESS_RESULT"],
                "diagnosis":  FILES["DIAG_RESULT"],
                "record":     FILES["RECORD_RESULT"],
                "careplan":   FILES["PLAN_RESULT"],
            }
            result = ""
            p = APP_DIR / res_map.get(key, "")
            if p.exists(): result = p.read_text("utf-8",errors="ignore")
            return self._ok_json({"running": running, "done": st["done"], "rc": st["rc"], "result": result, "stdout": st["stdout"], "stderr": st["stderr"]})

        return super().do_GET()

    def do_POST(self):
        ln = int(self.headers.get("Content-Length","0") or 0)
        body = self.rfile.read(ln) if ln>0 else b"{}"
        try: js = json.loads(body.decode("utf-8") or "{}")
        except: js = {}

        if self.path == "/run/assessment":
            S = js.get("S","").strip(); O = js.get("O","").strip()
            payload = (S + "\n<<<SEP>>>\n" + O).strip()
            ok = start_async("assessment", "assessment.py", "assessment.exe", payload); return self._ok_json({"started": bool(ok)})

        if self.path == "/run/diagnosis":
            ok = start_async("diagnosis", "diagnosis.py", "diagnosis.exe"); return self._ok_json({"started": bool(ok)})

        if self.path == "/run/record":
            ok = start_async("record", "record.py", "record.exe"); return self._ok_json({"started": bool(ok)})

        if self.path == "/run/careplan":
            ok = start_async("careplan", "careplan.py", "careplan.exe"); return self._ok_json({"started": bool(ok)})

        if self.path == "/cancel/assessment":
            kill_run("assessment"); return self._ok_json({"canceled": True})
        if self.path == "/cancel/diagnosis":
            kill_run("diagnosis");  return self._ok_json({"canceled": True})
        if self.path == "/cancel/record":
            kill_run("record");     return self._ok_json({"canceled": True})
        if self.path == "/cancel/careplan":
            kill_run("careplan");   return self._ok_json({"canceled": True})

        return self._err(404, "unknown endpoint")

def main():
    port = int(os.environ.get("PORT","8008"))
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/ui"
    print(f"* Nurse server on {url}")
    webbrowser.open(url)
    httpd.serve_forever()

if __name__ == "__main__":
    main()
