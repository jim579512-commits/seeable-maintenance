"""保養知識庫與共用常數。

知識庫內容放在 knowledge_base.json（可直接編輯），這裡只放讀取與查詢的工具函式。
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STAGES = ["接車完成", "檢查中", "保養作業中", "品質檢查", "洗車中", "可取車"]
DONE_STAGE = len(STAGES) - 1

TIERS = {
    "safety": {"label": "安全必要", "desc": "影響行車安全，建議今天處理"},
    "recommend": {"label": "建議處理", "desc": "今天開車沒有安全疑慮，但建議近期處理，避免變嚴重"},
    "can_wait": {"label": "可以下次", "desc": "目前狀況還可以，下次保養時再檢查即可"},
}

TOPICS = {
    "necessity": "必要性（為什麼要做）",
    "price": "費用",
    "timing": "可否延後",
    "consequence": "不處理的後果",
    "progress": "進度與時間",
    "policy": "保固／優惠",
    "photo": "照片詢問",
    "other": "其他",
}

POWERTRAIN_TEXT = {"gas": "汽油車", "hybrid": "油電車", "ev": "電動車"}

# 工資＝標準工時 × 每小時工資（示意），讓車主看得到零件與工資各多少
LABOR_RATE = 1200

# 車主上傳照片的初步判斷分級（安全優先：規則與 AI 取較緊急的一方）
URGENCY = {
    "stop": {"label": "請盡快安全停車", "desc": "若在行駛中，請找安全的地方停車，並聯絡道路救援或服務廠", "rank": 3},
    "soon": {"label": "建議盡快檢查", "desc": "可以慢慢開到服務廠，但請盡快讓技師檢查", "rank": 2},
    "unknown": {"label": "需要技師確認", "desc": "照片的資訊不足以判斷，請技師實際查看", "rank": 1},
    "watch": {"label": "可以先觀察", "desc": "目前看起來不急，下次保養時一併檢查即可", "rank": 0},
}


class KB:
    def __init__(self, path=None):
        path = path or os.path.join(BASE_DIR, "knowledge_base.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.items = data["items"]

    def get(self, code):
        return self.items.get(code)

    def name(self, code):
        item = self.items.get(code)
        return item["name"] if item else code

    def addon_codes(self):
        return [c for c, it in self.items.items() if it.get("addon")]

    def optional_codes(self, powertrain=None):
        """車主可以自己加選的非必要項目。"""
        out = []
        for c, it in self.items.items():
            if not it.get("optional"):
                continue
            if powertrain and it.get("powertrains") and powertrain not in it["powertrains"]:
                continue
            out.append(c)
        return out

    def breakdown(self, code, price=None, minutes=None):
        """把價格拆成零件與工資：工資＝工時 × LABOR_RATE（示意），其餘為零件。"""
        item = self.items.get(code) or {}
        price = item.get("price", 0) if price is None else price
        minutes = item.get("minutes", 15) if minutes is None else minutes
        if not price:
            return {"parts": 0, "labor": 0, "total": 0, "minutes": minutes, "included": True}
        labor = min(price, int(round(minutes / 60 * LABOR_RATE / 10.0)) * 10)
        return {"parts": price - labor, "labor": labor, "total": price, "minutes": minutes, "included": False}

    def price_table(self, powertrain, max_km=100000):
        """依里程列出每一次定期保養的項目與費用（示意）。"""
        rows = []
        for km in range(10000, max_km + 1, 10000):
            items = []
            for c in self.scheduled_items(km, powertrain):
                items.append(dict(self.breakdown(c), code=c, name=self.name(c)))
            rows.append({
                "km": km, "items": items,
                "parts": sum(i["parts"] for i in items), "labor": sum(i["labor"] for i in items),
                "total": sum(i["total"] for i in items), "minutes": sum(i["minutes"] for i in items),
            })
        return rows

    def scheduled_items(self, bucket_km, powertrain):
        """依里程產生定期保養工單範本（示意規則）。"""
        if powertrain == "ev":
            codes = ["tire_rotation", "inspection", "ev_battery_check"]
            if bucket_km % 20000 == 0:
                codes.append("cabin_filter")
            if bucket_km % 40000 == 0:
                codes.append("brake_fluid")
            return codes
        codes = ["engine_oil", "oil_filter", "tire_rotation", "inspection"]
        if powertrain == "hybrid":
            codes.append("hybrid_check")
        if bucket_km % 20000 == 0:
            codes.append("cabin_filter")
        if bucket_km % 40000 == 0:
            codes += ["air_filter", "brake_fluid"]
        if bucket_km % 80000 == 0:
            codes.append("atf")
        if bucket_km % 100000 == 0:
            codes += ["spark_plug", "coolant"]
        return codes

    def work_item(self, code, source="planned"):
        item = self.items[code]
        return {
            "code": code,
            "name": item["name"],
            "price": item.get("price", 0),
            "minutes": item.get("minutes", 15),
            "source": source,
        }

    def match_item(self, text, candidates=None):
        """在文字中找出提到的項目；多個符合時取最長的關鍵字（避免「輪胎對調」被當成「輪胎」）。"""
        if not text:
            return None
        pool = candidates if candidates is not None else list(self.items.keys())
        best, best_len = None, 0
        lowered = text.lower()
        for code in pool:
            item = self.items.get(code)
            if not item:
                continue
            words = list(item.get("keywords", [])) + [item["name"]]
            for w in words:
                if w and w.lower() in lowered and len(w) > best_len:
                    best, best_len = code, len(w)
        return best

    def rule_tier(self, code, value):
        """依原廠（示意）門檻判定分級；沒有量測值或沒有門檻時回傳 None。"""
        item = self.items.get(code)
        if not item or value is None or not item.get("rules"):
            return None
        for rule in item["rules"]:
            if value < rule["below"]:
                return rule["tier"]
        return item.get("default_tier", "can_wait")
