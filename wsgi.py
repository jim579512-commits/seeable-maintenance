"""雲端主機（Render）用的入口：gunicorn wsgi:app

在自己電腦上展示請照舊雙擊「啟動展示網站.bat」，不會用到這個檔案。
雲端版的密碼與金鑰都在 Render 的 Environment 頁面設定（見 app.cloud_config），不會放進 GitHub。
"""
import os
import time

if hasattr(time, "tzset"):  # Linux 雲端主機預設是 UTC：改成台灣時間，預約時間、預估完工時間才會對
    os.environ.setdefault("TZ", "Asia/Taipei")
    if os.environ["TZ"] == "Asia/Taipei" and not os.path.exists("/usr/share/zoneinfo/Asia/Taipei"):
        os.environ["TZ"] = "CST-8"  # 主機沒有時區資料時直接指定 UTC+8（台灣沒有日光節約時間）
    time.tzset()

from app import cloud_config, create_app  # noqa: E402

app = create_app(cloud_config(os.environ), data_dir=os.environ.get("DATA_DIR") or None)
