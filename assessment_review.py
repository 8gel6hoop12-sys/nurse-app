# -*- coding: utf-8 -*-
# アセスメント結果（assessment_result.txt）を看護師が編集 → assessment_final.txt に保存

import os
from pathlib import Path
from datetime import datetime

SRC = Path("assessment_result.txt")
DST = Path("assessment_final.txt")
BAK_DIR = Path("_backup")

def main():
    if not SRC.exists():
        print("[ERROR] assessment_result.txt が見つかりません。先に assessment.py を実行してください。")
        return

    print("=== Assessment Review ===")
    print(f"- 編集対象: {SRC.resolve()}")
    print(f"- 保存先   : {DST.resolve()}（上書き）")
    print("  → 今から既定のエディタで開きます。編集して保存後、ウィンドウを閉じてください。")
    try:
        # Windows なら既定アプリで開く
        os.startfile(SRC)  # type: ignore[attr-defined]
    except Exception:
        print("（自動起動に失敗。手動でファイルを開いて編集してください）")

    input("編集が終わったら Enter を押してください...")

    # バックアップ
    BAK_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = BAK_DIR / f"assessment_result_{ts}.txt"
    bak.write_text(SRC.read_text(encoding="utf-8"), encoding="utf-8")

    # final へ反映
    DST.write_text(SRC.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[OK] assessment_final.txt に保存しました。バックアップ: {bak.name}")

if __name__ == "__main__":
    main()
