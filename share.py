"""分享給不同網路的人：啟動展示網站，並用 Cloudflare 建立臨時公開網址（https）。

雙擊「分享給別人（臨時網址）.bat」執行。
- 不需要 Cloudflare 帳號；網址每次啟動都不一樣，關掉黑色視窗就失效。
- 一定會有展示密碼：config.json 沒設的話，每次啟動自動產生一組。
- 網站仍然跑在這台電腦上（本機 AI 可用），外面的人是透過 Cloudflare 連進來。
- 每次執行的完整過程記錄在 data\\share_log.txt（不含金鑰與密碼），出問題時可以直接看這個檔。
"""
import argparse
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

import app as appmod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLOUDFLARED = os.path.join(BASE_DIR, "tools", "cloudflared.exe")
# Cloudflare 官方 GitHub 發布的 2026.9.3 版 cloudflared-windows-amd64.exe 的 SHA256（發布頁與 API 兩處一致）
OFFICIAL_SHA256 = "f096265ec2fcbe9bb6e2d64268db167ced3fcbb83d894bdb9e2fcdb26f2ea7e2"
# 臨時網址是「幾個英文字用連字號串起來」.trycloudflare.com；排除 api.trycloudflare.com 等其他網址
URL_RE = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com(?![-a-z0-9.])")
ATTEMPTS = 3

_LOG = None
_LOG_PATH = ""
_JOB = None


def log(msg):
    if _LOG:
        _LOG.write(time.strftime("%H:%M:%S ") + msg + "\n")
        _LOG.flush()


def say(msg=""):
    print(msg, flush=True)
    log(msg)


def kill_with_parent(proc):
    """Windows：把 cloudflared 放進 Job Object。這個程式一結束（包含黑色視窗被直接關掉、被強制結束），
    系統就會一併結束 cloudflared，不會留下還通著的舊網址。"""
    global _JOB
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    if _JOB is None:
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9 = ExtendedLimitInformation
            return False
        _JOB = job  # 保持開著；本程式結束時系統自動關閉 → cloudflared 跟著結束
    return bool(k32.AssignProcessToJobObject(_JOB, int(proc._handle)))


def sha256_of(path):
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def start_tunnel(port, wait_s=45):
    """啟動一次 cloudflared。成功回傳 (proc, url, "")；失敗回傳 (None, "", 原因代碼)。"""
    proc = subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    if not kill_with_parent(proc):
        log("無法把 cloudflared 放進 Job Object（結束時可能要手動確認沒有殘留）")
    found = threading.Event()
    lines = []
    result = {"url": ""}

    def reader():
        for line in proc.stdout:
            line = line.rstrip()
            lines.append(line)
            log("[cloudflared] " + line)
            m = URL_RE.search(line)
            if m and not found.is_set():
                result["url"] = m.group(0)
                found.set()

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if found.wait(0.5):
            return proc, result["url"], ""
        if proc.poll() is not None:
            break
    if proc.poll() is None:
        proc.terminate()
    text = "\n".join(lines[-20:]).lower()
    if "429" in text or "too many requests" in text or "1015" in text or "rate limit" in text:
        return None, "", "rate"
    if any(k in text for k in ("no such host", "dial tcp", "i/o timeout", "network is unreachable", "connection refused")):
        return None, "", "network"
    return None, "", "unknown"


