"""線上預約：TOYOTA 服務廠與社區型保修站的需求判斷、透明報價、時段擁擠度與推薦（據點資料皆為示意）。

依 TOYOTA 官網：社區型保修站由原廠認證技師、使用正廠零件，主要做定期保養、輪胎、電瓶；
零件價格與工資費率和服務廠相同。服務廠能處理更多類型（檢修診斷、保固召回、油電／高壓系統、鈑金烤漆）。
所以這裡比較的不是價差，而是「哪一種據點適合這次的需求」與「哪個時段不用等」。
離峰優惠（平日午後工資打折）是本提案的分流設計，不是和泰現行優惠。
預估完工時間用同一個預估完工模型（eta.py），和到廠後車主手機上看到的一致。
"""
import datetime as dt
import math
import random
import zlib

import offline
from kb import LABOR_RATE, URGENCY

HOME_CENTER = "C01"  # 內部系統（今日工單）所在的服務廠
PLANT_TIMES = ["08:30", "09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
               "13:00", "13:30", "14:00", "14:30", "15:00", "15:30", "16:00"]
EVENING_TIMES = ["16:30", "17:00", "18:00", "18:30", "19:00", "19:30", "20:00"]
DAYS_AHEAD = 7
WEEKDAY = "一二三四五六日"
STATUS_TEXT = {"free": "空", "normal": "普通", "busy": "擁擠", "full": "額滿", "past": "已過", "late": "來不及"}
OFFPEAK = {"days": [0, 1, 2, 3, 4], "start": "13:30", "end": "15:30", "discount": 0.2}  # 全網一致的離峰設計（提案）

KIND_TEXT = {"plant": "服務廠", "station": "社區型保修站"}
CAP_TEXT = {"maintenance": "定期保養", "tire_battery": "輪胎、電瓶", "diagnosis_light": "小狀況檢查",
            "diagnosis": "檢修診斷", "warranty": "保固、召回、重大維修", "hv": "油電／高壓系統", "ev": "電動車", "body": "鈑金烤漆"}
PLANT_CAPS = ["maintenance", "tire_battery", "diagnosis_light", "diagnosis", "warranty", "hv"]
STATION_CAPS = ["maintenance", "tire_battery", "diagnosis_light"]


def _plant(cid, name, district, xy, bays, busy, extra_caps, amenities, rating, rating_n, advisors):
    return {"id": cid, "kind": "plant", "name": name, "district": district, "xy": xy, "bays": bays, "busy": busy,
            "labor_rate": LABOR_RATE, "offpeak": OFFPEAK, "caps": PLANT_CAPS + extra_caps, "closed": [6],
            "times": PLANT_TIMES, "weekend_times": PLANT_TIMES, "close": "17:30", "weekend_close": "17:30",
            "hours": "週一至週六 08:30–17:30",
            "amenities": amenities, "rating": rating, "rating_n": rating_n, "advisor": advisors[-1], "advisors": advisors}


def _station(cid, name, district, xy, busy, evening, open_sunday, rating, rating_n, advisors):
    return {"id": cid, "kind": "station", "name": name, "district": district, "xy": xy, "bays": 3, "busy": busy,
            "labor_rate": LABOR_RATE, "offpeak": OFFPEAK, "caps": list(STATION_CAPS), "closed": [] if open_sunday else [6],
            "times": PLANT_TIMES + (EVENING_TIMES if evening else []), "weekend_times": PLANT_TIMES,
            "close": "21:00" if evening else "17:30", "weekend_close": "17:30",
            "hours": ("平日 08:30–21:00" if evening else "平日 08:30–17:30") + ("・週六日 08:30–17:30" if open_sunday else "・週六 08:30–17:30"),
            "amenities": ["休息區"] + (["晚上也能保養"] if evening else []) + (["週日有開"] if open_sunday else []),
            "rating": rating, "rating_n": rating_n, "advisor": advisors[-1], "advisors": advisors}


