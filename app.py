"""看得見的保養｜展示網站（Flask）

啟動：雙擊「啟動展示網站.bat」，或執行  py -3 app.py
"""
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from collections import Counter

from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

import offline
from ai import MODE_LABELS, SOURCE_LABELS, AIEngine
from eta import EtaModel
from kb import DONE_STAGE, KB, LABOR_RATE, POWERTRAIN_TEXT, STAGES, TIERS, TOPICS, URGENCY
from seed import SCHEMA, build_seed
from store import Store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "host": "lan",
    "port": 5800,
    "open_browser": True,
    "access_code": "",
    "host_code": "",
    "ai": {
        "mode": "auto",
        "gemini_api_key": "",
        "gemini_models": ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"],
        "ollama_url": "http://127.0.0.1:11434",
        "ollama_model": "qwen2.5-7b-q4:latest",
        "ollama_vision_model": "",  # 本機看圖用的影像模型；空白＝本機模式不看車主照片，只依文字描述判斷
        "timeout_seconds": 60,
    },
}

MEDIA_TYPES = {".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image", ".gif": "image",
               ".mp4": "video", ".mov": "video", ".webm": "video", ".m4v": "video"}
STATUS_TEXT = {"draft": "專員審核中", "sent": "等車主決定", "approved": "車主已同意", "deferred": "車主選擇下次"}
PHOTO_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".heic": "image/heic", ".heif": "image/heif"}
OPTIONAL_LAST_STAGE = 2  # 進入「品質檢查」後就不能再加選自選項目
# 技師獎金試算規則（示意，可調整）：只獎勵被指名、回報品質與車主評價，不以追加金額或同意率計算，避免鼓勵推銷
BONUS_RULES = [
    {"key": "designated", "label": "被車主指名，並由本人施工", "amount": 100, "unit": "次"},
    {"key": "quality", "label": "完整回報（附照片或影片，並有量測值或具體說明）", "amount": 30, "unit": "筆"},
    {"key": "unedited", "label": "回報說明清楚，專員不需修改就送出", "amount": 10, "unit": "筆"},
    {"key": "five_star", "label": "車主給技師 5 顆星", "amount": 50, "unit": "次"},
]
TOPIC_SHORT = {k: v.split("（")[0] for k, v in TOPICS.items()}


def load_config(path=CONFIG_PATH):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            user = json.load(f)
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if k == "ai" and isinstance(v, dict):
                cfg["ai"].update({kk: vv for kk, vv in v.items() if not kk.startswith("_")})
            else:
                cfg[k] = v
    return cfg


def cloud_config(env):
    """雲端主機（Render）用的設定：密碼、金鑰一律從環境變數讀，不放進 GitHub。

    ACCESS_CODE 展示密碼（留空＝拿到網址的人都能看）、HOST_CODE 主持人密碼、GEMINI_API_KEY、
    SECRET_KEY（登入狀態用）、AI_MODE（auto／gemini／offline）。
    """
    def val(key):
        v = (env.get(key) or "").strip()
        return "" if v in ("-", "－") else v  # Render 的欄位不能留白時，填一個減號代表「不設定」

    cfg = load_config()
    cfg["cloud"] = True
    cfg["open_browser"] = False
    cfg["access_code"] = val("ACCESS_CODE")
    cfg["host_code"] = val("HOST_CODE")
    cfg["secret_key"] = val("SECRET_KEY")
    ai = cfg["ai"]
    ai["gemini_api_key"] = val("GEMINI_API_KEY")
    mode = (val("AI_MODE") or "auto").lower()
    ai["mode"] = mode if mode in ("auto", "gemini", "offline") else "auto"
    ai["cloud"] = True  # 雲端沒有本機 Ollama，不用去偵測
    return cfg


