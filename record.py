# -*- coding: utf-8 -*-
"""
record.py — 高速・内容保持版（並列 + キャッシュ + 重要行トリム）
診断ごとに ①テンプレ穴埋め版 と ②AI肉付け版 を並記して record_result.txt を出力。
※ 青四角（要約）生成は完全に削除のまま。

入力:
  - diagnosis_final.txt（GUI「選択を確定（保存）」）
  - assessment_final.txt（無ければ assessment_result.txt）
出力:
  - record_result.txt

環境変数（必要なら設定。未設定は安全なデフォルト）:
  OLLAMA_BASE=http://127.0.0.1:11434
  OLLAMA_MODEL=qwen2.5:7b-instruct

  # すぐ出したい時（AI完全オフ）:
  RECORD_DISABLE_AI=1

  # 速くAIを使いたい時（軽量モード）:
  RECORD_FAST=1               -> 便利な既定をまとめて有効化
  RECORD_AI_TOPK=1            -> AIは上位N件だけ（FAST既定=1 / 通常=99）
  RECORD_AI_BUDGET_SEC=25     -> AI合計時間の上限秒（0で無制限）
  RECORD_PER_CALL_TIMEOUT=20  -> 1回のAI問い合わせの最大秒（動的短縮あり）
  OLLAMA_NUM_PREDICT=700      -> 生成トークン上限（小さいほど速い）

  # 追加（本版の高速オプション）:
  RECORD_AI_WORKERS=4         -> AI並列呼び出し数（ローカルGPU/CPUに合わせて）
  RECORD_TRIM_CHARS=1400      -> AIに渡す本文の最大文字数（重要行を優先採用）
  RECORD_AI_CACHE=record_ai_cache.json  -> 応答キャッシュの保存先
"""

from __future__ import annotations
import os, re, time, json, unicodedata
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib

# ====== 入出力 ======
ASSESS1 = "assessment_final.txt"
ASSESS2 = "assessment_result.txt"
DIAG_FN = "diagnosis_final.txt"
OUT_FN  = "record_result.txt"

# ====== 環境フラグ／パラメータ ======
DISABLE_AI = os.getenv("RECORD_DISABLE_AI", "0") == "1"
FAST_MODE  = os.getenv("RECORD_FAST", "0") == "1"

def _env_int(name: str, default_if_fast: int, default_normal: int) -> int:
    v = os.getenv(name, "")
    if v.strip():
        try: return int(v)
        except: pass
    return default_if_fast if FAST_MODE else default_normal

def _env_float(name: str, default_if_fast: float, default_normal: float) -> float:
    v = os.getenv(name, "")
    if v.strip():
        try: return float(v)
        except: pass
    return default_if_fast if FAST_MODE else default_normal

def _env_str(name: str, default_if_fast: str, default_normal: str) -> str:
    v = os.getenv(name, "").strip()
    if v: return v
    return default_if_fast if FAST_MODE else default_normal

# 既存のパラメータ
AI_TOPK         = _env_int("RECORD_AI_TOPK",            1,    99)
AI_BUDGET_SEC   = _env_float("RECORD_AI_BUDGET_SEC",   25.0,   0.0)  # 0で無制限
PER_CALL_TO_DEF = _env_float("RECORD_PER_CALL_TIMEOUT",20.0,  45.0)
NUM_PREDICT     = _env_int("OLLAMA_NUM_PREDICT",       700,  1200)

# 新規の高速系
AI_WORKERS      = _env_int("RECORD_AI_WORKERS",          4,     3)   # 並列数
TRIM_CHARS      = _env_int("RECORD_TRIM_CHARS",       1400,  1600)   # トリム上限
AI_CACHE_PATH   = _env_str("RECORD_AI_CACHE", "record_ai_cache.json", "record_ai_cache.json")