# 據點（示意）：名稱、地點、評分、設備都是虛構的，只用來展示比較畫面
CENTERS = {c["id"]: c for c in [
    _plant("C01", "內湖服務廠", "台北市內湖區", (6.0, 5.0), 8, 0.05, ["ev", "body"], ["客休室", "兒童遊戲區", "電動車充電"],
           4.7, 812, ["陳專員", "黃專員", "李專員"]),
    _plant("C02", "南港服務廠", "台北市南港區", (8.6, 2.6), 6, 0.0, ["ev"], ["客休室", "接送服務"], 4.6, 455, ["葉專員", "高專員"]),
    _plant("C03", "松山服務廠", "台北市松山區", (3.0, 3.4), 5, 0.12, ["body"], ["客休室", "代步車"], 4.5, 603, ["洪專員", "蔡專員"]),
    _plant("C04", "汐止服務廠", "新北市汐止區", (11.6, 6.4), 10, -0.08, ["ev", "body"], ["客休室", "洗車", "電動車充電"],
           4.6, 389, ["鄭專員", "游專員"]),
    _plant("C05", "大直服務廠", "台北市中山區", (2.8, 6.6), 7, 0.08, [], ["客休室", "接送服務", "代步車"], 4.8, 927, ["賴專員", "簡專員"]),
    _station("S01", "東湖保修站", "台北市內湖區", (7.4, 7.0), -0.15, True, True, 4.7, 268, ["邱專員"]),
    _station("S02", "信義保修站", "台北市信義區", (4.2, 2.2), -0.05, False, False, 4.6, 341, ["方專員"]),
    _station("S03", "汐科保修站", "新北市汐止區", (10.4, 5.0), -0.12, True, False, 4.5, 152, ["彭專員"]),
]}

# 依用車習慣建議的自選項目（規則；畫面上會寫出原因）
SUGGEST = {
    "短程通勤": (["fuel_system_clean", "ac_clean"], "常短程通勤，引擎溫度常還沒升高就熄火，較容易積碳；冷氣也常開"),
    "長途高速": (["wheel_alignment", "glass_coating"], "常跑高速，輪胎角度與擋風玻璃的視線影響比較大"),
    "市區代步": (["ac_clean", "interior_sanitize"], "市區代步常開冷氣、也常載家人"),
}

# 這次要做什麼 → 適合哪一種據點（越前面越需要完整設備的服務廠）
NEED_ORDER = ["warranty", "body", "warning", "noise", "tire_battery", "maintenance"]
NEEDS = {
    "maintenance": {"label": "定期保養", "cap": "maintenance", "codes": None, "service": None,
                    "where": "社區型保修站或服務廠都可以",
                    "why": "定期保養在社區型保修站和服務廠都能做，零件價格和工資費率相同；保修站通常比較近、比較不用等。"},
    "tire_battery": {"label": "換輪胎、換電瓶", "cap": "tire_battery", "codes": ["tire_battery_check"], "service": "輪胎／電瓶更換",
                     "where": "社區型保修站或服務廠都可以",
                     "why": "輪胎、電瓶更換是社區型保修站的主要服務，價格和服務廠相同。"},
    "noise": {"label": "異音、抖動、小故障", "cap": "diagnosis_light", "codes": ["diagnosis"], "service": "車況檢測",
              "where": "可以先到社區型保修站檢查",
              "why": "單一的小狀況可以先讓保修站檢查；需要更完整的設備時，會協助轉到服務廠。"},
    "warning": {"label": "警示燈、油電／電子系統", "cap": "hv", "codes": ["diagnosis"], "service": "系統檢測",
                "where": "建議直接到服務廠",
                "why": "油電、高壓電池與電子系統需要專用診斷設備，服務廠比較完整，避免白跑一趟。"},
    "warranty": {"label": "保固、召回、重大維修", "cap": "warranty", "codes": ["warranty_check"], "service": "保固／召回檢查",
                 "where": "請到原廠服務廠",
                 "why": "保固、召回與重大維修由原廠服務廠處理，範圍內的費用由原廠負擔。"},
    "body": {"label": "鈑金、烤漆、事故", "cap": "body", "codes": ["body_estimate"], "service": "鈑金烤漆估價",
             "where": "請到有鈑噴設備的服務廠",
             "why": "鈑金烤漆需要鈑噴設備，只有部分服務廠有。"},
}
KEYWORDS = {
    "warranty": ["保固", "召回", "召修", "大修", "變速箱", "重大維修", "引擎故障"],
    "body": ["擦撞", "撞到", "事故", "鈑金", "板金", "烤漆", "凹陷", "車禍", "掉漆"],
    "warning": ["警示燈", "故障燈", "引擎燈", "高壓", "大電池", "油電系統", "電子系統", "儀表板", "螢幕", "雷達"],
    "noise": ["異音", "怪聲", "異響", "抖", "震動", "漏油", "漏水", "冒煙", "異味", "方向盤", "跑偏"],
    "tire_battery": ["輪胎", "換胎", "爆胎", "胎壓", "電瓶", "發不動", "沒電"],
    "maintenance": ["保養", "機油", "定保"],
}
TIPS = {
    "warranty": "帶著行照；收到召回通知的話，可以拍照先傳給專員，到廠更快。",
    "body": "走保險理賠的話，先通知保險公司並保留事故照片。",
    "warning": "記下警示燈的顏色與圖案（可以拍照），技師判斷更快。",
    "noise": "預約後可以在問答裡先拍照或描述在什麼情況下會出現。",
    "tire_battery": "車子發不動的話，可以先請道路救援協助。",
    "maintenance": "保養前一晚不用特別準備；有想問的事可以先寫在預約備註。",
}


