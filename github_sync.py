"""把目前的程式推上 GitHub（雙擊「更新到GitHub.bat」執行）。

第一次會一步一步帶你設定：GitHub 帳號名稱、倉庫網址，並跳出 GitHub 登入視窗。
之後每次改完程式雙擊一次，Render 看到 GitHub 有新版本，就會自動更新固定網址上的網站。

安全檢查：config.json（金鑰、密碼）、data 資料夾、cloudflared、企劃書不會被上傳；
送出前會再掃一次要上傳的內容，出現像 Gemini 金鑰或你設定的密碼就停下來、什麼都不送。
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+?(\.git)?/?$")
KEY_RE = re.compile(rb"AIza[0-9A-Za-z_\-]{30,}")
NEVER_FILES = ("config.json",)
NEVER_DIRS = ("data/", "tools/")
NEVER_EXT = (".docx", ".pdf", ".pptx", ".zip", ".pyc")


class Stop(Exception):
    """停下來、把原因告訴使用者（不是程式錯誤）。"""


def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True)
    out = r.stdout.decode("utf-8", "replace").strip()
    if check and r.returncode != 0:
        raise Stop("git " + " ".join(args[:2]) + " 失敗：" + (r.stderr.decode("utf-8", "replace").strip() or out))
    return out


def git_live(*args):
    """需要登入時用：畫面直接顯示 git 的訊息（GitHub 登入視窗也可能跳出來）。"""
    return subprocess.run(["git", *args], cwd=BASE_DIR).returncode


def ask(prompt, default=""):
    try:
        v = input(prompt).strip()
    except EOFError:
        v = ""
    return v or default


def local_secrets():
    """這台電腦 config.json 裡的金鑰與密碼（只用來比對，不會印出來）。太短的密碼容易誤判，只比對 8 碼以上的。"""
    path = os.path.join(BASE_DIR, "config.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return []
    vals = [(cfg.get("ai") or {}).get("gemini_api_key"), cfg.get("access_code"), cfg.get("host_code")]
    return [v.encode("utf-8") for v in vals if isinstance(v, str) and len(v.strip()) >= 8]


def setup_repo():
    if not os.path.isdir(os.path.join(BASE_DIR, ".git")):
        git("init", "-b", "main")
        print("已在這個資料夾建立 Git 版本紀錄。")
    git("config", "core.quotepath", "false")
    if not git("config", "user.name", check=False):
        print("\n第一次使用：請輸入你的 GitHub 帳號名稱（只用來標示是誰更新的）。")
        name = ask("GitHub 帳號名稱：")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", name):
            raise Stop("帳號名稱只會有英文、數字和 -，請確認後重新執行。")
        git("config", "user.name", name)
        git("config", "user.email", f"{name}@users.noreply.github.com")
    if not git("remote", "get-url", "origin", check=False):
        print("\n請貼上 GitHub 倉庫的網址（在 GitHub 建好空的倉庫後，瀏覽器網址列那一串）")
        print("例如：https://github.com/你的帳號/seeable-maintenance")
        url = ask("倉庫網址：").rstrip("/")
        if not REPO_RE.match(url):
            raise Stop("網址格式不對，應該長得像 https://github.com/帳號/倉庫名稱")
        if not url.endswith(".git"):
            url += ".git"
        git("remote", "add", "origin", url)
        print("已記住倉庫網址：" + url)


def staged_files():
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
                         cwd=BASE_DIR, capture_output=True).stdout
    return [p.decode("utf-8", "replace") for p in out.split(b"\0") if p]


def safety_check():
    files = staged_files()
    bad = [f for f in files if f in NEVER_FILES or f.startswith(NEVER_DIRS) or f.lower().endswith(NEVER_EXT)]
    if bad:
        git("reset", "-q")
        raise Stop("這些檔案不應該上傳，已全部取消，什麼都沒送出：\n  " + "\n  ".join(bad))
    secrets = local_secrets()
    for f in files:
        blob = subprocess.run(["git", "show", ":" + f], cwd=BASE_DIR, capture_output=True).stdout
        if KEY_RE.search(blob) or any(s in blob for s in secrets):
            git("reset", "-q")
            raise Stop(f"「{f}」裡面有看起來像金鑰或密碼的內容，已全部取消，什麼都沒送出。\n"
                       "請把金鑰、密碼只放在 config.json（不會上傳）或 Render 的 Environment。")
    return files


def main():
    if not shutil.which("git"):
        raise Stop("這台電腦沒有安裝 Git。請到 https://git-scm.com 下載安裝（一路按下一步即可），再重新執行。")
    setup_repo()
    git("add", "-A")
    files = safety_check()
    if files:
        print(f"\n這次要上傳 {len(files)} 個有變動的檔案。")
        msg = ask("這次改了什麼（直接按 Enter 會用日期）：", "更新 " + dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        git("commit", "-q", "-m", msg)
    elif git("rev-parse", "--verify", "HEAD", check=False):
        print("\n程式沒有新的變動，檢查 GitHub 是否已經是最新版本……")
    else:
        raise Stop("資料夾裡沒有可以上傳的檔案。")

    print("\n連線到 GitHub（第一次會跳出登入視窗：選「Sign in with your browser」→ 登入 → Authorize）……")
    if git_live("fetch", "origin") != 0:
        raise Stop("連不上 GitHub 倉庫。可能原因：\n"
                   "  1. 登入視窗被關掉或沒有登入完成 → 再執行一次\n"
                   "  2. 倉庫網址打錯 → 用記事本打開這個資料夾的 .git\\config，修改 url 那一行\n"
                   "  3. 登入的 GitHub 帳號沒有這個倉庫的權限")
    remote_main = git("rev-parse", "--verify", "-q", "origin/main", check=False)
    if remote_main and not git("merge-base", "HEAD", "origin/main", check=False):
        # GitHub 上的倉庫不是空的（例如建立時勾了 README）：合併進來，衝突一律以這台電腦的版本為準
        if subprocess.run(["git", "merge", "--allow-unrelated-histories", "-X", "ours", "--no-edit", "-q", "origin/main"],
                          cwd=BASE_DIR).returncode != 0:
            git("merge", "--abort", check=False)
            raise Stop("GitHub 上的倉庫已經有別的內容，無法自動合併。最簡單的做法：在 GitHub 刪掉這個倉庫，"
                       "重新建立一個「什麼都不勾」的空倉庫，再執行一次。")
    elif remote_main:
        # 有隊友先推了新版本：先合併進來再上傳
        if subprocess.run(["git", "merge", "--no-edit", "-q", "origin/main"], cwd=BASE_DIR).returncode != 0:
            git("merge", "--abort", check=False)
            raise Stop("GitHub 上有隊友改過同一段程式，無法自動合併，什麼都沒送出。請找會用 Git 的隊友協助處理。")
    if git_live("push", "-u", "origin", "main") != 0:
        raise Stop("上傳失敗，請看上面 git 的訊息；最常見的是登入沒有完成，再執行一次即可。")
    print("\n完成！GitHub 已經是最新版本。")
    print("Render 會在 3–5 分鐘內自動重新部署；完成後重新整理固定網址，就是新版本。")
    print("（還沒在 Render 建立網站的話，請照《操作說明》第 11 節做一次。）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Stop as e:
        print("\n[停止] " + str(e))
        sys.exit(1)
