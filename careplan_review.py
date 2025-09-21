# -*- coding: utf-8 -*-
# ケアプラン（careplan_result.txt）を看護師が編集 → careplan_final.txt に保存

import os
from pathlib import Path
from datetime import datetime

SRC = Path("careplan_result.txt")
DST = Path("careplan_final.txt")
BAK_DIR = Path("_backup")

def main():
    if not SRC.exists():
        print("[ERROR] careplan_result.txt が見つかりません。先に careplan.py を実行してください。")
        return

    print("=== Careplan Review ===")
    print(f"- 編集対象: {SRC.resolve()}")
    print(f"- 保存先   : {DST.resolve()}（上書き）")
    print("  → 今から既定のエディタで開きます。編集して保存後、ウィンドウを閉じてください。")
    try:
        os.startfile(SRC)  # type: ignore[attr-defined]
    except Exception:
        pass

    input("編集が終わったら Enter を押してください...")

    BAK_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = BAK_DIR / f"careplan_result_{ts}.txt"
    bak.write_text(SRC.read_text(encoding="utf-8"), encoding="utf-8")

    DST.write_text(SRC.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[OK] careplan_final.txt に保存しました。バックアップ: {bak.name}")

if __name__ == "__main__":
    main()
