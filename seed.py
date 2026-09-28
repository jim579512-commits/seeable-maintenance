"""示範資料（全部為虛構）。每次按「重置示範資料」都會以當下時間重新產生。"""
import datetime as dt
import random
import time

import offline

SCHEMA = 2  # 資料格式版本：改版時加 1，舊的示範資料會自動重建

# 技師（虛構）。等級、年資、專長皆為示意
TECHNICIANS = {
    "T01": {"id": "T01", "name": "周技師", "level": "高級技師", "years": 12, "specialties": ["油電系統", "底盤懸吊"],
            "intro": "擅長油電車檢修，講解細心，習慣用照片說明給車主看。"},
    "T02": {"id": "T02", "name": "吳技師", "level": "中級技師", "years": 7, "specialties": ["冷氣系統", "電系診斷"],
            "intro": "冷氣與電系問題診斷快，回報時習慣附上量測數據。"},
    "T03": {"id": "T03", "name": "林技師", "level": "高級技師", "years": 15, "specialties": ["引擎與變速箱", "電動車高壓系統"],
            "intro": "負責電動車與油電車大電池檢查（具高壓作業訓練，示意）。"},
    "T04": {"id": "T04", "name": "許技師", "level": "初級技師", "years": 3, "specialties": ["定期保養", "輪胎與定位"],
            "intro": "負責快速保養、輪胎與四輪定位，動作俐落。"},
}

# 技師本月表現（模擬示意，用來展示獎金試算表；今天的即時數字會再加上去）
SIM_TECH = {
    "T01": {"designated": 18, "reports": 42, "quality": 38, "unedited": 31, "five_star": 26, "rating_sum": 163.2, "rating_n": 34},
    "T02": {"designated": 9, "reports": 35, "quality": 29, "unedited": 24, "five_star": 17, "rating_sum": 133.4, "rating_n": 29},
    "T03": {"designated": 14, "reports": 28, "quality": 26, "unedited": 23, "five_star": 21, "rating_sum": 122.5, "rating_n": 25},
    "T04": {"designated": 4, "reports": 19, "quality": 13, "unedited": 11, "five_star": 8, "rating_sum": 72.0, "rating_n": 16},
}

SIM = {
    "label": "模擬示意數據（非實測），只用來展示儀表板長什麼樣子，請勿當成成效放進簡報",
    "weeks": ["導入前", "第 1 週", "第 2 週", "第 3 週", "第 4 週"],
    "reception_min": [12.5, 10.9, 10.1, 9.4, 9.1],
    "lounge_trips": [1.8, 1.3, 0.9, 0.7, 0.6],
    "addon_approval": [38, 44, 48, 51, 53],
    "repeat_questions": [2.3, 1.7, 1.3, 1.1, 1.0],
    "topics_30d": {"necessity": 128, "price": 83, "timing": 61, "progress": 52, "consequence": 34, "policy": 29, "photo": 17, "other": 22},
    "tier_outcomes_30d": {
        "safety": {"approved": 46, "deferred": 6},
        "recommend": {"approved": 71, "deferred": 54},
        "can_wait": {"approved": 9, "deferred": 38},
    },
    "ai_self_resolved_30d": {"answered": 341, "escalated": 68},
    "rating_30d": {"avg": 4.6, "n": 212},
}

