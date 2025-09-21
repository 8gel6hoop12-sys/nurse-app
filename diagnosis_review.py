# -*- coding: utf-8 -*-
"""
diagnosis_review.py
選択された診断（stdin）を diagnosis_candidates.json と突き合わせ、
詳細（定義/AI順位・類似度・スコア/曖昧一致/根拠/メタ情報）を整形して
diagnosis_final.txt へ保存する。
stdin 形式の例:
- [x] 384    息苦し自己管理不足
- [x] 448    気分不穏

※ nurse_app.py からは「- [x] {code}\t{label}」で渡る前提
"""

from __future__ import annotations
import sys, re, json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Tuple

DIAG_JSON       = "diagnosis_candidates.json"
DIAG_FINAL_TXT  = "diagnosis_final.txt"

def read_text(p: Path) -> str:
    if not p.exists(): return ""
    try:    return p.read_text(encoding="utf-8")
    except: return p.read_text(encoding="utf-8", errors="ignore")

def write_text(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8", errors="ignore")

def parse_selected(stdin_text: str) -> List[Tuple[str, str]]:
    """- [x] CODE<TAB or spaces>LABEL を抽出"""
    out: List[Tuple[str,str]] = []
    for ln in stdin_text.splitlines():
        m = re.match(r'\s*[-*]\s*\[(?:x|X)\]\s*([^\s\t]+)\s+(.+)$', ln.strip())
        if m:
            code = m.group(1).strip()
            label = m.group(2).strip()
            out.append((code, label))
    # 重複除去（順序保持）
    seen = set(); uniq = []
    for k in out:
        if k not in seen:
            seen.add(k); uniq.append(k)
    return uniq

def load_candidates() -> Dict[str, Any]:
    p = Path(DIAG_JSON)
    if not p.exists():
        return {"meta": {}, "candidates": []}
    try:
        return json.loads(read_text(p))
    except Exception:
        return {"meta": {}, "candidates": []}

def pick_candidate(code: str, label: str, cands: List[Dict[str,Any]]) -> Dict[str,Any] | None:
    """code/label で最も ai_rank の小さいものを返す"""
    # 1) code 完全一致を優先
    cs = [c for c in cands if str(c.get("code","")) == str(code)]
    if cs:
        return sorted(cs, key=lambda x: int(x.get("ai_rank", 999999)))[0]
    # 2) label 完全一致
    ls = [c for c in cands if str(c.get("label","")).strip() == label.strip()]
    if ls:
        return sorted(ls, key=lambda x: int(x.get("ai_rank", 999999)))[0]
    # 3) label の部分一致（ゆるめ）
    lbl = label.replace(" ", "")
    ps = [c for c in cands if lbl and lbl in str(c.get("label","")).replace(" ","")]
    if ps:
        return sorted(ps, key=lambda x: int(x.get("ai_rank", 999999)))[0]
    return None

def join_list(xs: List[str], bullet: str="・") -> str:
    xs = [str(x).strip() for x in xs if str(x).strip()]
    return bullet.join(xs)

def build_entry_block(c: Dict[str,Any], idx: int) -> str:
    """1件分の詳細ブロックを日本語で整形"""
    code = str(c.get("code",""))
    label = str(c.get("label",""))
    ai_rank = c.get("ai_rank","")
    ai_sim  = float(c.get("ai_sim",0.0))
    score   = float(c.get("score",0.0))

    lines: List[str] = []
    lines.append(f"{idx}. [{code}] {label}")
    if c.get("definition"):
        lines.append(f"    定義: {c['definition']}")
    lines.append(f"    AI順位: {ai_rank} / AI類似度: {ai_sim:.3f} / スコア: {score:.1f}")

    # 曖昧一致
    L = c.get("loose") or {}
    buf = []
    if L.get("診断指標"): buf.append("診断指標: " + join_list(L["診断指標"]))
    if L.get("関連因子"): buf.append("関連因子: " + join_list(L["関連因子"]))
    if L.get("危険因子"): buf.append("危険因子: " + join_list(L["危険因子"]))
    if L.get("定義語"):   buf.append("定義語: "   + join_list(L["定義語"]))
    if buf:
        lines.append("    曖昧一致:")
        for b in buf:
            lines.append("      - " + b)

    # スコア根拠 / AI根拠
    if c.get("reasons"):
        lines.append("    スコア根拠:")
        for r in c["reasons"][:10]:
            lines.append("      - " + str(r))
    if c.get("ai_ev"):
        lines.append("    AI根拠:")
        for ln in str(c["ai_ev"]).splitlines():
            if ln.strip():
                lines.append("      " + ln.strip())

    # メタ情報
    meta_parts = []
    def addm(j, k):
        v = c.get(k, "")
        if v: meta_parts.append(f"{j}:{v}")
    addm("一次焦点","primary_focus"); addm("二次焦点","secondary_focus"); addm("ケア対象","care_target")
    addm("解剖学的部位","anatomical_site"); addm("年齢下限","age_min"); addm("年齢上限","age_max")
    addm("臨床経過","clinical_course"); addm("診断の状態","diagnosis_state"); addm("状況的制約","situational_constraints")
    addm("領域","domain"); addm("分類","class"); addm("判断","judge")
    if meta_parts:
        lines.append("    メタ情報: " + " / ".join(meta_parts))

    return "\n".join(lines)

def main():
    # 1) 入力の選択リストを取得
    selected = parse_selected(sys.stdin.read())
    if not selected:
        # 空でも final は空書き出し（間違って保存したときのクリア用）
        write_text(Path(DIAG_FINAL_TXT), "")
        print("OK (no selection)"); return

    # 2) 候補JSONを読み込み、対応づけ
    data = load_candidates()
    cands: List[Dict[str,Any]] = data.get("candidates", [])

    enriched: List[Dict[str,Any]] = []
    for code, label in selected:
        c = pick_candidate(code, label, cands)
        if c is None:
            # JSONに見つからない場合は最小情報で作る
            c = {"code": code, "label": label, "ai_rank": 999999, "ai_sim": 0.0, "score": 0.0}
        enriched.append(c)

    # 3) AI順位の昇順に整列（元の要件）
    enriched.sort(key=lambda x: int(x.get("ai_rank", 999999)))

    # 4) ヘッダ + 本文を組み立て
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = [
        "===== 診断（確定版） =====",
        f"作成: {ts}",
        f"候補JSON: {DIAG_JSON} / 件数: {len(enriched)}",
    ]
    if isinstance(data.get("meta"), dict) and data["meta"]:
        meta_lines = []
        for k,v in data["meta"].items():
            meta_lines.append(f"  - {k}: {v}")
        header.append("ソース情報:")
        header.extend(meta_lines)

    blocks = []
    for i, c in enumerate(enriched, start=1):
        blocks.append(build_entry_block(c, i))

    final_text = "\n".join(header) + "\n\n" + "\n\n".join(blocks) + "\n"

    # 5) 保存
    write_text(Path(DIAG_FINAL_TXT), final_text)

    # 6) 端末用の軽い完了通知
    print("OK")

if __name__ == "__main__":
    main()
