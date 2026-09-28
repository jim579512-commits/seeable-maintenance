"""線上預約：TOYOTA 各服務廠的透明報價、時段擁擠度與推薦（全部為示意資料）。

原則：
- 零件是原廠統一價，每一廠都一樣；不同的是工資費率、離峰優惠、可預約時段與預估等待時間。
- 離峰優惠把車主從尖峰時段分流出去——這正是題目「尖峰接待說明不清、客休室等太久」的解法之一。
- 預估完工時間用同一個預估完工模型（eta.py），和到廠後車主手機上看到的一致。
"""
import datetime as dt
import math
import random
import zlib

HOME_CENTER = "C01"  # 內部系統（今日工單）所在的服務廠
SLOT_TIMES = ["08:30", "09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
              "13:00", "13:30", "14:00", "14:30", "15:00", "15:30", "16:00"]
DAYS_AHEAD = 7
WEEKDAY = "一二三四五六日"
STATUS_TEXT = {"free": "空", "normal": "普通", "busy": "擁擠", "full": "額滿", "past": "已過"}

# 服務廠（示意）：名稱、地點、費率、評分、服務項目都是虛構的，只用來展示比較畫面
CENTERS = {
    "C01": {"id": "C01", "name": "內湖服務廠", "district": "台北市內湖區", "xy": (6.0, 5.0), "bays": 8, "labor_rate": 1200,
            "offpeak": {"days": [0, 1, 2, 3, 4], "start": "13:30", "end": "15:30", "discount": 0.2}, "busy": 0.05,
            "amenities": ["客休室", "兒童遊戲區", "電動車充電"], "rating": 4.7, "rating_n": 812, "advisor": "李專員"},
    "C02": {"id": "C02", "name": "南港服務廠", "district": "台北市南港區", "xy": (8.6, 2.6), "bays": 6, "labor_rate": 1200,
            "offpeak": {"days": [0, 1, 2, 3, 4], "start": "13:00", "end": "15:00", "discount": 0.15}, "busy": 0.0,
            "amenities": ["客休室", "接送服務"], "rating": 4.6, "rating_n": 455, "advisor": "高專員"},
    "C03": {"id": "C03", "name": "松山服務廠", "district": "台北市松山區", "xy": (3.0, 3.4), "bays": 5, "labor_rate": 1260,
            "offpeak": {"days": [0, 1, 2, 3], "start": "14:00", "end": "15:30", "discount": 0.1}, "busy": 0.12,
            "amenities": ["客休室", "代步車"], "rating": 4.5, "rating_n": 603, "advisor": "蔡專員"},
    "C04": {"id": "C04", "name": "汐止服務廠", "district": "新北市汐止區", "xy": (11.6, 6.4), "bays": 10, "labor_rate": 1150,
            "offpeak": {"days": [0, 1, 2, 3, 4], "start": "13:00", "end": "15:30", "discount": 0.25}, "busy": -0.08,
            "amenities": ["客休室", "洗車", "電動車充電"], "rating": 4.6, "rating_n": 389, "advisor": "游專員"},
    "C05": {"id": "C05", "name": "大直服務廠", "district": "台北市中山區", "xy": (2.8, 6.6), "bays": 7, "labor_rate": 1230,
            "offpeak": {"days": [0, 1, 2, 3, 4], "start": "13:30", "end": "15:00", "discount": 0.15}, "busy": 0.08,
            "amenities": ["客休室", "接送服務", "代步車"], "rating": 4.8, "rating_n": 927, "advisor": "簡專員"},
}

