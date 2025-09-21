# -*- coding: utf-8 -*-
"""
careplan.py
Assessment全文（assessment_result.txt / assessment_final.txt） + NANDA候補（diagnosis_result.txt / diagnosis_final.txt）→
看護計画（careplan_result.txt）を現場仕様で自動生成

使い方:
  1) assessment.py を実行し assessment_result.txt を作る（レビュー後は assessment_final.txt を置く）
  2) diagnosis.py  を実行し diagnosis_result.txt  を作る（レビュー後は diagnosis_final.txt を置く）
  3) python careplan.py
     （任意）python careplan.py --assess assessment_result.txt --diag diagnosis_result.txt --out careplan_result.txt --verbose
"""

from __future__ import annotations
import argparse, re, unicodedata
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Tuple

# ===================== 引数 =====================
parser = argparse.ArgumentParser(description="Assessment+NANDA→看護計画（現場仕様）")
parser.add_argument("--assess", default="assessment_result.txt", help="アセスメント全文ファイル")
parser.add_argument("--diag",   default="diagnosis_result.txt",   help="NANDA候補結果ファイル")
parser.add_argument("--out",    default="careplan_result.txt",    help="保存ファイル名")
parser.add_argument("--verbose", action="store_true", help="詳細ログ")
ARGS = parser.parse_args()

def log(msg: str):
    if ARGS.verbose:
        print(msg, flush=True)

# ===================== 便利関数 =====================

def read_text(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"[ERROR] ファイルが見つかりません: {p.resolve()}")
    t = p.read_text(encoding="utf-8").strip()
    if not t:
        raise ValueError(f"[ERROR] 空のファイルです: {p.resolve()}")
    log(f"[READ] {p.resolve()} ({len(t)} chars)")
    return t

# --- FINAL優先の読み込みヘルパ（★追加） ---
def read_assessment_for_careplan(path_arg: str | None) -> str:
    """
    優先順位:
      1) --assess 指定
      2) assessment_final.txt
      3) assessment_result.txt
    """
    cands: list[Path] = []
    if path_arg: cands.append(Path(path_arg))
    cands += [Path("assessment_final.txt"), Path("assessment_result.txt")]
    for p in cands:
        if p.exists():
            t = p.read_text(encoding="utf-8").strip()
            if t:
                log(f"[READ] Assessment: {p.resolve()} ({len(t)} chars)")
                return t
    raise FileNotFoundError("[ERROR] assessment_final.txt / assessment_result.txt が見つかりません。")

def read_diagnosis_for_careplan(path_arg: str | None) -> str:
    """
    優先順位:
      1) --diag 指定
      2) diagnosis_final.txt
      3) diagnosis_result.txt
    見つからなければ空文字で続行（NANDAなし）
    """
    cands: list[Path] = []
    if path_arg: cands.append(Path(path_arg))
    cands += [Path("diagnosis_final.txt"), Path("diagnosis_result.txt")]
    for p in cands:
        if p.exists():
            t = p.read_text(encoding="utf-8").strip()
            if t:
                log(f"[READ] Diagnosis: {p.resolve()} ({len(t)} chars)")
                return t
    log("[WARN] diagnosis のファイルが見つからないため NANDA 候補なしで続行します。")
    return ""

def norm(s: str|None) -> str:
    if s is None: return ""
    return unicodedata.normalize("NFKC", s).lower()

_NUM = r"(\d+(?:\.\d+)?)"
import re as _re
def fnum(pat: str, text: str) -> float|None:
    m = _re.search(pat, text, flags=_re.IGNORECASE)
    return float(m.group(1)) if m else None

# ===================== Assessment抽出 =====================
def parse_vitals(text: str) -> Dict[str, float|None]:
    T    = fnum(r"(?:体温|t)\s*[:=]?\s*"+_NUM, text)
    HR   = fnum(r"(?:hr|心拍|脈拍)\s*[:=]?\s*"+_NUM, text)
    RR   = fnum(r"(?:rr|呼吸数)\s*[:=]?\s*"+_NUM, text)
    SpO2 = fnum(r"(?:spo2|ｓｐｏ２|サチュ|ｻﾁｭ)\s*[:=]?\s*"+_NUM, text)
    bp   = _re.search(r"\b(\d{2,3})\s*/\s*(\d{2,3})\b", text, flags=_re.IGNORECASE)
    SBP  = float(bp.group(1)) if bp else fnum(r"(?:sbp|収縮期|上の血圧)\s*[:=]?\s*"+_NUM, text)
    DBP  = float(bp.group(2)) if bp else fnum(r"(?:dbp|拡張期|下の血圧)\s*[:=]?\s*"+_NUM, text)
    MAP  = (SBP + 2*DBP)/3 if (SBP is not None and DBP is not None) else None
    NRS  = fnum(r"(?:nrs|疼痛(?:スケール)?)\D{0,6}"+_NUM, text)
    return {"T":T,"HR":HR,"RR":RR,"SpO2":SpO2,"SBP":SBP,"DBP":DBP,"MAP":MAP,"NRS":NRS}