# ====== Ollama ======
OLLAMA_BASE   = os.getenv("OLLAMA_BASE",  "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
CONNECT_TO    = 5.0  # 接続開始のタイムアウト（秒）

def _ollama_ok() -> bool:
    if DISABLE_AI:
        return False
    try:
        import requests
        r = requests.get(OLLAMA_BASE + "/api/tags", timeout=CONNECT_TO)
        return r.status_code == 200
    except Exception:
        return False

def _ollama_chat(system: str, user: str, timeout: float) -> str:
    """
    まず /api/chat、失敗時は /api/generate にフォールバック。
    """
    import requests
    # /api/chat
    try:
        r = requests.post(
            OLLAMA_BASE + "/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": NUM_PREDICT},
                "messages": [{"role":"system","content":system},{"role":"user","content":user}]
            },
            timeout=timeout
        )
        if r.status_code == 404:
            raise FileNotFoundError("apichat404")
        r.raise_for_status()
        return ((r.json().get("message") or {}).get("content") or "").strip()
    except Exception:
        # /api/generate
        prompt = f"### System\n{system}\n\n### User\n{user}\n"
        r = requests.post(
            OLLAMA_BASE + "/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": NUM_PREDICT},
            },
            timeout=timeout
        )
        r.raise_for_status()
        return (r.json().get("response") or "").strip()

# ====== ユーティリティ ======
def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")

def read_text(p: str) -> str:
    path = Path(p)
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""

def save_text(p: str, s: str):
    Path(p).write_text((s or "").rstrip() + "\n", encoding="utf-8")

def clean(s: str) -> str:
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def uniq_keep(xs: List[str]) -> List[str]:
    seen=set(); out=[]
    for x in xs:
        t = nfkc(x).strip()
        if t and t not in seen:
            seen.add(t); out.append(t)
    return out

# ====== アセスメント解析 ======
_NUM = r"(\d+(?:\.\d+)?)"
def fnum(pat: str, text: str) -> Optional[float]:
    m = re.search(pat, text, flags=re.IGNORECASE)
    return float(m.group(1)) if m else None

def parse_vitals(text: str) -> Dict[str, Optional[float]]:
    T    = fnum(r"(?:体温|t)\s*[:=]?\s*"+_NUM, text)
    HR   = fnum(r"(?:hr|心拍|脈拍)\s*[:=]?\s*"+_NUM, text)
    RR   = fnum(r"(?:rr|呼吸数)\s*[:=]?\s*"+_NUM, text)
    SpO2 = fnum(r"(?:spo2|ｓｐｏ２|サチュ)\s*[:=]?\s*"+_NUM, text)
    bp   = re.search(r"\b(\d{2,3})\s*/\s*(\d{2,3})\b", text, re.IGNORECASE)
    SBP  = float(bp.group(1)) if bp else fnum(r"(?:sbp|収縮期|上の血圧)\s*[:=]?\s*"+_NUM, text)
    DBP  = float(bp.group(2)) if bp else fnum(r"(?:dbp|拡張期|下の血圧)\s*[:=]?\s*"+_NUM, text)
    MAP  = (SBP + 2*DBP)/3 if (SBP is not None and DBP is not None) else None
    NRS  = fnum(r"(?:nrs|疼痛(?:スケール)?)\D{0,6}"+_NUM, text)
    return {"T":T,"HR":HR,"RR":RR,"SpO2":SpO2,"SBP":SBP,"DBP":DBP,"MAP":MAP,"NRS":NRS}

SYM = {
    "疼痛":["痛み","圧痛","腹痛","胸痛","頭痛","創部痛","疼痛"],
    "呼吸困難":["息苦しさ","呼吸苦","息切れ","起坐呼吸","SpO2","チアノーゼ"],
    "不安":["不安","緊張","ソワソワ","心配"],
    "倦怠感":["だるさ","倦怠感","疲労","易疲労"],
    "嘔気嘔吐":["吐き気","嘔気","嘔吐","むかつき"],
    "排便":["便秘","下痢","軟便","水様便"],
}
def symptom_hits(text: str) -> List[str]:
    t = nfkc(text)
    hits=[]
    for k,vs in SYM.items():
        for w in [k]+vs:
            if w in t:
                hits.append(w); break
    return uniq_keep(hits)

def abnormal_vitals(v: Dict[str, Optional[float]]) -> List[str]:
    o=[]
    if v.get("T") is not None and (v["T"]>=38.0 or v["T"]<=35.0): o.append(f"T{v['T']:.1f}")
    if v.get("HR") is not None and (v["HR"]>=100 or v["HR"]<=50): o.append(f"HR{int(v['HR'])}")
    if v.get("RR") is not None and (v["RR"]>=22 or v["RR"]<=10): o.append(f"RR{int(v['RR'])}")
    if v.get("SpO2") is not None and (v["SpO2"]<94): o.append(f"SpO2{int(v['SpO2'])}%")
    if v.get("SBP") is not None and (v["SBP"]<=100): o.append(f"SBP{int(v['SBP'])}")
    if v.get("MAP") is not None and (v["MAP"]<65): o.append(f"MAP{int(v['MAP'])}")
    if v.get("NRS") is not None and (v["NRS"]>=4): o.append(f"NRS{int(v['NRS'])}")
    return o

