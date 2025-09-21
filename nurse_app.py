# -*- coding: utf-8 -*-
""" nurse_app.py — 段階レビュー式 GUI（PyQt5）
    プライバシー徹底版（OpenAI 完全遮断・Ollamaローカル固定）
"""
from __future__ import annotations

# === 最小ブートストラップ & プライバシー徹底 ==============================
import os, sys, subprocess, platform
from pathlib import Path

# 1) 実行ディレクトリをこのファイルの場所に固定（相対パス崩れ対策）
try:
    APP_DIR = Path(__file__).resolve().parent
    os.chdir(APP_DIR)
except Exception:
    pass

# 2) PyQt5 を確保（※ネットに出ない：自動pipはしない）
def _ensure_pyqt5():
    try:
        import PyQt5  # noqa: F401
        return
    except Exception:
        sys.stderr.write(
            "[致命的] PyQt5 が見つかりません。仮想環境で下記を実行してください:\n"
            "  python -m pip install PyQt5>=5.15.9\n"
        )
        raise

_ensure_pyqt5()

# 3) HiDPI / Qt の既定（サイズ感固定）
os.environ.setdefault("PYTHONUTF8", "0")
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "0")
if platform.system().lower().startswith("win"):
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")

# 4) AI をローカル固定（OpenAI 完全遮断）
os.environ["AI_PROVIDER"]    = "ollama"
os.environ["AI_MODEL"]       = "qwen2.5:7b-instruct"
os.environ["OLLAMA_HOST"]    = "http://127.0.0.1:11434"   # ローカル限定
os.environ["AI_LOG_DISABLE"] = "1"
os.environ.pop("OPENAI_API_KEY", None)  # 万一 .env 等にあっても無効化
# ========================================================================

# 以降は元の先頭インポートに続けてOK
import os as _os, sys as _sys, subprocess as _subprocess, re, shutil, time, platform as _platform, json, getpass
from pathlib import Path as _Path
from typing import Optional, List, Dict, Any

# ==== Qt plugins self-heal (Windows) ====
def _fix_qt_plugin_path():
    try:
        import PyQt5
        from pathlib import Path
        root = Path(PyQt5.__file__).resolve().parent
        # PyQt5 の配下は環境で Qt or Qt5 のどちらか
        candidates = [root / "Qt" / "plugins", root / "Qt5" / "plugins"]
        base = next((p for p in candidates if p.exists()), None)
        if base:
            # 既存の壊れた設定はクリアして、正しい場所を明示
            for k in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
                v = os.environ.get(k, "")
                if v and not Path(v).exists():
                    os.environ.pop(k, None)
            os.environ.setdefault("QT_PLUGIN_PATH", str(base))
            os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(base / "platforms"))
    except Exception:
        pass

_fix_qt_plugin_path()
# ========================================

# （ここから下の PyQt5 import はそのままで大丈夫）
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QTextOption
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QTabWidget, QMessageBox, QStatusBar,
    QLineEdit, QCheckBox, QDialog, QFormLayout, QDialogButtonBox, QAction,
    QFileDialog, QProgressDialog, QSplitter, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QInputDialog, QToolBar, QAbstractItemView, QTableWidget,
    QTableWidgetItem, QGroupBox, QGridLayout, QStackedWidget, QPlainTextEdit,
    QScrollArea
)

# ====== 追加：Excel 閲覧に必要なライブラリ準備（※自動pipしない） ======
def _ensure_python_package(pkg: str) -> bool:
    try:
        __import__(pkg)
        return True
    except Exception:
        return False

_have_pd       = _ensure_python_package("pandas")
_have_openpyxl = _ensure_python_package("openpyxl")
_have_requests = _ensure_python_package("requests")

if _have_pd:
    import pandas as pd  # type: ignore
else:
    pd = None  # type: ignore

if not (_have_pd and _have_openpyxl and _have_requests):
    sys.stderr.write(
        "[注意] 一部の追加ライブラリが見つかりません（Excel閲覧などが無効になる可能性）。\n"
        "  必要なら: python -m pip install pandas openpyxl requests\n"
    )

# ================ 共通フォント（18px） ================
BASE_PX = 18
APP_FONT  = QFont("Meiryo");   APP_FONT.setPixelSize(BASE_PX)
MONO_FONT = QFont("Consolas"); MONO_FONT.setPixelSize(16)

# ================ ファイル名 ================
ASSESS_RESULT_TXT = "assessment_result.txt"
ASSESS_FINAL_TXT  = "assessment_final.txt"
DIAG_RESULT_TXT   = "diagnosis_result.txt"
DIAG_FINAL_TXT    = "diagnosis_final.txt"
DIAG_JSON         = "diagnosis_candidates.json"
RECORD_RESULT_TXT = "record_result.txt"
RECORD_FINAL_TXT  = "record_final.txt"
PLAN_RESULT_TXT   = "careplan_result.txt"
PLAN_FINAL_TXT    = "careplan_final.txt"
ENV_FILE          = ".env"
NANDA_XLSX        = "nanda_db.xlsx"
USER_XLSX_ROOT    = Path("user_xlsx")
APP_SETTINGS_JSON = Path("app_settings.json")

# 上側フォームの高さ（調整用）
SCROLL_HEIGHT = 260  # px

# ---- 配布ビルド(frozen)時のサブプロセス呼び出しヘルパー ----
def _cmd_for(script_py: str, exe_name: str) -> list[str]:
    """
    開発中: python.exe -X utf8 script.py
    配布版: 同フォルダの exe を直接呼ぶ
    """
    try:
        if getattr(sys, "frozen", False):  # PyInstaller で固めた場合
            return [str(Path(sys.executable).with_name(exe_name))]
    except Exception:
        pass
    return [sys.executable, "-X", "utf8", script_py]
# ------------------------------------------------------------

# ================ ユーティリティ ================
def read_text_safe(p: Path) -> str:
    if not p.exists(): return ""
    try:    return p.read_text(encoding="utf-8")
    except: return p.read_text(encoding="utf-8", errors="ignore")