def assess_priority(v: Dict[str, float|None], text: str) -> Tuple[str, int]:
    t = norm(text)
    score = 0
    if (v.get("SpO2") and v["SpO2"]<90) or any(w in t for w in ("呼吸困難","起坐呼吸","チアノーゼ")): score += 7
    elif v.get("SpO2") and v["SpO2"]<94: score += 3
    if v.get("RR") and (v["RR"]>=30 or v["RR"]<=8): score += 3
    if (v.get("MAP") and v["MAP"]<65) or (v.get("SBP") and v["SBP"]<90): score += 6
    if v.get("HR") and v["HR"] is not None and (v["HR"]>=130 or v["HR"]<=40): score += 3
    if any(w in t for w in ("意識低下","傾眠","見当識低下","せん妄","けいれん")): score += 5
    if v.get("T") and (v["T"]>=39 or v["T"]<=35): score += 2
    if v.get("NRS") and v["NRS"]>=7: score += 2
    if v.get("RR") and (v["RR"]>=25 or v["RR"]<=9): score += 1
    if v.get("SpO2") and v["SpO2"]<92: score += 1
    if v.get("SBP") and v["SBP"]<=100: score += 1
    level = "高" if score>=10 else ("中" if score>=5 else "低")
    return level, score

# ===================== NANDA候補の読み取り =====================
def parse_nanda_from_diag(diag_text: str) -> List[Dict[str, Any]]:
    lines = diag_text.splitlines()
    out: List[Dict[str, Any]] = []
    cur: Dict[str, Any] | None = None
    for ln in lines:
        s = ln.strip()
        m = _re.match(r"^\d+\.\s+(.*?)\s+\[(.*?)\].*?Score:([0-9.]+)", s)
        if m:
            if cur: out.append(cur)
            cur = {"label":m.group(1).strip(),"code":m.group(2).strip(),"score":float(m.group(3)),
                   "definition":"","reasons":[],"hint":""}
            continue
        if cur and s.startswith("定義:"):
            cur["definition"]=s.replace("定義:","",1).strip(); continue
        if cur and s.startswith("- "):
            cur["reasons"].append(s[2:].strip()); continue
        if cur and s.startswith("優先ヒント:"):
            cur["hint"]=s.replace("優先ヒント:","",1).strip(); continue
    if cur: out.append(cur)
    out.sort(key=lambda x: x.get("score",0.0), reverse=True)
    return out

