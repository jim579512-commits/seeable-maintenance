"""據點資料：TOYOTA 官網的維修據點（服務廠＋社區型保修站）、營業時間、現在有沒有營業。

資料來源是 TOYOTA 官網公開頁面本身在用的資料介面（不是正式對外的 API，欄位可能改變）：
- 維修據點查詢 https://www.toyota.com.tw/location.aspx?type=0 → POST /api/location.ashx（TYPE=0）
  地址、電話、座標、週一至週六／週日／國定假日的營業時間、今天的營業時間、鈑噴、夜間服務、AED、限高
- 社區型保修站 https://www.toyota.com.tw/TOYOTA_Satellite/ → /tire_inquiry/api/toyotav5
  保修站的夜間服務時段（例如 17:30–19:00）
啟動時先讀「上次抓到的資料」或內建快照（toyota_locations.json），再在背景向官網更新（每 6 小時一次）；
官網連不上時繼續用手上的資料，網站不會因此停擺。
官網沒有公開可預約時段與擁擠程度，這兩項仍是示意（正式導入時串接和泰的預約系統）。

更新內建快照：py -3 locations.py --update-snapshot
"""
import datetime as dt
import json
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.path.join(BASE_DIR, "toyota_locations.json")
MAIN_URL = "https://www.toyota.com.tw/api/location.ashx"
SAT_URL = "https://www.toyota.com.tw/tire_inquiry/api/toyotav5"
USER_AGENT = "Mozilla/5.0 (concept prototype for 2026 Hotai AI Hackathon; refresh every 6h)"
REFRESH_SECONDS = 6 * 3600  # 營業時間很少變動，6 小時抓一次就夠，也不會增加官網負擔
RETRY_SECONDS = 30 * 60  # 抓失敗後隔 30 分鐘再試
MODES = ("auto", "snapshot", "demo")
WEEKDAY = "一二三四五六日"
LUNCH = ("12:00", "12:30")  # 午休不排預約

# 國定假日（依行政院人事行政總處「115 年政府行政機關辦公日曆表」，含補假日）。
# 民間事業單位的補假日可以勞資協商調整，各據點實際營業以據點公告為準。
HOLIDAYS = {
    "2026-01-01": "開國紀念日",
    "2026-02-15": "春節", "2026-02-16": "春節", "2026-02-17": "春節", "2026-02-18": "春節", "2026-02-19": "春節",
    "2026-02-20": "春節（補假）",
    "2026-02-27": "和平紀念日（補假）", "2026-02-28": "和平紀念日",
    "2026-04-03": "兒童節（補假）", "2026-04-04": "兒童節", "2026-04-05": "清明節", "2026-04-06": "清明節（補假）",
    "2026-05-01": "勞動節", "2026-06-19": "端午節", "2026-09-25": "中秋節", "2026-09-28": "教師節",
    "2026-10-09": "國慶日（補假）", "2026-10-10": "國慶日",
    "2026-10-25": "光復節", "2026-10-26": "光復節（補假）",
    "2026-12-25": "行憲紀念日",
    "2027-01-01": "開國紀念日",
}


def holiday_name(day):
    return HOLIDAYS.get(day.isoformat(), "")


def haversine_km(a, b):
    """兩個經緯度之間的直線距離（公里）。"""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _hm(s):
    s = (s or "").strip()
    if len(s) == 5 and s[2] == ":" and s[:2].isdigit() and s[3:].isdigit():
        return s
    return ""


def parse_span(s):
    """'08:30~17:30' → ('08:30', '17:30')；空白、'~'、'公休' → None。"""
    parts = (s or "").replace("～", "~").split("~")
    if len(parts) != 2:
        return None
    a, b = _hm(parts[0]), _hm(parts[1])
    return (a, b) if a and b and a < b else None