def _score_line_importance(ln: str) -> int:
    """重要行スコアリング：数字/バイタル/症状/ゴードン/ヘンダーソンを優先"""
    s = nfkc(ln)
    score = 0
    if re.search(r"\d", s): score += 2
    if re.search(r"(SpO2|RR|HR|NRS|BP|SBP|DBP|MAP|体温|呼吸数|脈拍)", s, re.I): score += 3
    if any(w in s for w in ("息苦","呼吸","痛","嘔","下痢","便秘","不安","ふらつ","転倒")): score += 2
    if "ゴードン" in s or "Gordon" in s: score += 2
    if "ヘンダーソン" in s or "Henderson" in s: score += 2
    if "背景" in s: score += 1
    return score

def extract_blocks_from_assess(text: str) -> Dict[str,str]:
    """S/O を分けず、両方を結合して『SO』として返す。無ければ全文の要所を SO に入れる。"""
    t = clean(text)
    ms = re.search(r"(^|\n)\s*S[:：]\s*(.+?)(?=\n[OＳＯoｏ][:：]|\Z)", t, flags=re.S|re.I)
    mo = re.search(r"(^|\n)\s*O[:：]\s*(.+?)(?=\n[AＳＳsｓ][:：]|$)", t, flags=re.S|re.I)
    s_txt = ms and ms.group(2).strip() or ""
    o_txt = mo and mo.group(2).strip() or ""
    so_raw = "\n".join([x for x in [s_txt, o_txt] if x]) or t

    # 重要行優先トリム（TRIM_CHARS 以内に収める）
    if len(so_raw) > TRIM_CHARS:
        lines = [ln for ln in so_raw.splitlines() if ln.strip()]
        lines_sorted = sorted(lines, key=_score_line_importance, reverse=True)
        buf=[]; total=0
        for ln in lines_sorted:
            ln2 = ln.strip()
            if not ln2: continue
            if total + len(ln2) + 1 > TRIM_CHARS: continue
            buf.append(ln2); total += len(ln2) + 1
        if buf:
            so_use = "\n".join(buf)
        else:
            so_use = so_raw[:TRIM_CHARS]
    else:
        so_use = so_raw

    # 背景・枠組み（必要最低限の抽出のみ）
    bg = []
    for ln in t.splitlines():
        if any(key in ln for key in ("背景","既往","家族","生活","仕事","社会","経済","環境","支援","独居","同居","教育","嗜好")):
            bg.append(ln.strip())
    gordon = "\n".join([ln for ln in t.splitlines() if "ゴードン" in ln or "Gordon" in ln])
    hend   = "\n".join([ln for ln in t.splitlines() if "ヘンダーソン" in ln or "Henderson" in ln])
    return {"SO": so_use, "背景": "\n".join(bg[:6]), "ゴードン": gordon, "ヘンダーソン": hend, "全文": t}

# ====== diagnosis_final.txt のパース ======
def split_terms(s: str) -> List[str]:
    if not s: return []
    parts=[]
    for chunk in re.split(r"[|｜]", s):
        for sub in re.split(r"[、,;／/・\s]+", chunk):
            sub=sub.strip("・-・:：;、, ")
            if sub: parts.append(nfkc(sub))
    return uniq_keep(parts)