# ===================== 問題抽出 =====================
def extract_problems(assess_text: str, nanda_list: List[Dict[str, Any]], v: Dict[str,float|None]) -> Dict[str, List[str]]:
    t = assess_text; low = norm(t)
    now_hits, pot_hits, pos_hits = [], [], []

    symptom_map = {
        "呼吸": ["呼吸困難","起坐呼吸","喘鳴","咳","痰","SpO2低下","低酸素","チアノーゼ"],
        "循環": ["動悸","胸痛","冷汗","四肢冷感","めまい","ふらつき","低血圧","ショック"],
        "感染": ["発熱","悪寒","寒気","発赤","腫脹","排膿","感染"],
        "疼痛": ["疼痛","痛み","圧痛","NRS"],
        "消化": ["腹痛","嘔吐","悪心","下痢","便秘","食欲低下","摂取不良","脱水"],
        "神経": ["意識低下","傾眠","見当識低下","不穏","せん妄","頭痛","しびれ"],
        "皮膚": ["褥瘡","発赤","びらん","創部痛","浸出液"],
        "排泄": ["排尿痛","頻尿","尿閉","血尿","失禁"],
        "活動": ["歩行困難","ADL低下","倦怠感","脱力"],
        "睡眠": ["不眠","中途覚醒","睡眠障害"],
        "心理": ["不安","抑うつ","恐怖","意欲低下"],
        "安全": ["転倒","転落","誤嚥","窒息"],
    }
    for dom, kws in symptom_map.items():
        if any(k in t for k in kws):
            now_hits.append(f"{dom}領域の問題（{ '・'.join([k for k in kws if k in t][:4]) }）")

    risk_map = {
        "感染リスク": ["免疫低下","糖尿病","中心静脈","発熱","高体温","白血球","抗菌薬"],
        "転倒・転落リスク": ["ふらつき","歩行不安定","高齢","眠気","鎮静","視力低下","夜間頻尿"],
        "誤嚥リスク": ["嚥下","咳嗽","むせ","意識低下","麻痺"],
        "褥瘡リスク": ["長時間臥床","低栄養","体圧","発赤","寝たきり","体重減少"],
        "VTEリスク": ["長期臥床","手術後","浮腫","片麻痺","経口摂取不良"],
        "脱水リスク": ["摂取不良","食欲低下","下痢","嘔吐","尿量低下"],
        "低栄養リスク": ["食欲低下","摂取不良","体重減少","Alb","飲酒","外食"],
    }
    for label, kws in risk_map.items():
        if any(k in t for k in kws): pot_hits.append(label)

    if v.get("SpO2") is not None and v["SpO2"] < 94: pot_hits.append("低酸素血症の進行リスク")
    if v.get("SBP") is not None and v["SBP"] <= 100: pot_hits.append("循環不全の進行リスク")
    if v.get("MAP") is not None and v["MAP"] < 65: pot_hits.append("臓器低灌流リスク")

    if any(k in low for k in ("家族","支援","協力","介護力","相談")):
        pos_hits.append("家族/支援体制あり：継続活用")
    if any(k in low for k in ("理解","学習意欲","セルフケア","自己管理")):
        pos_hits.append("自己管理意欲あり：教育効果が期待できる")

    for d in (nanda_list or [])[:10]:
        now_hits.append(f"NANDA候補: {d['label']} [{d.get('code','')}]")

    def uniq(xs):
        seen=set(); out=[]
        for x in xs:
            if x and x not in seen:
                seen.add(x); out.append(x)
        return out

    return {"current": uniq(now_hits), "potential": uniq(pot_hits), "promotion": uniq(pos_hits)}

# ===================== 目標 =====================
def build_goals(v: Dict[str,float|None], assess_text: str, priority_level: str) -> Dict[str,List[str]]:
    short, long = [], []
    t = norm(assess_text)
    if v.get("NRS") is not None and v["NRS"] >= 4:
        short.append("24時間以内に疼痛NRS≦3（介入30–60分後の再評価）")
        long.append("退院時までに疼痛がADLを妨げない（NRS≦2）")
    if v.get("SpO2") is not None and v["SpO2"] < 94:
        short.append("24時間以内に安静時SpO₂≧94％（必要最小のO₂流量）")
        long.append("2週間以内に労作時もSpO₂≧94％を維持")
    if v.get("MAP") is not None and v["MAP"] < 65:
        short.append("6時間以内にMAP≧65mmHgを達成")
        long.append("起立・歩行後もSBP≧100mmHgを維持")
    if any(k in t for k in ("食欲低下","摂取不良","体重減少","脱水")):
        short.append("48時間以内に脱水兆候を改善（口腔湿潤・尿色淡黄）")
        long.append("2週間以内に必要摂取量を達成（例：1,400–1,800kcal/日）")
    if any(k in t for k in ("ふらつき","歩行困難","adl低下","倦怠感")):
        short.append("72時間以内にベッド⇄トイレ移乗が見守りで安全に実施")
        long.append("1～2週間で病棟内30mの自立/監視下歩行")
    if any(k in t for k in ("不眠","中途覚醒","睡眠障害")):
        short.append("1週間以内に入眠30分以内・中途覚醒≦1回/夜")
        long.append("退院時までに睡眠衛生が自立")
    if priority_level == "高" and not short:
        short.append("まずABCの安定化を最優先（赤旗是正と頻回評価）")
    return {"short": short or ["（短期目標の抽出根拠が不足）"],
            "long":  long  or ["（長期目標の抽出根拠が不足）"]}