# ---------------- 抓官網資料 ----------------
def _get(url, data=None, timeout=12):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/javascript, */*; q=0.01"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers.update({"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest",
                        "Referer": "https://www.toyota.com.tw/location.aspx?type=0"})
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def fetch_official(timeout=12):
    """向 TOYOTA 官網取得維修據點（必要）與保修站夜間時段（取不到也沒關係）。"""
    main = _get(MAIN_URL, {"TYPE": "0"}, timeout)
    if not isinstance(main, dict) or main.get("RS") != "OK" or not isinstance(main.get("DATA"), list):
        raise ValueError("官網據點資料格式和預期不同")
    try:
        sat = _get(SAT_URL, None, timeout)
        sat = sat if isinstance(sat, list) else []
    except Exception:  # noqa: BLE001 — 夜間時段只是補充資料
        sat = []
    return main["DATA"], sat


def normalize(main, sat, fetched_date):
    """官網欄位 → 這個網站用的據點資料（只留維修據點，不含營業所）。"""
    night_by_key = {}
    for x in sat or []:
        if x.get("NTISFLAG") == "Y":
            span = parse_span(f"{x.get('NTSROPENTIME', '')}~{x.get('NTSRCLOSETIME', '')}")
            if span:
                night_by_key[f"{x.get('DLRCD', '')}{x.get('BRNHCD', '')}"] = span
    out = {}
    for x in main or []:
        if str(x.get("TYPE")) != "2":  # 1＝營業所（賣車），2＝維修據點
            continue
        try:
            lat, lng = float(x.get("LAT")), float(x.get("LNG"))
        except (TypeError, ValueError):
            continue
        key = str(x.get("KEY") or "").strip()
        title = str(x.get("TITLE") or "").strip()
        if not key or not title or not lat or not lng:
            continue
        di = x.get("DATEINFO") or {}
        wflags = [f == "Y" for f in (di.get("Wflags") or [])][:6]
        wflags += [True] * (6 - len(wflags))
        today = str(x.get("TIME") or "").strip()
        out[key] = {
            "key": key, "kind": "station" if "保修站" in title else "plant", "name": title,
            "dealer": str(x.get("DEALER") or "").strip(), "city": str(x.get("CITY") or "").strip(),
            "area": str(x.get("AREA") or "").strip(), "addr": str(x.get("ADDR") or "").strip(),
            "tel": str(x.get("TEL") or "").strip(), "lat": round(lat, 6), "lng": round(lng, 6),
            "wflags": wflags, "weekday": parse_span(di.get("ODTIME") or ""),
            "sunday": parse_span(di.get("SDTIME")) if di.get("SDISFLAG") == "Y" else None,
            "holiday": parse_span(di.get("HDTIME")) if di.get("HDISFLAG") == "Y" else None,
            "night": night_by_key.get(key), "night_flag": x.get("NTISFLAG") == "Y",
            "body": x.get("BPISFLAG") == "Y", "aed": str(x.get("AED")) == "1",
            "limit_height": str(x.get("LIMITHEIGHT") or "").strip(),
            # 官網算好的「今天」營業時間（只在抓資料那一天有效，可反映臨時公休）
            "today": today, "today_date": fetched_date,
        }
    return out


# ---------------- 營業時間 ----------------
def _merge(ws):
    ws = sorted(w for w in ws if w)
    out = []
    for a, b in ws:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def windows(loc, day):
    """這個據點這一天的營業時段 [(開, 關)]；公休回傳 []。"""
    ds = day.isoformat()
    wd = day.weekday()
    night = loc.get("night") if wd <= 4 else None  # 夜間服務只算平日
    if loc.get("today_date") == ds and loc.get("today"):  # 官網對「今天」的判斷優先（含臨時公休）
        if "公休" in loc["today"]:
            return []
        span = parse_span(loc["today"])
        if span:
            return _merge([tuple(span), tuple(night) if night else None])
    if ds in HOLIDAYS:
        return [tuple(loc["holiday"])] if loc.get("holiday") else []
    if wd == 6:
        return [tuple(loc["sunday"])] if loc.get("sunday") else []
    if not loc.get("wflags", [True] * 6)[wd] or not loc.get("weekday"):
        return []
    return _merge([tuple(loc["weekday"]), tuple(night) if night else None])


def _add(hm, minutes):
    t = dt.datetime(2000, 1, 1, int(hm[:2]), int(hm[3:])) + dt.timedelta(minutes=minutes)
    return t.strftime("%H:%M")


def slot_times(loc, day):
    """可以線上預約的報到時間：營業時間內每 30 分鐘一個，最後一個在打烊前 30 分鐘，午休不排。"""
    out = []
    for a, b in windows(loc, day):
        t = a if a[3:] in ("00", "30") else _add(a, 30 - int(a[3:]) % 30)
        while _add(t, 30) <= b:
            if t not in LUNCH and t not in out:
                out.append(t)
            t = _add(t, 30)
    return out


def close_time(loc, day):
    ws = windows(loc, day)
    return ws[-1][1] if ws else ""


def span_text(ws):
    return "、".join(f"{a}–{b}" for a, b in ws) if ws else "公休"


def _day_word(d, today):
    diff = (d - today).days
    if diff == 1:
        return "明天"
    if diff == 2:
        return "後天"
    return f"{d.month}/{d.day}（{WEEKDAY[d.weekday()]}）"


def status(loc, now):
    """現在有沒有營業：{'open', 'state'(open/rest/closed), 'label', 'detail', 'today', 'holiday'}。"""
    today = now.date()
    ws = windows(loc, today)
    hm = now.strftime("%H:%M")
    hol = holiday_name(today)
    base = {"today": span_text(ws), "holiday": hol}
    for a, b in ws:
        if a <= hm < b:
            return dict(base, open=True, state="open", label="營業中", detail=f"{b} 打烊")
    later = [a for a, b in ws if hm < a]
    if later:
        return dict(base, open=False, state="rest", label="休息中", detail=f"今天 {later[0]} 開始營業")
    for i in range(1, 15):
        d = today + dt.timedelta(days=i)
        w = windows(loc, d)
        if w:
            nxt = f"{_day_word(d, today)} {w[0][0]} 營業"
            if ws:
                return dict(base, open=False, state="rest", label="休息中", detail=nxt)
            return dict(base, open=False, state="closed", label=f"今日公休（{hol}）" if hol else "今日公休", detail=nxt)
    return dict(base, open=False, state="closed", label="暫停營業", detail="請洽據點")


def week_rows(loc):
    """給畫面的營業時間表。"""
    days = [WEEKDAY[i] for i in range(6) if loc.get("wflags", [True] * 6)[i]]
    wk = loc.get("weekday")
    if len(days) == 6:
        rows = [("週一至週六", f"{wk[0]}–{wk[1]}" if wk else "請洽據點")]
    else:
        rows = [("週" + "、".join(days) if days else "週一至週六", f"{wk[0]}–{wk[1]}" if (wk and days) else "公休")]
    rows.append(("週日", f"{loc['sunday'][0]}–{loc['sunday'][1]}" if loc.get("sunday") else "公休"))
    rows.append(("國定假日", f"{loc['holiday'][0]}–{loc['holiday'][1]}" if loc.get("holiday") else "公休"))
    if loc.get("night"):
        rows.append(("平日夜間", f"{loc['night'][0]}–{loc['night'][1]}"))
    elif loc.get("night_flag"):
        rows.append(("夜間服務", "有（時段請洽據點）"))
    return rows


def hours_text(loc):
    return "・".join(f"{k} {v}" for k, v in week_rows(loc))


def feature_tags(loc):
    tags = []
    if loc.get("body"):
        tags.append("鈑金烤漆")
    if loc.get("sunday"):
        tags.append("週日營業")
    if loc.get("holiday"):
        tags.append("國定假日營業")
    if loc.get("night"):
        tags.append(f"平日晚上到 {loc['night'][1]}")
    elif loc.get("night_flag"):
        tags.append("夜間服務")
    if loc.get("aed"):
        tags.append("AED")
    h = loc.get("limit_height") or ""
    if h.isdigit() and int(h) > 0:
        tags.append(f"限高 {int(h) / 100:.2f} 公尺")
    return tags


def maps_url(loc):
    """Google 地圖網址（不需要金鑰）：顯示據點位置，可以直接規劃路線。"""
    q = urllib.parse.quote(f"TOYOTA {loc.get('dealer', '')}{loc.get('name', '')} {loc.get('addr', '')}".strip())
    return f"https://www.google.com/maps/search/?api=1&query={q}"


# ---------------- 資料提供者：官網 → 上次抓到的資料 → 內建快照 ----------------
class Provider:
    def __init__(self, mode="auto", snapshot_path=SNAPSHOT_PATH, cache_path=None, fetch=None, on_update=None):
        self.mode = mode if mode in MODES else "auto"
        self.snapshot_path, self.cache_path = snapshot_path, cache_path
        self.fetch = fetch or fetch_official
        self.on_update = on_update
        self.lock = threading.Lock()
        self.data = None
        self.origin = ""  # live（剛從官網抓到）／cache（上次抓到的）／snapshot（內建快照）
        self.error = ""
        self.last_try = 0.0
        self.fetching = False
        if self.mode != "demo":
            self._load_local()

    def _read(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d.get("locations"), dict) and d["locations"] else None
        except (OSError, ValueError, AttributeError):
            return None

    def _load_local(self):
        best = None
        for path, origin in ((self.cache_path, "cache"), (self.snapshot_path, "snapshot")):
            d = self._read(path) if path else None
            if d and (best is None or d.get("fetched_at", "") > best[0].get("fetched_at", "")):
                best = (d, origin)
        if best:
            self.data, self.origin = best

    @property
    def official(self):
        return self.mode != "demo" and bool(self.data)

    def fetched_ts(self):
        try:
            return dt.datetime.fromisoformat(self.data["fetched_at"]).timestamp()
        except (TypeError, KeyError, ValueError):
            return 0.0

    def refresh(self, wait=False):
        """向官網更新；wait=False 時在背景執行，不會拖慢網頁。"""
        if self.mode != "auto":
            return False
        with self.lock:
            if self.fetching:
                return False
            self.fetching = True
            self.last_try = time.time()

        def run():
            try:
                main, sat = self.fetch()
                now = dt.datetime.now()
                locs = normalize(main, sat, now.date().isoformat())
                if len(locs) < 20:
                    raise ValueError(f"官網只回傳 {len(locs)} 個維修據點，先沿用手上的資料")
                data = {"source": "TOYOTA 官網", "fetched_at": now.isoformat(timespec="seconds"),
                        "urls": [MAIN_URL, SAT_URL], "locations": locs}
                self.data, self.origin, self.error = data, "live", ""
                if self.cache_path:
                    try:
                        tmp = self.cache_path + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False)
                        os.replace(tmp, self.cache_path)
                    except OSError:
                        pass
                if self.on_update:
                    self.on_update(self)
            except Exception as e:  # noqa: BLE001 — 官網連不上時繼續用手上的資料
                self.error = f"{type(e).__name__}: {e}"[:200]
            finally:
                self.fetching = False

        if wait:
            run()
        else:
            threading.Thread(target=run, daemon=True).start()
        return True

    def maybe_refresh(self):
        if self.mode != "auto" or self.fetching or time.time() - self.last_try < RETRY_SECONDS:
            return
        if self.origin != "live" or time.time() - self.fetched_ts() > REFRESH_SECONDS:
            self.refresh()

    def info(self):
        if not self.official:
            return {"mode": self.mode, "official": False, "label": "示意據點（虛構）", "count": 0, "fetched_at": "",
                    "error": self.error, "origin": ""}
        locs = self.data["locations"].values()
        try:
            when = dt.datetime.fromisoformat(self.data["fetched_at"]).strftime("%Y/%m/%d %H:%M")
        except (KeyError, ValueError):
            when = ""
        label = {"live": "TOYOTA 官網（即時）", "cache": "TOYOTA 官網（上次抓到的資料）",
                 "snapshot": "TOYOTA 官網（內建快照）"}.get(self.origin, "TOYOTA 官網")
        return {"mode": self.mode, "official": True, "label": label, "origin": self.origin, "fetched_at": when,
                "count": len(self.data["locations"]), "plants": sum(1 for x in locs if x["kind"] == "plant"),
                "stations": sum(1 for x in locs if x["kind"] == "station"), "error": self.error,
                "fetching": self.fetching}


def update_snapshot(path=SNAPSHOT_PATH):
    main, sat = fetch_official()
    now = dt.datetime.now()
    locs = normalize(main, sat, now.date().isoformat())
    data = {"source": "TOYOTA 官網", "fetched_at": now.isoformat(timespec="seconds"), "urls": [MAIN_URL, SAT_URL],
            "locations": locs}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)
    return data


if __name__ == "__main__":
    if "--update-snapshot" in sys.argv:
        d = update_snapshot()
        n = d["locations"].values()
        print(f"已更新 {SNAPSHOT_PATH}：{len(d['locations'])} 個維修據點"
              f"（服務廠 {sum(1 for x in n if x['kind'] == 'plant')}、保修站 {sum(1 for x in n if x['kind'] == 'station')}）")
    else:
        print(__doc__)
