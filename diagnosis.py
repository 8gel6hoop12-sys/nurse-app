# -*- coding: utf-8 -*-
"""
diagnosis.py — 候補は“あり得るもの”を広く拾いつつ、
性別/年齢/明白カテゴリは厳格除外 → 緩い話題足切り → 上位K件のみAI（coarse→fine）
→ 総合スコア(加点方式・上限なし)でランキング。
入力は assessment_final.txt と（あれば）S/Oの素テキストのみ。
UI側は変更不要：テーブルの「スコア」列には総合スコアを出力。
"""

from __future__ import annotations
import os, re, sys, math, json, time, unicodedata, hashlib, pickle
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

# ========= 入出力 =========
ASSESS_FINAL_TXT = "assessment_final.txt"                 # 必須
S_FILE_CANDS = ["s_input.txt", "S.txt", "s.txt"]          # 任意
O_FILE_CANDS = ["o_input.txt", "O.txt", "o.txt"]          # 任意

NANDA_XLSX   = "nanda_db.xlsx"
ROWS_CACHE   = "nanda_rows_cache.json"   # Excel→行データ キャッシュ
VEC_CACHE    = "nanda_vec_cache.pkl"     # 定義ベクトル/IDF キャッシュ

RESULT_TXT   = "diagnosis_result.txt"
RESULT_JSON  = "diagnosis_candidates.json"
AI_CACHE_FN  = "diagnosis_ai_cache.json"

# ========= パラメータ（必要なら環境変数で上書き） =========
# AI
AI_TOPK            = int(os.getenv("DIAG_AI_TOPK", "40"))     # AIを当てる上限
COARSE_MIN_PASS    = float(os.getenv("DIAG_COARSE_MIN_PASS", "0.30"))
FINE_MIN_PASS      = float(os.getenv("DIAG_FINE_MIN_PASS",   "0.35"))

COARSE_CONCURRENCY = int(os.getenv("DIAG_COARSE_CONCURRENCY", "4"))
FINE_CONCURRENCY   = int(os.getenv("DIAG_FINE_CONCURRENCY",   "3"))
COARSE_BUDGET_SEC  = float(os.getenv("DIAG_COARSE_BUDGET_SEC", "0"))
FINE_BUDGET_SEC    = float(os.getenv("DIAG_FINE_BUDGET_SEC",   "0"))
AI_SNIPPET_CHARS   = int(os.getenv("DIAG_AI_SNIPPET", "1500"))

# 話題足切り（弱め）
MIN_DEF_SIM_KEEP    = float(os.getenv("DIAG_MIN_DEF_SIM", "0.05"))
MIN_RULE_SCORE_KEEP = float(os.getenv("DIAG_MIN_RULE",    "0.60"))

# 出力
SHOW_N              = int(os.getenv("DIAG_SHOW_N", "40"))     # 画面テキストの最大表示
OUT_REQUIRE_RELATED = os.getenv("DIAG_ONLY_RELATED", "1") == "1"   # 関連が弱いものは表示除外
OUT_TOP_FRAC        = float(os.getenv("DIAG_TOP_FRAC", "0.20"))    # バックアップで上位何割かを許容

# 厳格/緩和
STRICT_SEX_FILTER   = True
STRICT_AGE_FILTER   = True
STRICT_CATEGORY     = True
STRICT_CARETARGET   = True

# ルール寄与・ペナルティ（加点方式、スコア目安：上位~10点台）
W_DEF_SIM     = 2.0   # 定義ベクトル類似
W_COARSE_AI   = 3.5   # AI 粗一致
W_FINE_AI     = 4.5   # AI 精一致
W_RULE_DC     = 1.6   # 診断指標ヒット
W_RULE_RF     = 1.2   # 関連因子ヒット
W_RULE_RK     = 1.4   # 危険因子ヒット
W_HINT_RESP   = 1.0   # 呼吸/循環ヒント×バイタル
W_CAT_MATCH   = 0.8   # カテゴリ一致少ボーナス

P_SETTING_MISMATCH = 0.8
P_MIN_HITS_WEAK    = 0.8
P_CONTRADICT       = 1.0

TOKEN_MINLEN       = int(os.getenv("DIAG_TOKEN_MINLEN", "2"))
FUZZY_THRESHOLD    = float(os.getenv("DIAG_FUZZY_TH", "0.86"))