# ===================== O-P / T-P / E-P =====================
def build_observation_plan(priority_level: str, v: Dict[str,float|None], assess_text: str) -> List[str]:
    freq = "15–30分" if priority_level=="高" else ("1–2時間" if priority_level=="中" else "4–6時間")
    plan = [
        f"バイタルサイン（{freq}ごと／悪化時は即時）",
        "SpO₂モニタ、RR・呼吸パターン、咳・痰性状",
        "血圧（臥位/座位/立位）・MAP、末梢冷感/毛細血管再充満",
        "疼痛NRS：介入後30–60分で再評価、部位/性状/誘因",
        "水分出納（飲水・尿量・尿色・便回数/性状）、体重、口腔粘膜",
        "意識レベル/せん妄兆候、睡眠状況、転倒リスク指標",
        "創部/皮膚：発赤/浸出/圧迫部、デバイス圧迫部位",
        "必要に応じ検査：血算/CRP/電解質/腎肝機能、栄養指標（Alb/PreAlb）",
    ]
    t = norm(assess_text)
    if (v.get("SpO2") and v["SpO2"]<94) or any(k in t for k in ("呼吸困難","咳","痰")):
        plan.append("聴診（ラ音/喘鳴/分泌物），体位での呼吸変化")
    if any(k in t for k in ("せん妄","不穏","見当識低下")):
        plan.append("CAM-ICU等のスクリーニングを定時実施")
    return plan

def build_assistance_plan(v: Dict[str,float|None], assess_text: str, nanda_list: List[Dict[str,Any]], priority_level: str) -> List[str]:
    t = norm(assess_text); xs: List[str] = []
    if (v.get("SpO2") and v["SpO2"]<94) or any(k in t for k in ("呼吸困難","咳","痰")):
        xs += ["呼吸介助：安楽体位（セミファウラー/側臥位），呼吸理学療法（口すぼめ/深呼吸/咳介助）",
               "必要最小の酸素投与/湿化（医師指示），吸入/排痰介助（体位ドレナージ）"]
    if (v.get("MAP") and v["MAP"]<65) or (v.get("SBP") and v["SBP"]<90):
        xs += ["循環管理：補液/昇圧薬調整（医師指示），体位変換時の血圧変動に注意",
               "ショック徴候の監視（皮膚冷感/意識/尿量）と即時報告"]
    if v.get("NRS") and v["NRS"]>=4:
        xs += ["疼痛マネジメント：冷温罨法/体位調整/環境調整（閑静・遮光），医師と鎮痛薬調整",
               "非薬物的鎮痛（呼吸法/気晴らし/音楽等）＋鎮痛後の早期離床を促進"]
    if any(k in t for k in ("食欲低下","摂取不良","体重減少","脱水","飲酒","外食")):
        xs += ["栄養・水分：少量頻回，嚥下・嗜好に合わせた形態調整，必要時補助食品（栄養士と協働）",
               "経口困難持続時は少量補液/点滴計画を医師と協議，I/Oと体重で効果判定"]
    if any(k in t for k in ("ふらつき","歩行困難","adl低下","転倒","転落")):
        xs += ["離床/歩行リハ：段階的ゴール（見守り→監視→自立），補助具選択",
               "転倒・転落予防：環境整備，ナースコール教育，夜間導線/センサー活用"]
    if any(k in t for k in ("褥瘡","発赤","びらん","創部")):
        xs += ["体位変換q2-3h，体圧分散マット，保湿/保護，滲出量に応じたドレッシング"]
    if any(k in t for k in ("頻尿","排尿痛","便秘","下痢","失禁")):
        xs += ["排泄援助：トイレ誘導スケジュール化，便性状評価，必要時下剤/止瀉薬の連携"]
    if any(k in t for k in ("不安","抑うつ","独居","介護力","家族")):
        xs += ["心理的支援：不安の言語化・意思決定支援，必要時MSW/地域包括と連携",
               "退院調整：在宅サービス/家族教育/地域資源活用を早期に開始"]
    for d in (nanda_list or [])[:3]:
        xs.append(f"NANDA:{d['label']} に沿うケアを医師/リハ/栄養/薬剤と協働して具体化")
    return xs or ["（援助計画の抽出根拠が不足）"]

