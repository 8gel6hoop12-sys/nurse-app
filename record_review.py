# -*- coding: utf-8 -*-
"""
record_review.py — 記録レビュー/確定
入力 : 標準入力（優先）/ record_result.txt
出力 : record_final.txt
処理 : 体裁整形（空白/改行の正規化・重複段落の抑制）
依存 : なし（標準ライブラリ）
"""

from __future__ import annotations
import sys, re
from pathlib import Path

IN_TXT  = Path("record_result.txt")
OUT_TXT = Path("record_final.txt")

def _read_stdin() -> str:
    try:
        data = sys.stdin.read()
        return data if data is not None else ""
    except Exception:
        return ""

def _read_file(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""

def _clean(s: str) -> str:
    if not s:
        return s
    # 全角/半角スペースの連続 → 単一化
    s = re.sub(r"[ \t\u3000]+", " ", s)
    # 行末空白除去
    s = "\n".join(ln.rstrip() for ln in s.splitlines())
    # 同一段落の重複を除去
    paras = [p.strip() for p in s.split("\n\n")]
    seen, out = set(), []
    for p in paras:
        k = p.replace(" ", "")
        if p and k not in seen:
            seen.add(k); out.append(p)
    s = "\n\n".join(out)
    # 見出しの抜けやすい空行を補う（軽め）
    s = re.sub(r"(【青四角.+?】)\n(?!\n)", r"\1\n", s)
    s = re.sub(r"(【三部形式.+?】)\n(?!\n)", r"\1\n", s)
    s = re.sub(r"(【記録からの計画】)\n(?!\n)", r"\1\n", s)
    return s.strip() + "\n"

def main():
    src = _read_stdin().strip()
    if not src:
        src = _read_file(IN_TXT)
    if not src.strip():
        sys.stderr.write("record_review: 入力が空です（stdin / record_result.txt のいずれも空）。\n")
        sys.exit(1)
    out = _clean(src)
    OUT_TXT.write_text(out, encoding="utf-8")
    # 標準出力には軽いサマリのみ
    print(f"[OK] {OUT_TXT.name} に {len(out)} 文字を書き出しました。")

if __name__ == "__main__":
    main()