def parse_diagnosis_final(txt: str) -> List[Dict[str,any]]:
    items=[]
    cur=None
    def flush():
        nonlocal cur
        if cur:
            cur["診断指標"] = uniq_keep(cur.get("診断指標",[]))
            cur["関連因子"] = uniq_keep(cur.get("関連因子",[]))
            cur["危険因子"] = uniq_keep(cur.get("危険因子",[]))
            items.append(cur); cur=None

    for ln in txt.splitlines():
        m1 = re.match(r"^\s*-\s*\[\s*[xX]?\s*\]\s*([0-9A-Za-z\-]+)\s*\t?\s*(.+?)\s*$", ln)
        m2 = re.match(r"^\s*\d+\.\s*\[([0-9A-Za-z\-]+)\]\s*(.+?)\s*$", ln)
        if m1 or m2:
            flush()
            code = (m1.group(1) if m1 else m2.group(1))
            label= (m1.group(2) if m1 else m2.group(2))
            cur = {"code": code.strip(), "label": nfkc(label), "definition":"", "診断指標":[], "関連因子":[], "危険因子":[], "diagnosis_state":""}
            continue
        if cur is None:
            continue
        if "定義" in ln:
            m = re.search(r"定義[:：]\s*(.+)", ln)
            if m: cur["definition"] = nfkc(m.group(1)).strip()
        if "診断指標" in ln:
            m = re.search(r"診断指標[:：]\s*(.+)", ln)
            if m: cur["診断指標"] += split_terms(m.group(1))
        if "関連因子" in ln:
            m = re.search(r"関連因子[:：]\s*(.+)", ln)
            if m: cur["関連因子"] += split_terms(m.group(1))
        if "危険因子" in ln:
            m = re.search(r"危険因子[:：]\s*(.+)", ln)
            if m: cur["危険因子"] += split_terms(m.group(1))
        mstate = re.search(r"診断の状態[:：]\s*(問題焦点型|リスク型|ヘルスプロモーション)", ln)
        if mstate:
            cur["diagnosis_state"] = mstate.group(1)
    flush()
    # 推定（無ければ）
    for it in items:
        if not it["diagnosis_state"]:
            lab = it["label"]
            if any(k in lab for k in ("リスク","危険")):
                it["diagnosis_state"]="リスク型"
            elif any(k in lab for k in ("促進","準備")):
                it["diagnosis_state"]="ヘルスプロモーション"
            else:
                it["diagnosis_state"]="問題焦点型"
    return items

# ====== テンプレ生成（穴埋め） ======
def fmt_join(xs: List[str]) -> str:
    return "、".join(xs[:8]) if xs else "未評価"

def template_plain(di: Dict[str,any], assess: Dict[str,str]) -> str:
    tp = di.get("diagnosis_state","問題焦点型")
    if tp == "リスク型":
        return (
            f"リスク状態［{di['label']}］\n"
            f"根拠としては 〔危険因子〕: {fmt_join(di.get('危険因子',[]))}"
        )
    elif tp == "ヘルスプロモーション":
        base = di.get("診断指標",[]) or symptom_hits(assess.get("全文",""))
        return (
            f"促進準備状態［{di['label']}］\n"
            f"以下で明らか 〔意欲・診断指標〕: {fmt_join(base)}"
        )
    else:
        rf = di.get("関連因子",[])
        dc = di.get("診断指標",[]) or symptom_hits(assess.get("全文",""))
        return (
            f"［看護診断名］ {di['label']}\n"
            f"関連するのは 〔原因/関連因子〕: {fmt_join(rf)}\n"
            f"以下で明らか 〔症状・徴候/診断指標〕: {fmt_join(dc)}"
        )

# ====== AI肉付け版（キャッシュ・並列・時間ガード） ======
AI_REC_SYS = (
    "あなたは看護記録者。指定の診断タイプと材料を用い、日本語で段落文を作成する。"
    "前半: 背景/誘因→S/O本文の症状や数値→診断との結び付け。"
    "後半: 根拠（関連因子 or 危険因子 or 意欲）→介入の方向。"
    "箇条書き・記号は使わない。文字数の制約は一切設けない。"
)

