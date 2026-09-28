"""打包一份給隊友在他們自己電腦上執行的壓縮檔。

會自動排除／清掉：
- data 資料夾（目前的示範資料、你上傳的照片、分享紀錄、登入用的密鑰）
- config.json 裡的 Gemini 金鑰、展示密碼、主持人密碼（隊友要用自己的）
- Python 暫存檔（__pycache__、.pyc）
"""
import json
import os
import sys
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(BASE_DIR), "看得見的保養_給隊友.zip")
TOP = "看得見的保養"
SKIP_DIRS = {"data", "__pycache__", ".git", ".venv"}


def sanitized_config(path):
    with open(path, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    cfg.setdefault("ai", {})["gemini_api_key"] = ""
    cfg["access_code"] = ""
    cfg["host_code"] = ""
    return json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"


def main():
    count, removed = 0, []
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(BASE_DIR):
            for d in list(dirs):
                if d in SKIP_DIRS:
                    removed.append(os.path.relpath(os.path.join(root, d), BASE_DIR))
                    dirs.remove(d)
            for name in files:
                if name.endswith(".pyc"):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, BASE_DIR)
                arc = os.path.join(TOP, rel)
                if rel == "config.json":
                    z.writestr(arc, sanitized_config(full))
                else:
                    z.write(full, arc)
                count += 1
    size_mb = os.path.getsize(OUT) / 1024 / 1024
    print("\n已產生給隊友的壓縮檔：")
    print("  " + OUT)
    print(f"  共 {count} 個檔案，{size_mb:.1f} MB")
    print("\n已經排除或清空（隊友拿不到）：")
    for r in removed:
        print(f"  - {r} 資料夾（示範資料、上傳照片、紀錄）" if r == "data" else f"  - {r}")
    print("  - config.json 裡的 Gemini 金鑰、展示密碼、主持人密碼")
    print("\n隊友拿到後：")
    print("  1. 先安裝 Python 3（python.org，安裝時勾選 Add python.exe to PATH）")
    print("  2. 解壓縮 → 雙擊「安裝需要的套件.bat」（只要第一次）")
    print("  3. 雙擊「啟動展示網站.bat」")
    print("  AI 會依隊友自己的電腦決定：沒有 Gemini 金鑰或本機模型時，會用離線規則模式。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
