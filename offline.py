"""離線規則版的文字產生器。

沒有設定 AI（或 AI 連線失敗）時使用。內容完全依知識庫與工單資料用規則組合，不是 AI 生成；
畫面上會標示「離線規則」，避免把規則產生的內容誤當成 AI 的成果。
"""
import datetime as dt

from kb import TIERS, TOPICS, STAGES, DONE_STAGE, POWERTRAIN_TEXT, URGENCY

POLICY_WORDS = ["保固", "延保", "優惠", "折扣", "打折", "保險", "理賠", "退費", "退款", "發票", "點數", "贈品", "集點", "分期"]
HUMAN_WORDS = ["真人", "找專員", "叫專員", "客服", "找人", "打電話給"]
# 判斷順序有意義：「先不換會怎樣」要算後果，「先不做可以嗎」要算延後。
INTENTS = [
    ("progress", ["多久", "幾點", "什麼時候好", "何時好", "好了沒", "好了嗎", "進度", "還要等", "等多久", "完工", "取車", "還要多"]),
    ("consequence", ["會怎樣", "會怎麼樣", "後果", "風險", "危險", "有影響", "什麼影響"]),
    ("timing", ["下次", "延後", "晚點", "可以等", "撐", "先不要", "之後再", "以後再", "緩一緩", "先不換", "先不做", "不換可以", "不做可以"]),
    ("price", ["多少錢", "費用", "價格", "價錢", "貴", "便宜", "划算", "總共", "總價", "預算", "花多少", "幾錢"]),
    ("necessity", ["為什麼", "為何", "需要嗎", "一定要", "必要", "有需要", "幹嘛", "是什麼", "什麼是", "做什麼", "用途", "作用"]),
]


def _date(s):
    return dt.datetime.strptime(s, "%Y-%m-%d")


def months_between(a, b):
    return max(0, (b.year - a.year) * 12 + (b.month - a.month) - (1 if b.day < a.day else 0))


def last_done(visit, code):
    for h in reversed(visit.get("history", [])):
        if code in h.get("items", []):
            return h
    return None


def _ym(d):
    return f"{d.year} 年 {d.month} 月"


def personal_note(visit, code, kb, now):
    item = kb.get(code) or {}
    veh = visit["vehicle"]
    km_now = veh["odometer"]
    work = next((i for i in visit.get("items", []) if i["code"] == code), None)
    if work and work.get("source") == "optional":
        return "這是您自己加選的項目，不影響行車安全。" + (f"適合：{item['benefit']}。" if item.get("benefit") else "")
    if item.get("routine"):
        if code == "hybrid_check":
            return "油電車每次保養都會用診斷電腦檢查一次，確認電池與系統數據正常。"
        if code == "ev_battery_check":
            return "電動車每次保養都會檢查電池健康度，報告會一起放進今天的健康報告。"
        return "每次保養都會做，確認車況正常；發現問題會先拍給您看。"
    h = last_done(visit, code)
    buy = _date(veh["purchase_date"])
    if h is None:
        years = months_between(buy, now) / 12
        text = f"這是您的車第一次做這個項目（交車至今約 {years:.1f} 年、{km_now:,} 公里）"
        if item.get("interval_months") and months_between(buy, now) >= item["interval_months"]:
            text += "，已經超過建議週期，這次做剛好。"
        elif item.get("interval_km") and km_now >= item["interval_km"] * 0.9:
            text += "，里程已到建議週期。"
        else:
            text += "。"
        return text
    last_dt = _date(h["date"])
    months = months_between(last_dt, now)
    dkm = km_now - h["km"]
    text = f"上次是 {_ym(last_dt)}（{h['km']:,} 公里）做的，到現在約 {months} 個月、{dkm:,} 公里"
    due_km = item.get("interval_km") and dkm >= item["interval_km"] * 0.9
    due_mo = item.get("interval_months") and months >= item["interval_months"] * 0.9
    if due_km or due_mo:
        text += "，已到建議週期，這次做剛好。"
    else:
        text += "，還沒完全到週期；這次一起做是為了配合保養時程，也可以和專員討論。"
    return text