# ========= Ollama =========
OLLAMA_BASE   = os.getenv("OLLAMA_BASE",  "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
CONNECT_TO    = float(os.getenv("OLLAMA_CONNECT_TIMEOUT", "5"))
READ_TO       = float(os.getenv("OLLAMA_READ_TIMEOUT",    "20"))
RETRY         = int(os.getenv("OLLAMA_RETRY",             "1"))

_session = requests.Session()
def _get(url: str, timeout: float = CONNECT_TO): return _session.get(url, timeout=timeout)
def _post(url: str, payload: dict, timeout: float = READ_TO): return _session.post(url, json=payload, timeout=timeout)

def ollama_available() -> bool:
    try:
        r = _get(OLLAMA_BASE + "/api/tags")
        return r.status_code == 200
    except Exception:
        return False

# ========= 文字正規化 =========
def nfkc(s: str) -> str: return unicodedata.normalize("NFKC", s or "")
def norm(s: str|None) -> str:
    if not s: return ""
    t = nfkc(s).lower()
    t = re.sub(r"[　\s]+", " ", t)
    return t.strip()

def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore").strip() if p.exists() else ""

def read_assess_and_so() -> str:
    core = read_text(Path(ASSESS_FINAL_TXT))
    s_txt = next((t for fn in S_FILE_CANDS if (t:=read_text(Path(fn))) ), "")
    o_txt = next((t for fn in O_FILE_CANDS if (t:=read_text(Path(fn))) ), "")
    parts=[]
    if s_txt: parts.append("S: " + s_txt.strip())
    if o_txt: parts.append("O: " + o_txt.strip())
    if core:  parts.append(core)
    if not parts:
        raise FileNotFoundError("assessment_final.txt が見つからない/空、S/O もなし。")
    return "\n".join(parts)

def write_text(path: str, s: str):
    Path(path).write_text(s, encoding="utf-8")

# ========= rapidfuzz (任意) =========
try:
    from rapidfuzz.fuzz import ratio as _fzratio
    def _sim(a, b): return _fzratio(a, b) / 100.0
except Exception:
    def _sim(a, b): return SequenceMatcher(None, a, b).ratio()

# ========= Excel 読み & キャッシュ =========
COLMAP = {
    "code":"code","コード":"code","diagnosis_code":"code",
    "label":"label","診断名":"label","name":"label",
    "definition":"definition","定義":"definition",
    "defining_characteristics":"defining_characteristics","診断指標":"defining_characteristics",
    "related_factors":"related_factors","関連因子":"related_factors",
    "risk_factors":"risk_factors","危険因子":"risk_factors",
    "priority_hint":"priority_hint","優先ヒント":"priority_hint",
    "primary_focus":"primary_focus","一次焦点":"primary_focus",
    "secondary_focus":"secondary_focus","二次焦点":"secondary_focus",
    "care_target":"care_target","ケア対象":"care_target",
    "anatomical_site":"anatomical_site","解剖学的部位":"anatomical_site",
    "age_min":"age_min","年齢下限":"age_min",
    "age_max":"age_max","年齢上限":"age_max",
    "clinical_course":"clinical_course","臨床経過":"clinical_course",
    "diagnosis_state":"diagnosis_state","診断の状態":"diagnosis_state",
    "situational_constraints":"situational_constraints","状況的制約":"situational_constraints",
    "domain":"domain","領域":"domain",
    "class":"class","分類":"class",
    "judge":"judge","判断":"judge",
}

def _file_sig(p: Path) -> str:
    h=hashlib.sha1()
    h.update(str(p.stat().st_mtime_ns).encode())
    h.update(str(p.stat().st_size).encode())
    return h.hexdigest()

def load_nanda_rows(path: str) -> List[Dict[str,Any]]:
    p=Path(path)
    if not p.exists(): raise FileNotFoundError(f"{path} がありません。")
    sig=_file_sig(p)

    if Path(ROWS_CACHE).exists():
        try:
            obj=json.loads(Path(ROWS_CACHE).read_text(encoding="utf-8"))
            if obj.get("sig")==sig and isinstance(obj.get("rows"), list):
                return obj["rows"]
        except: pass

    df = pd.read_excel(p)
    normcols={}
    for c in df.columns:
        c0=str(c).strip()
        ckey=COLMAP.get(c0)
        if ckey is None:
            c1=re.sub(r"[（）\(\)\s]", "", c0).lower()
            ckey=COLMAP.get(c1, c0)
        normcols[c0]=ckey
    df=df.rename(columns=normcols)

    rows=[]
    for _,r in df.iterrows():
        d={}
        for want in set(COLMAP.values()):
            v=r.get(want, "")
            if isinstance(v,float) and math.isnan(v): v=""
            d[want]=str(v).strip() if v is not None else ""
        rows.append(d)

    Path(ROWS_CACHE).write_text(json.dumps({"sig":sig,"rows":rows}, ensure_ascii=False), encoding="utf-8")
    return rows

# ========= S/O・属性・カテゴリ =========
SETTING_KW = {
    "ICU": ["ICU","HCU","集中治療","人工呼吸器","挿管","人工呼吸"],
    "在宅": ["在宅","訪問","家屋","家族介護"],
    "外来": ["外来","クリニック"],
    "精神": ["精神科","うつ","不安障害","幻覚","妄想","向精神薬"],
    "術後": ["術後","手術後","POD","ドレーン","創部"],
    "リハ": ["リハ","リハビリ","PT","OT","ST"],
}
CATEGORY_KW = {
    "呼吸": ["呼吸","気道","酸素","SpO2","喘","RR","息切","酸素化","airway","breathing","oxygenation","COPD","喘息"],
    "循環": ["循環","ショック","血圧","SBP","MAP","脈拍","HR","出血","末梢冷感","circulation"],
    "排泄": ["排尿","排便","失禁","尿閉","便秘","下痢","ストーマ","カテーテル","尿量"],
    "栄養": ["栄養","食事","食欲","経口","嚥下","摂食","摂取","飲水","脱水","経管","BMI","体重"],
    "活動/ADL": ["歩行","移動","ADL","更衣","起居","セルフケア","活動","耐久","リハ","PT","OT","ST"],
    "睡眠/休息": ["睡眠","不眠","入眠","中途覚醒","休息","昼夜逆転"],
    "安全": ["転倒","転落","誤嚥","出血リスク","皮膚損傷","褥瘡","感染予防","安全","拘束"],
    "疼痛": ["痛み","疼痛","NRS","鎮痛"],
    "皮膚/創傷": ["褥瘡","発赤","びらん","皮膚","スキン","創部","創傷","ドレッシング","滲出"],
    "感染": ["感染","発熱","抗菌薬","白血球","CRP","敗血症"],
    "精神/情緒": ["不安","うつ","混乱","不穏","幻覚","妄想","ストレス","気分"],
    "知識/自己管理": ["教育","説明","理解","自己管理","アドヒアランス","服薬","指導","知識不足"],
    "妊娠/産科": ["妊娠","産褥","分娩","胎児","授乳","母乳","産科"],
    "コミュニケーション": ["コミュニケーション","意思疎通","聴力","視力","言語"],
    "手術/周術期": ["術前","術後","手術","麻酔","POD","ドレーン","創部"],
}

def parse_setting(text: str) -> set:
    t=nfkc(text); hits=set()
    for k,lst in SETTING_KW.items():
        if any(w in t for w in lst): hits.add(k)
    return hits

def extract_categories_from_text(text: str) -> set:
    t=nfkc(text); cats=set()
    for cat,kws in CATEGORY_KW.items():
        if any(w in t for w in kws): cats.add(cat)
    return cats

def extract_categories_from_row(row: dict) -> set:
    src = " ".join([
        row.get("primary_focus",""), row.get("secondary_focus",""),
        row.get("domain",""), row.get("class",""),
        row.get("label",""), row.get("definition","")
    ])
    return extract_categories_from_text(src)

def parse_demo(text: str) -> Dict[str, Any]:
    t=nfkc(text)
    sex=None
    if re.search(r"(?:\b|[^ぁ-んァ-ン一-龥])男(?:性)?\b|♂", t): sex="M"
    if re.search(r"(?:\b|[^ぁ-んァ-ン一-龥])女(?:性)?\b|♀|妊娠|産褥|授乳|母乳", t): sex="F"
    age=None
    m=re.search(r"(\d{1,3})\s*歳", t)
    if m:
        try: age=int(m.group(1))
        except: pass
    has_family = bool(re.search(r"家族|妻|夫|母|父|娘|息子|介護者|保護者|親|配偶者", t))
    return {"sex":sex,"age":age,"has_family":has_family}

# ========= トークン/曖昧一致 =========
KANJI=r"[一-龥]"; KATA=r"[ァ-ヶー]"; HIRA=r"[ぁ-ん]"; EN=r"[A-Za-z]"; NUM=r"[0-9]"
WORD_PAT=re.compile(rf"({KANJI}{{2,}}|{KATA}{{2,}}|{HIRA}{{3,}}|{EN}[A-Za-z\-]{{2,}}|{EN}{NUM}[A-Za-z0-9\-]{{1,}})")
JP_STOP=set(["こと","もの","ため","および","また","など","よう","これ","それ","にて","により","について","とは","的"])

def extract_def_terms(def_text: str, max_terms=16) -> List[str]:
    terms=[]
    for m in WORD_PAT.finditer(nfkc(def_text)):
        w=m.group(0).strip()
        if w and w not in JP_STOP and len(w)>=TOKEN_MINLEN:
            terms.append(w)
    seen=set(); out=[]
    for t in terms:
        if t not in seen:
            seen.add(t); out.append(t)
        if len(out)>=max_terms: break
    return out

def split_terms(s: str) -> List[str]:
    if not s: return []
    parts=[]
    for chunk in re.split(r"[|｜]", s):
        for sub in re.split(r"[、,;／/・]|[　\s]+", chunk):
            sub=nfkc(sub).strip()
            if sub and len(sub)>=TOKEN_MINLEN: parts.append(sub)
    seen=set(); out=[]
    for t in parts:
        if t not in seen: seen.add(t); out.append(t)
    return out

SYNONYMS={
    "疼痛":["痛い","痛み","苦痛","圧痛","腰痛","腹痛","胸痛","頭痛","創部痛","痛覚過敏"],
    "呼吸困難":["息苦しさ","息切れ","呼吸苦","呼吸困難感","起坐呼吸","労作時呼吸困難"],
    "不安":["心配","落ち着かない","そわそわ","緊張","恐れ","恐怖"],
    "倦怠感":["だるい","疲労","しんどい","易疲労","脱力"],
    "脱水":["口渇","尿量低下","皮膚乾燥","尿濃縮","飲水不足"],
    "転倒リスク":["ふらつき","歩行不安定","易転倒","失神既往"],
    "嚥下障害":["dysphagia","誤嚥","むせ","咽頭残留","嚥下機能低下"],
}

def expand_terms(term: str) -> List[str]:
    t=nfkc(term); out={t}
    for k,vs in SYNONYMS.items():
        if t==k or t in vs: out.update([k]+vs)
    return list(out)

# “正常/陰性”を見分ける簡易極性（語の前後±12文字に否定/良好語）
OK_WORDS=r"(?:なし|ない|良好|維持|保た|正常|安定|問題なし|みられず|陰性|改善)"
BAD_WORDS=r"(?:悪化|不良|低下|障害|困難|不足|増悪|異常|陽性|上昇|低下|増加)"

def _is_ok_window(text: str, idx: int, width: int=12) -> bool:
    a=max(0, idx-width); b=min(len(text), idx+width)
    win=text[a:b]
    return re.search(OK_WORDS, win) is not None

def _is_bad_window(text: str, idx: int, width: int=12) -> bool:
    a=max(0, idx-width); b=min(len(text), idx+width)
    win=text[a:b]
    return re.search(BAD_WORDS, win) is not None

def fuzzy_hits_with_polarity(text_norm: str, terms: List[str]) -> Tuple[List[str],List[str]]:
    """戻り値: (肯定ヒット, 正常/否定ヒット)"""
    pos=[]; ok=[]
    toks=text_norm.split()
    for term in terms:
        exps=expand_terms(term)
        found_idx=-1
        # 文字列包含
        for e in exps:
            e_n=norm(e)
            if not e_n: continue
            i=text_norm.find(e_n)
            if i!=-1:
                found_idx=i; break
        # トークン類似
        if found_idx==-1:
            for e in exps:
                e_n=norm(e)
                if not e_n: continue
                for token in toks:
                    if _sim(e_n, token)>=FUZZY_THRESHOLD:
                        found_idx=max(text_norm.find(token), 0)
                        break
                if found_idx!=-1: break

        if found_idx!=-1:
            if _is_ok_window(text_norm, found_idx):
                ok.append(term)
            else:
                # “悪化/低下..”等が近いなら肯定扱いを強める
                pos.append(term)
    # 重複除去
    pos_u=[]; seen=set()
    for t in pos:
        if t not in seen: seen.add(t); pos_u.append(t)
    ok_u=[]; seen=set()
    for t in ok:
        if t not in seen: seen.add(t); ok_u.append(t)
    return pos_u, ok_u

# ========= TF-IDF =========
JA_SEQ=re.compile(r"[一-龥ぁ-んァ-ン]+")
EN_SEQ=re.compile(r"[a-zA-Z][a-zA-Z\-]+")

def ja_char_ngrams(seq: str, nmin=2, nmax=4) -> List[str]:
    seq=re.sub(r"\s+","",seq); out=[]; L=len(seq)
    for n in range(nmin, nmax+1):
        for i in range(L-n+1): out.append(seq[i:i+n])
    return out

def tokenize(text: str) -> List[str]:
    t=text; toks=[]
    for m in JA_SEQ.finditer(t): toks+=ja_char_ngrams(m.group(0),2,4)
    for m in EN_SEQ.finditer(t.lower()): toks.append(m.group(0))
    words=[w for w in re.findall(r"[a-zA-Z]{3,}", t.lower())]
    toks+=[f"{words[i]}_{words[i+1]}" for i in range(len(words)-1)]
    stop=set(["こと","もの","ため","および","また","とは","的","など","にくい"])
    toks=[x for x in toks if len(x)>=2 and x not in stop]
    return toks[:120]

def tf(tokens: List[str]) -> Dict[str,float]:
    d={}
    for w in tokens: d[w]=d.get(w,0)+1
    return d

def idf(list_of_token_lists: List[List[str]]) -> Dict[str,float]:
    N=len(list_of_token_lists); df={}
    for toks in list_of_token_lists:
        for w in set(toks): df[w]=df.get(w,0)+1
    return {w:(math.log((N+1)/(dfw+1))+1.0) for w,dfw in df.items()}

def tfidf_vec(tokens: List[str], idfmap: Dict[str,float]) -> Dict[str,float]:
    tfmap=tf(tokens); return {w: tfmap[w]*idfmap.get(w,0.0) for w in tfmap if w in idfmap}

def cos_dict(a: Dict[str,float], b: Dict[str,float]) -> float:
    if not a or not b: return 0.0
    keys=set(a.keys()) & set(b.keys())
    dot=sum(a[k]*b[k] for k in keys)
    na=math.sqrt(sum(v*v for v in a.values())); nb=math.sqrt(sum(v*v for v in b.values()))
    if na==0 or nb==0: return 0.0
    return dot/(na*nb)

def build_definition_space(rows: List[Dict[str,Any]]):
    sig = ""
    try: sig=json.loads(Path(ROWS_CACHE).read_text(encoding="utf-8")).get("sig","")
    except: pass
    if Path(VEC_CACHE).exists():
        try:
            with open(VEC_CACHE,"rb") as f:
                obj=pickle.load(f)
            if obj.get("sig")==sig:
                return obj["idfmap"], obj["def_vecs"]
        except: pass
    defs=[r.get("definition","") for r in rows]
    def_tokens=[tokenize(nfkc(d)) for d in defs]
    idfmap=idf(def_tokens)
    def_vecs=[tfidf_vec(def_tokens[i], idfmap) for i in range(len(rows))]
    try:
        with open(VEC_CACHE,"wb") as f:
            pickle.dump({"sig":sig,"idfmap":idfmap,"def_vecs":def_vecs}, f)
    except: pass
    return idfmap, def_vecs

# ========= AI（coarse/fine） + キャッシュ =========
AI_SYS_COARSE = (
    "あなたは看護診断の意味一致チェッカーです。"
    "以下の『アセスメント本文（要旨）』と『看護診断（診断名/定義）』が臨床的に一致する可能性を 0.0〜1.0 で評価し、"
    '厳密JSON {"score": 0.0} のみを返してください。言い換え・含意の一致も評価してください。'
)
AI_SYS_FINE = (
    "あなたは看護診断の意味一致チェッカーです。"
    "『アセスメント本文（要旨）』に、提示する診断名/定義/診断指標/関連因子/危険因子が意味的に表れているかを評価し、"
    '厳密JSON {"matched":{"診断指標":[],"関連因子":[],"危険因子":[]}, "score":0.0} だけ返してください。'
    " matched は文字一致でなくても意味等価ならOK。score は 0.0〜1.0。"
)

def _trim_assess(src: str, limit: int = AI_SNIPPET_CHARS) -> str:
    t=nfkc(src)
    m=re.search(r"◆スクリー.*?アセスメント([\s\S]*?)◆データ分析", t)
    core=m.group(0) if m else t
    return (core[:limit]+"…") if len(core)>limit else core

def _ollama_chat(system: str, user: str, num_pred: int = 80) -> str:
    try:
        r=_post(OLLAMA_BASE+"/api/chat", {
            "model": OLLAMA_MODEL, "stream": False,
            "options": {"temperature": 0.2, "num_predict": num_pred},
            "messages": [{"role":"system","content":system},{"role":"user","content":user}]
        }, READ_TO)
        if r.status_code==404:
            prompt=f"### System\n{system}\n\n### User\n{user}\n"
            r=_post(OLLAMA_BASE+"/api/generate", {
                "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                "options": {"temperature": 0.2, "num_predict": num_pred}
            }, READ_TO)
        r.raise_for_status()
        data=r.json()
        if "message" in data:
            return (data.get("message",{}) or {}).get("content","") or ""
        return data.get("response","") or ""
    except Exception:
        return ""

def ask_ollama_json(system: str, user: str, num_pred: int = 80) -> Optional[dict]:
    for i in range(RETRY+1):
        try:
            txt=_ollama_chat(system, user, num_pred=num_pred)
            m=re.search(r"\{[\s\S]*\}", txt)
            return json.loads(m.group(0) if m else txt)
        except Exception:
            time.sleep(0.3*(i+1))
    return None

def load_cache() -> dict:
    p=Path(AI_CACHE_FN)
    if p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except: return {}
    return {}
def save_cache(c: dict):
    try: Path(AI_CACHE_FN).write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    except: pass

_CACHE=load_cache(); _CACHE.setdefault("coarse",{}); _CACHE.setdefault("fine",{})

def coarse_key(assess: str, label: str, definition: str) -> str:
    src="\n".join([OLLAMA_MODEL, norm(assess), norm(label), norm(definition)])
    return hashlib.sha1(src.encode()).hexdigest()
def fine_key(assess: str, label: str, definition: str, dc: List[str], rf: List[str], rk: List[str]) -> str:
    src="\n".join([OLLAMA_MODEL, norm(assess), norm(label), norm(definition), "|".join(dc), "|".join(rf), "|".join(rk)])
    return hashlib.sha1(src.encode()).hexdigest()

def ai_coarse(assess: str, label: str, definition: str) -> float:
    key=coarse_key(assess,label,definition)
    if key in _CACHE["coarse"]: return float(_CACHE["coarse"][key])
    if not ollama_available(): return 0.0
    data=ask_ollama_json(AI_SYS_COARSE, f"【看護診断】{label}\n【定義】{definition}\n\n【アセスメント本文（要旨）】\n{_trim_assess(assess)}", 80) or {}
    score=float(data.get("score",0.0) or 0.0)
    _CACHE["coarse"][key]=score
    return score

def ai_fine(assess: str, label: str, definition: str, dc_terms: List[str], rf_terms: List[str], rk_terms: List[str]) -> Tuple[float, Dict[str,List[str]]]:
    key=fine_key(assess,label,definition,dc_terms,rf_terms,rk_terms)
    if key in _CACHE["fine"]:
        v=_CACHE["fine"][key]
        return float(v.get("score",0.0)), {"診断指標":v.get("dc",[]),"関連因子":v.get("rf",[]),"危険因子":v.get("rk",[])}
    if not ollama_available():
        return 0.0, {"診断指標":[],"関連因子":[],"危険因子":[]}
    user=(
        f"【看護診断】{label}\n【定義】{definition}\n\n"
        f"【診断指標リスト】{', '.join(dc_terms) if dc_terms else '（なし）'}\n"
        f"【関連因子リスト】{', '.join(rf_terms) if rf_terms else '（なし）'}\n"
        f"【危険因子リスト】{', '.join(rk_terms) if rk_terms else '（なし）'}\n\n"
        "【アセスメント本文（要旨）】\n"+_trim_assess(assess)
    )
    data=ask_ollama_json(AI_SYS_FINE, user, 80) or {}
    score=float(data.get("score",0.0) or 0.0)
    matched=data.get("matched",{}) or {}
    ev={"診断指標":[nfkc(x).strip() for x in matched.get("診断指標",[]) if str(x).strip()],
        "関連因子":[nfkc(x).strip() for x in matched.get("関連因子",[]) if str(x).strip()],
        "危険因子":[nfkc(x).strip() for x in matched.get("危険因子",[]) if str(x).strip()]}
    _CACHE["fine"][key]={"score":score,"dc":ev["診断指標"],"rf":ev["関連因子"],"rk":ev["危険因子"]}
    return max(0.0,min(1.0,score)), ev

# ========= ルール/スコア =========
_NUM=r"(\d+(?:\.\d+)?)"
def fnum(pat: str, text: str) -> Optional[float]:
    m=re.search(pat, text, flags=re.IGNORECASE)
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

def score_match_blocks(text: str, row: Dict[str,Any]) -> Tuple[float, Dict[str,List[str]], List[str]]:
    """
    文字/同義/定義語による曖昧一致(+近接で正常/陰性は除外)を加点し、
    根拠ブロック(loose)と reasons を返す。
    """
    t=norm(text); reasons=[]
    dc_terms=split_terms(row.get("defining_characteristics",""))
    rf_terms=split_terms(row.get("related_factors",""))
    rk_terms=split_terms(row.get("risk_factors",""))
    pos_dc, ok_dc = fuzzy_hits_with_polarity(t, dc_terms)
    pos_rf, ok_rf = fuzzy_hits_with_polarity(t, rf_terms)
    pos_rk, ok_rk = fuzzy_hits_with_polarity(t, rk_terms)

    base = W_RULE_DC*len(pos_dc) + W_RULE_RF*len(pos_rf) + W_RULE_RK*len(pos_rk)
    if ok_dc or ok_rf or ok_rk:
        reasons.append(f"正常/陰性と判断: DC{len(ok_dc)} RF{len(ok_rf)} RK{len(ok_rk)}")
    if "痛" in row.get("label","") and parse_vitals(text).get("NRS") is not None:
        n=parse_vitals(text)["NRS"]
        if n>=7: base+=1.5; reasons.append("数値:NRS≥7")
        elif n>=4: base+=0.8; reasons.append("数値:NRS≥4")

    hint=norm(row.get("priority_hint","")); v=parse_vitals(text)
    if any(k in hint for k in ("呼吸","airway","breathing")):
        base += W_HINT_RESP*(1.0 + (1.0 if (v.get("SpO2") and v["SpO2"]<90) else 0.0))
    if any(k in hint for k in ("循環","circulation")):
        base += W_HINT_RESP*(1.0 + (1.0 if (v.get("MAP") and v["MAP"]<65) else 0.0))

    loose = {
        "定義語":   extract_def_terms(row.get("definition",""), 16),
        "診断指標": pos_dc,
        "関連因子": pos_rf,
        "危険因子": pos_rk,
    }
    return base, loose, reasons

def row_care_target_ok(row: dict, demo: dict) -> Tuple[bool,str]:
    ct=(row.get("care_target","") or "").strip()
    if not ct: return True, ""
    if re.search(r"家族|介護者|保護者|親|配偶者", ct) and not demo.get("has_family"):
        return (False if STRICT_CARETARGET else True, "ケア対象が家族だが本文に家族介入記載なし")
    return True, ""

def row_age_ok(row: dict, demo: dict) -> Tuple[bool,str]:
    amin=row.get("age_min",""); amax=row.get("age_max","")
    try: amin=int(amin) if str(amin).strip() else None
    except: amin=None
    try: amax=int(amax) if str(amax).strip() else None
    except: amax=None
    age=demo.get("age")
    if age is None or (amin is None and amax is None): return True, ""
    if amin is not None and age < amin:  return (False if STRICT_AGE_FILTER else True,  f"年齢{age}<最小{amin}")
    if amax is not None and age > amax:  return (False if STRICT_AGE_FILTER else True,  f"年齢{age}>最大{amax}")
    return True, ""

def row_sex_ok(row: dict, demo: dict) -> Tuple[bool,str]:
    sex = demo.get("sex")
    txt = " ".join([row.get("label",""), row.get("definition",""), row.get("anatomical_site","") or ""])
    female_flag = bool(re.search(r"子宮|卵巣|膣|会陰|産褥|授乳|母乳|乳房|乳腺|妊娠|産科", txt))
    male_flag   = bool(re.search(r"前立腺|精巣|陰嚢", txt))
    if female_flag and sex == "M": return (False if STRICT_SEX_FILTER else True, "男性×女性特異診断")
    if male_flag   and sex == "F": return (False if STRICT_SEX_FILTER else True, "女性×男性特異診断")
    return True, ""

def row_category_ok(row: dict, assess_cats: set) -> Tuple[bool,str]:
    if not STRICT_CATEGORY: return True, ""
    row_cats = extract_categories_from_row(row)
    if not assess_cats or not row_cats: return True, ""
    if assess_cats & row_cats: return True, f"カテゴリ一致({', '.join(sorted(assess_cats & row_cats))})"
    if len(assess_cats)>=1 and len(row_cats)>=1:
        return False, f"カテゴリ不一致(本文:{'/'.join(sorted(assess_cats))} vs 候補:{'/'.join(sorted(row_cats))})"
    return True, ""

def penalty_setting(row: dict, assess_settings: set) -> Tuple[float,str]:
    txt=nfkc(" ".join([row.get("situational_constraints",""),row.get("domain",""),row.get("class",""),
                       row.get("priority_hint",""),row.get("definition","")]))
    req=set()
    for k,lst in SETTING_KW.items():
        if any(w in txt for w in lst): req.add(k)
    if not req:  return 0.0,""
    lack=[k for k in req if k not in assess_settings]
    if not lack: return 0.0,""
    return P_SETTING_MISMATCH, f"場面根拠弱({', '.join(lack)})"

def penalty_contradict(assess: str, row: dict) -> Tuple[float,str]:
    t=norm(assess); v=parse_vitals(assess)
    lbl=row.get("label","")+" "+row.get("definition","")
    if re.search(r"呼吸|酸素|気道|SpO2|息切|喘", lbl):
        no_words=not re.search(r"呼吸|息|SpO2|喘|RR", t)
        spo2_ok=(v.get("SpO2") is not None and v["SpO2"]>=95)
        rr_ok=(v.get("RR") is not None and 12<=v["RR"]<=20)
        if no_words and (spo2_ok or rr_ok):
            return P_CONTRADICT, "呼吸所見/語彙が弱く矛盾"
    if re.search(r"痛|疼痛|pain", lbl) and not re.search(r"痛|NRS|鎮痛", t):
        return P_CONTRADICT, "疼痛所見/語彙が弱い"
    return 0.0, ""

# ========= 収集・選抜 =========
def collect(assess: str, rows: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    tnorm=norm(assess)
    assess_tokens=tokenize(tnorm)
    idfmap, def_vecs = build_definition_space(rows)
    assess_vec=tfidf_vec(assess_tokens, idfmap)

    demo     = parse_demo(assess)
    settings = parse_setting(assess)
    text_cats= extract_categories_from_text(assess)

    # 事前スコア（定義類似 + ルール粗計算）
    pre=[]
    for i,r in enumerate(rows):
        def_sim=cos_dict(assess_vec, def_vecs[i])
        rule_raw, _, _ = score_match_blocks(assess, r)
        pre.append((i, def_sim, rule_raw))

    # 厳格フィルタ
    hard_ok=[True]*len(rows); cat_reason=[""]*len(rows)
    for i,_ds,_rs in pre:
        r=rows[i]
        ok_ct,_ = row_care_target_ok(r, demo)
        ok_ag,_ = row_age_ok(r, demo)
        ok_sx,_ = row_sex_ok(r, demo)
        ok_cat, why_cat = row_category_ok(r, text_cats)
        hard_ok[i]= ok_ct and ok_ag and ok_sx and ok_cat
        cat_reason[i]= ("OK: "+why_cat) if (ok_cat and why_cat) else (("NG: "+why_cat) if why_cat else "")

    eligible=[i for i in range(len(rows)) if hard_ok[i]]
    if not eligible: eligible=list(range(len(rows)))

    # 緩い足切り
    kept=[]
    for i,ds,rs in pre:
        if i not in eligible: continue
        if ds < MIN_DEF_SIM_KEEP and rs < MIN_RULE_SCORE_KEEP:
            continue
        kept.append(i)
    if not kept: kept = eligible

    # 上位K件にAI
    def quick_score(i):
        ds, rr = pre[i][1], pre[i][2]
        bonus = W_CAT_MATCH if cat_reason[i].startswith("OK: カテゴリ一致") else 0.0
        return 0.6*ds + 0.4*min(1.0, rr/4.0) + 0.1*bonus

    order=sorted(kept, key=lambda i: quick_score(i), reverse=True)
    ai_targets=set(order[:AI_TOPK]) if AI_TOPK>0 else set()

    ai_coarse_s=[0.0]*len(rows)
    def task_coarse(i):
        r=rows[i]
        return i, ai_coarse(assess, r.get("label",""), r.get("definition",""))

    if ai_targets and ollama_available():
        futures=[]
        with ThreadPoolExecutor(max_workers=max(1, COARSE_CONCURRENCY)) as ex:
            for i in ai_targets: futures.append(ex.submit(task_coarse, i))
            end=(time.time()+COARSE_BUDGET_SEC) if COARSE_BUDGET_SEC>0 else None
            for fut in as_completed(futures, timeout=None if end is None else max(1,int(COARSE_BUDGET_SEC))):
                try:
                    i,s=fut.result(timeout=None if end is None else max(0.1,end-time.time()))
                    ai_coarse_s[i]=float(s)
                except Exception: pass

    # fine 対象（上位60% & 閾値通過）
    out=[None]*len(rows)
    co_list=sorted([(i, ai_coarse_s[i]) for i in ai_targets], key=lambda x:x[1], reverse=True)
    cut=max(1,int(math.ceil(0.6*len(co_list)))) if co_list else 0
    fine_pool=[i for i,_ in co_list[:cut] if ai_coarse_s[i]>=COARSE_MIN_PASS and ollama_available()]
    early_accept={i for i,s in co_list if s>=0.82}

    def build_cand(i, s_coarse: float, s_fine: float=0.0, ai_ev=None):
        r=rows[i]
        ds=pre[i][1]
        rule_raw, loose, reasons0 = score_match_blocks(assess, r)

        ok_ct, why_ct   = row_care_target_ok(r, demo)
        ok_age, why_age = row_age_ok(r, demo)
        ok_sex, why_sex = row_sex_ok(r, demo)
        ok_cat, why_cat = row_category_ok(r, text_cats)
        hard_ok = ok_ct and ok_age and ok_sex and ok_cat

        p1,w1 = penalty_setting(r, settings)
        p3,w3 = penalty_contradict(assess, r)
        # ヒット弱は“減点”（除外はしない）
        dc_hits = len(loose.get("診断指標",[])) + len((ai_ev or {}).get("診断指標",[]))
        rk_hits = len(loose.get("危険因子",[])) + len((ai_ev or {}).get("危険因子",[]))
        p2,w2 = (0.0,"")
        if "リスク" in (r.get("diagnosis_state","") or "") and rk_hits < 1:
            p2,w2 = P_MIN_HITS_WEAK, f"危険因子ヒット弱({rk_hits}/1)"
        if "問題焦点" in (r.get("diagnosis_state","") or "") and dc_hits < 1:
            p2,w2 = P_MIN_HITS_WEAK, f"診断指標ヒット弱({dc_hits}/1)"
        penalty = p1+p2+p3

        # 総合スコア（加点方式）
        total = (
            W_FINE_AI   * s_fine +
            W_COARSE_AI * s_coarse +
            W_DEF_SIM   * ds +
            rule_raw +
            (W_CAT_MATCH if (ok_cat and why_cat and "一致" in why_cat) else 0.0)
        ) - penalty

        # “関連あり”の判定（強めに）
        related_basic = (s_fine>=FINE_MIN_PASS) or (s_coarse>=COARSE_MIN_PASS and (ds>=0.12 or rule_raw>=1.5))
        related = hard_ok and (related_basic or total>=2.0)

        reasons=list(reasons0)
        for ok,why in ((ok_ct,why_ct),(ok_age,why_age),(ok_sex,why_sex),(ok_cat,why_cat)):
            if why: reasons.append(("OK: " if ok else "NG: ")+why)
        for w in (w1,w2,w3):
            if w: reasons.append(f"penalty: {w} (-{penalty:.1f})")

        return {
            "code": (r.get("code","") or "").strip() or "00000",
            "label": r.get("label","").strip() or "(診断名未設定)",
            "definition": r.get("definition",""),
            "loose": loose,
            "def_sim": float(ds),
            "rule_raw": float(rule_raw),
            "reasons": reasons,
            "ai_coarse": float(s_coarse),
            "ai_sim": float(s_fine),
            "ai_ev": ai_ev or {"診断指標":[],"関連因子":[],"危険因子":[]},
            # メタ（UIは読まなくてもOK）
            "primary_focus": r.get("primary_focus",""),
            "secondary_focus": r.get("secondary_focus",""),
            "care_target": r.get("care_target",""),
            "anatomical_site": r.get("anatomical_site",""),
            "age_min": r.get("age_min",""),
            "age_max": r.get("age_max",""),
            "clinical_course": r.get("clinical_course",""),
            "diagnosis_state": r.get("diagnosis_state",""),
            "situational_constraints": r.get("situational_constraints",""),
            "domain": r.get("domain",""),
            "class": r.get("class",""),
            "judge": r.get("judge",""),
            "priority_hint": r.get("priority_hint",""),
            # UI用フィールド：
            "related": related,
            "soft_score": round(total, 3),   # 内部名
            "score": round(total, 3),        # ← UIの「スコア」列はこの値を表示
        }

    def task_fine(i):
        r=rows[i]
        dc=split_terms(r.get("defining_characteristics",""))
        rf=split_terms(r.get("related_factors",""))
        rk=split_terms(r.get("risk_factors",""))
        s,ev=ai_fine(assess, r.get("label",""), r.get("definition",""), dc, rf, rk)
        return i,s,ev

    if fine_pool:
        futures=[]
        with ThreadPoolExecutor(max_workers=max(1,FINE_CONCURRENCY)) as ex:
            for i in fine_pool: futures.append(ex.submit(task_fine,i))
            end=(time.time()+FINE_BUDGET_SEC) if FINE_BUDGET_SEC>0 else None
            for fut in as_completed(futures, timeout=None if end is None else max(1,int(FINE_BUDGET_SEC))):
                try:
                    i,s,ev=fut.result(timeout=None if end is None else max(0.1,end-time.time()))
                    out[i]=build_cand(i, ai_coarse_s[i], s, ev)
                except Exception: pass

    for i in early_accept:
        if out[i] is None:
            out[i]=build_cand(i, ai_coarse_s[i], ai_coarse_s[i], {"診断指標":[],"関連因子":[],"危険因子":[]})

    for i in range(len(rows)):
        if out[i] is None:
            out[i]=build_cand(i, ai_coarse_s[i], 0.0, None)

    # 並べ替え
    cands=list(out)
    cands.sort(key=lambda x:( x["related"], round(x["score"],3), round(x["ai_sim"],3),
                              round(x["ai_coarse"],3), round(x["def_sim"],4), round(x["rule_raw"],3) ), reverse=True)
    for idx,c in enumerate(cands, start=1):
        c["ai_rank"]=idx

    # 関連が薄いのを落とす（全部ゼロの時は保険で上位だけ残す）
    if OUT_REQUIRE_RELATED:
        vis=[c for c in cands if c["related"] and c["score"]>0.0]
        if not vis:
            k=max(3, int(len(cands)*OUT_TOP_FRAC))
            vis=cands[:k]
        return vis
    return cands

# ========= 整形 =========
def join_list(xs: List[str], sep="・"): return sep.join(xs) if xs else ""

def format_block(c: Dict[str,Any]) -> str:
    L=[]
    L.append(f"- [ ] {c['code']}\t{c['label']}")
    if c["definition"]: L.append(f"    定義: {c['definition']}")
    L.append(f"    総合スコア: {c['score']:.2f}  (rank: {c['ai_rank']})")
    L.append(f"    内訳: ai_fine {c['ai_sim']:.2f} ×{W_FINE_AI} / ai_coarse {c['ai_coarse']:.2f} ×{W_COARSE_AI} / 定義適合 {c['def_sim']:.2f} ×{W_DEF_SIM} / ルール(raw): {c['rule_raw']:.1f}")
    if any(c["loose"].values()):
        L.append("    ①曖昧一致（文字/同義/定義語）:")
        if c["loose"]["定義語"]:   L.append(f"       定義語:   {join_list(c['loose']['定義語'])}")
        if c["loose"]["診断指標"]: L.append(f"       診断指標: {join_list(c['loose']['診断指標'])}")
        if c["loose"]["関連因子"]: L.append(f"       関連因子: {join_list(c['loose']['関連因子'])}")
        if c["loose"]["危険因子"]: L.append(f"       危険因子: {join_list(c['loose']['危険因子'])}")
    ai_ev=c.get("ai_ev",{})
    if any(ai_ev.get(k) for k in ("診断指標","関連因子","危険因子")):
        L.append("    ②AI意味一致（言い換え/含意）:")
        if ai_ev.get("診断指標"): L.append(f"       指標ヒット: {join_list(ai_ev['診断指標'])}")
        if ai_ev.get("関連因子"): L.append(f"       関連因子ヒット: {join_list(ai_ev['関連因子'])}")
        if ai_ev.get("危険因子"): L.append(f"       危険因子ヒット: {join_list(ai_ev['危険因子'])}")
    if c["reasons"]:
        L.append("       └ 根拠/ペナルティ内訳:")
        for r in c["reasons"][:12]: L.append(f"         - {r}")
    return "\n".join(L)

def rb_narrative(assess: str, top: Dict[str,Any]) -> str:
    v=parse_vitals(assess); abn=[]
    if v.get("T")  and (v["T"]>=38 or v["T"]<=35): abn.append(f"T{v['T']:.1f}")
    if v.get("HR") and (v["HR"]>=100 or v["HR"]<=50): abn.append(f"HR{int(v['HR'])}")
    if v.get("RR") and (v["RR"]>=22 or v["RR"]<=10):  abn.append(f"RR{int(v['RR'])}")
    if v.get("SpO2") and (v["SpO2"]<94):              abn.append(f"SpO2{int(v['SpO2'])}%")
    if v.get("SBP") and (v["SBP"]<=100):              abn.append(f"SBP{int(v['SBP'])}")
    if v.get("MAP") and (v["MAP"]<65):                abn.append(f"MAP{int(v['MAP'])}")
    if v.get("NRS") and (v["NRS"]>=4):                abn.append(f"NRS{int(v['NRS'])}")
    evid=(top["loose"].get("診断指標",[])+top.get("ai_ev",{}).get("診断指標",[])+top["loose"].get("関連因子",[]))[:3]
    parts=[]
    parts.append(f"{top['label']}[{top['code']}] を最有力（総合 {top['score']:.2f}）。")
    if evid: parts.append("根拠: " + "・".join(evid))
    if abn:  parts.append("所見: " + " ".join(abn))
    return " ".join(parts)

# ========= メイン =========
def main():
    assess = read_assess_and_so()
    rows   = load_nanda_rows(NANDA_XLSX)
    cands  = collect(assess, rows)

    lines=[]
    lines.append("="*100)
    lines.append(f"NANDA-I 看護診断 候補（性別/年齢 厳格・カテゴリは明確NGのみ除外・TOPKだけAI） {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("="*100)
    lines.append(f"[入力] {ASSESS_FINAL_TXT} (+ S/O があれば反映)")
    lines.append(f"[Excel] {NANDA_XLSX}（キャッシュ利用: {Path(ROWS_CACHE).exists()}）")
    lines.append(f"[設定] AI_TOPK={AI_TOPK}, coarse≥{COARSE_MIN_PASS}, fine≥{FINE_MIN_PASS}, "
                 f"MIN_DEF_SIM_KEEP={MIN_DEF_SIM_KEEP}, MIN_RULE_SCORE_KEEP={MIN_RULE_SCORE_KEEP}, SHOW_N={SHOW_N}")
    lines.append("")

    if not cands:
        lines.append("（候補なし：条件が厳し過ぎる可能性。S/O記載や語彙を見直すか、環境変数で足切りを緩めてください）")
    else:
        for c in cands[:SHOW_N]:
            lines.append(f"(順位:{c['ai_rank']})")
            lines.append(format_block(c))
            lines.append("")
        lines.append("—"*100)
        lines.append("【診断ナラティブ（要約）】")
        lines.append(rb_narrative(assess, cands[0]))
        lines.append("—"*100)
        lines.append("")
        lines.append("（レビュー手順）アプリで候補にチェック → 「選択を確定（保存）」で diagnosis_final.txt へ")

    out="\n".join(lines).rstrip()+"\n"
    print(out); write_text(RESULT_TXT, out); print(f"[SAVE] {RESULT_TXT}")

    # JSON は “候補”をそのまま。UIは c['score'] をテーブルに出す
    j={
        "meta":{
            "input": ASSESS_FINAL_TXT,
            "excel": NANDA_XLSX,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "ranking": "score(= ai_fine*Wf + ai_coarse*Wc + def_sim*Wd + rule_raw + bonuses - penalties)",
            "ollama_model": OLLAMA_MODEL,
            "ollama_ok": ollama_available(),
            "ai_topk": AI_TOPK,
            "coarse_min_pass": COARSE_MIN_PASS,
            "fine_min_pass": FINE_MIN_PASS,
            "min_def_sim_keep": MIN_DEF_SIM_KEEP,
            "min_rule_score_keep": MIN_RULE_SCORE_KEEP,
            "only_related": OUT_REQUIRE_RELATED,
        },
        "candidates": cands
    }
    write_text(RESULT_JSON, json.dumps(j, ensure_ascii=False, indent=2)); print(f"[SAVE] {RESULT_JSON}")

if __name__=="__main__":
    try:
        main()
    except Exception as e:
        msg=f"[FATAL] {e}"
        print(msg)
        try: write_text(RESULT_TXT, msg+"\n")
        except: pass
        try: save_cache(_CACHE)
        except: pass
        sys.exit(1)