def write_text_safe(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8", errors="ignore")

def ensure_file(fn: str):
    p = Path(fn)
    if not p.exists(): write_text_safe(p, "")

def dedupe(s: str) -> str:
    paras = [p.strip() for p in s.split("\n\n")]
    seen_p = set(); out_p = []
    for p in paras:
        key = p.replace(" ", "").replace("\u3000", "")
        if key and key not in seen_p:
            seen_p.add(key)
            seen_l = set(); out_l = []
            for ln in p.splitlines():
                ln = ln.rstrip()
                if ln and ln not in seen_l:
                    seen_l.add(ln); out_l.append(ln)
            out_p.append("\n".join(out_l))
    cleaned = "\n\n".join(out_p).strip()
    return cleaned or s.strip()

def alert(parent, title, msg): QMessageBox.warning(parent, title, msg)
def info(parent, title, msg):  QMessageBox.information(parent, title, msg)

# ================ アプリ設定（JSON） ================
def load_app_settings() -> Dict[str, Any]:
    if APP_SETTINGS_JSON.exists():
        try: return json.loads(read_text_safe(APP_SETTINGS_JSON))
        except: pass
    return {"use_so_template": True}

def save_app_settings(js: Dict[str, Any]) -> None:
    try: write_text_safe(APP_SETTINGS_JSON, json.dumps(js, ensure_ascii=False, indent=2))
    except: pass

# ================ 外部プロセス実行（非同期） ================
class ProcRunner(QThread):
    finished_ok  = pyqtSignal(str)
    finished_err = pyqtSignal(str)
    def __init__(self, cmd: list[str], stdin_text: str = "", env_overrides: dict | None = None, shell: bool=False):
        super().__init__()
        self.cmd = cmd; self.stdin_text = stdin_text
        self.env_overrides = env_overrides or {}; self.shell = shell
    def run(self):
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"]="utf-8"; env["PYTHONUTF8"]="1"
            env.setdefault("LANG","C.UTF-8"); env.setdefault("LC_ALL","C.UTF-8")
            env["AI_LOG_DISABLE"]="1"; env.update(self.env_overrides)
            proc = subprocess.Popen(
                self.cmd if not self.shell else " ".join(self.cmd),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="ignore", env=env, shell=self.shell)
            out, err = proc.communicate(input=self.stdin_text)
            if proc.returncode != 0:
                self.finished_err.emit(((err or "")+"\n"+(out or "")).strip() or f"returncode:{proc.returncode}")
            else:
                self.finished_ok.emit((out or "").strip())
        except Exception as e:
            self.finished_err.emit(str(e))

# ================ 無料AI（Ollama）承認/自動インストール ================
class OllamaConsentDialog(QDialog):
    def __init__(self, parent=None, model_name="qwen2.5:7b-instruct"):
        super().__init__(parent)
        self.setWindowTitle("無料AI（Ollama）の準備")
        self.setModal(True); self.setFont(APP_FONT); self.setStyleSheet(base_stylesheet())
        self.model_name = model_name
        title = QLabel("無料AI（Ollama）を準備します"); title.setStyleSheet("font-weight:600;")
        lbl = QLabel("【承認】を押すと、Ollama を自動インストールし、モデルを取得します。\n・回線状況により数分かかることがあります\n・OSによっては管理者権限の確認が表示されます"); lbl.setWordWrap(True)
        self.chk_pull = QCheckBox(f"モデルも自動で取得する（{self.model_name}）"); self.chk_pull.setChecked(True)
        self.btns = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.btn_ok = QPushButton("承認してインストール"); self.btn_ok.setStyleSheet("font-weight:600;"); self.btns.addButton(self.btn_ok, QDialogButtonBox.AcceptRole)
        self.btn_ok.clicked.connect(self.accept)
        lay = QVBoxLayout(self); lay.addWidget(title); lay.addWidget(lbl); lay.addWidget(self.chk_pull); lay.addWidget(self.btns)

# ================ Excel 閲覧ダイアログ ================
class ExcelViewerDialog(QDialog):
    def __init__(self, parent: QWidget, xlsx_path: Path):
        super().__init__(parent)
        self.setWindowTitle("NANDA Excel（閲覧専用）")
        self.setModal(True); self.setFont(APP_FONT); self.setStyleSheet(base_stylesheet())
        self.xlsx_path = xlsx_path
        lay = QVBoxLayout(self)
        topline = QHBoxLayout()
        self.search_box = QLineEdit(); self.search_box.setPlaceholderText("検索（診断名 / 定義 など）")
        btn_open = QPushButton("エクスプローラで開く"); btn_open.clicked.connect(self._open_folder)
        topline.addWidget(QLabel(f"ファイル: {self.xlsx_path.as_posix()}")); topline.addStretch(1)
        topline.addWidget(self.search_box); topline.addWidget(btn_open); lay.addLayout(topline)
        self.table = QTableWidget(); self.table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.table.setFont(APP_FONT); lay.addWidget(self.table)
        self.df_all: Optional['pd.DataFrame'] = None; self._load_excel(); self.search_box.textChanged.connect(self._apply_filter)
    def _open_folder(self):
        path = self.xlsx_path.resolve().parent
        if platform.system().lower().startswith("win"): os.startfile(str(path))
        elif platform.system().lower().startswith("darwin"): subprocess.run(["open", str(path)])
        else: subprocess.run(["xdg-open", str(path)])
    def _load_excel(self):
        try:
            if pd is None:
                raise RuntimeError("pandas が未導入のため表示できません。")
            self.df_all = pd.read_excel(self.xlsx_path)
        except Exception as e:
            alert(self, "読込失敗", f"Excel の読み込みに失敗しました。\n{e}"); self.df_all = None; return
        self._render_df(self.df_all)
    def _apply_filter(self, text: str):
        if self.df_all is None: return
        t = (text or "").strip()
        if not t: self._render_df(self.df_all); return
        df = self.df_all[self.df_all.apply(lambda r: any(t.lower() in str(v).lower() for v in r.values), axis=1)]
        self._render_df(df)
    def _render_df(self, df: 'pd.DataFrame'):
        self.table.clear(); self.table.setRowCount(len(df)); self.table.setColumnCount(len(df.columns))
        self.table.setHorizontalHeaderLabels([str(c) for c in df.columns])
        for i, (_, row) in enumerate(df.iterrows()):
            for j, v in enumerate(row.values):
                it = QTableWidgetItem("" if v is None else str(v)); self.table.setItem(i, j, it)
        self.table.resizeColumnsToContents()

# ================ スタイル ================
def base_stylesheet() -> str:
    return """
    * { font-size: 18px; }
    QMainWindow, QWidget { background: #FFF7FA; color: #333; }
    QLabel { color:#333; }
    QLineEdit, QTextEdit, QPlainTextEdit, QTableWidget, QTreeWidget {
        background: #FFFFFF; border: 1px solid #E9C7D5; border-radius: 8px; padding: 4px;
    }
    QPushButton {
        background: #F7CFE3; border: 1px solid #E7B6CE; padding: 4px 10px; border-radius: 10px; font-weight: 600;
    }
    QPushButton:hover { background: #FACFE6; }
    QTabBar::tab {
        background: #F4DDE6; border: 1px solid #E9C7D5; padding: 4px 10px;
        border-top-left-radius: 8px; border-top-right-radius: 8px; margin-right: 4px;
    }
    QTabBar::tab:selected { background: #FBE7F1; }
    QHeaderView::section { background: #F4DDE6; padding: 4px; border: 1px solid #E9C7D5; }
    QProgressDialog { background: #FFF7FA; }
    QToolBar { background: #FBE7F1; spacing: 8px; padding: 4px; border-bottom: 1px solid #E9C7D5; }
    """

# ================ S/O フォーム（2行入力） ================
def _new_2line_editor(placeholder: str, parent_font: QFont) -> QPlainTextEdit:
    te = QPlainTextEdit(); te.setFont(parent_font); te.setPlaceholderText(placeholder)
    te.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    fm = te.fontMetrics(); h = int(fm.lineSpacing() * 2 + 16)  # 2行 + 余白
    te.setFixedHeight(h)
    te.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded); te.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    return te

class SFormWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.setFont(APP_FONT)
        box = QGroupBox("S（主観）フォーム"); grid = QGridLayout(box)
        def add_row(r, label, placeholder):
            grid.addWidget(QLabel(label), r, 0); te = _new_2line_editor(placeholder, APP_FONT); grid.addWidget(te, r, 1); return te
        self.e_shuso     = add_row(0, "主訴",        "例）息苦しくて眠れない")
        self.e_keika     = add_row(1, "発症/経過",   "例）昨日から、階段昇降で増悪 など")
        self.e_bui       = add_row(2, "部位",        "例）胸部 / 右下腹部 など")
        self.e_seishitsu = add_row(3, "性質/程度",   "例）刺す痛み、NRS6/10")
        self.e_inyo      = add_row(4, "誘因/緩和",   "例）動作で増悪・座位で軽減")
        self.e_zuikan    = add_row(5, "随伴症状",    "例）発熱・咳・痰・悪心・便秘 など")
        self.e_life      = add_row(6, "生活/社会",   "例）独居・介護力・服薬状況 など")
        self.e_back      = add_row(7, "背景",        "例）過去の疾患、現在抱える問題")
        self.e_think     = add_row(8, "思考",        "例）患者の考えていること、宗教的思考など")
        self.e_etc       = add_row(9, "その他",      "自由記載（必要に応じて）")
        lay = QVBoxLayout(self); lay.setSpacing(6); lay.addWidget(box)
    def _val(self, te: QPlainTextEdit) -> str: return te.toPlainText().strip()
    def compose_text(self) -> str:
        xs = ["S: 主観"]
        def add(tag, te):
            v = self._val(te)
            if v: xs.append(f"{tag} {v}")
        add("主訴:", self.e_shuso); add("発症/経過:", self.e_keika); add("部位:", self.e_bui)
        add("性質/程度:", self.e_seishitsu); add("誘因/緩和:", self.e_inyo); add("随伴症状:", self.e_zuikan)
        add("生活/社会:", self.e_life); add("背景:", self.e_back); add("思考:", self.e_think); add("その他:", self.e_etc)
        return "\n".join(xs).strip()

class OFormWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.setFont(APP_FONT)
        box = QGroupBox("O（客観）フォーム"); grid = QGridLayout(box)
        def add_row(r, label, placeholder):
            grid.addWidget(QLabel(label), r, 0); te = _new_2line_editor(placeholder, APP_FONT); grid.addWidget(te, r, 1); return te
        self.e_name     = add_row(0, "名前・性別",        "例）山田太郎、男")
        self.e_T        = add_row(1, "体温(℃)",          "例）36.8")
        self.e_HR       = add_row(2, "脈拍/HR(/分)",      "例）80")
        self.e_RR       = add_row(3, "呼吸数(/分)",       "例）16")
        self.e_SpO2     = add_row(4, "SpO₂(%)",          "例）96")
        self.e_SBP      = add_row(5, "収縮期SBP",        "例）120")
        self.e_DBP      = add_row(6, "拡張期DBP",        "例）80")
        self.e_NRS      = add_row(7, "疼痛NRS/10",       "例）3")
        self.e_awareness= add_row(8, "意識",              "例）JCS 0 / GCS 15")
        self.e_resp     = add_row(9, "呼吸所見",          "例）呼吸音やや粗、咳嗽あり")
        self.e_circ     = add_row(10,"循環/皮膚",         "例）末梢冷感なし、浮腫なし")
        self.e_excrete  = add_row(11,"排泄/水分",         "例）尿量 0.8 mL/kg/h、便秘傾向")
        self.e_lab      = add_row(12,"検査",              "例）WBC / CRP / Na / K / Cr / Alb…")
        self.e_risk     = add_row(13,"リスク指標",        "例）転倒 / 誤嚥 / VTE / 褥瘡")
        self.e_active   = add_row(14,"活動",              "例）行った活動内容")
        self.e_high     = add_row(15,"身長",              "例）170cm")
        self.e_weight   = add_row(16,"体重",              "例）60kg")
        self.e_etc      = add_row(17,"その他",            "自由記載（デバイス、創部など）")
        lay = QVBoxLayout(self); lay.setSpacing(6); lay.addWidget(box)
    def _val(self, te: QPlainTextEdit) -> str: return te.toPlainText().strip()
    def compose_text(self) -> str:
        xs = ["O: 客観"]; vit = []
        if self._val(self.e_T):    vit.append(f"T{self._val(self.e_T)}")
        if self._val(self.e_HR):   vit.append(f"HR{self._val(self.e_HR)}")
        if self._val(self.e_RR):   vit.append(f"RR{self._val(self.e_RR)}")
        if self._val(self.e_SpO2): vit.append(f"SpO2 {self._val(self.e_SpO2)}%")
        sbp = self._val(self.e_SBP); dbp = self._val(self.e_DBP)
        if sbp and dbp: vit.append(f"BP {sbp}/{dbp}")
        elif sbp:       vit.append(f"SBP {sbp}")
        elif dbp:       vit.append(f"DBP {dbp}")
        if self._val(self.e_NRS): vit.append(f"NRS {self._val(self.e_NRS)}")
        if vit: xs.append("バイタル: " + ", ".join(vit))
        def add(tag, te):
            v = self._val(te)
            if v: xs.append(f"{tag} {v}")
        add("名前・性別:", self.e_name);add("意識:", self.e_awareness); add("呼吸所見:", self.e_resp); add("循環/皮膚:", self.e_circ)
        add("排泄/水分:", self.e_excrete); add("検査:", self.e_lab); add("リスク指標:", self.e_risk); add("活動:", self.e_active);add("身長:", self.e_high); add("体重:", self.e_weight);add("その他:", self.e_etc)
        return "\n".join(xs).strip()