def build_education_plan(assess_text: str, nanda_list: List[Dict[str,Any]]) -> List[str]:
    t = norm(assess_text)
    edu = ["疾患/治療の理解：病態・薬の目的/副作用・受診目安（Teach-back）",
           "薬物療法：服薬時間/飲み忘れ対策，副作用時の対応，自己中断のリスク",
           "セルフモニタ：体温/SpO₂/脈拍/血圧/体重/飲水・尿量の記録法"]
    if any(k in t for k in ("食欲低下","摂取不良","飲酒","外食","体重減少","低栄養")):
        edu.append("栄養：少量頻回・バランス・水分摂取/禁酒減酒・嚥下に合わせた工夫（栄養士と連携）")
    if any(k in t for k in ("呼吸困難","咳","痰","喘鳴")):
        edu.append("呼吸：呼吸法（口すぼめ/腹式），排痰法，体位での楽な呼吸")
    if any(k in t for k in ("転倒","ふらつき","歩行困難")):
        edu.append("安全：転倒予防（環境整備・動作手順・コールのタイミング）")
    if any(k in t for k in ("不安","抑うつ","せん妄","不眠")):
        edu.append("心理/睡眠衛生：刺激コントロール・日中活動・寝室環境・カフェイン/アルコール調整")
    if nanda_list:
        edu.append(f"NANDA:{nanda_list[0]['label']} に対する在宅セルフケア要点（Teach-back）")
    return edu

# ===================== 連携/リスク/評価 =====================
def build_collaboration_and_risk(assess_text: str, v: Dict[str,float|None]) -> Tuple[List[str], List[str]]:
    collab = ["医師（治療方針/鎮痛/酸素/補液/検査）",
              "栄養士（必要量/食形態/補助食品）",
              "リハ（離床・歩行/呼吸理学療法）",
              "薬剤師（副作用/相互作用/服薬アドヒアランス）",
              "MSW/地域包括（在宅支援/介護サービス/家族支援）"]
    risk = ["感染（手指衛生/デバイス管理）","転倒・転落","褥瘡","VTE","誤嚥・窒息","せん妄","薬剤有害事象"]
    flags = []
    if v.get("SpO2") and v["SpO2"]<90: flags.append("SpO₂<90%")
    if v.get("MAP") and v["MAP"]<65: flags.append("MAP<65mmHg")
    if v.get("SBP") and v["SBP"]<90: flags.append("SBP<90mmHg")
    if v.get("NRS") and v["NRS"]>=7: flags.append("NRS≥7")
    if flags: risk = [f"【赤旗】{', '.join(flags)}"] + risk
    return collab, risk

def build_evaluation_items(v: Dict[str,float|None], priority_level: str) -> List[str]:
    review = "q24h" if priority_level=="低" else ("q8-12h" if priority_level=="中" else "q2-4h")
    return [
        f"見直し周期：{review}（優先度変化/新規問題出現で随時更新）",
        "主要KPI：SpO₂（安静/労作）・RR・HR・SBP/MAP・NRS・I/O・体重・歩行距離・睡眠指標・栄養/検査値",
        "介入後の即時再評価：鎮痛後30–60分、酸素/体位/補液変更後15–30分",
        "退院調整KPI：自宅環境/家族支援/在宅サービス導入状況/教育達成（Teach-back）",
    ]

# ===================== 紙フォーマット風（O-P/T-P/E-P） =====================
def sheet_format(patient_name: str,
                 problem_title: str,
                 short_goals: List[str],
                 long_goals: List[str],
                 op: List[str], tp: List[str], ep: List[str]) -> str:
    """掲示物に貼れるような一枚サマリをテキストで成形"""
    def numlist(xs):
        return "\n".join([f"{i}. {x}" for i,x in enumerate(xs,1)]) if xs else "（記載なし）"
    today = datetime.now().strftime("%Y/%m/%d")
    hdr = [
        "",
        "="*96,
        "看護計画（シート要約／O-P・T-P・E-P）",
        "="*96,
        f"患者名：{patient_name or '未評価'}    評価日：{today}",
        f"看護問題：{problem_title or '未評価'}",
        f"長期目標：{(long_goals[0] if long_goals else '未評価')}",
        f"短期目標：{(short_goals[0] if short_goals else '未評価')}",
        "",
        "【計画内容】",
        "O-P（観察）",
        numlist(op),
        "",
        "T-P（援助）",
        numlist(tp),
        "",
        "E-P（教育）",
        numlist(ep),
    ]
    return "\n".join(hdr)