CARS = [
    {
        "id": "V001", "plate": "BKR-5821",
        "owner": {"name": "林小姐", "persona": "新手車主", "depth": "simple"},
        "vehicle": {"model": "Corolla Cross Hybrid", "year": 2023, "powertrain": "hybrid", "odometer": 40210, "purchase_date": "2023-03-10"},
        "advisor": "陳專員", "tech": "T01", "requested": True, "checkin_ago": 20, "stage": 1, "sent": True,
        "note": "主要示範車：指名周技師；技師回報 → 專員核可 → 車主決定 → 完工報告；可加選自選項目",
    },
    {
        "id": "V002", "plate": "RDA-2093",
        "owner": {"name": "王先生", "persona": "懂車族", "depth": "technical"},
        "vehicle": {"model": "RAV4 Hybrid", "year": 2021, "powertrain": "hybrid", "odometer": 80560, "purchase_date": "2021-02-18"},
        "advisor": "黃專員", "tech": "T02", "checkin_ago": 75, "stage": 2, "sent": True, "optional": ["ac_clean"],
        "extra_history": {60000: ["wiper"]},
        "note": "預約時自選冷氣清潔；一個追加項目等車主決定（雨刷），一個已延後（電瓶）",
    },
    {
        "id": "V003", "plate": "AKM-7719",
        "owner": {"name": "陳伯伯", "persona": "年長車主", "depth": "simple"},
        "vehicle": {"model": "Corolla Altis", "year": 2018, "powertrain": "gas", "odometer": 102300, "purchase_date": "2018-01-05"},
        "advisor": "陳專員", "tech": "T01", "checkin_ago": 150, "stage": 5, "sent": True,
        "extra_history": {70000: ["battery_12v"], 80000: ["tire"]},
        "note": "已完工、剛跨過 10 萬公里：示範愛車健康報告與里程碑",
    },
    {
        "id": "V004", "plate": "BPQ-3306",
        "owner": {"name": "張先生", "persona": "忙碌上班族", "depth": "simple"},
        "vehicle": {"model": "Yaris Cross", "year": 2024, "powertrain": "gas", "odometer": 10050, "purchase_date": "2024-11-20"},
        "advisor": "李專員", "tech": "T02", "checkin_ago": 3, "stage": 0, "sent": False,
        "note": "剛報到、說明頁還沒做：示範專員用 AI 產生說明頁",
    },
    {
        "id": "V005", "plate": "RCF-6618",
        "owner": {"name": "黃小姐", "persona": "一般車主", "depth": "simple"},
        "vehicle": {"model": "Sienta Hybrid", "year": 2022, "powertrain": "hybrid", "odometer": 30120, "purchase_date": "2022-06-30"},
        "advisor": "黃專員", "tech": "T02", "checkin_ago": 110, "stage": 3, "sent": True,
        "note": "車主拍了儀表警示燈照片；一個保固問題待回覆；一個技師回報待專員審核",
    },
    {
        "id": "V006", "plate": "BLT-0452",
        "owner": {"name": "李先生", "persona": "一般車主", "depth": "simple"},
        "vehicle": {"model": "Corolla Altis Hybrid", "year": 2022, "powertrain": "hybrid", "odometer": 50010, "purchase_date": "2022-01-15"},
        "advisor": "李專員", "tech": "T04", "appointment_in": 30, "stage": -1, "sent": False,
        "note": "預約未到：示範到廠前指定技師、預先加選自選項目、報到",
    },
    {
        "id": "V007", "plate": "EAA-1203",
        "owner": {"name": "吳小姐", "persona": "電動車新手", "depth": "simple"},
        "vehicle": {"model": "bZ4X", "year": 2025, "powertrain": "ev", "odometer": 20030, "purchase_date": "2025-01-08"},
        "advisor": "李專員", "tech": "T03", "requested": True, "appointment_in": 60, "stage": -1, "sent": False,
        "note": "預約未到的電動車：已指名電動車專長的林技師；示範電動車的保養項目",
    },
]


def _history(kb, car, now, rng):
    veh = car["vehicle"]
    buy = dt.datetime.strptime(veh["purchase_date"], "%Y-%m-%d")
    total_days = max(1, (now - buy).days)
    current = veh["odometer"]
    hist = [{
        "date": (buy + dt.timedelta(days=32)).strftime("%Y-%m-%d"),
        "km": 1000 + rng.randint(-120, 180),
        "type": "1,000 公里滿月檢查",
        "items": ["inspection"],
    }]
    bucket = 10000
    extra = car.get("extra_history", {})
    while bucket < current - 5000:
        d = buy + dt.timedelta(days=int(total_days * bucket / current))
        items = kb.scheduled_items(bucket, veh["powertrain"]) + extra.get(bucket, [])
        hist.append({
            "date": d.strftime("%Y-%m-%d"),
            "km": bucket + rng.randint(-150, 260),
            "type": f"{bucket:,} 公里定期保養",
            "items": items,
        })
        bucket += 10000
    return hist