def explanation(visit, kb, now=None):
    now = now or dt.datetime.now()
    owner, veh = visit["owner"], visit["vehicle"]
    items = visit["items"]
    total = sum(i["price"] for i in items)
    routine = [i for i in items if (kb.get(i["code"]) or {}).get("routine")]
    optional = [i for i in items if i.get("source") == "optional"]
    added = [i for i in items if i.get("source") == "addon"]
    scheduled_n = len(items) - len(routine) - len(optional) - len(added)
    if owner.get("depth") == "technical":
        greeting = f"{owner['name']}您好，今天的工單共 {len(items)} 個項目，下面附上各項目的技術說明與這台車的保養紀錄。"
    else:
        greeting = f"{owner['name']}您好！今天幫您的 {veh['model']} 做{visit['service_type']}，下面用簡單的方式說明每個項目。"
    summary = (
        f"今天共 {len(items)} 個項目，預估費用 NT$ {total:,}（示意價格）。"
        f"其中 {scheduled_n} 項是依里程或時間到期的更換，{len(routine)} 項是每次都會做的檢查"
        + (f"，{len(optional)} 項是您自己加選的" if optional else "") + "。"
        "技師檢查時若發現其他需要處理的地方，會先拍照或錄影給您看，由您決定要不要做。"
    )
    return {
        "greeting": greeting,
        "summary": summary,
        "items": {i["code"]: {"personal_note": personal_note(visit, i["code"], kb, now)} for i in items},
    }


ADDON_TITLES = {
    "brake_pad": "煞車來令片變薄了",
    "tire": "輪胎胎紋變淺了",
    "battery_12v": "12V 電瓶健康度下降",
    "wiper": "雨刷膠條硬化了",
    "throttle_clean": "節氣門有些積碳",
    "cabin_filter": "冷氣濾網比較髒",
    "air_filter": "空氣濾芯比較髒",
}

SUGGESTIONS = {
    "safety": "這項和行車安全直接相關，建議今天就處理。",
    "recommend": "今天開回家沒有安全疑慮，但建議近期處理；今天一起做可以省下再跑一趟的時間。",
    "can_wait": "目前狀況還可以，不用今天處理，我們會在下次保養時再幫您檢查。",
}


def _measure_sentence(code, value, unit, item):
    if value is None:
        return ""
    if code == "brake_pad":
        return f"技師量到您的來令片剩 {value:g} mm（新品約 {item.get('new_value', 10)} mm）。"
    if code == "tire":
        return f"技師量到胎紋深度剩 {value:g} mm（新胎約 {item.get('new_value', 8)} mm）。"
    if code == "battery_12v":
        return f"電瓶測試器顯示健康度 {value:g}%。"
    return f"技師量測值：{value:g} {unit or ''}。"


def addon_draft(visit, code, value, tech_note, kb):
    item = kb.get(code) or {}
    name = item.get("name", code)
    rule_tier = kb.rule_tier(code, value)
    tier = rule_tier or item.get("default_tier", "recommend")
    position = ""
    if tech_note:
        if "前" in tech_note:
            position = "前輪" if code in ("brake_pad", "tire") else ""
        elif "後" in tech_note:
            position = "後輪" if code in ("brake_pad", "tire") else ""
    title = position + ADDON_TITLES.get(code, f"{name}需要注意")
    plain = item.get("one_liner", "") + _measure_sentence(code, value, item.get("unit"), item)
    if rule_tier and item.get("rule_text"):
        tier_reason = "依原廠門檻判定：" + item["rule_text"]
    else:
        tier_reason = TIERS[tier]["desc"] + "。"
    return {
        "title": title,
        "plain": plain,
        "tier": tier,
        "tier_reason": tier_reason,
        "consequence": item.get("skip", ""),
        "suggestion": SUGGESTIONS[tier],
        "rule_tier": rule_tier,
    }


def classify_topic(question):
    q = question or ""
    if any(w in q for w in POLICY_WORDS):
        return "policy"
    for topic, words in INTENTS:
        if any(w in q for w in words):
            return topic
    return "other"


def _handoff(visit):
    return f"我已經把問題轉給{visit['advisor']}，專員會盡快在這裡回覆您，或到客休室找您。"


WAIT_WORDS = ["久", "慢", "無聊", "趕", "來不及", "急", "還要多", "等很"]
WORRY_WORDS = ["擔心", "怕", "會不會", "危險", "嚴重", "貴", "壞掉", "安全嗎"]


