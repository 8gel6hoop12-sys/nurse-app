# -*- coding: utf-8 -*-
"""
assessment_fast.py  (UI refresh + better screening narrative)

S/O自由記述 → 自動で S/O を分配 → 抽出/解析 → 2種類の本文を連結出力。
- 出力1: 従来フォーマットの包括アセスメント（S/O原文は非表示）
- 出力2: 工程版（スクリーン → 詳細 → 分析 → クラスタ → 候補 → 優先 → ワイズマン4段テンプレ段落）
- 追加: ゴードン11 & ヘンダーソン14 を“具体語句ベース”で自動生成（AI分類＋ルール補完）
- 追加: 身長/体重から BMI を自動算出＆区分付与
- 追加: AI（Ollama）で 原因/誘因/強み/将来像 を推測（使えない時はローカル補完）
- 強化: 全角→半角正規化、バイタル抽出の網羅性、Wiseman段落IndexError対策
- 強化(UI): 参考評価を ↑/↓/↔ の簡潔表記に変更（凡例1行のみ）
- 強化(UI): スクリーニングアセスメントをサンプル本風のまとまり文＋要点箇条書きで出力

保存:
  assessment_result.txt …… 本文2本（包括→工程）
  final.txt（= assessment_final.txt）…… レビュー用チェック
  assessment_review.txt …… クイックサマリ

CLI例:
  python assessment_fast.py --so "S: 息苦しい… O: SpO2 92% HR 108 T 38.1 BP 98/56 NRS 7 尿量 0.3 mL/kg/h"
  python assessment_fast.py --s "S記述..." --o "O記述..."
環境例:
  pip install requests
  set OLLAMA_MODEL=llama3:latest
  set FAST_MODE=1        # AIを使わない高速モード
"""
from __future__ import annotations

import re, os, json, argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import unicodedata

# ========== Ollama ラッパ ==========
import requests

def ollama_base() -> str:
    return os.getenv("OLLAMA_BASE", "http://127.0.0.1:11434").rstrip("/")

def ollama_model() -> str:
    return os.getenv("OLLAMA_MODEL", "llama3:latest")