# 依用車習慣建議的自選項目（規則；畫面上會寫出原因）
SUGGEST = {
    "短程通勤": (["fuel_system_clean", "ac_clean"], "常短程通勤，引擎溫度常還沒升高就熄火，較容易積碳；冷氣也常開"),
    "長途高速": (["wheel_alignment", "glass_coating"], "常跑高速，輪胎角度與擋風玻璃的視線影響比較大"),
    "市區代步": (["ac_clean", "interior_sanitize"], "市區代步常開冷氣、也常載家人"),
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


def is_offpeak(center, day, hm):
    op = center["offpeak"]
    return day.weekday() in op["days"] and op["start"] <= hm < op["end"]


def base_fill(center, day, hm):
    """這個時段原本就被預約的比例（示意）：平日上午、週六是尖峰，平日午後是離峰；週日公休。"""
    h = int(hm[:2]) + int(hm[3:]) / 60
    wd = day.weekday()
    if wd == 5:
        f = 0.95 if h < 12 else 0.7
    elif h < 11:
        f = 0.85
    elif h < 13:
        f = 0.6
    elif h < 15.5:
        f = 0.3
    else:
        f = 0.55
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
        combos = [(load, hour) for load in range(1, 16) for hour in range(8, 18)]
        vals = eta_model.remaining_many([(minutes, 0, load, hour, 0, 0, is_hybrid) for load, hour in combos])
        tab = dict(zip(combos, vals))
        if len(_ETA_CACHE) > 64:
            _ETA_CACHE.clear()
        _ETA_CACHE[key] = tab
    return tab


def quote(kb, codes, center, offpeak=False, sources=None):
    """透明報價：每一項拆成零件（各廠相同）、工資（依該廠費率）、離峰折扣。"""
    sources = sources or {}
    lines = []
    for c in codes:
        item = kb.get(c) or {}
        bd = kb.breakdown(c)
        if bd["included"]:
            lines.append({"code": c, "name": item.get("name", c), "source": sources.get(c, "planned"), "parts": 0, "labor": 0,
                          "discount": 0, "total": 0, "minutes": bd["minutes"], "included": True})
            continue
        labor = _r10(bd["minutes"] / 60 * center["labor_rate"])
        disc = _r10(labor * center["offpeak"]["discount"]) if offpeak else 0
        lines.append({"code": c, "name": item.get("name", c), "source": sources.get(c, "planned"), "parts": bd["parts"],
                      "labor": labor, "discount": disc, "total": bd["parts"] + labor - disc, "minutes": bd["minutes"],
                      "included": False})
    tot = {k: sum(ln[k] for ln in lines) for k in ("parts", "labor", "discount", "total", "minutes")}
    return dict(tot, lines=lines, labor_rate=center["labor_rate"], offpeak=offpeak)


def day_slots(center, day, now, booked, minutes, is_hybrid, eta_model):
    """某一天的每個時段：擁擠程度、是否離峰、預估報到等候與完工時間。booked＝{(廠, 'YYYY-MM-DD HH:MM'): 已預約數}。"""
    out = []
    cap = slot_capacity(center)
    tab = eta_table(eta_model, minutes, is_hybrid)
    for hm in SLOT_TIMES:
        start = dt.datetime.combine(day, dt.time(int(hm[:2]), int(hm[3:])))
        key = start.strftime("%Y-%m-%dT%H:%M")
        if day.weekday() == 6:
            out.append({"hm": hm, "iso": key, "status": "closed"})
            continue
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
        total_min = max(20, int(round(tab[(min(load, 15), min(max(hour, 8), 17))] / 5.0)) * 5)
        out.append({"hm": hm, "iso": key, "status": status, "status_text": STATUS_TEXT[status], "offpeak": offpeak,
                    "wait": wait, "duration": total_min, "done_hm": (start + dt.timedelta(minutes=total_min)).strftime("%H:%M")})
    return out


def compare(kb, eta_model, owner, codes, sources, now, booked, day_filter=None):
    """比較各服務廠：每一廠找出最推薦的時段、最早可約、最便宜；再挑出整體推薦。"""
    pt = owner["vehicle"]["powertrain"]
    today = now.date()
    days = [today + dt.timedelta(days=i) for i in range(DAYS_AHEAD)]
    if day_filter:
        days = [d for d in days if d.isoformat() == day_filter] or days
    rows = []
    for c in CENTERS.values():
        q_norm = quote(kb, codes, c, False, sources)
        q_off = quote(kb, codes, c, True, sources)
        dist = distance_km(owner["home"]["xy"], c["xy"])
        best = earliest = cheapest = fastest = None
        n_free = 0
        for i, d in enumerate(days):
            for s in day_slots(c, d, now, booked, q_norm["minutes"], pt == "hybrid", eta_model):
                if s["status"] not in ("free", "normal", "busy"):
                    continue
                n_free += 1
                price = (q_off if s["offpeak"] else q_norm)["total"]
                cand = dict(s, day=d.isoformat(), day_label=day_label(d, today), price=price, days_ahead=(d - today).days)
                score = price / 250 + s["duration"] / 20 + cand["days_ahead"] * 0.9 + (2.5 if s["status"] == "busy" else 0)
                cand["score"] = score
                if best is None or score < best["score"]:
                    best = cand
                if earliest is None:
                    earliest = cand
                if cheapest is None or price < cheapest["price"]:
                    cheapest = cand
                if fastest is None or s["duration"] < fastest["duration"]:
                    fastest = cand
        if not best:
            continue
        rows.append({"center": c, "distance": dist, "normal": q_norm, "offpeak": q_off, "best": best, "earliest": earliest,
                     "cheapest": cheapest, "fastest": fastest, "slots_open": n_free,
                     "score": best["score"] + dist * 0.35})
    if not rows:
        return {"rows": [], "recommended": None}
    rows.sort(key=lambda r: r["score"])
    rec = rows[0]
    avg = sum(r["normal"]["total"] for r in rows) / len(rows)
    reasons = []
    if rec["best"]["offpeak"]:
        reasons.append(f"離峰時段工資打 {round((1 - rec['center']['offpeak']['discount']) * 10, 1):g} 折，省 NT$ {rec['offpeak']['discount']:,}")
    if rec["best"]["price"] < avg:
        reasons.append(f"比各廠一般時段平均便宜約 NT$ {int(avg - rec['best']['price']):,}")
    reasons.append(f"預估 {rec['best']['duration']} 分鐘完工（{rec['best']['status_text']}時段，報到約等 {rec['best']['wait']} 分鐘）")
    reasons.append(f"距離約 {rec['distance']} 公里")
    rec["reasons"] = reasons
    for r in rows:
        r["tags"] = []
    min_price = min(r["cheapest"]["price"] for r in rows)
    min_dur = min(r["fastest"]["duration"] for r in rows)
    min_dist = min(r["distance"] for r in rows)
    first = min(r["earliest"]["iso"] for r in rows)
    for r in rows:
        if r is rec:
            r["tags"].append("推薦")
        if r["cheapest"]["price"] == min_price:
            r["tags"].append("最省錢")
        if r["fastest"]["duration"] == min_dur:
            r["tags"].append("最快完工")
        if r["distance"] == min_dist:
            r["tags"].append("最近")
        if r["earliest"]["iso"] == first:
            r["tags"].append("最早有空")
    return {"rows": rows, "recommended": rec}


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