def _r10(x):
    return int(round(x / 10.0)) * 10


def distance_km(a, b):
    """示意距離：直線距離乘上 1.25 當作道路距離。"""
    return round(math.hypot(a[0] - b[0], a[1] - b[1]) * 1.25, 1)


def day_label(d, today):
    diff = (d - today).days
    tag = "今天" if diff == 0 else ("明天" if diff == 1 else ("後天" if diff == 2 else ""))
    base = f"{d.month}/{d.day}（{WEEKDAY[d.weekday()]}）"
    return f"{tag} {base}".strip() if tag else base


def slot_times(center, day):
    """這個據點這一天可以預約的時段；公休日回傳空的。"""
    wd = day.weekday()
    if wd in center.get("closed", [6]):
        return []
    return center.get("weekend_times", PLANT_TIMES) if wd >= 5 else center.get("times", PLANT_TIMES)


def close_time(center, day):
    """這個據點這一天的打烊時間（HH:MM）。"""
    return center.get("weekend_close", "17:30") if day.weekday() >= 5 else center.get("close", "17:30")


def is_offpeak(center, day, hm):
    op = center["offpeak"]
    return day.weekday() in op["days"] and op["start"] <= hm < op["end"]


def base_fill(center, day, hm):
    """這個時段原本就被預約的比例（示意）：平日上午、週六是尖峰，平日午後與晚上比較空。"""
    h = int(hm[:2]) + int(hm[3:]) / 60
    wd = day.weekday()
    if wd >= 5:
        f = 0.95 if h < 12 else 0.7
    elif h < 11:
        f = 0.85
    elif h < 13:
        f = 0.6
    elif h < 15.5:
        f = 0.3
    elif h < 17:
        f = 0.55
    else:
        f = 0.35
    f += center.get("busy", 0)
    rng = random.Random(zlib.crc32(f"{center['id']}|{day.isoformat()}|{hm}".encode("utf-8")))
    return max(0.0, min(1.0, f + rng.uniform(-0.18, 0.18)))