def _bucket(km):
    return int(round(km / 10000.0)) * 10000


def build_seed(kb, eta_text_fn=None):
    now_ts = time.time()
    now = dt.datetime.fromtimestamp(now_ts)
    rng = random.Random(7)
    state = {"schema": SCHEMA, "created_at": now_ts, "seq": {"V": 7, "A": 0, "M": 0, "P": 0}, "visits": {}, "addons": {},
             "sim": SIM, "technicians": {k: dict(v) for k, v in TECHNICIANS.items()},
             "sim_tech": {k: dict(v) for k, v in SIM_TECH.items()}}

    def msg_id():
        state["seq"]["M"] += 1
        return f"M{state['seq']['M']:04d}"

    for car in CARS:
        veh = car["vehicle"]
        bucket = _bucket(veh["odometer"])
        visit = {
            "id": car["id"],
            "plate": car["plate"],
            "owner": dict(car["owner"]),
            "vehicle": dict(veh),
            "service_type": f"{bucket:,} 公里定期保養",
            "bucket_km": bucket,
            "advisor": car["advisor"],
            "technician_id": car["tech"],
            "technician": TECHNICIANS[car["tech"]]["name"],
            "requested_tech_id": car["tech"] if car.get("requested") else None,
            "tech_rating": None,
            "photo_checks": [],
            "note": car.get("note", ""),
            "stage": car["stage"],
            "stage_times": {},
            "items": [kb.work_item(c) for c in kb.scheduled_items(bucket, veh["powertrain"])]
                     + [kb.work_item(c, source="optional") for c in car.get("optional", [])],
            "explanation": None,
            "addon_ids": [],
            "chat": [],
            "concerns": None,
            "report": None,
            "rating": None,
            "history": _history(kb, car, now, rng),
            "version": 1,
        }
        if car["stage"] >= 0:
            checkin = now_ts - car["checkin_ago"] * 60
            visit["appointment_at"] = checkin - 5 * 60
            visit["checkin_at"] = checkin
            span = car["checkin_ago"] * 60
            for s in range(car["stage"] + 1):
                visit["stage_times"][str(s)] = checkin + span * s / (car["stage"] + 1)
        else:
            visit["appointment_at"] = now_ts + car["appointment_in"] * 60
            visit["checkin_at"] = None
        if car["sent"]:
            exp = offline.explanation(visit, kb, now)
            exp.update({"source": "offline", "generated_at": visit["checkin_at"] + 120, "sent": True,
                        "sent_at": visit["checkin_at"] + 240, "edited": False})
            visit["explanation"] = exp
        state["visits"][car["id"]] = visit

    def add_addon(vid, code, value, note, status, sent_ago=None, decided_ago=None):
        visit = state["visits"][vid]
        state["seq"]["A"] += 1
        aid = f"A{state['seq']['A']:03d}"
        item = kb.get(code)
        draft = offline.addon_draft(visit, code, value, note, kb)
        draft["source"] = "offline"
        created = now_ts - (sent_ago or 5) * 60 - 180
        addon = {
            "id": aid, "visit_id": vid, "item_code": code, "item_name": item["name"],
            "measure": {"value": value, "unit": item.get("unit", "")} if value is not None else None,
            "tech_note": note, "technician": visit["technician"], "technician_id": visit["technician_id"],
            "advisor": visit["advisor"],
            "media": [{"type": "image", "url": "/static/img/samples/" + item["sample"], "sample": True}],
            "price": item["price"], "minutes": item["minutes"],
            "draft": draft, "final": None, "status": status, "ask_advisor": False,
            "created_at": created, "sent_at": None, "decided_at": None,
        }
        if status in ("sent", "approved", "deferred"):
            addon["final"] = dict(draft)
            addon["sent_at"] = now_ts - sent_ago * 60
        if status in ("approved", "deferred"):
            addon["decided_at"] = now_ts - decided_ago * 60
        if status == "approved":
            visit["items"].append(kb.work_item(code, source="addon"))
        state["addons"][aid] = addon
        visit["addon_ids"].append(aid)
        return aid

    add_addon("V002", "wiper", None, "前雨刷膠條硬化，刮拭有殘影，建議換一對", "sent", sent_ago=12)
    add_addon("V002", "battery_12v", 62, "12V 電瓶測試健康度 62%，CCA 偏低但可正常啟動，先觀察", "deferred", sent_ago=40, decided_ago=30)
    add_addon("V003", "brake_pad", 4.0, "前輪來令片剩 4mm，碟盤正常無溝痕", "approved", sent_ago=110, decided_ago=104)
    add_addon("V003", "throttle_clean", None, "節氣門輕微積碳，怠速正常", "deferred", sent_ago=108, decided_ago=104)
    add_addon("V005", "tire", 2.4, "前輪胎紋 2.4mm，胎壁無龜裂，後輪 4.5mm", "draft")

    def ask(vid, question, ago, about=None, escalate_pending=True):
        visit = state["visits"][vid]
        addons = [state["addons"][a] for a in visit["addon_ids"]]
        eta_text = eta_text_fn(visit, state) if eta_text_fn else "專員會通知您取車時間。"
        res = offline.answer(visit, addons, question, kb, eta_text, about=about)
        ts = now_ts - ago * 60
        visit["chat"].append({"id": msg_id(), "role": "user", "text": question, "ts": ts, "topic": res["topic"],
                              "escalated": res["escalate"], "resolved": not (res["escalate"] and escalate_pending)})
        visit["chat"].append({"id": msg_id(), "role": "ai", "text": res["answer"], "ts": ts + 3, "source": "offline"})

    ask("V002", "雨刷多少錢？", 10)
    ask("V002", "電瓶可以下次再換嗎？", 34)
    ask("V003", "為什麼要換火星塞？", 120)
    ask("V005", "這次保養總共多少錢？", 70)
    ask("V005", "延長保固要怎麼申請？", 25)

    # 車主拍照上傳（示範沒有影像 AI 時，依文字描述做的初步整理）
    def photo(vid, text, sample, ago):
        visit = state["visits"][vid]
        codes = [i["code"] for i in visit["items"]]
        res = offline.photo_triage(text, kb, codes)
        res.update({"source": "offline", "vision": False, "note": "目前沒有使用 AI，以下依您的文字描述整理"})
        ts = now_ts - ago * 60
        state["seq"]["P"] += 1
        pid = f"P{state['seq']['P']:03d}"
        url = "/static/img/samples/" + sample
        um = {"id": msg_id(), "role": "user", "kind": "photo", "text": text, "image": url, "ts": ts,
              "topic": "photo", "escalated": True, "resolved": False, "photo_id": pid}
        am = {"id": msg_id(), "role": "ai", "kind": "photo_analysis", "ts": ts + 3, "source": "offline",
              "urgency": res["urgency"], "escalated": True,
              "text": offline.format_photo_reply(res, visit["advisor"], visit["technician"])}
        visit["chat"] += [um, am]
        visit["photo_checks"].append({"id": pid, "msg_id": um["id"], "url": url, "text": text, "ts": ts,
                                      "urgency": res["urgency"], "summary": res["summary"], "status": "new",
                                      "checked_by": None, "checked_at": None, "sample": True})

    photo("V005", "儀表板亮了一個黃色警示燈，是什麼意思？", "warning_light.svg", 15)

    for visit in state["visits"].values():
        visit["chat"].sort(key=lambda m: m["ts"])
    return state