def warm(res, question, visit):
    """給等待中的車主一點情緒價值：先接住感受（等太久、擔心），再給具體資訊與可以做的事。不浮誇、不承諾做不到的事。"""
    if res.get("escalate") or res.get("topic") == "policy":
        return res
    q = question or ""
    t = res["answer"]
    in_shop = 0 <= visit.get("stage", -1) < 5
    waited = any(w in q for w in WAIT_WORDS)
    if any(w in q for w in WORRY_WORDS):
        t = "會擔心很正常，我們一起把狀況看清楚。" + t
    if res.get("topic") == "progress" or waited:
        if waited:
            t = "讓您久等了，謝謝您的耐心。" + t
        if in_shop:
            t += " 等待的時候，可以到下方「客休」分頁點飲品、安排安靜座位，或選擇先離開，完工前我們會通知您。"
    elif res.get("topic") == "price":
        t += " 所有費用都會先讓您看過、您同意了才會做，不會自動加價。"
    elif res.get("topic") == "necessity":
        t += " 有看不懂的地方隨時再問我，我再用更白話的方式說。"
    return dict(res, answer=t)


def answer(visit, addons, question, kb, eta_text, about=None):
    """規則式問答：回傳 {answer, escalate, topic}。不懂車的車主也看得懂，並照顧等待中的感受。"""
    return warm(_answer_core(visit, addons, question, kb, eta_text, about), question, visit)


def _answer_core(visit, addons, question, kb, eta_text, about=None):
    q = (question or "").strip()
    topic = classify_topic(q)
    advisor = visit["advisor"]
    if topic == "policy":
        return {
            "answer": "這個問題和您車輛的保固或優惠資格有關，需要專員查您的車籍資料才能正確回答。" + _handoff(visit),
            "escalate": True,
            "topic": "policy",
        }
    if any(w in q for w in HUMAN_WORDS):
        return {"answer": f"好的，已經通知{advisor}。" + _handoff(visit), "escalate": True, "topic": "other"}

    item_codes = [i["code"] for i in visit["items"]]
    addon_by_code = {a["item_code"]: a for a in addons}
    code = kb.match_item(q, item_codes + list(addon_by_code.keys()))
    if code is None and about:
        about_addon = next((a for a in addons if a["id"] == about), None)
        if about_addon:
            code = about_addon["item_code"]
    item = kb.get(code) if code else None
    addon = addon_by_code.get(code) if code else None
    work = next((i for i in visit["items"] if i["code"] == code), None)

    if topic == "progress":
        return {"answer": eta_text + " 進度有變化時，說明頁會自動更新。", "escalate": False, "topic": "progress"}

    if topic == "price":
        if work or addon:
            price = (work or addon)["price"]
            note = (item or {}).get("price_note", "")
            extra = f"（{note}）" if note else ""
            return {
                "answer": f"「{(item or {}).get('name', code)}」的費用是 NT$ {price:,}{extra}。以上是示意價格，實際金額以結帳為準。",
                "escalate": False,
                "topic": "price",
            }
        total = sum(i["price"] for i in visit["items"])
        pending = [a for a in addons if a["status"] == "sent"]
        text = f"今天工單預估 NT$ {total:,}"
        if pending:
            text += f"；另外有 {len(pending)} 個追加項目等您決定，全部同意會再增加 NT$ {sum(a['price'] for a in pending):,}"
        return {"answer": text + "。以上是示意價格，實際金額以結帳為準。", "escalate": False, "topic": "price"}

    if item is None:
        if topic == "necessity" or topic == "other":
            names = "、".join(i["name"] for i in visit["items"][:6])
            if topic == "other":
                return {
                    "answer": "這個問題我沒辦法從今天的工單和原廠資料確認，為了避免給您錯誤的資訊，" + _handoff(visit),
                    "escalate": True,
                    "topic": "other",
                }
            nums = "①②③④⑤"
            plain = [f"{nums[n]}{i['name']}：{(kb.get(i['code']) or {}).get('one_liner', '').rstrip('。')}"
                     for n, i in enumerate([x for x in visit["items"] if (kb.get(x["code"]) or {}).get("one_liner")][:5])]
            text = ("今天主要做這幾件事：" + "；".join(plain) + "。") if plain else f"今天的項目有：{names}。"
            return {
                "answer": text + "想多了解哪一項，直接問我，例如「為什麼要換煞車油？」",
                "escalate": False,
                "topic": "necessity",
            }
        return {
            "answer": "請問您想問的是哪一個項目呢？例如「煞車油」「冷氣濾網」，我可以針對那個項目說明。",
            "escalate": False,
            "topic": topic,
        }

    tier = None
    if addon:
        tier = (addon.get("final") or addon.get("draft") or {}).get("tier")

    if topic == "consequence":
        text = f"如果先不處理「{item['name']}」：{item.get('skip', '')}"
        if tier == "can_wait":
            text += " 不過以目前量測到的狀況，技師判斷可以等到下次保養再處理。"
        return {"answer": text, "escalate": False, "topic": "consequence"}

    if topic == "timing":
        if addon:
            text = {
                "safety": f"「{item['name']}」和行車安全直接相關，不建議延後。若今天時間不方便，請和{advisor}約最近的時間回廠。",
                "recommend": f"可以延後，今天開回家沒有安全疑慮；但建議近期處理，避免狀況變嚴重。",
                "can_wait": "可以的。以目前的狀況，下次保養時再幫您檢查即可，不需要今天處理。",
            }.get(tier, "可以和專員討論處理的時間。")
        else:
            text = f"「{item['name']}」是依里程或時間到期的定期項目（{item.get('interval_text', '依原廠建議')}），建議照週期完成；若有預算考量，可以請{advisor}幫您排優先順序。"
        return {"answer": text, "escalate": False, "topic": "timing"}

    # 必要性／一般說明：先一句白話（這是什麼、為什麼要做），再用生活比喻，最後才是您的車的狀況
    text = item.get("one_liner") or item.get("detail", "")
    if item.get("analogy"):
        text += " 打個比方：" + item["analogy"]
    if work:
        text += " " + personal_note(visit, code, kb, dt.datetime.now())
    return {"answer": text, "escalate": False, "topic": "necessity" if topic in ("necessity", "other") else topic}