def hm(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return d.replace(year=d.year + y, month=m + 1, day=1)


PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


def private_ips():
    """只找區網（私有網段）的 IP。

    刻意排除：公開 IP（寬頻撥接時電腦會直接拿到公開 IP，綁上去等於開放給整個網際網路）、
    169.254.x.x（沒連上的網卡）、Radmin VPN 的 26.x.x.x。
    """
    cands = []
    try:  # 先放「對外連線」會用的那張網卡
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        cands.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            cands.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in cands:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if any(addr in n for n in PRIVATE_NETS) and ip not in out:
            out.append(ip)
    return out


def resolve_hosts(host_setting):
    """回傳 (要綁定的位址清單, 手機網址用的區網 IP 或 None, 給使用者看的提醒)。"""
    h = (host_setting or "lan").strip().lower()
    if h in ("127.0.0.1", "localhost"):
        return ["127.0.0.1"], None, "目前設定只開放給這台電腦（config.json 的 host 是 127.0.0.1）。"
    if h == "0.0.0.0":
        return ["0.0.0.0"], (private_ips() or [None])[0], (
            "注意：host 設成 0.0.0.0 會開放給電腦上所有網路。用寬頻撥接上網時，網路上任何人都可能連進來，建議改回 lan。")
    if h == "lan":
        ips = private_ips()
        if ips:
            return ["127.0.0.1", ips[0]], ips[0], ""
        return ["127.0.0.1"], None, (
            "目前沒有連上 Wi-Fi 或手機熱點（區網），所以只開放給這台電腦。要用手機展示：讓電腦和手機連同一個 Wi-Fi 或熱點，再重新啟動網站。")
    return ["127.0.0.1", h], h, ""


def create_app(cfg=None, data_dir=None):
    cfg = cfg or load_config()
    data_dir = data_dir or os.path.join(BASE_DIR, "data")
    upload_dir = os.path.join(data_dir, "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    cloud = bool(cfg.get("cloud"))  # 在雲端主機上執行：所有連線都經過主機的代理，沒有「主持電腦」

    app = Flask(__name__)
    if cloud:
        # 雲端主機在前面處理 https：讀取它轉送的原始網址與來源 IP；登入狀態只走 https
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
        app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax")
    app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.json.ensure_ascii = False

    kb = KB()
    eta_model = EtaModel()
    engine = AIEngine(cfg.get("ai"), kb)
    store = Store(os.path.join(data_dir, "state.json"))
    asset_v = str(int(time.time()))
    app.jinja_env.filters["money"] = lambda n: f"{int(n or 0):,}"
    app.jinja_env.filters["hm"] = hm
    app.jinja_env.filters["kbname"] = kb.name

    # ---------------- 展示密碼（分享到外面時才需要） ----------------
    access_code = str(cfg.get("access_code") or "").strip()
    secret = str(cfg.get("secret_key") or "").strip()
    if secret:
        app.secret_key = secret.encode("utf-8")
    else:
        key_path = os.path.join(data_dir, "secret.key")
        if not os.path.exists(key_path):
            with open(key_path, "wb") as f:
                f.write(os.urandom(32))
        with open(key_path, "rb") as f:
            app.secret_key = f.read()
    app.permanent_session_lifetime = dt.timedelta(days=7)
    gate_token = hashlib.sha256(("hotai:" + access_code).encode("utf-8")).hexdigest()[:24]
    # 主持人密碼：給要一起管理展示的隊友，登入後權限和主持電腦相同（重置、切換 AI、看分享卡片）
    host_code = str(cfg.get("host_code") or "").strip()
    if host_code and host_code == access_code:
        host_code = ""  # 兩組一樣就分不出主持人和一般試用者，停用主持人密碼
    host_token = hashlib.sha256(("hotai-host:" + host_code).encode("utf-8")).hexdigest()[:24]

    def is_proxied():
        return any(h in request.headers for h in ("Cf-Ray", "Cf-Connecting-Ip", "X-Forwarded-For", "Forwarded"))

    def is_local_request():
        """真的是在這台電腦上用 localhost 開的（不是經由 Cloudflare 等通道轉進來的）。雲端上一律不是。"""
        if cloud:
            return False
        host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
        return request.remote_addr in ("127.0.0.1", "::1") and host in ("localhost", "127.0.0.1", "::1") and not is_proxied()

    def is_host():
        """主持電腦本身，或輸入過主持人密碼的隊友。"""
        if is_local_request():
            return True
        return bool(host_code) and session.get("host") == host_token

    app.is_local_request = is_local_request
    app.is_host = is_host

    @app.before_request
    def require_code():
        if request.path in ("/healthz", "/robots.txt"):
            return None
        if not access_code:
            if cloud:
                return None  # 雲端版沒設展示密碼＝公開展示（重置、AI 設定仍要主持人密碼）
            # 沒設展示密碼時，拒絕所有經由外部通道轉進來的連線（例如沒關乾淨的舊分享網址）
            if is_proxied() and not request.path.startswith("/static/"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "這個展示網站目前沒有開放外部分享"}), 403
                return "這個展示網站目前沒有開放外部分享。", 403, {"Content-Type": "text/plain; charset=utf-8"}
            return None
        p = request.path
        if p.startswith("/static/") or p == "/enter":
            return None
        if is_local_request():  # 主持人自己的電腦不用輸入密碼
            return None
        if session.get("gate") == gate_token:
            return None
        if p.startswith("/api/") or p.startswith("/uploads/"):
            return jsonify({"error": "請先輸入展示密碼（重新整理頁面）"}), 401
        return redirect("/enter?next=" + urllib.parse.quote(request.full_path.rstrip("?")))

    login_fails = {}  # {來源: [失敗時間...]}，"*" 是全部加總；10 分鐘內太多次就先擋下

    def client_id():
        return request.headers.get("Cf-Connecting-Ip") or request.remote_addr or "?"

    def login_blocked():
        now = time.time()
        for k in list(login_fails):
            login_fails[k] = [t for t in login_fails[k] if now - t < 600]
            if not login_fails[k]:
                del login_fails[k]
        return len(login_fails.get(client_id(), [])) >= 10 or len(login_fails.get("*", [])) >= 100

    @app.route("/enter", methods=["GET", "POST"])
    def enter():
        nxt = request.values.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"  # 只允許站內網址，避免被導到外部網站
        err = ""
        if request.method == "POST" and login_blocked():
            err = "嘗試太多次了，請 10 分鐘後再試"
        elif request.method == "POST":
            code_in = request.form.get("code", "").strip()
            if host_code and hmac.compare_digest(code_in, host_code):
                session.permanent = True
                session["gate"] = gate_token
                session["host"] = host_token
                return redirect(nxt)
            if access_code and hmac.compare_digest(code_in, access_code):
                session.permanent = True
                session["gate"] = gate_token
                session.pop("host", None)
                return redirect(nxt)
            err = "密碼不對，請再試一次"
            for k in (client_id(), "*"):
                login_fails.setdefault(k, []).append(time.time())
        return render_template("enter.html", nxt=nxt, err=err, asset_v=asset_v,
                               host_login=cloud and not access_code)

    @app.get("/healthz")
    def healthz():
        return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.get("/robots.txt")
    def robots():
        return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.after_request
    def safety_headers(resp):
        # 展示網站不要被搜尋引擎收錄；上傳的檔案不要被瀏覽器猜成別的格式
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        return resp

    # ---------------- 計算用的小工具 ----------------
    def addons_of(state, visit):
        return [state["addons"][a] for a in visit.get("addon_ids", []) if a in state["addons"]]

    def shop_load(state):
        return sum(1 for v in state["visits"].values() if 0 <= v["stage"] < DONE_STAGE)

    def compute_eta(state, visit, now=None):
        now = now or time.time()
        st = visit["stage"]
        if st < 0:
            return {"state": "waiting", "remaining": None, "hm": hm(visit.get("appointment_at")),
                    "text": f"預約時間 {hm(visit.get('appointment_at'))}"}
        if st >= DONE_STAGE:
            return {"state": "done", "remaining": 0, "hm": hm(visit["stage_times"].get(str(DONE_STAGE))),
                    "text": "車輛已完工，可以取車了"}
        std = sum(i["minutes"] for i in visit["items"] if i.get("source") != "addon")
        add = sum(i["minutes"] for i in visit["items"] if i.get("source") == "addon")
        entered = visit["stage_times"].get(str(st)) or visit.get("checkin_at") or now
        elapsed = max(0.0, (now - entered) / 60)
        hour = dt.datetime.fromtimestamp(now).hour
        rem = eta_model.remaining(std, add, shop_load(state), hour, st, elapsed,
                                  visit["vehicle"]["powertrain"] == "hybrid")
        rem = max(5, int(round(rem / 5.0)) * 5)  # 以 5 分鐘為單位，避免每次更新都跳動
        t = now + rem * 60
        return {"state": "working", "remaining": rem, "hm": hm(t), "text": f"預計 {hm(t)} 可以取車（約再 {rem} 分鐘）"}

    def eta_sentence(state, visit):
        return f"目前進度是「{offline.stage_name(visit['stage'])}」，{compute_eta(state, visit)['text']}。"

    def record_line(visit, code):
        item = kb.get(code) or {}
        if item.get("routine"):
            return "每次保養都會做"
        h = offline.last_done(visit, code)
        if not h:
            return "這台車第一次做"
        d = dt.datetime.strptime(h["date"], "%Y-%m-%d")
        return f"上次：{d.year} 年 {d.month} 月・{h['km']:,} 公里"

    def touch(visit):
        visit["version"] = visit.get("version", 0) + 1
        visit["updated_at"] = time.time()

    def get_visit(state, vid):
        v = state["visits"].get(vid)
        if not v:
            abort(404)
        return v

    def final_of(addon):
        return addon.get("final") or addon["draft"]

    def addon_public(addon):
        f = final_of(addon)
        return {
            "id": addon["id"], "visit_id": addon["visit_id"], "item_code": addon["item_code"],
            "item_name": addon["item_name"], "status": addon["status"], "status_text": STATUS_TEXT.get(addon["status"], ""),
            "title": f["title"], "tier": f["tier"], "tier_label": TIERS[f["tier"]]["label"],
            "price": addon["price"], "minutes": addon["minutes"], "media": addon["media"],
            "measure": addon.get("measure"), "tech_note": addon.get("tech_note", ""), "technician": addon.get("technician"),
            "draft_pending": addon.get("draft_pending", False), "created_hm": hm(addon.get("created_at")),
            "sent_hm": hm(addon.get("sent_at")), "decided_hm": hm(addon.get("decided_at")),
            "source": f.get("source", "offline"),
        }

    def customer_items(visit):
        exp = visit.get("explanation") or {}
        notes = exp.get("items", {})
        out = []
        for it in visit["items"]:
            k = kb.get(it["code"]) or {}
            out.append({
                "code": it["code"], "name": it["name"], "price": it["price"], "source": it.get("source", "planned"),
                "one_liner": k.get("one_liner", ""), "analogy": k.get("analogy", ""), "detail": k.get("detail", ""),
                "tech": k.get("tech", ""), "interval": k.get("interval_text", ""), "benefit": k.get("benefit", ""),
                "note": (notes.get(it["code"]) or {}).get("personal_note", ""), "record": record_line(visit, it["code"]),
                "bd": kb.breakdown(it["code"], it["price"], it.get("minutes")),
            })
        return out

    def tech_public(state, tid):
        """技師給車主看的資料：等級、年資、專長、評分（本月模擬＋今日）、目前手上車輛數。"""
        t = (state.get("technicians") or {}).get(tid)
        if not t:
            return None
        s = (state.get("sim_tech") or {}).get(tid, {})
        rs, rn, load = s.get("rating_sum", 0), s.get("rating_n", 0), 0
        for v in state["visits"].values():
            if v.get("technician_id") == tid:
                if 0 <= v["stage"] < DONE_STAGE:
                    load += 1
                if v.get("tech_rating"):
                    rs += v["tech_rating"]["stars"]
                    rn += 1
        return dict(t, initial=t["name"][0], rating=round(rs / rn, 1) if rn else None, rating_n=rn, load=load)

    def techs_public(state):
        return [tech_public(state, tid) for tid in (state.get("technicians") or {})]

    def optional_list(visit):
        """這台車可以加選的自選項目（已在必要項目裡的不重複列）。"""
        pt = visit["vehicle"]["powertrain"]
        chosen = {i["code"] for i in visit["items"] if i.get("source") == "optional"}
        required = {i["code"] for i in visit["items"] if i.get("source", "planned") == "planned"}
        out = []
        for c in kb.optional_codes(pt):
            if c in required:
                continue
            k = kb.get(c)
            out.append(dict(kb.breakdown(c), code=c, name=k["name"], one_liner=k.get("one_liner", ""),
                            benefit=k.get("benefit", ""), selected=c in chosen))
        return out

    def price_view(pt, visit=None):
        # 預設列到 10 萬公里；車子已經超過時，延伸到「下次」那一級
        rows = kb.price_table(pt, max_km=max(100000, visit["bucket_km"] + 10000) if visit else 100000)
        marks = {}
        if visit:
            for h in visit.get("history", []):
                b = int(round(h["km"] / 10000.0)) * 10000
                if b >= 10000:
                    marks[b] = {"kind": "done", "text": "已完成・" + h["date"][:7].replace("-", "/")}
            marks[visit["bucket_km"]] = {"kind": "now", "text": "本次"}
            marks.setdefault(visit["bucket_km"] + 10000, {"kind": "next", "text": "下次"})
        for r in rows:
            r["mark"] = marks.get(r["km"])
        optional = [dict(kb.breakdown(c), code=c, name=kb.name(c), one_liner=kb.get(c).get("one_liner", ""),
                         benefit=kb.get(c).get("benefit", "")) for c in kb.optional_codes(pt)]
        addons = [dict(kb.breakdown(c), code=c, name=kb.name(c), note=kb.get(c).get("price_note", ""),
                       rule=kb.get(c).get("rule_text", "")) for c in kb.addon_codes()]
        return rows, optional, addons

    def tech_stats(state):
        """技師績效與獎金試算：本月（模擬）＋今天（即時）。"""
        techs = state.get("technicians") or {}
        keys = ("designated", "reports", "quality", "unedited", "five_star", "rating_sum", "rating_n")
        live = {tid: dict.fromkeys(keys, 0) for tid in techs}
        checked = dict.fromkeys(techs, 0)
        for v in state["visits"].values():
            tid = v.get("technician_id")
            if tid in live:
                if v.get("requested_tech_id") == tid and v["stage"] >= 0:
                    live[tid]["designated"] += 1
                r = v.get("tech_rating")
                if r:
                    live[tid]["rating_sum"] += r["stars"]
                    live[tid]["rating_n"] += 1
                    live[tid]["five_star"] += 1 if r["stars"] == 5 else 0
            for pc in v.get("photo_checks", []):
                if pc.get("checked_by") in checked:
                    checked[pc["checked_by"]] += 1
        for a in state["addons"].values():
            tid = a.get("technician_id")
            if tid not in live:
                continue
            live[tid]["reports"] += 1
            detail = a.get("measure") is not None or len((a.get("tech_note") or "").strip()) >= 8
            if a.get("media") and detail:
                live[tid]["quality"] += 1
            f = a.get("final")
            if f is not None and a["status"] != "draft" and not f.get("edited"):
                live[tid]["unedited"] += 1
        rows = []
        for tid, t in techs.items():
            sim = (state.get("sim_tech") or {}).get(tid, {})
            month = {k: sim.get(k, 0) + live[tid][k] for k in keys}
            items = [{"label": r["label"], "count": month[r["key"]], "amount": r["amount"], "unit": r["unit"],
                      "subtotal": month[r["key"]] * r["amount"]} for r in BONUS_RULES]
            rows.append({
                "id": tid, "name": t["name"], "level": t["level"], "years": t["years"], "month": month,
                "live": dict(live[tid], photos_checked=checked[tid]),
                "rating": round(month["rating_sum"] / month["rating_n"], 1) if month["rating_n"] else None,
                "quality_rate": round(100 * month["quality"] / max(1, month["reports"])),
                "unedited_rate": round(100 * month["unedited"] / max(1, month["reports"])),
                "bonus": sum(i["subtotal"] for i in items), "bonus_items": items,
                "live_bonus": sum(live[tid][r["key"]] * r["amount"] for r in BONUS_RULES),
            })
        return {"rows": rows, "rules": BONUS_RULES,
                "note": "本月數字＝模擬示意＋今天的即時操作。獎金只看被指名、回報品質與車主評價，不看追加金額或同意率，"
                        "避免鼓勵推銷；安全必要項目被車主延後也不扣分。"}

    def exp_payload(visit, exp):
        """說明頁（草稿或已送出版本）給專員編輯器用的資料。"""
        if not exp:
            return None
        src = exp.get("source", "offline")
        return {
            "greeting": exp.get("greeting", ""), "summary": exp.get("summary", ""), "source": src,
            "source_label": "這份說明" + SOURCE_LABELS[src], "sent": bool(exp.get("sent")),
            "sent_hm": hm(exp.get("sent_at")), "edited": bool(exp.get("edited")),
            "items": [{"code": i["code"], "name": i["name"], "price": i["price"],
                       "one_liner": (kb.get(i["code"]) or {}).get("one_liner", ""), "record": record_line(visit, i["code"]),
                       "note": (exp.get("items", {}).get(i["code"]) or {}).get("personal_note", "")}
                      for i in visit["items"]],
        }

    def totals(state, visit):
        def sums(src):
            lst = [i for i in visit["items"] if i.get("source", "planned") == src]
            bds = [kb.breakdown(i["code"], i["price"], i.get("minutes")) for i in lst]
            return {"total": sum(i["price"] for i in lst), "parts": sum(b["parts"] for b in bds),
                    "labor": sum(b["labor"] for b in bds), "count": len(lst)}
        req, opt, add = sums("planned"), sums("optional"), sums("addon")
        pending = sum(a["price"] for a in addons_of(state, visit) if a["status"] == "sent")
        return {"planned": req["total"], "optional": opt["total"], "added": add["total"], "pending": pending,
                "total": req["total"] + opt["total"] + add["total"], "req": req, "opt": opt, "add": add,
                "parts": req["parts"] + opt["parts"] + add["parts"], "labor": req["labor"] + opt["labor"] + add["labor"],
                "labor_rate": LABOR_RATE}

    def customer_state(state, visit):
        ads = addons_of(state, visit)
        exp = visit.get("explanation") or {}
        return {
            "version": visit.get("version", 0),
            "stage": visit["stage"],
            "stage_name": offline.stage_name(visit["stage"]),
            "stage_times": {k: hm(t) for k, t in visit["stage_times"].items()},
            "eta": compute_eta(state, visit),
            "explanation_sent": bool(exp.get("sent")),
            "items_sig": f"{len(visit['items'])}-{sum(i['price'] for i in visit['items'])}-{exp.get('sent_at')}"
                         f"-{visit.get('technician_id')}-{visit.get('requested_tech_id')}",
            "pending": [addon_public(a) for a in ads if a["status"] == "sent"],
            "report_ready": visit["stage"] >= DONE_STAGE,
            "last_msg": visit["chat"][-1]["id"] if visit.get("chat") else "",
        }

    def board(state):
        now = time.time()
        rows, todo = [], []
        active = sorted([v for v in state["visits"].values() if v["stage"] >= 0], key=lambda v: v.get("checkin_at") or 0)
        q_total = q_ai = 0
        for v in active:
            ads = addons_of(state, v)
            esc = [m for m in v["chat"] if m["role"] == "user" and m.get("escalated") and not m.get("resolved")]
            users = [m for m in v["chat"] if m["role"] == "user"]
            q_total += len(users)
            q_ai += sum(1 for m in users if not m.get("escalated"))
            exp = v.get("explanation") or {}
            rows.append({
                "id": v["id"], "plate": v["plate"], "model": v["vehicle"]["model"], "owner": v["owner"]["name"],
                "persona": v["owner"].get("persona", ""), "service": v["service_type"], "advisor": v["advisor"],
                "stage": v["stage"], "stage_name": offline.stage_name(v["stage"]), "eta": compute_eta(state, v, now),
                "checkin_hm": hm(v.get("checkin_at")), "explanation_sent": bool(exp.get("sent")),
                "addons": {s: sum(1 for a in ads if a["status"] == s) for s in ("draft", "sent", "approved", "deferred")},
                "questions": len(users), "questions_pending": len(esc), "rating": (v.get("rating") or {}).get("stars"),
                "technician": v.get("technician", ""),
                "requested": (state.get("technicians") or {}).get(v.get("requested_tech_id"), {}).get("name", ""),
                "optional": sum(1 for i in v["items"] if i.get("source") == "optional"),
                "photos_pending": sum(1 for p in v.get("photo_checks", []) if p["status"] == "new"),
            })
            for a in ads:
                if a["status"] == "draft":
                    todo.append({"kind": "review", "ts": a["created_at"], "href": f"/sa/addon/{a['id']}",
                                 "text": f"{v['plate']}｜技師回報「{a['item_name']}」，待審核"
                                         + ("（AI 草擬中）" if a.get("draft_pending") else "")})
            for m in esc:
                if m.get("kind") == "photo":
                    todo.append({"kind": "photo", "ts": m["ts"], "href": f"/sa/visit/{v['id']}#chat",
                                 "text": f"{v['plate']}｜車主上傳照片：「{m['text'] or '（沒有說明）'}」"})
                else:
                    todo.append({"kind": "question", "ts": m["ts"], "href": f"/sa/visit/{v['id']}#chat",
                                 "text": f"{v['plate']}｜車主問：「{m['text']}」"})
            if not exp.get("sent") and v["stage"] < DONE_STAGE:
                todo.append({"kind": "explain", "ts": v.get("checkin_at") or now, "href": f"/sa/visit/{v['id']}",
                             "text": f"{v['plate']}｜說明頁還沒送給車主"})
        todo.sort(key=lambda t: t["ts"])
        for t in todo:
            t["hm"] = hm(t["ts"])
        appts = sorted([v for v in state["visits"].values() if v["stage"] < 0], key=lambda v: v.get("appointment_at") or 0)
        return {
            "now": hm(now),
            "rows": rows,
            "todo": todo,
            "appointments": [{"id": v["id"], "plate": v["plate"], "model": v["vehicle"]["model"], "owner": v["owner"]["name"],
                              "service": v["service_type"], "hm": hm(v.get("appointment_at")),
                              "powertrain": POWERTRAIN_TEXT.get(v["vehicle"]["powertrain"], ""),
                              "requested": (state.get("technicians") or {}).get(v.get("requested_tech_id"), {}).get("name", ""),
                              "optional": sum(1 for i in v["items"] if i.get("source") == "optional"),
                              "photos": len(v.get("photo_checks", []))} for v in appts],
            "stats": {
                "cars": len(active),
                "working": sum(1 for r in rows if r["stage"] < DONE_STAGE),
                "done": sum(1 for r in rows if r["stage"] >= DONE_STAGE),
                "waiting_customer": sum(r["addons"]["sent"] for r in rows),
                "to_review": sum(r["addons"]["draft"] for r in rows),
                "questions_pending": sum(r["questions_pending"] for r in rows),
                "ai_rate": round(100 * q_ai / q_total) if q_total else None,
                "questions": q_total,
            },
            "ai": engine.status(),
        }

    def report_data(state, visit):
        ads = addons_of(state, visit)
        veh = visit["vehicle"]
        now = dt.datetime.now()
        done = [{"name": i["name"], "source": i.get("source", "planned")} for i in visit["items"]]
        findings = []
        for a in ads:
            if a["status"] in ("draft",):
                continue
            f = final_of(a)
            measure = f"{a['measure']['value']:g} {a['measure']['unit']}".strip() if a.get("measure") else None
            findings.append({
                "id": a["id"], "name": a["item_name"], "title": f["title"], "tier": f["tier"],
                "tier_label": TIERS[f["tier"]]["label"], "status": a["status"],
                "status_text": {"approved": "今天已處理", "deferred": "這次先不處理，下次保養會再檢查",
                                "sent": "尚未決定"}.get(a["status"], ""),
                "measure": measure, "watch": (kb.get(a["item_code"]) or {}).get("skip", ""),
                "media": a["media"][0] if a.get("media") else None,
            })
        km = veh["odometer"]
        next_km = visit["bucket_km"] + 10000
        buy = dt.datetime.strptime(veh["purchase_date"], "%Y-%m-%d")
        months_owned = max(1, offline.months_between(buy, now))
        per_month = max(300.0, km / months_owned)
        months_to_next = int(min(12, max(1, round((next_km - km) / per_month))))
        nd = add_months(now, months_to_next)
        next_items = kb.scheduled_items(next_km, veh["powertrain"])
        deferred = [{"name": a["item_name"], "price": a["price"]} for a in ads if a["status"] == "deferred"]
        forecast = sum((kb.get(c) or {}).get("price", 0) for c in next_items)
        hist = visit.get("history", [])
        prev_km = hist[-1]["km"] if hist else 0
        milestone = None
        for m in (300000, 250000, 200000, 150000, 100000, 50000):
            if prev_km < m <= km:
                milestone = f"{m // 10000} 萬公里"
                break
        timeline = [{"date": h["date"][:7].replace("-", "/"), "km": h["km"], "type": h["type"]} for h in hist[-4:]]
        timeline.append({"date": now.strftime("%Y/%m"), "km": km, "type": visit["service_type"] + "（今天）"})
        return {
            "done": done, "findings": findings, "milestone": milestone, "timeline": timeline,
            "next": {"km": next_km, "date": f"{nd.year} 年 {nd.month} 月", "items": [kb.name(c) for c in next_items],
                     "forecast": forecast, "deferred": deferred, "forecast_max": forecast + sum(d["price"] for d in deferred),
                     "text": f"約 {nd.year} 年 {nd.month} 月或 {next_km:,} 公里（先到者為準）"},
            "total": totals(state, visit)["total"],
        }

    def dashboard(state):
        visits = list(state["visits"].values())
        qs = []
        for v in visits:
            for m in v["chat"]:
                if m["role"] == "user":
                    qs.append((v, m))
        topics_live = Counter(m.get("topic", "other") for _, m in qs)
        answered = sum(1 for _, m in qs if not m.get("escalated"))
        tier_live = {t: {"approved": 0, "deferred": 0} for t in TIERS}
        decisions = []
        for a in state["addons"].values():
            if a["status"] in ("approved", "deferred"):
                tier_live[final_of(a)["tier"]][a["status"]] += 1
                if a.get("sent_at") and a.get("decided_at"):
                    decisions.append((a["decided_at"] - a["sent_at"]) / 60)
        ratings = [v["rating"]["stars"] for v in visits if v.get("rating")]
        sim = state["sim"]
        topics_all = {t: sim["topics_30d"].get(t, 0) + topics_live.get(t, 0) for t in TOPICS}
        tier_all = {t: {s: sim["tier_outcomes_30d"][t][s] + tier_live[t][s] for s in ("approved", "deferred")} for t in TIERS}
        esc_policy = sum(1 for _, m in qs if m.get("escalated") and m.get("topic") == "policy")
        tstats = tech_stats(state)
        recent = sorted(qs, key=lambda vm: -vm[1]["ts"])[:10]
        top_topic = max(topics_all.items(), key=lambda kv: kv[1])[0]
        cw = tier_all["can_wait"]
        sf = tier_all["safety"]
        insights = [
            f"車主最常問的是「{TOPIC_SHORT[top_topic]}」：說明頁已把「您的車為什麼這次要做」放在每個項目裡，可持續觀察這類提問是否下降。",
            f"「可以下次」的項目有 {round(100 * cw['deferred'] / max(1, sum(cw.values())))}% 被車主延後，"
            f"「安全必要」同意率 {round(100 * sf['approved'] / max(1, sum(sf.values())))}%：誠實分級有被車主採納，安全項目沒有因此被拒絕。",
            "保固、優惠類問題規則上一律轉專員；若和泰授權 AI 讀取車籍保固資料，可再提高 AI 自行解答率。"
            + (f"（今天已有 {esc_policy} 則）" if esc_policy else ""),
        ]
        if tstats["rows"]:
            top = max(tstats["rows"], key=lambda r: r["month"]["designated"])
            insights.append(f"本月被車主指名最多的是{top['name']}（{top['month']['designated']} 次）。技師獎金只看被指名、"
                            "回報品質與車主評價，不看追加金額，避免為了獎金推銷。")
        return {
            "sim": sim,
            "topics": [{"key": t, "label": TOPIC_SHORT[t], "live": topics_live.get(t, 0), "all": topics_all[t]} for t in TOPICS],
            "tiers": [{"key": t, "label": TIERS[t]["label"], "live": tier_live[t], "all": tier_all[t]} for t in TIERS],
            "live": {
                "questions": len(qs), "answered": answered,
                "ai_rate": round(100 * answered / len(qs)) if qs else None,
                "decided": sum(tier_live[t][s] for t in TIERS for s in ("approved", "deferred")),
                "approval_rate": (round(100 * sum(tier_live[t]["approved"] for t in TIERS)
                                        / max(1, sum(tier_live[t][s] for t in TIERS for s in ("approved", "deferred"))))),
                "decision_min": round(sum(decisions) / len(decisions), 1) if decisions else None,
                "rating": round(sum(ratings) / len(ratings), 1) if ratings else None, "ratings": len(ratings),
            },
            "recent": [{"plate": v["plate"], "text": m["text"], "topic": TOPIC_SHORT.get(m.get("topic", "other"), "其他"),
                        "escalated": bool(m.get("escalated")), "hm": hm(m["ts"]), "photo": m.get("kind") == "photo"}
                       for v, m in recent],
            "insights": insights,
            "eta_model": eta_model.info(),
            "techs": tstats,
        }

    def seed_fn():
        return build_seed(kb, eta_text_fn=lambda visit, state: eta_sentence(state, visit))

    store.load_or_seed(seed_fn, schema=SCHEMA)

    def ctx_common():
        return {"asset_v": asset_v, "ai": engine.status(), "tiers": TIERS, "stages": STAGES,
                "source_labels": SOURCE_LABELS, "mode_labels": MODE_LABELS, "urgency": URGENCY,
                "optional_last_stage": OPTIONAL_LAST_STAGE, "labor_rate": LABOR_RATE}

    def customer_extras(state, v):
        """車主頁共用：負責技師、可指定的技師、可加選的自選項目。"""
        return {"tech": tech_public(state, v.get("technician_id")),
                "requested": tech_public(state, v.get("requested_tech_id")) if v.get("requested_tech_id") else None,
                "techs": techs_public(state), "optional_items": optional_list(v),
                "can_choose_tech": v["stage"] <= 0, "can_choose_optional": v["stage"] <= OPTIONAL_LAST_STAGE}

    # ---------------- 頁面 ----------------
    @app.route("/")
    def hub():
        state = store.snapshot()
        port = cfg.get("port", 5800)
        visits = []
        for v in sorted(state["visits"].values(), key=lambda x: x["id"]):
            visits.append({"id": v["id"], "plate": v["plate"], "model": v["vehicle"]["model"], "owner": v["owner"]["name"],
                           "persona": v["owner"].get("persona", ""), "stage_name": offline.stage_name(v["stage"]),
                           "note": v.get("note", "")})
        bound = cfg.get("_bound") or {}
        lan = f"http://{bound['lan_ip']}:{port}" if bound.get("lan_ip") else ""
        host = is_host()
        site = request.url_root.rstrip("/")
        return render_template("hub.html", visits=visits, lan=lan, lan_note=bound.get("note", ""), port=port,
                               eta_info=eta_model.info(), share=(cfg.get("_share") if host else None),
                               host_only=(bool(access_code) or cloud) and not host,
                               co_host=host and not is_local_request(), cloud=cloud, site=site,
                               cloud_code=(access_code if (cloud and host) else ""),
                               host_code_set=bool(host_code), host_code_short=bool(host_code) and len(host_code) < 8,
                               public_gemini=cloud and not access_code and engine.mode == "gemini",
                               **ctx_common())

    @app.route("/c/<vid>")
    def c_home(vid):
        state = store.snapshot()
        v = get_visit(state, vid)
        if v["stage"] < 0:
            return render_template("c_wait.html", v=v, tab="home", totals=totals(state, v),
                                   **customer_extras(state, v), **ctx_common())
        ads = addons_of(state, v)
        exp = v.get("explanation") or {}
        return render_template(
            "c_home.html", v=v, tab="home", exp=exp if exp.get("sent") else None, items=customer_items(v),
            cstate=customer_state(state, v), totals=totals(state, v), decided=[addon_public(a) for a in ads if a["status"] in ("approved", "deferred")],
            pt=POWERTRAIN_TEXT.get(v["vehicle"]["powertrain"], ""), **customer_extras(state, v), **ctx_common())

    @app.route("/c/<vid>/addon/<aid>")
    def c_addon(vid, aid):
        state = store.snapshot()
        v = get_visit(state, vid)
        a = state["addons"].get(aid)
        if not a or a["visit_id"] != vid or a["status"] == "draft":
            abort(404)
        return render_template("c_addon.html", v=v, tab="home", a=a, ap=addon_public(a), f=final_of(a),
                               item=kb.get(a["item_code"]) or {}, bd=kb.breakdown(a["item_code"], a["price"], a["minutes"]),
                               reporter=tech_public(state, a.get("technician_id")), **ctx_common())

    @app.route("/c/<vid>/chat")
    def c_chat(vid):
        state = store.snapshot()
        v = get_visit(state, vid)
        about = request.args.get("about")
        about_addon = state["addons"].get(about) if about else None
        if about_addon and about_addon["visit_id"] != vid:
            about_addon = None
        if about_addon:
            n = about_addon["item_name"]
            chips = [f"{n}不處理會怎樣？", f"{n}可以下次再處理嗎？", f"{n}多少錢？", f"為什麼要處理{n}？"]
        else:
            names = [i["name"] for i in v["items"] if not (kb.get(i["code"]) or {}).get("routine")]
            first = names[0] if names else "機油"
            chips = ["還要等多久？", "這次總共多少錢？", f"為什麼要做{first}？", "有哪些項目可以下次再做？"]
            if len(names) > 1:
                chips.append(f"{names[1]}不做會怎樣？")
        return render_template("c_chat.html", v=v, tab="chat", chips=chips, about=about_addon, **ctx_common())

    @app.route("/c/<vid>/report")
    def c_report(vid):
        state = store.snapshot()
        v = get_visit(state, vid)
        ready = v["stage"] >= DONE_STAGE
        data = report_data(state, v) if ready else None
        return render_template("c_report.html", v=v, tab="report", ready=ready, rdata=data, report=v.get("report"),
                               stage_name=offline.stage_name(v["stage"]), eta=compute_eta(state, v),
                               tech=tech_public(state, v.get("technician_id")), **ctx_common())

    @app.route("/prices")
    @app.route("/c/<vid>/prices")
    def prices(vid=None):
        state = store.snapshot()
        v = get_visit(state, vid) if vid else None
        pt = request.args.get("pt") or (v["vehicle"]["powertrain"] if v else "hybrid")
        if pt not in POWERTRAIN_TEXT:
            pt = "hybrid"
        rows, optional, addons = price_view(pt, v if (v and v["vehicle"]["powertrain"] == pt) else None)
        return render_template("c_prices.html", v=v, tab="prices", pt=pt, pt_text=POWERTRAIN_TEXT, rows=rows,
                               optional=optional, addons=addons, **ctx_common())

    @app.route("/sa")
    def sa_board():
        return render_template("sa_board.html", role="服務專員｜接待看板", **ctx_common())

    @app.route("/sa/visit/<vid>")
    def sa_visit(vid):
        state = store.snapshot()
        v = get_visit(state, vid)
        if v["stage"] < 0:
            return redirect("/sa")
        exp_sent = v.get("explanation") if (v.get("explanation") or {}).get("sent") else None
        planned_codes = [i["code"] for i in v["items"] if i.get("source") != "addon"]
        catalog = [{"code": c, "name": it["name"], "price": it.get("price", 0), "on": c in planned_codes}
                   for c, it in kb.items.items() if not it.get("addon") and not it.get("optional")]
        chosen_optional = [i for i in v["items"] if i.get("source") == "optional"]
        return render_template(
            "sa_visit.html", role="服務專員｜車輛工作區", v=v, catalog=catalog,
            draft_payload=exp_payload(v, v.get("explanation_draft")), sent_payload=exp_payload(v, exp_sent),
            totals=totals(state, v), eta=compute_eta(state, v),
            pt=POWERTRAIN_TEXT.get(v["vehicle"]["powertrain"], ""),
            stage_times={k: hm(t) for k, t in v["stage_times"].items()}, topic_short=TOPIC_SHORT,
            history=list(reversed(v.get("history", [])))[:5], kbname=kb.name, chosen_optional=chosen_optional,
            optional_items=optional_list(v),
            techs=techs_public(state), tech=tech_public(state, v.get("technician_id")),
            requested=tech_public(state, v.get("requested_tech_id")) if v.get("requested_tech_id") else None,
            **ctx_common())

    @app.route("/sa/addon/<aid>")
    def sa_addon(aid):
        state = store.snapshot()
        a = state["addons"].get(aid)
        if not a:
            abort(404)
        v = get_visit(state, a["visit_id"])
        return render_template("sa_addon.html", role="服務專員｜追加項目審核", a=a, ap=addon_public(a), v=v,
                               f=final_of(a), item=kb.get(a["item_code"]) or {}, **ctx_common())

    @app.route("/tech")
    def tech():
        state = store.snapshot()
        cars = [{"id": v["id"], "plate": v["plate"], "model": v["vehicle"]["model"], "stage_name": offline.stage_name(v["stage"]),
                 "technician": v["technician"], "technician_id": v.get("technician_id"),
                 "requested": v.get("requested_tech_id") == v.get("technician_id") and bool(v.get("requested_tech_id"))}
                for v in sorted(state["visits"].values(), key=lambda x: x.get("checkin_at") or 0)
                if 0 <= v["stage"] < DONE_STAGE]
        items = []
        for c in kb.addon_codes() + ["cabin_filter", "air_filter"]:
            it = kb.get(c)
            items.append({"code": c, "name": it["name"], "unit": it.get("unit", ""), "sample": it.get("sample", ""),
                          "price": it.get("price", 0), "rule_text": it.get("rule_text", "")})
        return render_template("tech.html", role="技師回報", cars=cars, items=items, techs=techs_public(state),
                               **ctx_common())

    @app.route("/dashboard")
    def dash():
        return render_template("dashboard.html", role="管理者｜車主之聲", **ctx_common())

    @app.route("/lounge")
    def lounge():
        return render_template("lounge.html", **ctx_common())

    @app.route("/uploads/<path:name>")
    def uploads(name):
        return send_from_directory(upload_dir, name)

    # ---------------- 車主端 API ----------------
    @app.get("/api/visit/<vid>/state")
    def api_state(vid):
        state = store.snapshot()
        return jsonify(customer_state(state, get_visit(state, vid)))

    @app.get("/api/visit/<vid>/chat")
    def api_chat_list(vid):
        state = store.snapshot()
        v = get_visit(state, vid)
        return jsonify({"messages": [dict(m, hm=hm(m["ts"])) for m in v["chat"]]})

    @app.post("/api/visit/<vid>/chat")
    def api_chat(vid):
        body = request.get_json(silent=True) or {}
        q = (body.get("question") or "").strip()[:300]
        about = body.get("about") or None
        if not q:
            return jsonify({"error": "請輸入問題"}), 400
        state = store.snapshot()
        v = get_visit(state, vid)
        res = engine.answer(v, addons_of(state, v), q, eta_sentence(state, v), about=about)

        def apply(st):
            vv = st["visits"][vid]
            now = time.time()
            st.setdefault("seq", {}).setdefault("M", 0)
            st["seq"]["M"] += 1
            um = {"id": f"M{st['seq']['M']:04d}", "role": "user", "text": q, "ts": now, "topic": res["topic"],
                  "escalated": res["escalate"], "resolved": not res["escalate"], "about": about}
            st["seq"]["M"] += 1
            am = {"id": f"M{st['seq']['M']:04d}", "role": "ai", "text": res["answer"], "ts": now + 0.1,
                  "source": res.get("source", "offline"), "escalated": res["escalate"]}
            vv["chat"] += [um, am]
            touch(vv)
            return [dict(um, hm=hm(um["ts"])), dict(am, hm=hm(am["ts"]))]

        return jsonify({"messages": store.mutate(apply)})

    @app.post("/api/addon/<aid>/decide")
    def api_decide(aid):
        body = request.get_json(silent=True) or {}
        decision = body.get("decision")
        if decision not in ("approve", "defer"):
            return jsonify({"error": "decision 必須是 approve 或 defer"}), 400

        def apply(st):
            a = st["addons"].get(aid)
            if not a or a["status"] != "sent":
                return None
            v = st["visits"][a["visit_id"]]
            a["status"] = "approved" if decision == "approve" else "deferred"
            a["decided_at"] = time.time()
            if decision == "approve":
                wi = kb.work_item(a["item_code"], source="addon")
                wi["price"], wi["minutes"] = a["price"], a["minutes"]
                v["items"].append(wi)
            touch(v)
            return addon_public(a)

        res = store.mutate(apply)
        if res is None:
            return jsonify({"error": "這個項目已經決定過了"}), 409
        state = store.snapshot()
        v = state["visits"][res["visit_id"]]
        return jsonify({"addon": res, "eta": compute_eta(state, v)})

    @app.post("/api/visit/<vid>/rating")
    def api_rating(vid):
        stars = int((request.get_json(silent=True) or {}).get("stars") or 0)
        if not 1 <= stars <= 5:
            return jsonify({"error": "請給 1–5 顆星"}), 400

        def apply(st):
            v = get_visit(st, vid)
            v["rating"] = {"stars": stars, "ts": time.time()}
            touch(v)

        store.mutate(apply)
        return jsonify({"ok": True})

    @app.post("/api/visit/<vid>/report")
    def api_report(vid):
        state = store.snapshot()
        v = get_visit(state, vid)
        if v["stage"] < DONE_STAGE:
            return jsonify({"error": "車輛還沒完工"}), 409
        force = bool((request.get_json(silent=True) or {}).get("force"))
        if v.get("report") and not force:
            return jsonify({"report": v["report"]})
        text = engine.report_text(v, report_data(state, v))
        text["generated_at"] = time.time()

        def apply(st):
            vv = st["visits"][vid]
            vv["report"] = text
            touch(vv)

        store.mutate(apply)
        return jsonify({"report": text})

    # ---------------- 專員端 API ----------------
    @app.get("/api/board")
    def api_board():
        return jsonify(board(store.snapshot()))

    @app.post("/api/visit/<vid>/checkin")
    def api_checkin(vid):
        def apply(st):
            v = get_visit(st, vid)
            if v["stage"] >= 0:
                return False
            now = time.time()
            v["stage"], v["checkin_at"] = 0, now
            v["stage_times"] = {"0": now}
            touch(v)
            return True

        store.mutate(apply)
        return jsonify({"ok": True, "href": f"/sa/visit/{vid}"})

    @app.post("/api/visit/<vid>/items")
    def api_items(vid):
        codes = [c for c in (request.get_json(silent=True) or {}).get("codes", [])
                 if kb.get(c) and not kb.get(c).get("addon") and not kb.get(c).get("optional")]
        if not codes:
            return jsonify({"error": "工單至少要有一個項目"}), 400

        def apply(st):
            v = get_visit(st, vid)
            kept = [i for i in v["items"] if i.get("source") in ("addon", "optional")]
            v["items"] = [kb.work_item(c) for c in codes] + kept
            touch(v)

        store.mutate(apply)
        state = store.snapshot()
        return jsonify({"ok": True, "totals": totals(state, state["visits"][vid])})

    @app.post("/api/visit/<vid>/explanation/generate")
    def api_exp_generate(vid):
        state = store.snapshot()
        v = get_visit(state, vid)
        exp = engine.explanation(v)
        exp["generated_at"] = time.time()
        exp["sent"] = False

        def apply(st):
            vv = st["visits"][vid]
            vv["explanation_draft"] = exp
            touch(vv)

        store.mutate(apply)
        return jsonify(exp_payload(v, exp))

    @app.post("/api/visit/<vid>/explanation/send")
    def api_exp_send(vid):
        body = request.get_json(silent=True) or {}

        def apply(st):
            v = get_visit(st, vid)
            draft = v.get("explanation_draft")
            if not draft:
                return False
            if body.get("greeting"):
                draft["greeting"] = str(body["greeting"])[:300]
            if body.get("summary"):
                draft["summary"] = str(body["summary"])[:500]
            for code, note in (body.get("notes") or {}).items():
                if code in draft["items"] and str(note).strip():
                    if draft["items"][code]["personal_note"] != str(note).strip():
                        draft["edited"] = True
                    draft["items"][code]["personal_note"] = str(note).strip()[:300]
            draft["sent"], draft["sent_at"] = True, time.time()
            v["explanation"] = draft
            v["explanation_draft"] = None
            touch(v)
            return True

        ok = store.mutate(apply)
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "請先產生說明頁"}), 409))

    @app.post("/api/visit/<vid>/stage")
    def api_stage(vid):
        action = (request.get_json(silent=True) or {}).get("action", "next")

        def apply(st):
            v = get_visit(st, vid)
            s = v["stage"]
            if action == "next" and 0 <= s < DONE_STAGE:
                s += 1
                v["stage_times"][str(s)] = time.time()
            elif action == "prev" and s > 0:
                v["stage_times"].pop(str(s), None)
                s -= 1
                if v.get("report"):
                    v["report"] = None
            v["stage"] = s
            touch(v)
            return s

        s = store.mutate(apply)
        state = store.snapshot()
        return jsonify({"stage": s, "stage_name": offline.stage_name(s), "eta": compute_eta(state, state["visits"][vid])})

    @app.post("/api/visit/<vid>/reply")
    def api_reply(vid):
        text = ((request.get_json(silent=True) or {}).get("text") or "").strip()[:500]
        if not text:
            return jsonify({"error": "請輸入回覆內容"}), 400

        def apply(st):
            v = get_visit(st, vid)
            for m in v["chat"]:
                if m["role"] == "user" and m.get("escalated"):
                    m["resolved"] = True
            st["seq"]["M"] = st["seq"].get("M", 0) + 1
            v["chat"].append({"id": f"M{st['seq']['M']:04d}", "role": "advisor", "text": text, "ts": time.time(),
                              "name": v["advisor"]})
            touch(v)

        store.mutate(apply)
        return jsonify({"ok": True})

    @app.post("/api/visit/<vid>/concerns")
    def api_concerns(vid):
        state = store.snapshot()
        v = get_visit(state, vid)
        res = engine.concerns(v)
        res["generated_at"] = time.time()

        def apply(st):
            st["visits"][vid]["concerns"] = res

        store.mutate(apply)
        return jsonify(dict(res, source_label=SOURCE_LABELS[res.get("source", "offline")]))

    @app.get("/api/visit/<vid>/addons")
    def api_visit_addons(vid):
        state = store.snapshot()
        v = get_visit(state, vid)
        return jsonify({"addons": [addon_public(a) for a in addons_of(state, v)]})

    @app.get("/api/addon/<aid>")
    def api_addon(aid):
        state = store.snapshot()
        a = state["addons"].get(aid)
        if not a:
            abort(404)
        return jsonify({"addon": addon_public(a), "draft": a["draft"], "final": a.get("final"),
                        "source_label": SOURCE_LABELS[a["draft"].get("source", "offline")]})

    @app.post("/api/addon/<aid>/regenerate")
    def api_regenerate(aid):
        state = store.snapshot()
        a = state["addons"].get(aid)
        if not a:
            abort(404)
        v = state["visits"][a["visit_id"]]
        value = a["measure"]["value"] if a.get("measure") else None
        draft = engine.addon_draft(v, a["item_code"], value, a.get("tech_note", ""), a["price"], a["minutes"])

        def apply(st):
            st["addons"][aid]["draft"] = draft
            st["addons"][aid]["draft_pending"] = False
            touch(st["visits"][a["visit_id"]])

        store.mutate(apply)
        return jsonify({"draft": draft, "source_label": SOURCE_LABELS[draft["source"]]})

    @app.post("/api/addon/<aid>/send")
    def api_addon_send(aid):
        body = request.get_json(silent=True) or {}

        def apply(st):
            a = st["addons"].get(aid)
            if not a or a["status"] not in ("draft", "sent"):
                return None
            f = dict(a["draft"])
            for k in ("title", "plain", "tier_reason", "consequence", "suggestion"):
                if isinstance(body.get(k), str) and body[k].strip():
                    f[k] = body[k].strip()[:600]
            if body.get("tier") in TIERS:
                f["tier"] = body["tier"]
            try:
                a["price"] = max(0, int(body.get("price", a["price"])))
                a["minutes"] = max(0, int(body.get("minutes", a["minutes"])))
            except (TypeError, ValueError):
                pass
            f["edited"] = any(f.get(k) != a["draft"].get(k) for k in ("title", "plain", "tier", "tier_reason", "consequence", "suggestion"))
            a["final"] = f
            a["status"] = "sent"
            a["sent_at"] = time.time()
            touch(st["visits"][a["visit_id"]])
            return addon_public(a)

        res = store.mutate(apply)
        if res is None:
            return jsonify({"error": "這個項目已經由車主決定，不能再修改"}), 409
        return jsonify({"addon": res})

    # ---------------- 技師端 API ----------------
    @app.post("/api/tech/addon")
    def api_tech_addon():
        vid = request.form.get("visit_id", "")
        code = request.form.get("item_code", "")
        note = (request.form.get("note") or "").strip()[:500]
        raw = (request.form.get("measure") or "").strip()
        sample = request.form.get("use_sample") == "1"
        tech_id = request.form.get("technician_id") or ""
        state = store.snapshot()
        if vid not in state["visits"] or not kb.get(code):
            return jsonify({"error": "請選擇車輛與項目"}), 400
        value = None
        if raw:
            try:
                value = float(raw.replace("mm", "").replace("%", "").strip())
            except ValueError:
                return jsonify({"error": "量測值請只填數字，例如 3 或 62"}), 400
        media = []
        f = request.files.get("media")
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in MEDIA_TYPES:
                return jsonify({"error": f"不支援的檔案格式 {ext}，請用 jpg / png / mp4 / mov"}), 400
            name = uuid.uuid4().hex + ext
            f.save(os.path.join(upload_dir, name))
            media.append({"type": MEDIA_TYPES[ext], "url": f"/uploads/{name}", "sample": False})
        elif sample and kb.get(code).get("sample"):
            media.append({"type": "image", "url": "/static/img/samples/" + kb.get(code)["sample"], "sample": True})
        v = state["visits"][vid]
        item = kb.get(code)
        draft = offline.addon_draft(v, code, value, note, kb)
        draft["source"] = "offline"
        pending = engine.mode != "offline"

        def apply(st):
            st["seq"]["A"] = st["seq"].get("A", 0) + 1
            aid = f"A{st['seq']['A']:03d}"
            vv = st["visits"][vid]
            tid = tech_id if tech_id in (st.get("technicians") or {}) else vv.get("technician_id")
            tname = (st.get("technicians") or {}).get(tid, {}).get("name", vv["technician"])
            st["addons"][aid] = {
                "id": aid, "visit_id": vid, "item_code": code, "item_name": item["name"],
                "measure": {"value": value, "unit": item.get("unit", "")} if value is not None else None,
                "tech_note": note, "technician": tname, "technician_id": tid, "advisor": vv["advisor"], "media": media,
                "price": item.get("price", 0), "minutes": item.get("minutes", 20), "draft": draft, "final": None,
                "status": "draft", "draft_pending": pending, "created_at": time.time(), "sent_at": None, "decided_at": None,
            }
            vv["addon_ids"].append(aid)
            touch(vv)
            return aid

        aid = store.mutate(apply)
        if pending:
            def run():
                d = engine.addon_draft(v, code, value, note, item.get("price", 0), item.get("minutes", 20))

                def upd(st):
                    a = st["addons"].get(aid)
                    if a and a["status"] == "draft":
                        a["draft"] = d
                    if a:
                        a["draft_pending"] = False
                        touch(st["visits"][vid])

                store.mutate(upd)

            threading.Thread(target=run, daemon=True).start()
        return jsonify({"ok": True, "id": aid})

    @app.get("/api/technicians")
    def api_technicians():
        return jsonify({"technicians": techs_public(store.snapshot())})

    @app.post("/api/visit/<vid>/technician")
    def api_technician(vid):
        body = request.get_json(silent=True) or {}
        tid = body.get("technician_id") or ""
        by = body.get("by", "customer")

        def apply(st):
            v = get_visit(st, vid)
            techs = st.get("technicians") or {}
            if by == "advisor":
                if tid not in techs:
                    return "bad"
                if v["stage"] >= DONE_STAGE:
                    return "late"
            else:
                if v["stage"] > 0:
                    return "late_customer"
                if not tid:  # 取消指定
                    v["requested_tech_id"] = None
                    touch(v)
                    return "ok"
                if tid not in techs:
                    return "bad"
                v["requested_tech_id"] = tid
            v["technician_id"] = tid
            v["technician"] = techs[tid]["name"]
            touch(v)
            return "ok"

        res = store.mutate(apply)
        if res == "bad":
            return jsonify({"error": "找不到這位技師"}), 400
        if res == "late_customer":
            return jsonify({"error": "技師已經開始作業，現在不能更換指定的技師"}), 409
        if res == "late":
            return jsonify({"error": "車輛已完工，不能再更換技師"}), 409
        state = store.snapshot()
        v = state["visits"][vid]
        return jsonify({"ok": True, "tech": tech_public(state, v.get("technician_id")),
                        "requested": v.get("requested_tech_id")})

    @app.post("/api/visit/<vid>/optional")
    def api_optional(vid):
        body = request.get_json(silent=True) or {}
        code = body.get("code") or ""
        on = bool(body.get("on"))
        item = kb.get(code)
        if not item or not item.get("optional"):
            return jsonify({"error": "這不是可以自選的項目"}), 400

        def apply(st):
            v = get_visit(st, vid)
            if v["stage"] > OPTIONAL_LAST_STAGE:
                return "late"
            if item.get("powertrains") and v["vehicle"]["powertrain"] not in item["powertrains"]:
                return "na"
            if on:
                if any(i["code"] == code for i in v["items"]):
                    return "ok"
                v["items"].append(kb.work_item(code, source="optional"))
            else:
                v["items"] = [i for i in v["items"] if not (i["code"] == code and i.get("source") == "optional")]
            touch(v)
            return "ok"

        res = store.mutate(apply)
        if res == "late":
            return jsonify({"error": "車輛已進入品質檢查，這次不能再加選，下次保養再選"}), 409
        if res == "na":
            return jsonify({"error": "這個項目不適用您的車型"}), 400
        state = store.snapshot()
        v = state["visits"][vid]
        return jsonify({"ok": True, "totals": totals(state, v), "eta": compute_eta(state, v)})

    @app.post("/api/visit/<vid>/photo")
    def api_photo(vid):
        state = store.snapshot()
        v = get_visit(state, vid)
        f = request.files.get("photo")
        text = (request.form.get("text") or "").strip()[:300]
        if not f or not f.filename:
            return jsonify({"error": "請先選一張照片"}), 400
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in PHOTO_TYPES:
            return jsonify({"error": f"不支援的照片格式 {ext}，請用 jpg / png / heic"}), 400
        data = f.read()
        if len(data) > 15 * 1024 * 1024:
            return jsonify({"error": "照片太大（上限 15 MB）"}), 400
        name = uuid.uuid4().hex + ext
        with open(os.path.join(upload_dir, name), "wb") as out:
            out.write(data)
        url = f"/uploads/{name}"
        res = engine.analyze_photo(v, data, PHOTO_TYPES[ext], text)
        reply = offline.format_photo_reply(res, v["advisor"], v["technician"])

        def apply(st):
            vv = st["visits"][vid]
            now = time.time()
            st["seq"]["P"] = st["seq"].get("P", 0) + 1
            pid = f"P{st['seq']['P']:03d}"
            st["seq"]["M"] = st["seq"].get("M", 0) + 1
            um = {"id": f"M{st['seq']['M']:04d}", "role": "user", "kind": "photo", "text": text, "image": url,
                  "ts": now, "topic": "photo", "escalated": True, "resolved": False, "photo_id": pid}
            st["seq"]["M"] += 1
            am = {"id": f"M{st['seq']['M']:04d}", "role": "ai", "kind": "photo_analysis", "text": reply,
                  "ts": now + 0.1, "source": res.get("source", "offline"), "urgency": res["urgency"],
                  "escalated": True, "note": res.get("note", "")}
            vv["chat"] += [um, am]
            vv.setdefault("photo_checks", []).append({
                "id": pid, "msg_id": um["id"], "url": url, "text": text, "ts": now, "urgency": res["urgency"],
                "summary": res["summary"], "status": "new", "checked_by": None, "checked_at": None, "sample": False})
            touch(vv)
            return [dict(um, hm=hm(um["ts"])), dict(am, hm=hm(am["ts"]))]

        return jsonify({"messages": store.mutate(apply), "analysis": res})

    @app.post("/api/visit/<vid>/photo/<pid>/check")
    def api_photo_check(vid, pid):
        tid = (request.get_json(silent=True) or {}).get("technician_id") or ""

        def apply(st):
            v = get_visit(st, vid)
            pc = next((x for x in v.get("photo_checks", []) if x["id"] == pid), None)
            if not pc:
                return None
            if pc["status"] == "checked":
                return "already"
            techs = st.get("technicians") or {}
            tech = techs.get(tid) or techs.get(v.get("technician_id")) or {"id": None, "name": v["technician"]}
            pc.update(status="checked", checked_by=tech["id"], checked_at=time.time())
            for m in v["chat"]:
                if m.get("photo_id") == pid:
                    m["resolved"] = True
            st["seq"]["M"] = st["seq"].get("M", 0) + 1
            v["chat"].append({"id": f"M{st['seq']['M']:04d}", "role": "tech", "name": tech["name"], "ts": time.time(),
                              "text": f"{tech['name']}已經看過您的照片，保養時會一併檢查；有發現會拍照回報給您。"})
            touch(v)
            return "ok"

        res = store.mutate(apply)
        if res is None:
            return jsonify({"error": "找不到這張照片"}), 404
        return jsonify({"ok": True, "already": res == "already"})

    @app.get("/api/tech/photos")
    def api_tech_photos():
        state = store.snapshot()
        out = []
        for v in state["visits"].values():
            if v["stage"] >= DONE_STAGE:
                continue
            for pc in v.get("photo_checks", []):
                out.append(dict(pc, visit_id=v["id"], plate=v["plate"], model=v["vehicle"]["model"],
                                technician=v.get("technician"), technician_id=v.get("technician_id"),
                                stage_name=offline.stage_name(v["stage"]), hm=hm(pc["ts"]),
                                urgency_label=URGENCY.get(pc["urgency"], URGENCY["unknown"])["label"]))
        out.sort(key=lambda x: (x["status"] != "new", -x["ts"]))
        return jsonify({"photos": out})

    @app.get("/api/tech/performance")
    def api_tech_performance():
        return jsonify(tech_stats(store.snapshot()))

    @app.post("/api/visit/<vid>/tech_rating")
    def api_tech_rating(vid):
        stars = int((request.get_json(silent=True) or {}).get("stars") or 0)
        if not 1 <= stars <= 5:
            return jsonify({"error": "請給 1–5 顆星"}), 400

        def apply(st):
            v = get_visit(st, vid)
            v["tech_rating"] = {"stars": stars, "ts": time.time(), "technician_id": v.get("technician_id")}
            touch(v)

        store.mutate(apply)
        return jsonify({"ok": True})

    @app.get("/api/tech/recent")
    def api_tech_recent():
        state = store.snapshot()
        rows = sorted(state["addons"].values(), key=lambda a: -a["created_at"])[:12]
        out = []
        for a in rows:
            v = state["visits"][a["visit_id"]]
            out.append(dict(addon_public(a), plate=v["plate"], model=v["vehicle"]["model"]))
        return jsonify({"items": out})

    # ---------------- 管理／系統 API ----------------
    @app.get("/api/dashboard")
    def api_dashboard():
        return jsonify(dashboard(store.snapshot()))

    def host_only():
        """分享到外面時（有展示密碼），重置與 AI 設定只允許主持人操作：主持電腦本身，或輸入主持人密碼的隊友。"""
        if (access_code or cloud) and not is_host():
            return jsonify({"error": "這個按鈕只有主持人能按（主持電腦，或用主持人密碼登入的隊友）"}), 403
        return None

    @app.get("/api/ai/status")
    def api_ai_status():
        return jsonify(engine.status())

    @app.post("/api/ai/detect")
    def api_ai_detect():
        denied = host_only()
        if denied:
            return denied
        engine.cfg.update({k: v for k, v in load_config().get("ai", {}).items() if not k.startswith("_")})
        st = engine.detect()
        engine.warmup_async()
        return jsonify(st)

    @app.post("/api/ai/test")
    def api_ai_test():
        denied = host_only()
        if denied:
            return denied
        return jsonify(dict(engine.test(), status=engine.status()))

    @app.post("/api/reset")
    def api_reset():
        denied = host_only()
        if denied:
            return denied
        store.reset(seed_fn)
        return jsonify({"ok": True})

    @app.get("/api/share")
    def api_share():
        share = cfg.get("_share")
        if not share or not is_host():
            return jsonify({"error": "目前沒有在分享"}), 404
        return jsonify(share)

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "找不到資料"}), 404
        return render_template("not_found.html", **ctx_common()), 404

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"error": "檔案太大（上限 80 MB），請改拍短一點的影片或照片"}), 413

    app.engine = engine
    app.store = store
    app.kb = kb
    return app