def ollama_available(timeout: float = 1.2) -> bool:
    try:
        r = requests.get(f"{ollama_base()}/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False

def ollama_chat(system: str, user: str, num_predict: int = 512, temp: float = 0.2, timeout: int = 40) -> str:
    """/api/chat へ stream=False で投げる（短めの応答に制限）"""
    data = {
        "model": ollama_model(),
        "messages": [{"role": "system", "content": system},
                     {"role": "user",   "content": user}],
        "options": {"temperature": temp, "num_predict": num_predict},
        "stream": False,
    }
    r = requests.post(f"{ollama_base()}/api/chat", json=data, timeout=timeout)
    r.raise_for_status()
    js = r.json()
    return js.get("message", {}).get("content", "") or js.get("response", "")

# ========== 全体テキスト・抽出用 ==========
S = ""; O = ""; ALL = ""

meta: Dict[str, Any] = {}
behav: Dict[str, bool] = {}
facts: Dict[str, Optional[float]] = {}
sites: List[str] = []
quals: List[str] = []
assoc: List[str] = []
PRIO = "低"; NEWS2 = 0
SPO2_RED = 90.0; SPO2_TARGET_DEFAULT = 94.0
MAP_LOW = 65.0; NRS_RED = 7
URINE_LOW_MLKG_H = 0.5
PAIN_GOAL_DEFAULT = 3

SPO2_TARGET = SPO2_TARGET_DEFAULT
PAIN_GOAL = PAIN_GOAL_DEFAULT

NUM = r"(\d+(?:\.\d+)?)"

REF_NOTE = "※ ↑/↓/↔ は参考評価（簡易目安）"

def normalize_text(t: str) -> str:
    if not t: return ""
    s = unicodedata.normalize("NFKC", t)
    s = s.replace("％", "%")
    return s

def sfloat(x) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except Exception:
        return None

def fnum(pat: str, txt: str) -> Optional[float]:
    m = re.search(pat, txt, flags=re.IGNORECASE)
    return sfloat(m.group(1)) if m else None

def fstr(pat: str, txt: str, grp: int = 1) -> Optional[str]:
    m = re.search(pat, txt, flags=re.IGNORECASE|re.S)
    if not m: return None
    try: return m.group(grp).strip()
    except IndexError: return m.group(0).strip()

def pick(words: List[str], text: str) -> List[str]:
    return [w for w in words if w and (w in text)]

# ========== S/O 自動分配 ==========
S_MARKERS = (r"^\s*(S|Ｓ|Subjective|主観|主訴|自覚症状)\s*[:：】>）]?", r"^【\s*S\s*】", r"^＜\s*S\s*＞")
O_MARKERS = (r"^\s*(O|Ｏ|Objective|客観|所見|身体所見|バイタル|観察)\s*[:：】>）]?", r"^【\s*O\s*】", r"^＜\s*O\s*＞")

def _has_marker(line: str, pats: Tuple[str, ...]) -> bool:
    return any(re.search(p, line, flags=re.IGNORECASE) for p in pats)

def _strip_quotes(s: str) -> str:
    return s.strip().strip('「」“”""').strip()

def smart_split_so(so_text: str) -> Tuple[str, str]:
    so_text = normalize_text(so_text or "")
    if not so_text.strip(): return "", ""
    lines = [_strip_quotes(ln) for ln in so_text.splitlines() if ln.strip()]
    cur = None; s_buf: List[str] = []; o_buf: List[str] = []; undecided: List[str] = []
    for ln in lines:
        if _has_marker(ln, S_MARKERS):
            cur="S"; ln = re.sub("|".join(S_MARKERS), "", ln, flags=re.IGNORECASE).strip()
            if ln: s_buf.append(ln); continue
        if _has_marker(ln, O_MARKERS):
            cur="O"; ln = re.sub("|".join(O_MARKERS), "", ln, flags=re.IGNORECASE).strip()
            if ln: o_buf.append(ln); continue
        if cur=="S": s_buf.append(ln); continue
        if cur=="O": o_buf.append(ln); continue
        undecided.append(ln)

    # RR/HR の混同を避ける正規表現
    vit_pat = r"(?:\bBT|\b(?<![A-Z])T[:=]?\s*\d|\b体温|\bHR\b|\bP(?![a-z])|\bPulse|\b脈拍\b|\bRR\b|\b呼吸数\b|\bSpO?2\b|\bSat\b|\bサチュ\b|血圧|BP|SBP|DBP|MAP|NRS|\d{2,3}\s*/\s*\d{2,3})"
    obj_kw  = r"(?:所見|検査|発赤|腫脹|圧痛|反跳痛|筋性防御|聴診|打診|触診|皮膚|チアノーゼ|胸部|腹部|X線|CT|採血|尿量)"
    subj_kw = r"(?:訴え|痛い|辛い|だるい|しびれ|吐き気|悪心|食欲|眠れ|不安|こわい|息苦しい|下痢|便秘|ふらつき|めまい|発熱感|寒気|悪寒|むかむか)"

    def classify(ln: str) -> str:
        if re.search(vit_pat,ln) or re.search(obj_kw,ln): return "O"
        if re.search(subj_kw,ln): return "S"
        if re.search(r"\d",ln) and re.search(r"(?:%|mmHg|mL|L/min|/h|/分|回)", ln): return "O"
        return "S"

    for ln in undecided: (o_buf if classify(ln)=="O" else s_buf).append(ln)

    def recheck(buf: List[str]) -> Tuple[List[str], List[str]]:
        s2: List[str]=[]; o2: List[str]=[]
        for ln in buf: (o2 if classify(ln)=="O" else s2).append(ln)
        return s2, o2
    s_fix, o_from_s = recheck(s_buf); o_fix, s_from_o = recheck(o_buf)
    s_buf = s_fix + s_from_o; o_buf = o_fix + o_from_s
    return ("\n".join(s_buf).strip(), "\n".join(o_buf).strip())

# ========== 解析 ==========
def parse_all():
    global meta, behav, facts, sites, quals, assoc, PRIO, NEWS2, SPO2_TARGET, PAIN_GOAL

    ALL_norm = normalize_text(ALL)

    meta = {
        "background": fstr(r"(?:^|[\n\r])\s*背景\s*[:：]\s*(.+)", ALL_norm),
        "age"  : fnum(r"(?:年齢|Age)\s*[:：]?\s*" + NUM, ALL_norm),
        "sex"  : fstr(r"(?:性別|Sex)\s*[:：]?\s*(男性|女性|男|女)", ALL_norm),
        "living": fstr(r"(独居|同居|一人暮らし|在宅|施設入所)", ALL_norm),
        "job"  : fstr(r"(?:職業|仕事)\s*[:：]?\s*(.+)", ALL_norm),
        "dx"   : fstr(r"(?:既往|診断|主病名)\s*[:：]?\s*(.+)", ALL_norm),
        "meds" : fstr(r"(?:服薬|内服|投薬)\s*[:：]?\s*(.+)", ALL_norm),
        "allergy": fstr(r"(?:ｱﾚﾙｷﾞｰ|アレルギー)\s*[:：]?\s*(.+)", ALL_norm),
        "diet" : fstr(r"(外食|飲酒|高脂肪|不規則|過食|少食)", ALL_norm, grp=1),
        "lang" : fstr(r"(?:言語|文化)\s*[:：]?\s*(.+)", ALL_norm),
        "goal_spo2": fnum(r"(?:SpO2(?:目標)?|SpO2\s*target)\s*[:：]?\s*"+NUM, ALL_norm),
        "goal_pain": fnum(r"(?:疼痛目標|NRS目標)\s*[:：]?\s*"+NUM, ALL_norm),
        "height_cm": fnum(r"(?:身長|Ht|height)\s*[:：]?\s*"+NUM+r"\s*cm", ALL_norm) or fnum(r"(?:身長|Ht|height)\s*[:：]?\s*"+NUM, ALL_norm),
        "weight_kg": fnum(r"(?:体重|Wt|weight)\s*[:：]?\s*"+NUM+r"\s*kg", ALL_norm) or fnum(r"(?:体重|Wt|weight)\s*[:：]?\s*"+NUM, ALL_norm),
    }
    SPO2_TARGET = meta["goal_spo2"] if meta.get("goal_spo2") else SPO2_TARGET_DEFAULT
    PAIN_GOAL   = int(meta["goal_pain"]) if meta.get("goal_pain") is not None else PAIN_GOAL_DEFAULT

    meta["BMI"] = None; meta["BMI_class"] = "未評価"
    if meta.get("height_cm") and meta.get("weight_kg"):
        h = float(meta["height_cm"])/100.0; w = float(meta["weight_kg"])
        if h>0:
            bmi = w/(h*h); meta["BMI"] = round(bmi,1)
            if bmi < 18.5: meta["BMI_class"] = "低体重"
            elif bmi < 25: meta["BMI_class"] = "普通体重"
            elif bmi < 30: meta["BMI_class"] = "過体重"
            else: meta["BMI_class"] = "肥満"

    try_pat   = r"(?:水分.*(?:摂|飲).*|安静にして様子見|体位(?:変換)?|温罨法|温め|市販薬)"
    avoid_pat = r"(?:(?:鎮痛|薬|受診).*(?:拒|避)|自己判断で.*中止)"
    seek_pat  = r"(?:(?:家族|友人|近所).*(?:相談|連絡)|救急|コール|看護師.*呼)"
    behav.update({
        "try"   : bool(re.search(try_pat, ALL_norm)),
        "avoid" : bool(re.search(avoid_pat, ALL_norm)),
        "seek"  : bool(re.search(seek_pat, ALL_norm)),
    })

    facts["T"]    = fnum(r"(?:体温|BT|(?<![A-Z])T)\s*[:=]?\s*"+NUM, ALL_norm)
    facts["HR"]   = fnum(r"(?:\bHR\b|P(?![a-z])|Pulse|心拍|脈拍)\s*[:=]?\s*"+NUM, ALL_norm)
    facts["RR"]   = fnum(r"(?:\bRR\b|呼吸数)\s*[:=]?\s*"+NUM, ALL_norm)
    facts["SpO2"] = fnum(r"(?:SpO2|SPO2|Sat|ｻﾁｭ|サチュ|酸素飽和度)\s*[:=]?\s*"+NUM, ALL_norm)

    m_bp = re.search(r"\b(\d{2,3})\s*/\s*(\d{2,3})\b", ALL_norm)
    facts["SBP"]  = fnum(r"(?:SBP|収縮期|上の血圧|BP\s*[:=]?)\s*"+NUM, ALL_norm) or (sfloat(m_bp.group(1)) if m_bp else None)
    facts["DBP"]  = fnum(r"(?:DBP|拡張期|下の血圧)\s*[:=]?\s*"+NUM, ALL_norm) or (sfloat(m_bp.group(2)) if m_bp else None)
    facts["MAP"]  = fnum(r"(?:MAP)\s*[:=]?\s*"+NUM, ALL_norm)
    if facts.get("MAP") is None and facts.get("SBP") is not None and facts.get("DBP") is not None:
        facts["MAP"] = (facts["SBP"] + 2*facts["DBP"])/3

    facts["NRS"]  = fnum(r"(?:NRS|疼痛(?:スケール)?|痛み(?:スコア)?)\D{0,6}"+NUM, ALL_norm)
    facts["Urine_mLkgph"] = fnum(r"(?:尿量|尿\s*量)[^/]{0,20}"+NUM+r"\s*mL\s*/\s*kg\s*/\s*h", ALL_norm)

    pain_sites = ["頭痛","胸痛","腹痛","背部痛","腰痛","創部痛","咽頭痛","関節痛","筋肉痛"]
    pain_quals = ["鈍痛","刺痛","しめつけ","ズキズキ","疝痛","灼熱感","放散痛","締め付け感"]
    assoc_sx   = ["嘔吐","下痢","便秘","悪心","発熱","食欲低下","呼吸困難","咳","痰","めまい","ふらつき","寒気","悪寒","発赤","腫脹","排膿","夜間頻尿","失禁"]
    sites[:] = pick(pain_sites, ALL_norm); quals[:] = pick(pain_quals, ALL_norm); assoc[:] = pick(assoc_sx, ALL_norm)

    def news_rr(v):   return 3 if v is not None and (v <= 8 or v >= 25) else (1 if v and (9 <= v <= 11 or 21 <= v <= 24) else 0)
    def news_spo2(v): return 0 if v is None or v >= 96 else (1 if v >= 94 else (2 if v >= 92 else 3))
    def news_temp(v): return 3 if v is not None and v <= 35.0 else (1 if v and (35.1 <= v <= 36.0 or 38.1 <= v <= 39.0) else (2 if v and v > 39.0 else 0))
    def news_hr(v):   return 3 if v is not None and (v <= 40 or v > 130) else (1 if v and (41 <= v <= 50 or 91 <= v <= 110) else (2 if v and (111 <= v <= 130) else 0))
    def news_sbp(v):  return 3 if v is not None and (v <= 90 or v >= 220) else (2 if v and 91 <= v <= 100 else (1 if v and 101 <= v <= 110 else 0))
    NEWS2_local = sum([news_rr(facts.get("RR")), news_spo2(facts.get("SpO2")), news_temp(facts.get("T")), news_hr(facts.get("HR")), news_sbp(facts.get("SBP"))])
    globals()["NEWS2"] = NEWS2_local

    def priority_level():
        red = []
        if facts.get("SpO2") is not None and facts["SpO2"] < SPO2_RED: red.append("SpO₂<90%")
        if facts.get("MAP")  is not None and facts["MAP"]  < MAP_LOW:  red.append("MAP<65")
        if facts.get("SBP")  is not None and facts["SBP"]  < 90:       red.append("SBP<90")
        if facts.get("NRS")  is not None and facts["NRS"]  >= NRS_RED: red.append("NRS≥7")
        if red or NEWS2_local >= 7: return "高"
        elif NEWS2_local >= 5:      return "中"
        else:                       return "低"
    globals()["PRIO"] = priority_level()

# ---- 参考評価（↑/↓/↔ の簡潔表記） ------------------------------------------
def _ref_arrow(name: str, v: Optional[float]) -> Optional[str]:
    if v is None: return None
    if name == "T":
        if v < 35.0: return "↓低体温"
        if v < 36.1: return "↓やや低"
        if v <= 37.9: return "↔"
        if v <= 39.0: return "↑発熱"
        return "↑高熱"
    if name == "HR":
        if v <= 50: return "↓徐脈傾向"
        if v <= 90: return "↔"
        if v <= 110: return "↑やや高"
        if v <= 130: return "↑高"
        return "↑↑著明"
    if name == "RR":
        if v <= 11: return "↓やや低"
        if v <= 20: return "↔"
        if v <= 24: return "↑やや多呼吸"
        return "↑多呼吸"
    if name == "SpO2":
        if v <= 89: return "↓↓著明低下"
        if v <= 93: return "↓低下"
        if v <= 95: return "△境界"
        return "↔"
    if name == "SBP":
        if v <= 100: return "↓低値"
        if v <= 130: return "↔"
        if v <= 139: return "↑やや高"
        return "↑高値"
    if name == "DBP":
        if v <= 60: return "↓低値"
        if v <= 89: return "↔"
        return "↑高値"
    if name == "MAP":
        return "↓低灌流懸念" if v < 65 else "↔"
    if name == "NRS":
        if v >= 7: return "↑強い痛み"
        if v >= 4: return "↑中等度"
        return "↔軽度"
    return None

def _annotate_term_compact(term: str) -> str:
    t = term
    lab = None
    if re.search(r"SpO?2|サチュ|酸素飽和", t):  lab = _ref_arrow("SpO2", facts.get("SpO2"))
    elif re.search(r"\bRR\b|呼吸数", t):        lab = _ref_arrow("RR", facts.get("RR"))
    elif re.search(r"\bHR\b|脈拍|Pulse", t):    lab = _ref_arrow("HR", facts.get("HR"))
    elif re.search(r"体温|BT|(?<![A-Z])\bT\b", t):  lab = _ref_arrow("T", facts.get("T"))
    elif re.search(r"\bMAP\b", t):              lab = _ref_arrow("MAP", facts.get("MAP"))
    elif re.search(r"SBP|収縮期|上の血圧", t):   lab = _ref_arrow("SBP", facts.get("SBP"))
    elif re.search(r"DBP|拡張期|下の血圧", t):   lab = _ref_arrow("DBP", facts.get("DBP"))
    elif re.search(r"NRS|疼痛", t):             lab = _ref_arrow("NRS", facts.get("NRS"))
    if lab and "[" not in t:
        t = f"{t}[{lab}]"
    return t

# ========== AI 一括（原因/誘因/強み/将来像 + ゴードン/ヘンダーソン要約） ==========
def parse_json_loose(s: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", s, flags=re.S)
    try:
        return json.loads(m.group(0) if m else s)
    except Exception:
        return {}

def ai_all_in_one() -> Dict[str, Any]:
    base = {
        "causes": [], "aggravators": [], "strengths": [],
        "trajectory": "", "gordon": {}, "henderson": {}, "paragraph": ""
    }
    if os.getenv("FAST_MODE","0")=="1" or (not ollama_available(1.2)):
        return base

    system = "あなたは日本語の臨床看護アセスメント支援AI。出力は必ずJSONのみ。簡潔・具体。"
    user = (
        "次のS/Oを読み、以下を日本語で推定しJSONで返す。過度な想像は避け、入力にない事実は書かない。\n"
        "{\n"
        "  \"causes\": [\"原因/病態の推定(最大3)\"],\n"
        "  \"aggravators\": [\"誘因/増悪因子(最大2)\"],\n"
        "  \"strengths\": [\"強み(最大3)\"],\n"
        "  \"trajectory\": \"将来像の短文\",\n"
        "  \"gordon\": {\"健康認識・健康管理\":\"…\",\"栄養・代謝\":\"…\",\"排泄\":\"…\",\"活動・運動\":\"…\",\"睡眠・休息\":\"…\",\"認知・知覚\":\"…\",\"自己知覚・自己概念\":\"…\",\"役割・関係\":\"…\",\"性・生殖\":\"…\",\"コーピング/ストレス耐性\":\"…\",\"価値・信念\":\"…\"},\n"
        "  \"henderson\": {\"1呼吸\":\"…\",\"2食事・水分\":\"…\",\"3排泄\":\"…\",\"4移動・体位\":\"…\",\"5睡眠・休息\":\"…\",\"6衣服の着脱\":\"…\",\"7体温調節\":\"…\",\"8身体清潔・整容\":\"…\",\"9危険回避\":\"…\",\"10コミュニケーション\":\"…\",\"11信仰・価値\":\"…\",\"12仕事・達成\":\"…\",\"13遊び・余暇\":\"…\",\"14学習・成長\":\"…\"}\n"
        "}\n"
        "【S】\n"+(S[:4000] or "")+"\n【O】\n"+(O[:4000] or "")+"\n"
        "短く具体に。空欄は作らない。"
    )
    try:
        raw = ollama_chat(system, user, num_predict=512, temp=0.1, timeout=40)
        js = parse_json_loose(raw)
        for k in base:
            if k in js: base[k] = js[k]
    except Exception:
        pass
    return base

# ========== 語句のAI分類 & ルール補完 ==========
def _parse_json_dict(s: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", s, flags=re.S)
    try:
        return json.loads(m.group(0) if m else "{}")
    except Exception:
        return {}

def ai_classify_terms_from_SO() -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    if not ollama_available(1.2) or os.getenv("FAST_MODE","0") == "1":
        return {}, {}
    system = (
        "あなたは日本語の看護アセスメント分類AIです。出力は必ずJSONのみ。"
        "SとOの本文に実際に登場する語句だけを短いフレーズで抜粋し、"
        "ゴードン11領域とヘンダーソン14項目に振り分けてください。"
        "新しい事実や推測語は書かないでください。最大でも各5語句。"
        "語句は入力文字列の一部をそのまま転写してください。"
    )
    keys_g = ["健康認識・健康管理","栄養・代謝","排泄","活動・運動","睡眠・休息",
              "認知・知覚","自己知覚・自己概念","役割・関係","性・生殖",
              "コーピング/ストレス耐性","価値・信念"]
    keys_h = [f"{i}{n}" for i, n in enumerate(
        ["呼吸","食事・水分","排泄","移動・体位","睡眠・休息","衣服の着脱","体温調節",
         "身体清潔・整容","危険回避","コミュニケーション","信仰・価値",
         "仕事・達成","遊び・余暇","学習・成長"], start=1)]
    schema = {"gordon": {k:[] for k in keys_g}, "henderson": {k:[] for k in keys_h}}
    user = "【S】\n"+(S[:3500] or "")+"\n【O】\n"+(O[:3500] or "")+"\n"+json.dumps(schema, ensure_ascii=False)
    try:
        js = _parse_json_dict(ollama_chat(system, user, num_predict=700, temp=0.1, timeout=50))
        return js.get("gordon", {}) or {}, js.get("henderson", {}) or {}
    except Exception:
        return {}, {}

def _phrases_from_text(txt: str) -> List[str]:
    t = normalize_text(txt)
    parts = re.split(r"[。；;、,\n\r/]|・|\s{2,}", t)
    return [p.strip() for p in parts if p.strip()]

def _add_term(mp: Dict[str, List[str]], key: str, term: str, limit: int = 6):
    if not term: return
    L = mp.setdefault(key, [])
    if term not in L and len(L) < limit:
        L.append(term)

def harvest_terms_rule_based() -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    g: Dict[str, List[str]] = {}
    h: Dict[str, List[str]] = {}
    phrases = _phrases_from_text(S + "\n" + O)
    if facts.get("SpO2") is not None:
        _add_term(g, "認知・知覚", f"SpO2 {int(facts['SpO2'])}%")
        _add_term(h, "1呼吸", f"SpO2 {int(facts['SpO2'])}%")
    if facts.get("RR") is not None:
        _add_term(h, "1呼吸", f"呼吸数 {int(facts['RR'])}/分")
    if facts.get("HR") is not None:
        _add_term(h, "9危険回避", f"脈拍 {int(facts['HR'])}/分")
    if facts.get("SBP") is not None and facts.get("DBP") is not None:
        _add_term(h, "9危険回避", f"血圧 {int(facts['SBP'])}/{int(facts['DBP'])}mmHg")
    if facts.get("T") is not None:
        _add_term(h, "7体温調節", f"体温 {facts['T']:.1f}℃")
    if facts.get("NRS") is not None:
        _add_term(g, "認知・知覚", f"NRS {int(facts['NRS'])}")

    KW_G = {
        "健康認識・健康管理": ["受診","服薬","自己管理","血圧手帳","指導","通院","既往","高血圧","糖尿","喘息"],
        "栄養・代謝": ["食欲","摂取","飲水","体重","嘔気","悪心","嘔吐","脱水"],
        "排泄": ["便秘","下痢","排尿","尿","失禁","残尿","夜間頻尿","便"],
        "活動・運動": ["歩行","ふらつき","易疲労","ADL","起立","横になる","階段","呼吸困難"],
        "睡眠・休息": ["不眠","眠れ","中途覚醒","熟睡","睡眠"],
        "認知・知覚": ["痛み","しびれ","めまい","視力","聴力","感覚","NRS"],
        "自己知覚・自己概念": ["不安","心配","抑うつ","怖い"],
        "役割・関係": ["家族","同居","独居","仕事","介護"],
        "性・生殖": ["妊娠","月経","性","更年期"],
        "コーピング/ストレス耐性": ["自己対処","様子見","相談","コール","支援要請"],
        "価値・信念": ["宗教","信仰","価値","希望"],
    }
    KW_H = {
        "1呼吸": ["呼吸","息苦","咳","痰","喘鳴","SpO2","サチュ"],
        "2食事・水分": ["食事","食欲","摂取","飲水","水分"],
        "3排泄": ["便秘","下痢","排尿","尿","失禁","便"],
        "4移動・体位": ["歩行","起立","体位","ふらつき","杖","車椅子","横にな"],
        "5睡眠・休息": ["不眠","眠れ","中途覚醒","睡眠"],
        "6衣服の着脱": ["着替え","衣服"],
        "7体温調節": ["体温","発熱","寒気","悪寒"],
        "8身体清潔・整容": ["清拭","入浴","整容","爪切り","清潔"],
        "9危険回避": ["転倒","危険","誤嚥","服薬","血圧","脈拍"],
        "10コミュニケーション": ["会話","伝達","コミュニケ","連絡"],
        "11信仰・価値": ["宗教","信仰","価値"],
        "12仕事・達成": ["仕事","職業","復職"],
        "13遊び・余暇": ["趣味","余暇","レジャー"],
        "14学習・成長": ["指導","教育","学習","セルフケア"],
    }
    for ph in phrases:
        for k, kws in KW_G.items():
            if any(w in ph for w in kws): _add_term(g, k, ph)
        for k, kws in KW_H.items():
            if any(w in ph for w in kws): _add_term(h, k, ph)

    if meta.get("background"):
        _add_term(g, "健康認識・健康管理", meta["background"])
        _add_term(g, "役割・関係", meta["background"])

    return g, h

# ========== ゴードン/ヘンダーソン 出力（UIシンプル版） ==========
def _summary_rule_based() -> Tuple[Dict[str,str], Dict[str,str]]:
    text = normalize_text(ALL)
    g: Dict[str, str] = {}; h: Dict[str, str] = {}
    def _has(*k): return any(kw in text for kw in k)

    bits=[]
    if meta.get("dx"): bits.append(f"既往:{meta['dx']}")
    if meta.get("meds"): bits.append("服薬あり")
    if behav.get("avoid"): bits.append("受診/薬回避傾向")
    if behav.get("try"): bits.append("自己対処あり")
    g["健康認識・健康管理"] = " / ".join(bits) if bits else ""

    nut=[]
    if _has("食欲低下","食欲不振"): nut.append("食欲低下")
    if meta.get("BMI") is not None: nut.append(f"BMI {meta['BMI']:.1f}（{meta['BMI_class']}）")
    if facts.get("T") and facts["T"] >= 38.0: nut.append(f"体温 {facts['T']:.1f}℃[{_ref_arrow('T',facts['T'])}]")
    g["栄養・代謝"] = "、".join(nut)

    ex=[]
    if _has("便秘"): ex.append("便秘")
    if _has("下痢"): ex.append("下痢")
    if _has("失禁"): ex.append("失禁")
    if facts.get("Urine_mLkgph") is not None: ex.append(f"尿量 {facts['Urine_mLkgph']:.2f} mL/kg/h")
    g["排泄"] = "、".join(ex)

    act=[]
    if _has("ふらつき","易疲労","呼吸困難","歩行困難","ADL低下"): act.append("活動耐容低下/歩行不安定")
    g["活動・運動"] = "、".join(act)

    g["睡眠・休息"] = "睡眠不良" if _has("眠れ","中途覚醒","不眠") else ""

    cog=[]
    if facts.get("NRS") is not None: cog.append(f"NRS {int(facts['NRS'])}[{_ref_arrow('NRS',facts['NRS'])}]")
    if _has("しびれ","感覚異常","めまい"): cog.append("感覚症状あり")
    g["認知・知覚"] = "、".join(cog)

    g["自己知覚・自己概念"] = "不安あり" if _has("不安","こわい","心配") else ""
    g["役割・関係"] = f"同居:{meta['living']}" if meta.get("living") else ""
    g["性・生殖"] = ""
    cp=[]
    if behav.get("try"): cp.append("自己対処あり")
    if behav.get("seek"): cp.append("支援要請あり")
    if behav.get("avoid"): cp.append("回避傾向")
    g["コーピング/ストレス耐性"] = "・".join(cp)
    g["価値・信念"] = ""

    h["1呼吸"] = (f"SpO2 {int(facts['SpO2'])}%[{_ref_arrow('SpO2',facts['SpO2'])}]"
                   if facts.get("SpO2") is not None else ("呼吸困難" if _has("呼吸困難","息苦しい") else ""))
    h["2食事・水分"] = ("食欲/摂取の記載あり" if _has("食欲","摂取","飲水") else "")
    h["3排泄"] = g["排泄"]
    h["4移動・体位"] = ("ふらつき/易疲労" if _has("ふらつき","易疲労","歩行困難","起立困難") else "")
    h["5睡眠・休息"] = ("睡眠不良" if _has("眠れ","中途覚醒","不眠") else "")
    h["6衣服の着脱"] = ""
    h["7体温調節"] = (f"体温 {facts['T']:.1f}℃[{_ref_arrow('T',facts['T'])}]" if facts.get("T") is not None else "")
    h["8身体清潔・整容"] = ""
    h["9危険回避"] = ("転倒/循環の注意" if _has("ふらつき","転倒","めまい") else "")
    h["10コミュニケーション"] = ("支援要請あり" if behav.get("seek") else "")
    h["11信仰・価値"] = ""
    h["12仕事・達成"] = (f"職業:{meta['job']}" if meta.get("job") else "")
    h["13遊び・余暇"] = ""
    h["14学習・成長"] = ("セルフケア学習が必要" if PRIO!="低" else "")
    return g, h

def build_gordon_concrete(ai: Dict[str,Any]) -> str:
    ai_g, _ = ai_classify_terms_from_SO()
    rule_g, _ = harvest_terms_rule_based()
    g_sum, _ = _summary_rule_based()

    keys = ["健康認識・健康管理","栄養・代謝","排泄","活動・運動","睡眠・休息",
            "認知・知覚","自己知覚・自己概念","役割・関係","性・生殖",
            "コーピング/ストレス耐性","価値・信念"]

    lines = ["【ゴードン11】 " + REF_NOTE]
    for k in keys:
        toks: List[str] = []
        for t in (ai_g.get(k) or []): toks.append(_annotate_term_compact(t))
        for t in (rule_g.get(k) or []):
            at = _annotate_term_compact(t)
            if at not in toks: toks.append(at)
        if (not toks) and g_sum.get(k): toks = [g_sum[k]]
        s = "・".join([x for x in toks if x]) if toks else "—"
        lines.append(f"- {k}: {s}")
    return "\n".join(lines)

def build_henderson_concrete(ai: Dict[str,Any]) -> str:
    _, ai_h = ai_classify_terms_from_SO()
    _, rule_h = harvest_terms_rule_based()
    _, h_sum = _summary_rule_based()

    keys = [f"{i}{n}" for i, n in enumerate(
        ["呼吸","食事・水分","排泄","移動・体位","睡眠・休息","衣服の着脱","体温調節",
         "身体清潔・整容","危険回避","コミュニケーション","信仰・価値",
         "仕事・達成","遊び・余暇","学習・成長"], start=1)]

    lines = ["【ヘンダーソン14】 " + REF_NOTE]
    for k in keys:
        toks: List[str] = []
        for t in (ai_h.get(k) or []): toks.append(_annotate_term_compact(t))
        for t in (rule_h.get(k) or []):
            at = _annotate_term_compact(t)
            if at not in toks: toks.append(at)
        if (not toks) and h_sum.get(k): toks = [h_sum[k]]
        s = "・".join([x for x in toks if x]) if toks else "—"
        lines.append(f"- {k}: {s}")
    return "\n".join(lines)

# ========== 文章部品 ==========
def fmt_vitals() -> str:
    xs=[]
    if facts.get("T") is not None: xs.append(f"T {facts['T']:.1f}℃[{_ref_arrow('T',facts['T'])}]")
    if facts.get("HR") is not None: xs.append(f"HR {int(facts['HR'])}/分[{_ref_arrow('HR',facts['HR'])}]")
    if facts.get("RR") is not None: xs.append(f"RR {int(facts['RR'])}/分[{_ref_arrow('RR',facts['RR'])}]")
    if facts.get("SBP") is not None and facts.get("DBP") is not None:
        xs.append(f"BP {int(facts['SBP'])}/{int(facts['DBP'])}mmHg[{_ref_arrow('SBP',facts['SBP'])}]")
    if facts.get("SpO2") is not None: xs.append(f"SpO2 {facts['SpO2']:.0f}%[{_ref_arrow('SpO2',facts['SpO2'])}]")
    if facts.get("MAP") is not None: xs.append(f"MAP {facts['MAP']:.1f}mmHg[{_ref_arrow('MAP',facts['MAP'])}]")
    if facts.get("NRS") is not None: xs.append(f"NRS {int(facts['NRS'])}/10[{_ref_arrow('NRS',facts['NRS'])}]")
    if facts.get("Urine_mLkgph") is not None: xs.append(f"尿量 {facts['Urine_mLkgph']:.2f}mL/kg/h")
    return "・".join(xs) if xs else "特記所見なし"

def background_sentence() -> str:
    bits=[]
    if meta.get("background"): bits.append(str(meta["background"]))
    if meta.get("living"): bits.append(str(meta["living"]))
    if meta.get("diet"): bits.append("生活習慣:"+str(meta["diet"]))
    if meta.get("dx"): bits.append("既往:"+str(meta["dx"]))
    if meta.get("meds"): bits.append("服薬:"+str(meta["meds"]))
    if meta.get("allergy"): bits.append("ｱﾚﾙｷﾞ:"+str(meta["allergy"]))
    if meta.get("job"): bits.append("職業/役割:"+str(meta["job"]))
    if meta.get("BMI") is not None: bits.append(f"BMI:{meta['BMI']:.1f}（{meta['BMI_class']}）")
    return "、".join(bits) if bits else "背景の特記は現時点で未把握"

def symptoms_sentence() -> str:
    p1 = f"主症状は{'・'.join(sites)}" if sites else "主症状はSに記載の自覚症状"
    p2 = f"（性質:{'・'.join(quals)}）" if quals else ""
    p3 = f"、随伴:{'・'.join(assoc)}" if assoc else ""
    p4 = f"、疼痛はNRS{int(facts['NRS'])}" if facts.get("NRS") is not None else ""
    return p1+p2+p3+p4+"。"

def behavior_sentence() -> str:
    s=[]
    if behav.get("try"): s.append("安静/体位/飲水等の自己対処あり")
    if behav.get("avoid"): s.append("受診/薬回避傾向あり")
    if behav.get("seek"): s.append("家族や医療者へ支援要請あり")
    return "、".join(s)

def risk_sentence() -> str:
    flags=[]
    if facts.get("SpO2") is not None and facts["SpO2"] < SPO2_RED: flags.append("SpO₂<90%")
    if facts.get("MAP")  is not None and facts["MAP"]  < MAP_LOW:  flags.append("MAP<65mmHg")
    if facts.get("SBP")  is not None and facts["SBP"]  < 90:       flags.append("SBP<90mmHg")
    if facts.get("NRS")  is not None and facts["NRS"]  >= NRS_RED: flags.append("NRS≥7")
    risk = "赤旗なし" if not flags else "赤旗:"+", ".join(flags)
    return f"NEWS2 {NEWS2}、{risk}"

def priority_sentence() -> str:
    return f"総合優先度は「{PRIO}」。"

# ========== スクリーニング（サンプル本風のまとまり文＋要点） ==========
def _join_nonempty(sep: str, *xs: str) -> str:
    return sep.join([x for x in xs if x])

def build_screening_sections() -> str:
    lines=[]
    # 健康認識・健康管理
    g_sum, h_sum = _summary_rule_based()
    mg = g_sum.get("健康認識・健康管理","")
    if any([meta.get("background"), mg, meta.get("meds"), meta.get("dx")]):
        sent = _join_nonempty("。", 
            background_sentence(),
            ("受療/服薬: "+ meta["meds"]) if meta.get("meds") else "",
            ("既往: "+ meta["dx"]) if meta.get("dx") else ""
        )
        if sent: lines += ["■ 健康認識・健康管理パターン",
                           "スクリー二ングアセスメント: " + sent + "。"]
        if mg:
            lines += ["データ分析:", "  ■ " + mg]
        lines.append("")

    # 栄養・代謝
    nut_pts=[]
    if "食欲低下" in ALL: nut_pts.append("食欲低下")
    if meta.get("BMI") is not None: nut_pts.append(f"BMI {meta['BMI']:.1f}（{meta['BMI_class']}）")
    if facts.get("T") is not None: nut_pts.append(f"体温 {facts['T']:.1f}℃[{_ref_arrow('T',facts['T'])}]")
    if any(nut_pts):
        sent = "食事/水分の状況: " + "・".join(nut_pts)
        lines += ["■ 栄養・代謝パターン",
                  "スクリー二ングアセスメント: " + sent + "。",
                  "データ分析:"]
        for p in nut_pts: lines.append("  ■ " + p)
        lines.append("")

    # 排泄
    ex=[]
    if "便秘" in ALL: ex.append("便秘")
    if "下痢" in ALL: ex.append("下痢")
    if facts.get("Urine_mLkgph") is not None: ex.append(f"尿量 {facts['Urine_mLkgph']:.2f}mL/kg/h")
    if any(ex):
        lines += ["■ 排泄パターン",
                  "スクリー二ングアセスメント: " + "・".join(ex) + "。",
                  "データ分析:"]
        for p in ex: lines.append("  ■ " + p)
        lines.append("")

    # 活動・運動
    act=[]
    if "ふらつき" in ALL or "易疲労" in ALL: act.append("ふらつき/易疲労")
    if facts.get("RR") is not None: act.append(f"RR {int(facts['RR'])}/分[{_ref_arrow('RR',facts['RR'])}]")
    if facts.get("HR") is not None: act.append(f"HR {int(facts['HR'])}/分[{_ref_arrow('HR',facts['HR'])}]")
    if any(act):
        lines += ["■ 活動・運動パターン",
                  "スクリー二ングアセスメント: " + "・".join(act) + "。",
                  "データ分析:"]
        for p in act: lines.append("  ■ " + p)
        lines.append("")

    # 睡眠・休息
    if "眠れ" in ALL or "中途覚醒" in ALL or "不眠" in ALL:
        lines += ["■ 睡眠・休息パターン",
                  "スクリー二ングアセスメント: 睡眠不良に関する記述あり。",
                  "データ分析:",
                  "  ■ 睡眠障害の記述"]
        lines.append("")

    # 共通まとめ
    vitals = fmt_vitals()
    lines += ["■ バイタル（参考目安）: " + vitals,
              "■ NEWS2/赤旗: " + risk_sentence()]
    return "\n".join(lines)

# ========== ワイズマン4段テンプレ（単一段落） ==========
def _choose_assessment_item() -> str:
    if facts.get("SpO2") and facts["SpO2"] < 94: return "呼吸・循環（酸素化）"
    if facts.get("NRS") and facts["NRS"] >= 4:   return "認知・知覚（疼痛）"
    if meta.get("BMI") and meta["BMI"] and meta["BMI"] < 18.5: return "栄養・代謝（低栄養/摂取不足）"
    if "ふらつき" in (ALL or "") or "転倒" in (ALL or ""): return "安全・危険回避（転倒リスク）"
    return "健康管理状況"

def _collect_info_points() -> List[str]:
    pts: List[str] = []
    if sites: pts.append("主症状:" + "・".join(sites))
    if assoc: pts.append("随伴:" + "・".join(assoc))
    vitals = fmt_vitals()
    if vitals and (vitals != "特記所見なし"): pts.append("数値所見:" + vitals)
    if meta.get("BMI") is not None: pts.append(f"BMI:{meta['BMI']:.1f}（{meta['BMI_class']}）")
    if behav.get("try"):  pts.append("自己対処あり")
    if behav.get("seek"): pts.append("支援要請あり")
    if len(pts) < 2:
        if not pts: pts.append("S/O所見:記載を要約")
        pts.append("数値所見:" + (vitals or "未取得"))
    return pts[:3]

def build_wiseman_paragraph(ai: Dict[str, Any]) -> str:
    item = _choose_assessment_item()
    infos = _collect_info_points() or []
    vitals = fmt_vitals()
    pad = ["S/O所見:未整理", "数値所見:" + (vitals or "未取得"), None]
    padded = (infos + pad)[:3]
    info1, info2, info3 = padded[0], padded[1], padded[2]

    inappropriate = (PRIO!="低") or ("赤旗" in risk_sentence()) or (facts.get("NRS") and facts["NRS"]>=4)
    base_interp = "適切でない" if inappropriate else "概ね適切"
    will = "向上させたいという意欲がみられる" if (behav.get("try") or behav.get("seek")) else "意欲は記載から不明"
    interp = f"{base_interp}＋{will}"

    if item.startswith("呼吸"): problem="酸素化低下"
    elif item.startswith("認知"): problem="疼痛コントロール不良"
    elif item.startswith("栄養"): problem="摂取不足/低栄養"
    elif item.startswith("安全"): problem="転倒リスク増大"
    else: problem="健康管理状況の不適切"

    causes = (ai.get("causes") or [])[:3]
    aggr   = (ai.get("aggravators") or [])[:2]
    strg   = (ai.get("strengths") or [])[:3]
    if not causes:
        if facts.get("SpO2") and facts["SpO2"]<94: causes.append("低酸素/呼吸器感染の関与")
        if meta.get("BMI") and meta["BMI"] and meta["BMI"]<18.5: causes.append("摂取低下と体重減少の持続")
        if facts.get("NRS") and facts["NRS"]>=4: causes.append("鎮痛不足/炎症の持続")
    if not strg:
        if behav.get("try"):  strg.append("自己対処の試みがある")
        if behav.get("seek"): strg.append("家族や医療者へ支援要請できている")

    cause_s = "・".join(causes) if causes else "原因は未特定"
    induce_s = "・".join(aggr) if aggr else "明確な誘因は未特定"
    strength_s = "・".join(strg) if strg else "強みは現時点で明確ではない"
    traj = ai.get("trajectory") or (f"優先度{PRIO}、NEWS2 {NEWS2}。"
                                    f"{'短周期で再評価が必要' if PRIO!='低' else '計画的観察で安定化を図る'}")

    out = (
        f"【アセスメント項目:{item}】については、情報①（{info1}）や情報②（{info2}）"
        + (f"、情報③（{info3}）" if info3 else "")
        + f"ということがあった。このことから、解釈の結果は「{interp}」と考えられる。"
          f"よって、「{problem}（実在型問題）」を問題に挙げる。この問題の原因には、{cause_s}が考えられる。"
          f"また、{induce_s}がこの問題の誘因となっていると考えられる。一方、{strength_s}が当該アセスメント項目の強みである。"
          f"以上より、解釈の結果は、今後「{traj}」という将来像になると考えられる。"
    )
    return out

# ========== 本文組み立て ==========
def build_legacy_body(ai: Dict[str,Any]) -> str:
    L=[]
    L.append("="*92)
    L.append(f"包括アセスメント（従来形式） {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    L.append("="*92)
    L.append("\n【背景】\n"+background_sentence())
    L.append("\n【症状】\n"+symptoms_sentence())
    L.append("\n【検査・所見（バイタル含む）】\n"+fmt_vitals())
    beh = behavior_sentence()
    if beh: L.append("\n【行動傾向】\n"+beh)
    L.append("\n【リスク評価】\n"+risk_sentence())
    L.append("\n【優先度】\n"+priority_sentence())
    L.append("\n"+build_gordon_concrete(ai))
    L.append("\n"+build_henderson_concrete(ai))
    return "\n".join(L)

def build_engineered_body(ai: Dict[str,Any]) -> str:
    L=[]
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    L.append(f"＝ 看護アセスメント（工程版） {now} ＝\n")

    # ← ここをサンプル本風のブロックに刷新
    L.append("◆スクリー二ングアセスメント\n" + build_screening_sections() + "\n")

    L.append("◆詳細アセスメント（示唆ある場合）")
    L.append("・脱水/低栄養/低酸素/疼痛など示唆時、摂取量・尿量・体重推移、SpO₂推移、誘因/緩和因子、腹部所見/便性状など深掘り。\n")

    L.append("◆データ分析")
    L.append(f"・赤旗/リスク: {risk_sentence()}")
    causes = ai.get("causes") or []
    aggr   = ai.get("aggravators") or []
    L.append("・AI原因推測: " + (" / ".join(causes[:3]) if causes else "推定情報なし"))
    if aggr: L.append("・AI誘因: " + " / ".join(aggr[:2]))
    L.append("")

    L.append("◆情報のクラスタリング（パターン）")
    clusters=[]
    if ("呼吸困難" in assoc) or facts.get("SpO2") is not None: clusters.append("呼吸/酸素化")
    if (facts.get("NRS") is not None) or sites: clusters.append("疼痛")
    if "食欲低下" in ALL or meta.get("BMI") is not None: clusters.append("栄養・代謝")
    if meta.get("living") or behav.get("seek"): clusters.append("役割/支援")
    L.append("・"+(" / ".join(clusters) if clusters else "クラスター限定的")+"\n")

    L.append("◆診断/介入の優先順位付け")
    L.append(f"・優先度: {PRIO}（NEWS2 {NEWS2}、SpO₂目標≧{int(SPO2_TARGET)}%、疼痛目標NRS≦{PAIN_GOAL}）\n")

    L.append("◆アセスメント段落（ワイズマン4段テンプレ）")
    L.append(build_wiseman_paragraph(ai))
    return "\n".join(L)

# ========== レビュー出力 ==========
def build_final_checklist() -> str:
    reds=[]
    if facts.get("SpO2") is not None and facts["SpO2"]<SPO2_RED: reds.append("SpO₂<90%")
    if facts.get("MAP")  is not None and facts["MAP"] < MAP_LOW:  reds.append("MAP<65mmHg")
    if facts.get("SBP")  is not None and facts["SBP"] < 90:       reds.append("SBP<90mmHg")
    if facts.get("NRS")  is not None and facts["NRS"] >= NRS_RED: reds.append("NRS≥7")
    L=[]
    L.append("="*92)
    L.append("確認項目（レビュー用）")
    L.append("="*92)
    L.append(f"- NEWS2: {NEWS2}")
    L.append(f"- 赤旗: {('なし' if not reds else ', '.join(reds))}")
    L.append(f"- 主要バイタル: {fmt_vitals()}")
    L.append(f"- 優先度: {PRIO}")
    if meta.get("BMI") is not None: L.append(f"- BMI: {meta['BMI']:.1f}（{meta['BMI_class']}）")
    L.append(f"- 目標: SpO₂≧{int(SPO2_TARGET)}%, 疼痛NRS≦{PAIN_GOAL}")
    beh = behavior_sentence()
    L.append("- 行動傾向: " + (beh if beh else "未評価"))
    missing=[]
    if facts.get("T") is None: missing.append("体温")
    if facts.get("HR") is None: missing.append("脈拍")
    if facts.get("RR") is None: missing.append("呼吸数")
    if facts.get("SBP") is None or facts.get("DBP") is None: missing.append("血圧")
    if facts.get("SpO2") is None: missing.append("SpO₂")
    if "食欲" not in ALL and "摂取" not in ALL: missing.append("栄養/摂取")
    L.append("- 欠落の可能性: "+("なし" if not missing else "・".join(missing)))
    L.append("")
    return "\n".join(L)

def _write_quick_review():
    lines=["【レビューQuick】","・バイタル: "+fmt_vitals(),"・NEWS2/赤旗: "+risk_sentence()]
    Path("assessment_review.txt").write_text("\n".join(lines), encoding="utf-8")

# ========== APIライク ==========
def _mix_texts(s_text: Optional[str], o_text: Optional[str], so_text: Optional[str]) -> str:
    pieces=[]
    for t in (s_text, o_text, so_text):
        if t and str(t).strip(): pieces.append(str(t).strip())
    return "\n".join(pieces).strip()

def build_from_SO_any(s_text: Optional[str]=None, o_text: Optional[str]=None, so_text: Optional[str]=None) -> str:
    global S, O, ALL
    mix = _mix_texts(s_text, o_text, so_text)
    S, O = smart_split_so(mix) if mix else ("","")
    ALL  = (S + "\n" + O).strip()
    parse_all()
    ai = ai_all_in_one()
    legacy = build_legacy_body(ai)
    engineered = build_engineered_body(ai)
    out = legacy + "\n" + ("-"*92) + "\n" + engineered
    Path("assessment_result.txt").write_text(out, encoding="utf-8")
    checklist = build_final_checklist()
    Path("final.txt").write_text(checklist, encoding="utf-8")
    Path("assessment_final.txt").write_text(checklist, encoding="utf-8")
    _write_quick_review()
    return out

def generate_assessment() -> str:
    p = Path("assessment_result.txt")
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""

# ========== CLI ==========
def main():
    ap = argparse.ArgumentParser(description="S/O混在OK→自動再分配→2本本文を連結出力（S/O原文は非表示）")
    ap.add_argument("--s", default=None, help="Sテキスト")
    ap.add_argument("--o", default=None, help="Oテキスト")
    ap.add_argument("--so", default=None, help="SとOをまとめて1本で渡す（S:,O:や数値所見から自動分配）")
    ap.add_argument("--fast", action="store_true", help="AIを使わない高速モード（FAST_MODE=1 と同義）")
    args = ap.parse_args()
    if args.fast: os.environ["FAST_MODE"]="1"

    if args.s is None and args.o is None and args.so is None:
        print("\n" + "="*92)
        print(f"自動文章化（{datetime.now().strftime('%Y-%m-%d %H:%M')}）")
        print("="*92)
        print("S+O（まとめて入力OK／終了は EOF）:")
        buf=[]
        while True:
            try:
                line=input()
            except EOFError:
                break
            if line.strip()=="EOF": break
            buf.append(line)
        mix="\n".join(buf).strip()
        out = build_from_SO_any(so_text=mix)
    else:
        out = build_from_SO_any(s_text=args.s, o_text=args.o, so_text=args.so)

    print(out)
    print("\n[保存] assessment_result.txt / final.txt（assessment_final.txt） / assessment_review.txt")
    print("=== 完了 ===")

if __name__ == "__main__":
    main()