def concerns(visit):
    qs = [m for m in visit.get("chat", []) if m["role"] == "user"]
    if not qs:
        return {"summary": "車主目前還沒有提出問題。", "points": [], "suggest": "交車時可以簡短確認今天的項目與下次回廠時間。"}
    counts = {}
    for m in qs:
        counts[m.get("topic", "other")] = counts.get(m.get("topic", "other"), 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    summary = f"車主問了 {len(qs)} 個問題，最在意：" + "、".join(f"{TOPICS[t].split('（')[0]}（{n}）" for t, n in ranked[:3])
    points = []
    for m in qs[-5:]:
        flag = "已轉專員" if m.get("escalated") else "AI 已回答"
        points.append(f"「{m['text']}」— {TOPICS.get(m.get('topic', 'other'), '其他').split('（')[0]}，{flag}")
    top = ranked[0][0]
    suggest = {
        "price": "交車時再次說明總金額，並區分哪些是定期項目、哪些是追加項目。",
        "timing": "交車時說清楚哪些項目可以延後、下次回廠會再檢查什麼。",
        "policy": "有保固／優惠問題待回覆，請先查車籍資料再回覆車主。",
        "necessity": "車主對項目的必要性有疑問，交車時可用實際照片或舊零件說明。",
        "consequence": "車主在意不處理的風險，說明時請把安全與非安全項目分開講。",
        "progress": "車主在意時間，交車時可主動告知實際完工時間與原因。",
    }.get(top, "交車時簡短確認今天的項目與下次回廠時間。")
    return {"summary": summary, "points": points, "suggest": suggest}


def report_text(visit, data):
    veh = visit["vehicle"]
    if data.get("milestone"):
        headline = f"恭喜！您的 {veh['model']} 已經陪您走過 {data['milestone']}"
    else:
        headline = f"今天的保養完成了，{veh['model']} 狀況良好"
    done_n = len(data["done"])
    msg = f"今天共完成 {done_n} 個項目"
    approved = [f for f in data["findings"] if f["status"] == "approved"]
    deferred = [f for f in data["findings"] if f["status"] == "deferred"]
    if approved:
        msg += "，包含您同意追加的" + "、".join(f["name"] for f in approved)
    msg += "。"
    if deferred:
        msg += "您選擇下次再處理的" + "、".join(f["name"] for f in deferred) + "，我們已經記錄，下次保養會再幫您檢查。"
    msg += "下次回廠時間與預估費用都整理在下面，讓您提早安排。"
    tips = []
    for f in deferred:
        tips.append(f"{f['name']}：{f.get('watch', '若有異狀請提早回廠檢查')}")
    pt = veh.get("powertrain")
    if pt == "hybrid":
        tips.append("油電車的電池散熱進氣口請不要被物品擋住（位置依車型而異），電池散熱好，壽命更長。")
    elif pt == "ev":
        tips.append("長時間停放時，電量維持在兩成到八成之間，對電池比較友善。")
    else:
        tips.append("長途出遊前，花一分鐘看一下胎壓與雨刷，行車更安心。")
    return {"headline": headline, "message": msg, "tips": tips[:3]}


def powertrain_text(veh):
    return POWERTRAIN_TEXT.get(veh.get("powertrain"), "")


def stage_name(stage):
    if stage is None or stage < 0:
        return "尚未報到"
    return STAGES[min(stage, DONE_STAGE)]


# ---------------- 車主上傳照片：依文字描述做初步分級（看不到圖片時的保底） ----------------
PHOTO_RULES = [
    # 同一級裡，描述越完整的詞放越前面（例如「黃色警示燈」先於「黃色」），回覆時引用的字才會自然
    ("stop", ["冒煙", "起火", "著火", "焦味", "燒焦", "煞車失靈", "煞車變軟", "煞車踩不下", "踩不下去", "水溫過高", "過熱",
              "機油燈", "紅色警示燈", "紅色的燈", "紅色燈", "紅燈", "紅色", "爆胎", "輪胎鼓包", "鼓包"]),
    ("soon", ["漏油", "漏水", "滴油", "滴水", "異音", "怪聲", "異響", "抖動", "黃色警示燈", "黃色的燈", "黃色燈", "黃燈",
              "警示燈", "故障燈", "引擎燈", "黃色", "胎壓", "裂", "割傷", "偏", "方向盤"]),
    ("watch", ["刮痕", "擦傷", "小刮", "髒", "起霧", "雨刷", "灰塵", "水痕"]),
]
PHOTO_ADVICE = {
    "stop": "如果車子還在路上，請先找安全的地方停車，不要繼續行駛，並聯絡道路救援或服務廠。",
    "soon": "建議盡快讓技師檢查；如果車子已經在服務廠，技師會在這次保養一併確認。",
    "watch": "看起來不急，下次保養時請技師一併確認即可；若情況變嚴重請提早回廠。",
    "unknown": "照片的資訊不足以判斷，技師會實際查看後回覆您。",
}


def photo_triage(text, kb=None, candidates=None):
    """沒有影像 AI 時，依車主的文字描述做初步分級。回傳 {summary, urgency, advice, related, matched}。"""
    t = (text or "").strip()
    urgency, matched = "unknown", ""
    for level, words in PHOTO_RULES:
        hit = next((w for w in words if w in t), None)
        if hit:
            urgency, matched = level, hit
            break
    related = ""
    if kb is not None and t:
        code = kb.match_item(t, candidates)
        related = kb.name(code) if code else ""
    if not t:
        summary = "已收到您的照片。目前的模式無法直接判讀圖片，專員與技師會實際查看。"
    elif matched:
        judge = {"stop": "可能和行車安全有關", "soon": "需要技師進一步確認",
                 "watch": "聽起來不影響行車安全，技師會再確認"}[urgency]
        summary = f"您的描述提到「{matched}」，{judge}" + (f"，可能與「{related}」相關" if related else "") + "。"
    else:
        summary = "已收到您的照片與說明，專員與技師會實際查看。"
    return {"summary": summary, "urgency": urgency, "advice": PHOTO_ADVICE[urgency], "related": related,
            "matched": matched}


def format_photo_reply(res, advisor, technician):
    """把照片判讀結果整理成給車主看的回覆（AI 或規則都用同一個格式）。"""
    u = URGENCY.get(res.get("urgency"), URGENCY["unknown"])
    lines = [f"初步判斷：{res['summary']}", f"緊急程度：{u['label']}（{u['desc']}）", f"建議：{res['advice']}"]
    if res.get("related"):
        lines.append(f"可能相關的項目：{res['related']}")
    lines.append(f"照片已轉給{advisor}與負責的{technician}，實際狀況以技師檢查為準。")
    return "\n".join(lines)