def main():
    global _LOG, _LOG_PATH
    from werkzeug.serving import make_server

    ap = argparse.ArgumentParser(description="用臨時公開網址分享展示網站")
    ap.add_argument("--port", type=int, help="改用別的埠（測試用）")
    ap.add_argument("--data-dir", help="改用別的資料夾（測試用）")
    ap.add_argument("--no-browser", action="store_true", help="不要自動開瀏覽器")
    args = ap.parse_args()

    data_dir = args.data_dir or os.path.join(BASE_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    _LOG_PATH = os.path.join(data_dir, "share_log.txt")
    _LOG = open(_LOG_PATH, "w", encoding="utf-8")
    log(f"開始分享｜Python {sys.version.split()[0]}｜{sys.executable}")

    if not os.path.exists(CLOUDFLARED):
        say("\n[錯誤] 找不到 tools\\cloudflared.exe。")
        say("這是 Cloudflare 官方的連線工具，請從 https://github.com/cloudflare/cloudflared/releases 下載")
        say("cloudflared-windows-amd64.exe，改名為 cloudflared.exe 放進 tools 資料夾。")
        return 1
    if sha256_of(CLOUDFLARED) != OFFICIAL_SHA256:
        say("\n[錯誤] tools\\cloudflared.exe 的檔案指紋和 Cloudflare 官方 2026.9.3 版不一樣，")
        say("可能是下載不完整、被防毒軟體改動，或檔案被換掉；為了安全不執行。請重新下載官方檔案。")
        return 1
    try:
        cfg = appmod.load_config()
    except ValueError as e:
        say(f"\n[錯誤] config.json 格式有誤（常見原因：少了逗號或引號）：{e}")
        return 1
    if args.port:
        cfg["port"] = args.port
    if args.no_browser:
        cfg["open_browser"] = False
    port = int(cfg.get("port", 5800))

    code = str(cfg.get("access_code") or "").strip()
    generated = not code
    if generated:  # 分享到外面一定要有密碼
        code = "toyota" + "".join(secrets.choice("23456789") for _ in range(4))
        cfg["access_code"] = code
    host_code = str(cfg.get("host_code") or "").strip()
    if host_code == code:
        say("[提醒] config.json 的 host_code 和 access_code 一樣，分不出主持人和一般試用者，這次改用自動產生的主持人密碼。")
        host_code = ""
    host_generated = not host_code
    if host_generated:  # 給要一起管理展示的隊友：較長、較難猜
        host_code = "host-" + "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))
    cfg["host_code"] = host_code

    hosts, lan_ip, note = appmod.resolve_hosts(cfg.get("host"))
    log(f"埠 {port}｜綁定 {hosts}｜AI 設定 {cfg.get('ai', {}).get('mode')}")
    if any(not appmod.port_free(h, port) for h in hosts):
        say(f"\n[錯誤] {port} 埠已經被佔用。")
        say("最常見的原因：「啟動展示網站」或另一個分享視窗已經開著。請先把那個黑色視窗關掉，再重新執行分享。")
        return 1

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    say("正在準備示範資料與預測模型……")
    share = {"url": "", "code": code, "status": "starting", "generated": generated,
             "host_code": host_code, "host_generated": host_generated}
    cfg["_share"] = share
    cfg["_bound"] = {"lan_ip": lan_ip, "note": note}
    app = appmod.create_app(cfg, data_dir=args.data_dir)
    for srv in [make_server(h, port, app, threaded=True) for h in hosts]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    if cfg.get("open_browser", True):
        webbrowser.open(f"http://localhost:{port}/")  # 展示入口上方的卡片會顯示分享進度

    proc, url = None, ""
    for attempt in range(1, ATTEMPTS + 1):
        say(f"正在向 Cloudflare 申請臨時公開網址（第 {attempt} 次，約 5–20 秒）……")
        proc, url, reason = start_tunnel(port)
        if proc:
            break
        if reason == "rate":
            say("Cloudflare 暫時限制建立新網址（短時間內建立太多次），這是正常的保護機制。")
        elif reason == "network":
            say("連不上 Cloudflare：請確認這台電腦有網路。")
        else:
            say("這次沒有拿到網址。")
        if attempt < ATTEMPTS:
            wait = 20 * attempt
            say(f"{wait} 秒後自動重試……")
            time.sleep(wait)

    if not proc:
        share["status"] = "failed"
        say(f"\n[錯誤] 試了 {ATTEMPTS} 次都沒有拿到公開網址。")
        say(f"網站在這台電腦上仍然可以用：http://localhost:{port}")
        say(f"詳細紀錄：{_LOG_PATH}")
        say("可以過幾分鐘再關掉這個視窗重新執行一次。")
        while True:
            time.sleep(3600)

    share["url"] = url
    share["status"] = "ready"
    st = app.engine.status()
    say("\n==================================================")
    say("  看得見的保養｜臨時公開網址已建立")
    say("==================================================")
    say(f"  給別人的網址：{url}")
    say(f"  車主頁直接連：{url}/c/V001")
    print(f"  展示密碼　　：{code}" + ("（這次啟動自動產生；想固定請在 config.json 設 access_code）" if generated else ""), flush=True)
    print(f"  主持人密碼　：{host_code}（只給要一起管理的隊友：可重置、切換 AI；不要給一般試用者）", flush=True)
    say("  （剛建立的網址偶爾要等 10–30 秒才打得開）")
    say(f"\n  你自己在這台電腦開：http://localhost:{port}（不用輸入密碼）")
    say(f"  AI 模式：{st['label']}")
    say("\n  關閉這個視窗，網站和公開網址都會一起停止。")
    say("==================================================\n")
    app.engine.warmup_async()
    try:
        while proc.poll() is None:
            time.sleep(1)
        share["status"] = "failed"
        say("\n[提醒] 臨時公開網址的連線中斷了。網站在這台電腦上還能用；要再分享，請關掉視窗重新執行。")
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        rc = 0
    except Exception:  # noqa: BLE001  任何預期外的錯誤都要留下紀錄
        tb = traceback.format_exc()
        print("\n[錯誤] 程式發生預期外的錯誤：\n" + tb, flush=True)
        log(tb)
        if _LOG_PATH:
            print(f"詳細紀錄：{_LOG_PATH}")
        rc = 1
    sys.exit(rc)