def slot_capacity(center):
    return max(2, center["bays"] // 3)


_ETA_CACHE = {}


def eta_table(eta_model, minutes, is_hybrid):
    """同一個工單在各種「在修台數 × 時段」下的預估完工分鐘數：一次批次預估後快取（比較頁要算幾百個時段）。"""
    key = (id(eta_model), int(minutes), bool(is_hybrid))
    tab = _ETA_CACHE.get(key)
    if tab is None:
        combos = [(load, hour) for load in range(1, 16) for hour in range(8, 21)]
        vals = eta_model.remaining_many([(minutes, 0, load, hour, 0, 0, is_hybrid) for load, hour in combos])
        tab = dict(zip(combos, vals))
        if len(_ETA_CACHE) > 64:
            _ETA_CACHE.clear()
        _ETA_CACHE[key] = tab
    return tab


def quote(kb, codes, center, offpeak=False, sources=None):
    """透明報價：每一項拆成零件、工資（全網相同費率）、離峰折扣。"""
    sources = sources or {}
    lines = []
    for c in codes:
        item = kb.get(c) or {}
        bd = kb.breakdown(c)
        if bd["included"]:
            lines.append({"code": c, "name": item.get("name", c), "source": sources.get(c, "planned"), "parts": 0, "labor": 0,
                          "discount": 0, "total": 0, "minutes": bd["minutes"], "included": True,
                          "note": item.get("price_note", "")})
            continue
        labor = _r10(bd["minutes"] / 60 * center["labor_rate"])
        disc = _r10(labor * center["offpeak"]["discount"]) if offpeak else 0
        lines.append({"code": c, "name": item.get("name", c), "source": sources.get(c, "planned"), "parts": bd["parts"],
                      "labor": labor, "discount": disc, "total": bd["parts"] + labor - disc, "minutes": bd["minutes"],
                      "included": False, "note": item.get("price_note", "")})
    tot = {k: sum(ln[k] for ln in lines) for k in ("parts", "labor", "discount", "total", "minutes")}
    return dict(tot, lines=lines, labor_rate=center["labor_rate"], offpeak=offpeak)


def day_slots(center, day, now, booked, minutes, is_hybrid, eta_model):
    """某一天的每個時段：擁擠程度、是否離峰、預估報到等候與完工時間。booked＝{(據點, 'YYYY-MM-DDTHH:MM'): 已預約數}。"""
    times = slot_times(center, day)
    if not times:
        return [{"hm": "", "iso": day.isoformat(), "status": "closed"}]
    out = []
    cap = slot_capacity(center)
    tab = eta_table(eta_model, minutes, is_hybrid)
    close = close_time(center, day)
    close_at = dt.datetime.combine(day, dt.time(int(close[:2]), int(close[3:])))
    for hm in times:
        start = dt.datetime.combine(day, dt.time(int(hm[:2]), int(hm[3:])))
        key = start.strftime("%Y-%m-%dT%H:%M")
        if start < now + dt.timedelta(minutes=30):  # 半小時內的時段不開放線上預約
            out.append({"hm": hm, "iso": key, "status": "past"})
            continue
        filled = round(base_fill(center, day, hm) * cap) + booked.get((center["id"], key), 0)
        ratio = filled / cap
        status = "full" if filled >= cap else ("busy" if ratio >= 0.75 else ("normal" if ratio >= 0.4 else "free"))
        offpeak = is_offpeak(center, day, hm) and status in ("free", "normal")
        load = max(1, round((0.35 + 0.65 * min(1.0, ratio)) * center["bays"]))  # 同時在廠的車（示意）
        hour = int(hm[:2])
        wait = round(5 + load * 2.5 + (8 if 9 <= hour <= 11 else 0))  # 報到後等專員接待（和預估完工模型同一套假設）
        total_min = max(20, int(round(tab[(min(load, 15), min(max(hour, 8), 20))] / 5.0)) * 5)
        done = start + dt.timedelta(minutes=total_min)
        if status != "full" and done > close_at:  # 打烊前做不完 → 不開放（例如晚上的時段只適合短項目）
            status, offpeak = "late", False
        out.append({"hm": hm, "iso": key, "status": status, "status_text": STATUS_TEXT[status], "offpeak": offpeak,
                    "wait": wait, "duration": total_min, "done_hm": done.strftime("%H:%M"), "close": close})
    return out


def can_handle(center, need, powertrain):
    """這個據點能不能處理這次的需求；不行的話回傳原因。"""
    cap = NEEDS.get(need, NEEDS["maintenance"])["cap"]
    if cap not in center["caps"]:
        if center["kind"] == "station":
            return False, f"不處理「{NEEDS[need]['label']}」，要到服務廠"
        return False, f"沒有「{NEEDS[need]['label']}」需要的設備（示意）"
    if powertrain == "ev" and "ev" not in center["caps"]:
        return False, "沒有電動車高壓作業資格（示意）"
    return True, ""


def triage(owner, choice=None, text=""):
    """需求判斷（規則版）：車主選的情況＋文字描述 → 這次適合哪一種據點。

    安全優先：描述裡出現比選項更需要完整設備的字眼（例如選了定期保養卻寫「召回」），採用較完整的據點。
    """
    pt = owner["vehicle"]["powertrain"]
    t = (text or "").strip()
    reasons = []
    need = choice if choice in NEEDS else None
    hit = None
    for n in NEED_ORDER:
        w = next((w for w in KEYWORDS[n] if w in t), None)
        if w:
            hit = (n, w)
            break
    if hit and (need is None or NEED_ORDER.index(hit[0]) < NEED_ORDER.index(need)):
        if need is not None:
            reasons.append(f"您的描述提到「{hit[1]}」，所以改用需要較完整設備的判斷")
        need = hit[0]
    need = need or "maintenance"
    if need == "noise" and pt in ("hybrid", "ev"):
        need = "warning"
        reasons.append("油電車與電動車的異音可能和高壓系統有關，建議直接到有診斷設備的服務廠")
    urgency = offline.photo_triage(t)["urgency"] if t else "unknown"
    info = NEEDS[need]
    tips = []
    if urgency == "stop":
        tips.append("如果車子還在路上，請先找安全的地方停車；有冒煙、煞車異常時，請聯絡道路救援，不要自己開過去。")
    tips.append(TIPS[need])
    if pt == "ev":
        reasons.append("電動車只會列出有高壓作業資格的據點（示意）")
    station_ok = info["cap"] in STATION_CAPS and pt != "ev"
    return {"need": need, "label": info["label"], "where": info["where"], "why": info["why"], "reasons": reasons,
            "urgency": urgency, "urgency_label": URGENCY[urgency]["label"], "tips": tips, "station_ok": station_ok,
            "source": "offline"}


def compare(kb, eta_model, owner, codes, sources, now, booked, day_filter=None, need="maintenance"):
    """比較能處理這次需求的據點：每一間找出最推薦的時段、最早可約、最快完工；再挑出整體推薦。"""
    pt = owner["vehicle"]["powertrain"]
    today = now.date()
    days = [today + dt.timedelta(days=i) for i in range(DAYS_AHEAD)]
    if day_filter:
        days = [d for d in days if d.isoformat() == day_filter] or days
    rows, excluded = [], []
    for c in CENTERS.values():
        ok, why = can_handle(c, need, pt)
        if not ok:
            excluded.append({"center": c, "reason": why})
            continue
        q_norm = quote(kb, codes, c, False, sources)
        q_off = quote(kb, codes, c, True, sources)
        dist = distance_km(owner["home"]["xy"], c["xy"])
        best = earliest = fastest = evening = None
        n_free = 0
        for d in days:
            for s in day_slots(c, d, now, booked, q_norm["minutes"], pt == "hybrid", eta_model):
                if s["status"] not in ("free", "normal", "busy"):
                    continue
                n_free += 1
                price = (q_off if s["offpeak"] else q_norm)["total"]
                cand = dict(s, day=d.isoformat(), day_label=day_label(d, today), price=price, days_ahead=(d - today).days)
                cand["score"] = price / 250 + s["duration"] / 20 + cand["days_ahead"] * 0.9 + (2.5 if s["status"] == "busy" else 0)
                if best is None or cand["score"] < best["score"]:
                    best = cand
                if earliest is None:
                    earliest = cand
                if fastest is None or s["duration"] < fastest["duration"]:
                    fastest = cand
                if evening is None and s["hm"] >= "17:00":
                    evening = cand
        if not best:
            excluded.append({"center": c, "reason": "這段時間已經額滿或公休"})
            continue
        rows.append({"center": c, "distance": dist, "normal": q_norm, "offpeak": q_off, "best": best, "earliest": earliest,
                     "fastest": fastest, "evening": evening, "slots_open": n_free, "score": best["score"] + dist * 0.35})
    if not rows:
        return {"rows": [], "recommended": None, "excluded": excluded}
    rows.sort(key=lambda r: r["score"])
    rec = rows[0]
    reasons = []
    if rec["center"]["kind"] == "station":
        reasons.append("社區型保修站：價格和服務廠相同，通常比較近、比較不用等")
    if rec["best"]["offpeak"]:
        reasons.append(f"離峰時段工資打 {round((1 - rec['center']['offpeak']['discount']) * 10, 1):g} 折，省 NT$ {rec['offpeak']['discount']:,}")
    reasons.append(f"預估 {rec['best']['duration']} 分鐘完工（{rec['best']['status_text']}時段，報到約等 {rec['best']['wait']} 分鐘）")
    reasons.append(f"距離約 {rec['distance']} 公里")
    last = owner.get("last_center")
    if rec["center"]["id"] == last:
        reasons.append("您上次就是在這裡保養，保養紀錄都在")
    rec["reasons"] = reasons
    min_dur = min(r["fastest"]["duration"] for r in rows)
    min_dist = min(r["distance"] for r in rows)
    first = min(r["earliest"]["iso"] for r in rows)
    for r in rows:
        r["tags"] = []
        if r is rec:
            r["tags"].append("推薦")
        if r["center"]["id"] == last:
            r["tags"].append("上次回廠")
        if r["fastest"]["duration"] == min_dur:
            r["tags"].append("最快完工")
        if r["distance"] == min_dist:
            r["tags"].append("最近")
        if r["earliest"]["iso"] == first:
            r["tags"].append("最早有空")
        if r["evening"]:
            r["tags"].append("晚上也能約")
    return {"rows": rows, "recommended": rec, "excluded": excluded}


def find_slot(center, iso, now, booked, minutes, is_hybrid, eta_model):
    """找出某個時段（YYYY-MM-DDTHH:MM）目前的狀態；不存在就回傳 None。"""
    try:
        start = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return None
    for s in day_slots(center, start.date(), now, booked, minutes, is_hybrid, eta_model):
        if s["iso"] == iso:
            return s
    return None