# ===================== 本文生成 =====================
def render_careplan(assess_text: str, diag_text: str) -> str:
    v = parse_vitals(assess_text)
    prio_level, news_like = assess_priority(v, assess_text)
    nanda_list = parse_nanda_from_diag(diag_text) if diag_text else []

    problems = extract_problems(assess_text, nanda_list, v)
    goals    = build_goals(v, assess_text, prio_level)
    observe  = build_observation_plan(prio_level, v, assess_text)
    assist   = build_assistance_plan(v, assess_text, nanda_list, prio_level)
    edu      = build_education_plan(assess_text, nanda_list)
    collab, risk = build_collaboration_and_risk(assess_text, v)
    evals    = build_evaluation_items(v, prio_level)

    def j(xs): return "\n".join(f"  - {x}" for x in xs if x)

    header = [
        "="*96,
        f"看護計画（自動生成）  {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "="*96,
        f"優先度：{prio_level}（スコア目安 {news_like}）",
        "主要バイタル: " + " / ".join([
            f"体温:{v['T']:.1f}℃" if v.get("T") is not None else "体温:-",
            f"脈拍:{int(v['HR'])}/分" if v.get("HR") is not None else "脈拍:-",
            f"呼吸数:{int(v['RR'])}/分" if v.get("RR") is not None else "呼吸数:-",
            f"SpO₂:{int(v['SpO2'])}%" if v.get("SpO2") is not None else "SpO₂:-",
            (f"血圧:{int(v['SBP'])}/{int(v['DBP'])}mmHg" if (v.get('SBP') is not None and v.get('DBP') is not None) else "血圧:-"),
            (f"MAP:{v['MAP']:.1f}mmHg" if v.get("MAP") is not None else "MAP:-"),
            (f"NRS:{int(v['NRS'])}" if v.get("NRS") is not None else "NRS:-"),
        ])
    ]

    body = [
        "\n【1. 看護問題（抽出）】",
        "  ＜現在の問題＞",
        j(problems["current"]) or "  - （抽出なし）",
        "  ＜潜在的問題（リスク）＞",
        j(problems["potential"]) or "  - （抽出なし）",
        "  ＜健康増進・良い傾向＞",
        j(problems["promotion"]) or "  - （抽出なし）",

        "\n【2. 看護目標（SMART・患者目線）】",
        "  ＜短期（～数日）＞",
        j(goals["short"]),
        "  ＜長期（～退院/数週）＞",
        j(goals["long"]),

        "\n【3. 観察計画（O-P）】",
        j(observe),

        "\n【4. 援助計画（看護介入）】",
        j(assist),

        "\n【5. 教育計画（患者/家族・Teach-back）】",
        j(edu),

        "\n【6. 連携・調整】",
        j(collab),

        "\n【7. リスク管理/安全（赤旗優先）】",
        j(risk),

        "\n【8. 評価指標と見直し】",
        j(evals),

        "\n【9. 共有/引継ぎ】",
        "  - 本計画は診療方針/ケアプランに準拠。新規所見や患者希望の変化に応じて即時アップデート。",
        "  - 多職種カンファレンスでの合意内容を反映し、家族/在宅関係者へ共有。",
    ]

    # —— 紙フォーマット風まとめ —— 
    patient_name = "未評価"
    title = nanda_list[0]["label"] if nanda_list else (problems["current"][0] if problems["current"] else "未評価")
    sheet = sheet_format(
        patient_name=patient_name,
        problem_title=title,
        short_goals=goals["short"][:2],
        long_goals=goals["long"][:2],
        op=observe[:10],
        tp=assist[:10],
        ep=edu[:10],
    )

    footer = [
        "\n— ソース —",
        "  assessment_result.txt / assessment_final.txt（アセスメント全文）",
        "  diagnosis_result.txt  / diagnosis_final.txt（NANDA候補・優先順）",
        sheet
    ]

    return "\n".join(header + body + footer) + "\n"

# ===================== main =====================
def main():
    # ★FINAL優先で読み込み
    try:
        assess_text = read_assessment_for_careplan(ARGS.assess)
    except Exception as e:
        print(e); return
    diag_text = read_diagnosis_for_careplan(ARGS.diag)

    result = render_careplan(assess_text, diag_text)
    print(result)
    Path(ARGS.out).write_text(result, encoding="utf-8")
    log(f"[SAVE] {Path(ARGS.out).resolve()}")

if __name__ == "__main__":
    main()