# ================ NurseApp ================
class NurseApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("看護アシスタント（段階レビュー）")
        self.resize(1360, 920); self.setFont(APP_FONT)
        self.setStatusBar(QStatusBar()); self.setStyleSheet(base_stylesheet())
        self.busy: bool = False

        # 設定読込
        self.app_settings = load_app_settings()
        self.use_so_template: bool = bool(self.app_settings.get("use_so_template", True))

        # ツールバー
        self.toolbar = QToolBar("メインツール"); self.toolbar.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, self.toolbar)
        btn_excel = QAction("NANDA Excel（閲覧）", self); btn_excel.triggered.connect(self.open_excel_viewer)
        self.toolbar.addAction(btn_excel)

        # メニュー（OpenAI連携は撤去）
        menubar = self.menuBar(); menu_settings = menubar.addMenu("設定")
        self.act_toggle_so = QAction("S/Oテンプレを使う", self, checkable=True)
        self.act_toggle_so.setChecked(self.use_so_template); self.act_toggle_so.toggled.connect(self._on_toggle_so_template)
        menu_settings.addAction(self.act_toggle_so)

        tabs = QTabWidget()
        tabs.addTab(self._tab_assessment(), "1) アセスメント")
        tabs.addTab(self._tab_diagnosis(),  "2) 診断")
        tabs.addTab(self._tab_record(),     "3) 記録")
        tabs.addTab(self._tab_careplan(),   "4) 計画")
        self.setCentralWidget(tabs)

        self.th_assess = self.th_diag = self.th_record = self.th_plan = None
        self.th_ollama_pull = self.th_ollama_install = None

        for fn in [ASSESS_RESULT_TXT, ASSESS_FINAL_TXT,
                   DIAG_RESULT_TXT,  DIAG_FINAL_TXT,
                   RECORD_RESULT_TXT, RECORD_FINAL_TXT,
                   PLAN_RESULT_TXT,   PLAN_FINAL_TXT]:
            ensure_file(fn)

        # 案内 & ローカルAI準備
        self.first_time_ai_banner()
        self.prepare_free_ai()

        try:
            self.user_excel_path = self.ensure_user_excel_copy()
        except Exception as e:
            self.user_excel_path = None; self.statusBar().showMessage(f"Excelコピーの準備に失敗: {e}", 5000)

    # ---------- 設定の保存 ----------
    def _on_toggle_so_template(self, checked: bool):
        self.use_so_template = bool(checked); self._refresh_so_stack()
        self.app_settings["use_so_template"] = self.use_so_template; save_app_settings(self.app_settings)
        self.statusBar().showMessage(f"S/Oテンプレは {'ON' if checked else 'OFF'} です。", 3000)

    # ---------- Excel コピー&同期 ----------
    def ensure_user_excel_copy(self) -> Optional[Path]:
        src = Path(NANDA_XLSX)
        if not src.exists():
            self.statusBar().showMessage("nanda_db.xlsx が見つかりません。Excel閲覧はスキップされます。", 8000); return None
        user = getpass.getuser() or "user"
        dst = USER_XLSX_ROOT / user / "nanda_db.xlsx"; dst.parent.mkdir(parents=True, exist_ok=True)
        need_copy = (not dst.exists()) or (src.stat().st_mtime > dst.stat().st_mtime)
        if need_copy: shutil.copy2(src, dst)
        return dst

    def open_excel_viewer(self):
        try:
            p = self.ensure_user_excel_copy()
            if not p: alert(self, "ファイルなし", "nanda_db.xlsx が見つかりません。"); return
            dlg = ExcelViewerDialog(self, p); dlg.exec_()
        except Exception as e:
            alert(self, "Excel 閲覧エラー", str(e))

    # ---------- 無料AI（Ollama） ----------
    def prepare_free_ai(self):
        model = os.environ.get("AI_MODEL", "qwen2.5:7b-instruct")
        if shutil.which("ollama") is None:
            dlg = OllamaConsentDialog(self, model_name=model)
            if dlg.exec_() != QDialog.Accepted:
                info(self, "無料AIの設定", "Ollama の準備はスキップされました。後から自動案内します。"); return
            self._install_ollama_with_progress(); return
        try:
            cp = subprocess.run(["ollama","list"], capture_output=True, text=True, encoding="utf-8", errors="ignore")
            out = (cp.stdout or "") + "\n" + (cp.stderr or ""); has_model = any(model in ln for ln in out.splitlines())
        except Exception: has_model = False
        if not has_model: self._pull_model_with_progress(model)
        else: self.statusBar().showMessage(f"無料AIモデル（{model}）は準備済みです。", 3000)

    def _install_ollama_with_progress(self):
        sysname = platform.system().lower()
        self.statusBar().showMessage("無料AI（Ollama）をインストールしています…")
        dlg = QProgressDialog("Ollama をインストール中…", None, 0, 0, self)
        dlg.setWindowModality(Qt.ApplicationModal); dlg.setCancelButton(None); dlg.setAutoClose(True)
        dlg.setMinimumDuration(0); dlg.setLabelText("Ollama をインストール中…"); dlg.setStyleSheet(base_stylesheet()); dlg.show()
        if "windows" in sysname:
            if shutil.which("winget"):
                cmd = ["powershell","-NoProfile","-ExecutionPolicy","Bypass","winget install --id Ollama.Ollama -e --accept-package-agreements --accept-source-agreements"]
            elif shutil.which("choco"):
                cmd = ["powershell","-NoProfile","-ExecutionPolicy","Bypass","choco install ollama -y"]
            else:
                dlg.close(); info(self,"手動インストールのお願い","winget / choco が見つかりませんでした。\nhttps://ollama.com/download から Windows 用インストーラを実行してください。"); return
        elif "darwin" in sysname:
            if shutil.which("brew"): cmd = ["brew","install","--cask","ollama"]
            else:
                dlg.close(); info(self,"手動インストールのお願い","Homebrew が見つかりませんでした。\nhttps://ollama.com/download から .dmg を入手してください。"); return
        else:
            cmd = ["bash","-lc","curl -fsSL https://ollama.com/install.sh | sh"]
        self.th_ollama_install = ProcRunner(cmd)
        self.th_ollama_install.finished_ok.connect(lambda out: self._ollama_install_ok(dlg, out))
        self.th_ollama_install.finished_err.connect(lambda err: self._ollama_install_err(dlg, err))
        self.th_ollama_install.start()

    def _ollama_install_ok(self, dlg: QProgressDialog, out: str):
        dlg.close(); self.statusBar().showMessage("Ollama のインストールが完了しました。", 5000); time.sleep(1.0)
        if shutil.which("ollama") is None:
            alert(self,"Ollama 未検出","直後のため認識されていない可能性。アプリを再起動してください。"); return
        self._pull_model_with_progress(os.environ.get("AI_MODEL","qwen2.5:7b-instruct"))

    def _ollama_install_err(self, dlg: QProgressDialog, err: str):
        dlg.close(); alert(self,"Ollama インストール失敗",f"エラー:\n{err}\n\n手動でも入れられます: https://ollama.com/download")

    def _pull_model_with_progress(self, model: str):
        self.statusBar().showMessage(f"無料AIモデル（{model}）を取得中…")
        dlg = QProgressDialog("無料AIモデルを取得中です。しばらくお待ちください…", None, 0, 0, self)
        dlg.setWindowModality(Qt.ApplicationModal); dlg.setCancelButton(None); dlg.setAutoClose(True)
        dlg.setMinimumDuration(0); dlg.setLabelText(f"{model} をダウンロード中…"); dlg.setStyleSheet(base_stylesheet()); dlg.show()
        self.th_ollama_pull = ProcRunner(["ollama","pull",model])
        self.th_ollama_pull.finished_ok.connect(lambda out: self._ollama_pull_ok(dlg, out))
        self.th_ollama_pull.finished_err.connect(lambda err: self._ollama_pull_err(dlg, err))
        self.th_ollama_pull.start()

    def _ollama_pull_ok(self, dlg: QProgressDialog, out: str):
        dlg.close(); self.statusBar().showMessage("無料AIモデルの取得が完了しました。", 5000)
        info(self,"無料AIの準備が完了","Ollama モデルの準備が完了しました。診断/記録が利用できます。")

    def _ollama_pull_err(self, dlg: QProgressDialog, err: str):
        dlg.close(); alert(self,"無料AIモデルの取得に失敗", f"エラー:\n{err}\n\n手動実行例:  ollama pull {os.environ.get('AI_MODEL','qwen2.5:7b-instruct')}")

    # ---------- 初回案内 ----------
    def first_time_ai_banner(self):
        msg = ("本アプリはプライバシー徹底モードで動作します：\n"
               "・ローカルの無料AI（Ollama）だけで推論します\n"
               "・AI処理ログは残しません")
        info(self, "プライバシー徹底モード", msg)

    # ---------- Tab: アセスメント ----------
    def _tab_assessment(self) -> QWidget:
        w = QWidget(); root = QVBoxLayout(w); root.setSpacing(6)
        vsplit = QSplitter(Qt.Vertical)

        # 上側（ガイド + S/Oフォーム + ボタン）
        top = QWidget(); lay = QVBoxLayout(top); lay.setSpacing(6)
        guide = QLabel("操作の流れ： ① 下の S / O を入力（設定で『S/Oテンプレ』ONのときは表形式） ②「アセスメント作成」 ③ 結果を編集 →「確定（保存）」")
        guide.setWordWrap(True); guide.setStyleSheet("font-weight:600;"); guide.setMaximumHeight(48)
        lay.addWidget(guide)

        # S/O をスタックで切替
        self.s_stack = QStackedWidget()
        s_plain = QWidget(); s_v = QVBoxLayout(s_plain)
        self.s_edit = QTextEdit(); self.s_edit.setFont(APP_FONT); self.s_edit.setPlaceholderText("（自由記載）")
        s_v.addWidget(self.s_edit)
        self.s_form = SFormWidget()
        self.s_stack.addWidget(s_plain); self.s_stack.addWidget(self.s_form)

        self.o_stack = QStackedWidget()
        o_plain = QWidget(); o_v = QVBoxLayout(o_plain)
        self.o_edit = QTextEdit(); self.o_edit.setFont(APP_FONT)
        self.o_edit.setPlaceholderText("（自由記載。例：T38.2, HR102, RR24, SpO2 95%, BP 138/85 など）")
        o_v.addWidget(self.o_edit)
        self.o_form = OFormWidget()
        self.o_stack.addWidget(o_plain); self.o_stack.addWidget(self.o_form)

        forms_container = QWidget()
        forms_row = QHBoxLayout(forms_container); forms_row.setSpacing(8)
        colS = QVBoxLayout(); colS.setSpacing(4); colS.addWidget(QLabel("S（主観的情報）")); colS.addWidget(self.s_stack)
        colO = QVBoxLayout(); colO.setSpacing(4); colO.addWidget(QLabel("O（客観的情報）")); colO.addWidget(self.o_stack)
        forms_row.addLayout(colS); forms_row.addLayout(colO)

        scroll = QScrollArea()
        scroll.setWidget(forms_container)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(SCROLL_HEIGHT)
        lay.addWidget(scroll)

        btns = QHBoxLayout()
        self.btn_assess_run  = QPushButton("アセスメント作成")
        self.btn_assess_show = QPushButton("最新結果を表示")
        self.btn_assess_save = QPushButton("確定（保存）")
        for b in (self.btn_assess_run, self.btn_assess_show, self.btn_assess_save): btns.addWidget(b)
        lay.addLayout(btns)

        # 下側（結果ビュー）
        bottom = QWidget(); blay = QVBoxLayout(bottom); blay.setSpacing(6)
        self.assess_view = QTextEdit(); self.assess_view.setFont(MONO_FONT)
        blay.addWidget(self.assess_view)
        self.assess_hint = QLabel("（アセスメント結果がここに表示されます）"); blay.addWidget(self.assess_hint)

        vsplit.addWidget(top); vsplit.addWidget(bottom)
        vsplit.setStretchFactor(0, 1); vsplit.setStretchFactor(1, 4)
        vsplit.setSizes([SCROLL_HEIGHT, 1040])
        root.addWidget(vsplit)

        self.btn_assess_run.clicked.connect(self.run_assessment)
        self.btn_assess_show.clicked.connect(self.show_assessment_result)
        self.btn_assess_save.clicked.connect(self.save_assessment_final)

        self._refresh_so_stack()
        return w

    def _refresh_so_stack(self):
        self.s_stack.setCurrentIndex(1 if self.use_so_template else 0)
        self.o_stack.setCurrentIndex(1 if self.use_so_template else 0)

    # ---------- 診断候補 表示 ----------
    def _build_diag_block(self, c: Dict[str,Any], idx: int) -> str:
        lines = [f"{idx}. [{c.get('code','')}] {c.get('label','')}"]
        if c.get("definition"): lines.append(f"    定義: {c['definition']}")
        try:
            lines.append(f"    AI順位: {int(c.get('ai_rank',0))} / AI類似度: {float(c.get('ai_sim',0.0)):.3f} / スコア: {float(c.get('score',0.0)):.1f}")
        except Exception: pass
        if c.get("loose"):
            L = c["loose"]; buf=[]
            if L.get("診断指標"): buf.append("診断指標: " + "・".join(L["診断指標"]))
            if L.get("関連因子"): buf.append("関連因子: " + "・".join(L["関連因子"]))
            if L.get("危険因子"): buf.append("危険因子: " + "・".join(L["危険因子"]))
            if L.get("定義語"):   buf.append("定義語:   " + "・".join(L["定義語"]))
            if buf: lines.append("    曖昧一致:"); [lines.append("      - " + b) for b in buf]
        if c.get("reasons"):
            lines.append("    スコア根拠:"); [lines.append("      - " + str(r)) for r in c["reasons"][:10]]
        if c.get("ai_ev"):
            lines.append("    AI根拠:"); [lines.append("      " + ln.strip()) for ln in str(c["ai_ev"]).splitlines() if ln.strip()]
        meta_parts=[]
        def addm(j,k):
            v=c.get(k,""); 
            if v: meta_parts.append(f"{j}:{v}")
        addm("一次焦点","primary_focus"); addm("二次焦点","secondary_focus"); addm("ケア対象","care_target")
        addm("解剖学的部位","anatomical_site"); addm("年齢下限","age_min"); addm("年齢上限","age_max")
        addm("臨床経過","clinical_course"); addm("診断の状態","diagnosis_state"); addm("状況的制約","situational_constraints")
        addm("領域","domain"); addm("分類","class"); addm("判断","judge")
        if meta_parts: lines.append("    メタ情報: " + " / ".join(meta_parts))
        return "\n".join(lines)

    def _update_diag_view_from_checks(self):
        selected_items = [it for it in self._iter_items() if it.checkState(0) == Qt.Checked]
        if not selected_items:
            self.diag_view.setPlainText("（候補のチェックを入れると、ここに選択内容のみが表示されます）"); return
        selected_items.sort(key=lambda it: int(it.text(1) or "999999"))
        blocks = []
        for i, it in enumerate(selected_items, start=1):
            c = it.data(0, Qt.UserRole) or {}; blocks.append(self._build_diag_block(c, i))
        self.diag_view.setPlainText("\n\n".join(blocks))

    # ---------- Tab: 診断 ----------
    def _tab_diagnosis(self) -> QWidget:
        w = QWidget(); outer = QVBoxLayout(w)
        head = QLabel("①「診断を作成」 → ② 候補のチェックボックスをクリックで選択 → ③ 「確定（保存）」\n下の欄には“選択中のみ”が表示され、内容がそのまま保存されます。")
        head.setWordWrap(True); head.setStyleSheet("font-weight:600;"); outer.addWidget(head)

        btns = QHBoxLayout()
        self.btn_diag_run  = QPushButton("診断を作成")
        self.btn_diag_show = QPushButton("最新結果（テキスト）を表示")
        self.btn_diag_save = QPushButton("選択を確定（保存）")
        self.btn_select_top = QPushButton("AI上位をまとめて選択…")
        for b in (self.btn_diag_run, self.btn_diag_show, self.btn_select_top, self.btn_diag_save): btns.addWidget(b)
        outer.addLayout(btns)

        split = QSplitter(Qt.Horizontal)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["選択","AI順位","スコア","Code","診断名"])
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        self.tree.header().resizeSection(0, 80); self.tree.header().resizeSection(1, 80)
        self.tree.header().resizeSection(2, 90); self.tree.header().resizeSection(3, 100)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemSelectionChanged.connect(self._on_item_selected)
        self.tree.itemChanged.connect(self._on_item_changed)
        split.addWidget(self.tree)

        right = QWidget(); rlay = QVBoxLayout(right)
        self.detail = QTextEdit(); self.detail.setFont(MONO_FONT); self.detail.setReadOnly(True)
        rlay.addWidget(QLabel("候補の詳細（選択中の1件）")); rlay.addWidget(self.detail)
        split.addWidget(right)

        split.setStretchFactor(0,3); split.setStretchFactor(1,2)
        outer.addWidget(split)

        self.diag_view = QTextEdit(); self.diag_view.setFont(MONO_FONT)
        self.diag_view.setPlaceholderText("（候補のチェックを入れると、ここに“選択中のみ”が表示されます）")
        outer.addWidget(self.diag_view)

        self.btn_diag_run.clicked.connect(self.run_diagnosis)
        self.btn_diag_show.clicked.connect(self.show_diagnosis_result_text)
        self.btn_diag_save.clicked.connect(self.save_diagnosis_final_from_checks)
        self.btn_select_top.clicked.connect(self.select_top_n)
        return w

    def load_candidates_into_table(self):
        self.tree.clear(); p = Path(DIAG_JSON)
        if not p.exists():
            self.detail.setPlainText("（候補JSONがありません。診断を作成してください）"); return
        try: data = json.loads(read_text_safe(p))
        except Exception as e:
            self.detail.setPlainText(f"候補JSONの読込に失敗: {e}"); return
        cands: List[Dict[str, Any]] = data.get("candidates", [])
        cands.sort(key=lambda x: (int(x.get("ai_rank", 999999)), -float(x.get("ai_sim", 0))), reverse=False)
        for _, c in enumerate(cands, start=1):
            item = QTreeWidgetItem(self.tree)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            item.setCheckState(0, Qt.Unchecked)
            item.setText(1, str(c.get("ai_rank",""))); item.setText(2, f"{float(c.get('score',0)): .1f}".strip())
            item.setText(3, c.get("code","")); item.setText(4, c.get("label",""))
            item.setData(0, Qt.UserRole, c)

    def _on_item_selected(self):
        items = self.tree.selectedItems()
        if not items: self.detail.clear(); return
        c = items[0].data(0, Qt.UserRole) or {}; self.detail.setPlainText(self._build_diag_block(c, 1))

    def _on_item_changed(self, item: QTreeWidgetItem, col: int):
        checked = sum(1 for i in self._iter_items() if i.checkState(0) == Qt.Checked)
        self.statusBar().showMessage(f"選択中: {checked} 件", 2000); self._update_diag_view_from_checks()

    def _iter_items(self):
        for i in range(self.tree.topLevelItemCount()): yield self.tree.topLevelItem(i)

    def select_top_n(self):
        n, ok = QInputDialog.getInt(self, "AI上位の一括選択", "いくつ選びますか？", 3, 1, 50, 1)
        if not ok: return
        items = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        items.sort(key=lambda it: int(it.text(1) or "9999"))
        for i, it in enumerate(items): it.setCheckState(0, Qt.Checked if i < n else it.checkState(0))
        self.statusBar().showMessage(f"AI上位 {n} 件を選択しました。", 3000); self._update_diag_view_from_checks()

    def save_diagnosis_final_from_checks(self):
        t = self.diag_view.toPlainText().strip()
        if not t or "候補のチェック" in t:
            alert(self, "保存できません", "保存対象の本文が空です。候補にチェックを入れてください。"); return
        write_text_safe(Path(DIAG_FINAL_TXT), t + "\n"); info(self, "保存しました", f"{DIAG_FINAL_TXT} に保存しました。")

    # ================= アセスメント処理 =================
    def run_assessment(self):
        if self.busy: alert(self,"実行中","他の処理が実行中です。終了をお待ちください。"); return
        if self.use_so_template:
            s = self.s_form.compose_text().strip(); o = self.o_form.compose_text().strip()
        else:
            s = self.s_edit.toPlainText().strip(); o = self.o_edit.toPlainText().strip()
        if not s and not o:
            alert(self, "入力不足", "S または O を入力してください。"); return
        self.busy = True; self.statusBar().showMessage("アセスメントを作成中…")
        payload = s + "\n<<<SEP>>>\n" + o
        cmd = _cmd_for("assessment.py", "assessment.exe")
        # 子プロセスにも OpenAI を無効化した環境を**強制**注入
        env_over = {
            "AI_PROVIDER": "ollama",
            "OLLAMA_HOST": "http://127.0.0.1:11434",
            "OPENAI_API_KEY": "",
            "AI_LOG_DISABLE": "1",
        }
        self.th_assess = ProcRunner(cmd, stdin_text=payload, env_overrides=env_over)
        self.th_assess.finished_ok.connect(self._assess_ok); self.th_assess.finished_err.connect(self._assess_err)
        self.th_assess.start()

    def _assess_ok(self, out: str):
        self.busy = False; self.statusBar().clearMessage()
        p = Path(ASSESS_RESULT_TXT)
        text = read_text_safe(p) or out or f"（{ASSESS_RESULT_TXT} は未作成/空です）"
        self.assess_view.setPlainText(dedupe(text))
        self.assess_hint.setText(f"（編集して「確定（保存）」を押すと {ASSESS_FINAL_TXT} に保存されます）")

    def _assess_err(self, err: str):
        self.busy = False; self.statusBar().clearMessage()
        p = Path(ASSESS_RESULT_TXT); fallback = read_text_safe(p)
        alert(self, "アセスメント作成に失敗", f"{err}\n（それでも {ASSESS_RESULT_TXT} があれば表示します）")
        if fallback: self.assess_view.setPlainText(dedupe(fallback))
        else:        self.assess_view.setPlainText(f"（{ASSESS_RESULT_TXT} は未作成/空です）")

    def show_assessment_result(self):
        p = Path(ASSESS_RESULT_TXT)
        t = read_text_safe(p) or f"（{ASSESS_RESULT_TXT} は未作成/空です）"
        self.assess_view.setPlainText(dedupe(t))

    def save_assessment_final(self):
        t = self.assess_view.toPlainText().strip()
        if not t: alert(self,"保存できません","内容が空です。"); return
        write_text_safe(Path(ASSESS_FINAL_TXT), t); info(self,"保存しました", f"{ASSESS_FINAL_TXT} に保存しました。")

    # ================= 診断処理 =================
    def run_diagnosis(self):
        if self.busy: alert(self,"実行中","他の処理が実行中です。"); return
        self.busy = True; self.statusBar().showMessage("診断を作成中…")
        cmd = _cmd_for("diagnosis.py", "diagnosis.exe")
        env_over = {
            "AI_PROVIDER": "ollama",
            "OLLAMA_HOST": "http://127.0.0.1:11434",
            "OPENAI_API_KEY": "",
            "AI_LOG_DISABLE": "1",
        }
        self.th_diag = ProcRunner(cmd, env_overrides=env_over)
        self.th_diag.finished_ok.connect(self._diag_ok); self.th_diag.finished_err.connect(self._diag_err); self.th_diag.start()

    def _diag_ok(self, out: str):
        self.busy = False; self.statusBar().clearMessage()
        ptxt = Path(DIAG_RESULT_TXT)
        text = read_text_safe(ptxt) or out or f"（{DIAG_RESULT_TXT} は未作成/空です）"
        self.diag_view.setPlainText(dedupe(text))
        self.load_candidates_into_table(); self._update_diag_view_from_checks()

    def _diag_err(self, err: str):
        self.busy = False; self.statusBar().clearMessage()
        ptxt = Path(DIAG_RESULT_TXT); fallback = read_text_safe(ptxt)
        alert(self, "診断作成に失敗", f"{err}\n（それでも {DIAG_RESULT_TXT} があれば表示します）")
        if fallback: self.diag_view.setPlainText(dedupe(fallback))
        else:        self.diag_view.setPlainText(f"（{DIAG_RESULT_TXT} は未作成/空です）")

    def show_diagnosis_result_text(self):
        p = Path(DIAG_RESULT_TXT)
        t = read_text_safe(p) or f"（{DIAG_RESULT_TXT} は未作成/空です）"
        self.diag_view.setPlainText(dedupe(t))

    # ================= 記録処理 =================
    def run_record(self):
        if self.busy: alert(self,"実行中","他の処理が実行中です。"); return
        self.busy = True; self.statusBar().showMessage("記録を作成中…")
        cmd = _cmd_for("record.py", "record.exe")
        env_over = {
            "AI_PROVIDER": "ollama",
            "OLLAMA_HOST": "http://127.0.0.1:11434",
            "OPENAI_API_KEY": "",
            "AI_LOG_DISABLE": "1",
        }
        self.th_record = ProcRunner(cmd, env_overrides=env_over)
        self.th_record.finished_ok.connect(self._record_ok); self.th_record.finished_err.connect(self._record_err); self.th_record.start()

    def _record_ok(self, out: str):
        self.busy = False; self.statusBar().clearMessage()
        p = Path(RECORD_RESULT_TXT); text = read_text_safe(p) or out or f"（{RECORD_RESULT_TXT} は未作成/空です）"
        self.record_view.setPlainText(dedupe(text))
        # ヒント更新のみ（新しい QLabel を作らない）
        self.record_hint.setText(f"（編集して「確定（保存）」を押すと {RECORD_FINAL_TXT} に保存されます）")

    def _record_err(self, err: str):
        self.busy = False; self.statusBar().clearMessage()
        p = Path(RECORD_RESULT_TXT); fallback = read_text_safe(p)
        alert(self, "記録の作成に失敗", f"{err}\n（それでも {RECORD_RESULT_TXT} があれば表示します）")
        if fallback: self.record_view.setPlainText(dedupe(fallback))
        else:        self.record_view.setPlainText(f"（{RECORD_RESULT_TXT} は未作成/空です）")

    def show_record_result(self):
        p = Path(RECORD_RESULT_TXT)
        t = read_text_safe(p) or f"（{RECORD_RESULT_TXT} は未作成/空です）"
        self.record_view.setPlainText(dedupe(t))

    def save_record_final(self):
        t = self.record_view.toPlainText().strip()
        if not t: alert(self,"保存できません","内容が空です。"); return
        write_text_safe(Path(RECORD_RESULT_TXT), t)
        self.statusBar().showMessage("記録を確定（review）しています…")
        cmd = _cmd_for("record_review.py", "record_review.exe")
        env_over = {
            "AI_PROVIDER": "ollama",
            "OLLAMA_HOST": "http://127.0.0.1:11434",
            "OPENAI_API_KEY": "",
            "AI_LOG_DISABLE": "1",
        }
        th = ProcRunner(cmd, stdin_text=t, env_overrides=env_over)
        th.finished_ok.connect(lambda out: (self.statusBar().clearMessage(), info(self,"保存しました", f"{RECORD_FINAL_TXT} に保存しました。")))
        th.finished_err.connect(lambda err: (self.statusBar().clearMessage(), alert(self,"確定に失敗", f"{err}\n（編集内容は {RECORD_RESULT_TXT} に保存済みです）")))
        th.start()

    # ================= 計画処理 =================
    def _tab_record(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        head = QLabel("①「記録を作成」 → ② 結果（record_result.txt）を編集 → ③ 「確定（保存）」")
        head.setWordWrap(True); head.setStyleSheet("font-weight:600;"); lay.addWidget(head)
        btns = QHBoxLayout()
        self.btn_record_run  = QPushButton("記録を作成")
        self.btn_record_show = QPushButton("最新結果を表示")
        self.btn_record_save = QPushButton("確定（保存）")
        for b in (self.btn_record_run, self.btn_record_show, self.btn_record_save): btns.addWidget(b)
        lay.addLayout(btns)
        self.record_view = QTextEdit(); self.record_view.setFont(MONO_FONT); lay.addWidget(self.record_view)
        self.record_hint = QLabel("（record.py の出力がここに表示されます。編集後に「確定（保存）」）"); lay.addWidget(self.record_hint)
        self.btn_record_run.clicked.connect(self.run_record)
        self.btn_record_show.clicked.connect(self.show_record_result)
        self.btn_record_save.clicked.connect(self.save_record_final)
        return w

    def _tab_careplan(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        head = QLabel("①「計画を作成」 → ② 編集 → ③ 「確定（保存）」"); head.setWordWrap(True); head.setStyleSheet("font-weight:600;"); lay.addWidget(head)
        btns = QHBoxLayout()
        self.btn_plan_run  = QPushButton("計画を作成")
        self.btn_plan_show = QPushButton("最新結果を表示")
        self.btn_plan_save = QPushButton("確定（保存）")
        for b in (self.btn_plan_run, self.btn_plan_show, self.btn_plan_save): btns.addWidget(b)
        lay.addLayout(btns)
        self.plan_view = QTextEdit(); self.plan_view.setFont(MONO_FONT); lay.addWidget(self.plan_view)
        self.plan_hint = QLabel("（看護計画がここに表示されます）"); lay.addWidget(self.plan_hint)
        self.btn_plan_run.clicked.connect(self.run_careplan)
        self.btn_plan_show.clicked.connect(self.show_careplan_result)
        self.btn_plan_save.clicked.connect(self.save_careplan_final)
        return w

    def run_careplan(self):
        if self.busy: alert(self,"実行中","他の処理が実行中です。"); return
        self.busy = True; self.statusBar().showMessage("看護計画を作成中…")
        cmd = _cmd_for("careplan.py", "careplan.exe")
        env_over = {
            "AI_PROVIDER": "ollama",
            "OLLAMA_HOST": "http://127.0.0.1:11434",
            "OPENAI_API_KEY": "",
            "AI_LOG_DISABLE": "1",
        }
        self.th_plan = ProcRunner(cmd, env_overrides=env_over)
        self.th_plan.finished_ok.connect(self._plan_ok); self.th_plan.finished_err.connect(self._plan_err); self.th_plan.start()

    def _plan_ok(self, out: str):
        self.busy = False; self.statusBar().clearMessage()
        p = Path(PLAN_RESULT_TXT); text = read_text_safe(p) or out or f"（{PLAN_RESULT_TXT} は未作成/空です）"
        self.plan_view.setPlainText(dedupe(text)); self.plan_hint.setText(f"（編集して「確定（保存）」を押すと {PLAN_FINAL_TXT} に保存されます）")

    def _plan_err(self, err: str):
        self.busy = False; self.statusBar().clearMessage()
        p = Path(PLAN_RESULT_TXT); fallback = read_text_safe(p)
        alert(self, "計画作成に失敗", f"{err}\n（それでも {PLAN_RESULT_TXT} があれば表示します）")
        if fallback: self.plan_view.setPlainText(dedupe(fallback))
        else:        self.plan_view.setPlainText(f"（{PLAN_RESULT_TXT} は未作成/空です）")

    def show_careplan_result(self):
        p = Path(PLAN_RESULT_TXT); t = read_text_safe(p) or f"（{PLAN_RESULT_TXT} は未作成/空です）"
        self.plan_view.setPlainText(dedupe(t))

    def save_careplan_final(self):
        t = self.plan_view.toPlainText().strip()
        if not t: alert(self,"保存できません","内容が空です。"); return
        write_text_safe(Path(PLAN_FINAL_TXT), t); info(self,"保存しました", f"{PLAN_FINAL_TXT} に保存しました。")

# ================ エントリ ================
def main():
    os.environ["PYTHONIOENCODING"] = "utf-8"; os.environ["PYTHONUTF8"] = "1"
    app = QApplication(sys.argv); app.setFont(APP_FONT)
    win = NurseApp(); win.show(); sys.exit(app.exec_())

# 便利エイリアス（エディタから呼べるように）
def run_app():
    return main()

if __name__ == "__main__":
    main()