def _ai_cache_load(path:str) -> dict:
    if Path(path).exists():
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _ai_cache_save(path:str, cache:dict):
    try:
        Path(path).write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _narrative_key(di: Dict[str,any], assess: Dict[str,str], vnote: str) -> str:
    # モデル＋診断＋（短縮済）SO/背景/G/H/バイタル からキーを作成
    src = json.dumps({
        "model": OLLAMA_MODEL,
        "code": di.get("code",""),
        "label": di.get("label",""),
        "def": di.get("definition",""),
        "rf": di.get("関連因子",[])[:12],
        "rk": di.get("危険因子",[])[:12],
        "dc": di.get("診断指標",[])[:12],
        "tp": di.get("diagnosis_state","問題焦点型"),
        "SO": assess.get("SO",""),
        "BG": assess.get("背景",""),
        "G": assess.get("ゴードン",""),
        "H": assess.get("ヘンダーソン",""),
        "V": vnote,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(src.encode("utf-8","ignore")).hexdigest()

def _build_user_prompt(di: Dict[str,any], assess: Dict[str,str], vnote: str) -> str:
    tp = di.get("diagnosis_state","問題焦点型")
    return (
        f"【診断タイプ】{tp}\n"
        f"【看護診断名】{di['label']} [{di['code']}]\n"
        f"【定義】{di.get('definition','')}\n"
        f"【関連因子】{', '.join(di.get('関連因子',[])[:12])}\n"
        f"【危険因子】{', '.join(di.get('危険因子',[])[:12])}\n"
        f"【診断指標】{', '.join(di.get('診断指標',[])[:12])}\n\n"
        f"【S/O本文（重要行優先・短縮）】\n{assess.get('SO','')}\n\n"
        f"【背景】\n{assess.get('背景','')}\n"
        f"【ゴードン】\n{assess.get('ゴードン','')}\n"
        f"【ヘンダーソン】\n{assess.get('ヘンダーソン','')}\n"
        f"【バイタル所見】{vnote}\n"
    )

def ai_narrative_once(di: Dict[str,any], assess: Dict[str,str], vnote: str,
                      allow_ai: bool, per_call_timeout: float,
                      cache: dict) -> str:
    key = _narrative_key(di, assess, vnote)
    if key in cache:
        return cache[key]
    if allow_ai:
        try:
            text = clean(_ollama_chat(AI_REC_SYS, _build_user_prompt(di, assess, vnote), timeout=per_call_timeout))
            if text:
                cache[key] = text
                return text
        except Exception:
            pass
    # フォールバック
    tp = di.get("diagnosis_state","問題焦点型")
    if tp=="リスク型":
        text = clean(f"{di['label']}に関し、背景やS/Oから {fmt_join(di.get('危険因子',[]))} が重なり、"
                     f"{fmt_join(di.get('診断指標',[]) or symptom_hits(assess.get('SO','')))} が示唆される。{vnote} を踏まえ、"
                     f"危険因子の低減と早期兆候の観察、教育と環境調整を進める。")
    elif tp=="ヘルスプロモーション":
        text = clean(f"{di['label']}について、S/Oと背景から意欲や理解が確認でき、"
                     f"{fmt_join(di.get('診断指標',[]) or symptom_hits(assess.get('SO','')))} が根拠となる。{vnote} を確認しつつ、"
                     f"自己管理手順の共有と達成指標を伴う指導を行う。")
    else:
        text = clean(f"{di['label']}では、背景やS/Oから {fmt_join(di.get('関連因子',[]))} が関与し、"
                     f"{fmt_join(di.get('診断指標',[]) or symptom_hits(assess.get('SO','')))} がみられる。{vnote} を考慮し、"
                     f"疼痛・呼吸・循環・安全の観察と生活調整、教育を中心に介入する。")
    cache[key] = text
    return text

# ====== メイン ======
def main():
    assess_text = read_text(ASSESS1) or read_text(ASSESS2)
    if not assess_text:
        raise SystemExit("assessment_final.txt / assessment_result.txt が見つかりません。")
    diag_text   = read_text(DIAG_FN)
    if not diag_text:
        raise SystemExit("diagnosis_final.txt が見つかりません。（診断タブで保存してください）")

    assess = extract_blocks_from_assess(assess_text)
    v      = parse_vitals(assess_text)
    vnote  = " ".join(abnormal_vitals(v)) or "特記すべき急性異常はない"
    diags  = parse_diagnosis_final(diag_text)

    allow_ai_global = (not DISABLE_AI) and _ollama_ok()
    start_time = time.time()

    # ===== 先にテンプレを全部作る =====
    templated = []
    for di in diags:
        templated.append(template_plain(di, assess))

    # ===== AI肉付け（上位 AI_TOPK 件・並列・時間配分） =====
    ai_count = min(AI_TOPK, len(diags))
    cache = _ai_cache_load(AI_CACHE_PATH)

    # 残り時間に応じて 1呼び出しtimeoutを動的短縮
    def dyn_timeout(pending: int) -> float:
        if AI_BUDGET_SEC <= 0:  # 予算無制限
            return PER_CALL_TO_DEF
        elapsed = time.time() - start_time
        remain  = max(0.0, AI_BUDGET_SEC - elapsed)
        # 作業残が0は呼ばないので安全
        # 余裕を持って9割を割り振り（失敗リカバリ余地）
        if pending <= 0 or remain <= 0:
            return 0.5
        return max(3.0, min(PER_CALL_TO_DEF, 0.9 * remain / pending))

    ai_texts = [""] * len(diags)

    if ai_count > 0 and allow_ai_global:
        # 並列実行（最大 AI_WORKERS スレッド）
        # 実行順は先頭 ai_count 件のみ
        idxs = list(range(ai_count))
        def task(idx: int):
            # ここで都度 timeout を見直す（残タスク基準）
            to = dyn_timeout(pending = max(1, len(pending_set())))
            return idx, ai_narrative_once(diags[idx], assess, vnote, allow_ai=True, per_call_timeout=to, cache=cache)

        # スレッド間で残数を見るためのクロージャ
        running = set()
        def pending_set():  # 残り（発行していない or 実行中）
            return set(idxs) - set(done_order)

        done_order = []
        with ThreadPoolExecutor(max_workers=max(1, AI_WORKERS)) as ex:
            futs = []
            for i in idxs:
                f = ex.submit(task, i)
                running.add(i); futs.append(f)
            for f in as_completed(futs):
                try:
                    i, txt = f.result()
                except Exception:
                    # フォールバック
                    i = idxs[len(done_order)]
                    txt = ai_narrative_once(diags[i], assess, vnote, allow_ai=False, per_call_timeout=0.0, cache=cache)
                ai_texts[i] = txt
                done_order.append(i)
                # 予算超過なら以降はフォールバックに切替
                if AI_BUDGET_SEC > 0 and (time.time() - start_time) >= AI_BUDGET_SEC:
                    break

        # 予算切れで未生成分はフォールバック
        for i in range(ai_count):
            if not ai_texts[i]:
                ai_texts[i] = ai_narrative_once(diags[i], assess, vnote, allow_ai=False, per_call_timeout=0.0, cache=cache)
    else:
        # 全てフォールバック
        for i in range(ai_count):
            ai_texts[i] = ai_narrative_once(diags[i], assess, vnote, allow_ai=False, per_call_timeout=0.0, cache=cache)

    # 残り（AI_TOPK超過分）はフォールバック
    for i in range(ai_count, len(diags)):
        ai_texts[i] = ai_narrative_once(diags[i], assess, vnote, allow_ai=False, per_call_timeout=0.0, cache=cache)

    # キャッシュ保存
    _ai_cache_save(AI_CACHE_PATH, cache)

    # ===== 出力組み立て（青四角は無しのまま） =====
    lines=[]
    lines.append("="*100)
    lines.append(f"記録（診断ごとにテンプレ/AI肉付け［S/O結合・重要行トリム・並列・時間ガード付］) {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("="*100)
    lines.append(f"[入力] {('assessment_final.txt' if Path(ASSESS1).exists() else 'assessment_result.txt')} / diagnosis_final.txt")
    lines.append(f"[設定] FAST={int(FAST_MODE)} TOPK={AI_TOPK} WORKERS={AI_WORKERS} BUDGET={AI_BUDGET_SEC or '∞'}s PER_CALL≤{PER_CALL_TO_DEF}s TRIM≤{TRIM_CHARS}chars")
    lines.append("")

    if not diags:
        lines.append("（診断が見つかりません。diagnosis_final.txt を確認してください）")
    else:
        for idx, di in enumerate(diags, start=1):
            lines.append("—"*100)
            lines.append(f"◆ 看護診断 {idx}: {di['label']} [{di['code']}]　（診断の状態: {di.get('diagnosis_state','問題焦点型')}）")
            lines.append("")
            # ①テンプレ穴埋め版
            lines.append("【テンプレ穴埋め版】")
            lines.append(templated[idx-1])
            lines.append("")
            # ②AI肉付け版
            allow_ai_this = (idx <= AI_TOPK) and allow_ai_global
            lines.append("【AI 肉付け版】" + ("" if allow_ai_this else "（フォールバック/キャッシュ）"))
            lines.append(ai_texts[idx-1])
            lines.append("")

    out = "\n".join(lines).rstrip() + "\n"
    print(out)
    save_text(OUT_FN, out)
    print(f"[SAVE] {OUT_FN}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = f"[FATAL] {e}"
        print(msg)
        try: save_text(OUT_FN, msg)
        except: pass
        raise