def port_free(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    from werkzeug.serving import make_server

    try:
        cfg = load_config()
    except ValueError as e:
        print(f"\n[錯誤] config.json 格式有誤（常見原因：少了逗號或引號）：{e}")
        return 1
    port = int(cfg.get("port", 5800))
    hosts, lan_ip, note = resolve_hosts(cfg.get("host"))
    busy = [h for h in hosts if not port_free(h, port)]
    if busy:
        print(f"\n[錯誤] {port} 埠已經被佔用。")
        print("最常見的原因：已經有一個「看得見的保養」黑色視窗開著。請先把舊視窗關掉再重新啟動；")
        print("或在 config.json 把 port 改成別的數字（例如 5801）。")
        return 1
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    print("正在準備示範資料與預測模型……")
    cfg["_bound"] = {"lan_ip": lan_ip, "note": note}
    app = create_app(cfg)
    servers = [make_server(h, port, app, threaded=True) for h in hosts]
    st = app.engine.status()
    print("\n==============================================")
    print("  看得見的保養｜展示網站已啟動")
    print("==============================================")
    print(f"  電腦開啟：http://localhost:{port}")
    if lan_ip:
        print(f"  手機開啟：http://{lan_ip}:{port}   （手機與電腦要連同一個 Wi-Fi 或熱點）")
    if note:
        print("  " + note)
    print(f"  AI 模式：{st['label']}")
    print(f"           {st['detail']}")
    print("  關閉這個視窗，網站就會停止。")
    print("==============================================\n")
    app.engine.warmup_async()
    if cfg.get("open_browser", True):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}/")).start()
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
