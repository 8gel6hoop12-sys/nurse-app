# -*- coding: utf-8 -*-
"""
nurseapp_one.py
- 既存の assessment.py / diagnosis.py / careplan.py / record.py / record_review.py をそのままサブプロセス実行
- HTTPエンドポイント提供（/run/* と /files/*, /healthz）
- Windows用カスタムURLスキーム nurseapp://start の登録/起動ランチャも内蔵
使い方:
  1) 初回のみ（WindowsでURL起動したい場合）:
     python nurseapp_one.py --install-protocol
  2) 手動起動: python nurseapp_one.py --serve
  3) URL起動(ランチャ): nurseapp://start  → --start 相当
"""
import os, sys, json, subprocess, socket, time
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

HOST = os.environ.get("NURSE_UI_HOST", "127.0.0.1")
PORT = int(os.environ.get("NURSE_UI_PORT", "8008"))

APP_DIR = Path(__file__).resolve().parent
os.chdir(APP_DIR)

# プライバシー固定の環境（OpenAI遮断、Ollama固定）
ENV_OVER = os.environ.copy()
ENV_OVER.update({
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "AI_PROVIDER": "ollama",
    "AI_MODEL": ENV_OVER.get("AI_MODEL", "qwen2.5:7b-instruct"),
    "OLLAMA_HOST": ENV_OVER.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
    "AI_LOG_DISABLE": "1",
    "OPENAI_API_KEY": "",
})

ALLOW_FILES = {
    "assessment_result.txt","assessment_final.txt",
    "diagnosis_result.txt", "diagnosis_final.txt",
    "record_result.txt",    "record_final.txt",
    "careplan_result.txt",  "careplan_final.txt",
    "diagnosis_candidates.json",
}

def _read_text(p: Path) -> str:
    if not p.exists(): return ""
    try: return p.read_text(encoding="utf-8")
    except: return p.read_text(encoding="utf-8", errors="ignore")

def _write_json(h: BaseHTTPRequestHandler, code: int, obj: dict):
    b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    for k,v in {
        "Content-Type": "application/json; charset=utf-8",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Content-Length": str(len(b)),
    }.items():
        h.send_header(k, v)
    h.end_headers(); h.wfile.write(b)

def _write_text(h: BaseHTTPRequestHandler, code: int, text: str):
    b = text.encode("utf-8", errors="ignore")
    h.send_response(code)
    for k,v in {
        "Content-Type": "text/plain; charset=utf-8",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Content-Length": str(len(b)),
    }.items():
        h.send_header(k, v)
    h.end_headers(); h.wfile.write(b)

def run_script(script_py: str, stdin_text: str = "") -> tuple[int, str, str]:
    cmd = [sys.executable, "-X", "utf8", script_py]
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="ignore", env=ENV_OVER, cwd=str(APP_DIR))
        out, err = p.communicate(input=stdin_text)
        return p.returncode, out or "", err or ""
    except Exception as e:
        return 1, "", str(e)

class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        try: length = int(self.headers.get("Content-Length") or "0")
        except: length = 0
        body_raw = self.rfile.read(length) if length>0 else b""
        try: body = json.loads(body_raw.decode("utf-8")) if body_raw else {}
        except: body = {}

        if path == "/run/assessment":
            S = body.get("S",""); O = body.get("O","")
            payload = (S + "\n<<<SEP>>>\n" + O).strip()
            rc, out, err = run_script("assessment.py", stdin_text=payload)
            return _write_json(self, 200 if rc==0 else 500, {"ok": rc==0, "stdout": out, "stderr": err})

        if path == "/run/diagnosis":
            rc, out, err = run_script("diagnosis.py", "")
            return _write_json(self, 200 if rc==0 else 500, {"ok": rc==0, "stdout": out, "stderr": err})

        if path == "/run/record":
            rc, out, err = run_script("record.py", body.get("text",""))
            return _write_json(self, 200 if rc==0 else 500, {"ok": rc==0, "stdout": out, "stderr": err})

        if path == "/run/record_review":
            rc, out, err = run_script("record_review.py", body.get("text",""))
            return _write_json(self, 200 if rc==0 else 500, {"ok": rc==0, "stdout": out, "stderr": err})

        if path == "/run/careplan":
            rc, out, err = run_script("careplan.py", "")
            return _write_json(self, 200 if rc==0 else 500, {"ok": rc==0, "stdout": out, "stderr": err})

        return _write_json(self, 404, {"ok": False, "error": "unknown endpoint"})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/healthz"):
            return _write_json(self, 200, {"ok": True, "cwd": str(APP_DIR)})

        if parsed.path.startswith("/files/"):
            name = unquote(parsed.path[len("/files/"):])
            if name not in ALLOW_FILES:
                return _write_text(self, 403, "forbidden")
            p = APP_DIR / name
            if not p.exists():
                return _write_text(self, 404, f"{name} not found")
            if p.suffix.lower() == ".json":
                try: js = json.loads(_read_text(p) or "{}")
                except: js = {}
                return _write_json(self, 200, js)
            else:
                return _write_text(self, 200, _read_text(p))

def is_up(host=HOST, port=PORT, timeout=0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def serve():
    httpd = HTTPServer((HOST, PORT), Handler)
    print(f"[nurse-ui api] http://{HOST}:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

def start():
    # 既に起動済みなら何もしない
    if is_up(): return 0
    # 自分自身を --serve で非同期起動（コンソール非表示に配慮）
    creationflags = 0; startupinfo = None
    if os.name == "nt":
        try:
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        except Exception:
            pass
    subprocess.Popen([sys.executable, "-X", "utf8", __file__, "--serve"],
                     cwd=str(APP_DIR), env=ENV_OVER,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=creationflags, startupinfo=startupinfo)
    # 立ち上がり待機
    for _ in range(20):
        if is_up(): return 0
        time.sleep(0.4)
    return 0

def install_protocol_windows():
    # WindowsのHKCUに nurseapp:// を登録 → 実行時にこのスクリプトへ --start で渡す
    try:
        import winreg
    except Exception:
        print("Windows以外では不要です。")
        return 1
    py = Path(sys.executable)
    pythonw = py.with_name("pythonw.exe") if py.suffix.lower()==".exe" else py
    if pythonw.name.lower() == "python.exe":
        pythonw = py  # 無ければpython.exeで代用
    cmd = f'"{pythonw}" -X utf8 "{Path(__file__).resolve()}" --start "%1"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\nurseapp") as k0:
        winreg.SetValueEx(k0, None, 0, winreg.REG_SZ, "URL:nurseapp Protocol")
        winreg.SetValueEx(k0, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(k0, r"shell\open\command") as k1:
            winreg.SetValueEx(k1, None, 0, winreg.REG_SZ, cmd)
    print("登録完了: nurseapp://start でローカルサーバを起動できます。")
    return 0

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--serve" in args:
        serve()
    elif "--start" in args:
        sys.exit(start())
    elif "--install-protocol" in args:
        sys.exit(install_protocol_windows())
    else:
        print("Usage:")
        print("  python nurseapp_one.py --serve               # サーバ起動")
        print("  python nurseapp_one.py --start               # 既起動チェック→なければ非同期起動")
        print("  python nurseapp_one.py --install-protocol    # Windows: nurseapp:// を登録")
