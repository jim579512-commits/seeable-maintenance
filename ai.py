"""AI 引擎：Gemini（雲端）／Ollama（本機）／離線規則，三選一。

- 每個任務都先用離線規則產生一份「保底版本」，AI 成功時再用 AI 的內容覆蓋。
  所以 AI 斷線、逾時或輸出格式錯誤時，畫面仍然有內容，並標示為「離線規則」。
- 分級（安全必要／建議處理／可以下次）有量測門檻時以門檻為準，不交給 AI 自由判斷。
- 保固、優惠等資格問題一律轉專員，規則優先於模型輸出。
"""
import base64
import datetime as dt
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import offline
from kb import DONE_STAGE, POWERTRAIN_TEXT, TIERS, TOPICS, URGENCY

SYSTEM = """你是 TOYOTA 服務廠的「AI 保養說明助理」，協助服務專員把維修保養內容解釋給一般車主聽。
必須遵守：
1. 只能使用我提供的資料（工單、車輛保養紀錄、原廠知識庫、技師紀錄、進度）。資料裡沒有的事不要編造，尤其是價格、診斷結果、保固與優惠條件。
2. 一律使用台灣繁體中文與台灣慣用說法（例如：機油、來令片、電瓶、公里），不要出現簡體字或英文句子。
3. 語氣親切、清楚、簡短，不推銷、不恐嚇；和安全有關的事要講清楚，可以延後的事就誠實說可以延後。
4. 只輸出指定格式的 JSON，不要輸出其他文字。"""

MODE_LABELS = {
    "gemini": "Gemini（雲端 AI）",
    "ollama": "本機 AI（Ollama）",
    "offline": "離線規則（沒有使用 AI）",
}
SOURCE_LABELS = {
    "gemini": "由 AI（Gemini）撰寫",
    "ollama": "由本機 AI 撰寫",
    "offline": "由系統依原廠資料以規則產生",
}


class AIError(Exception):
    pass


def extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "是", "y")
    return False


def _clean(s, limit=400):
    return str(s).strip()[:limit] if s is not None else ""


def shrink_image(data, mime, max_side=1600):
    """把手機照片縮到最長邊 1600 像素再送去看圖，省流量也比較快；沒有 Pillow 或格式不支援就原樣送出。"""
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(data))
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001
        return data, mime


class AIEngine:
    def __init__(self, cfg, kb, log=print):
        self.cfg = dict(cfg or {})
        self.kb = kb
        self.log = log
        self.mode = "offline"
        self.detail = ""
        self.last_error = ""
        self.last_ok = None
        self.gemini_model = None
        self._cc = None
        try:
            import opencc  # 本機模型偶爾會漏出簡體字，用 OpenCC 轉成台灣繁體

            self._cc = opencc.OpenCC("s2tw")
        except Exception:  # noqa: BLE001
            self._cc = None
        self.detect()

    # ---------- 模式偵測 ----------
    def _key(self):
        return (self.cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()

    def _ollama_url(self):
        return (self.cfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")

    def _ollama_model(self):
        return self.cfg.get("ollama_model") or "qwen2.5-7b-q4:latest"

    def _ollama_available(self):
        try:
            with urllib.request.urlopen(self._ollama_url() + "/api/tags", timeout=2) as r:
                tags = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            return False, f"連不到本機 Ollama（{self._ollama_url()}）：{e}"
        want = self._ollama_model()
        names = [m.get("name", "") for m in tags.get("models", [])]
        base = want.split(":")[0]
        if want in names or any(n.split(":")[0] == base for n in names):
            return True, ""
        return False, f"Ollama 裡找不到模型 {want}（現有：{', '.join(names) or '無'}）"

    def detect(self):
        want = (self.cfg.get("mode") or "auto").lower()
        self.gemini_model = None
        where = "雲端主機的環境變數" if self.cfg.get("cloud") else "config.json"
        if want == "offline":
            self.mode, self.detail = "offline", f"{where}設定為離線模式"
            return self.status()
        if want in ("auto", "gemini") and self._key():
            models = self.cfg.get("gemini_models") or ["gemini-3.8-flash"]
            self.mode, self.detail = "gemini", f"模型：{models[0]}（依序嘗試：{'、'.join(models)}）"
            return self.status()
        if want == "gemini":
            self.mode, self.detail = "offline", f"{where}指定 gemini，但沒有填 Gemini 金鑰"
            return self.status()
        if self.cfg.get("cloud"):
            self.mode, self.detail = "offline", "雲端版沒有設定 GEMINI_API_KEY（雲端也沒有本機模型），所以用離線規則"
            return self.status()
        ok, msg = self._ollama_available()
        if ok:
            self.mode, self.detail = "ollama", f"模型：{self._ollama_model()}（本機執行，資料不離開電腦）"
        else:
            self.mode = "offline"
            self.detail = msg if want == "ollama" else "沒有填 Gemini 金鑰，也找不到本機 Ollama，所以用離線規則"
        return self.status()

    def status(self):
        return {
            "mode": self.mode,
            "label": MODE_LABELS[self.mode],
            "detail": self.detail,
            "cloud": self.mode == "gemini",
            "last_error": self.last_error,
            "last_ok": self.last_ok,
            "slow": self.mode == "ollama",
            "vision": self.vision_status()[0],
        }

    def vision_status(self):
        """目前的 AI 能不能看圖片：回傳 (能否, 給車主看的說明)。"""
        if self.mode == "gemini":
            return True, "由 Gemini 看圖判讀"
        if self.mode == "ollama":
            vm = (self.cfg.get("ollama_vision_model") or "").strip()
            if vm:
                return True, f"由本機影像模型 {vm} 看圖判讀"
            return False, "目前的本機 AI 看不懂圖片，以下依您的文字描述整理"
        return False, "目前沒有使用 AI，以下依您的文字描述整理"

    # ---------- 底層呼叫 ----------
    def _gemini(self, system, prompt, temperature, images=None):
        key = self._key()
        models = list(self.cfg.get("gemini_models") or ["gemini-3.8-flash"])
        if self.gemini_model:
            models = [self.gemini_model] + [m for m in models if m != self.gemini_model]
        timeout = float(self.cfg.get("timeout_seconds") or 60)
        last = None
        parts_in = [{"inlineData": {"mimeType": m, "data": base64.b64encode(b).decode("ascii")}} for m, b in (images or [])]
        parts_in.append({"text": prompt})
        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": parts_in}],
                "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-goog-api-key": key},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code == 404:
                    last = f"找不到模型 {model}"
                    continue
                if e.code == 429:  # 各型號的免費額度分開計算，換下一個型號再試
                    last = f"{model} 的免費額度用完或未開放（HTTP 429），請稍後再試"
                    continue
                if e.code in (400, 401, 403):
                    raise AIError(f"Gemini 拒絕請求（HTTP {e.code}），金鑰可能錯誤或沒有權限：{detail}")
                raise AIError(f"Gemini 錯誤（HTTP {e.code}）：{detail}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise AIError(f"連不上 Gemini（網路或逾時）：{e}")
            cands = data.get("candidates") or []
            if not cands:
                raise AIError("Gemini 沒有回傳內容：" + json.dumps(data.get("promptFeedback", {}), ensure_ascii=False)[:200])
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if not text.strip():
                raise AIError("Gemini 回傳空白內容")
            self.gemini_model = model
            self.detail = f"模型：{model}"
            return text
        raise AIError(last or "沒有可用的 Gemini 模型")

    def _ollama(self, system, prompt, temperature, max_tokens, images=None):
        model = self._ollama_model()
        user_msg = {"role": "user", "content": prompt}
        if images:
            vm = (self.cfg.get("ollama_vision_model") or "").strip()
            if not vm:
                raise AIError("本機模型不能看圖片（config.json 沒有設定 ollama_vision_model）")
            model = vm
            user_msg["images"] = [base64.b64encode(b).decode("ascii") for _, b in images]
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, user_msg],
            "stream": False,
            "format": "json",
            "keep_alive": "15m",
            "options": {"temperature": temperature, "num_ctx": 4096, "num_predict": max_tokens},
        }
        if model.startswith("qwen3"):
            body["think"] = False
        timeout = max(120.0, float(self.cfg.get("timeout_seconds") or 60) * 3)
        data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        for attempt in range(2):
            req = urllib.request.Request(self._ollama_url() + "/api/chat", data=data_bytes,
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                return (data.get("message") or {}).get("content", "")
            except urllib.error.HTTPError as e:
                # 這台電腦冷啟動載入模型時偶爾會 HTTP 500，重試一次通常就好
                if e.code == 500 and attempt == 0:
                    time.sleep(2)
                    continue
                raise AIError(f"Ollama 錯誤（HTTP {e.code}）")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise AIError(f"連不上本機 Ollama 或逾時：{e}")
        raise AIError("Ollama 沒有回應")

    def _to_tw(self, obj):
        if self._cc is None:
            return obj
        if isinstance(obj, str):
            return self._cc.convert(obj)
        if isinstance(obj, list):
            return [self._to_tw(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self._to_tw(v) for k, v in obj.items()}
        return obj

    def run_json(self, task, prompt, max_tokens, temperature=0.3, images=None):
        """呼叫目前模式的 AI，回傳 (dict 或 None, 來源)。失敗時回傳 (None, 'offline')。
        images：[(mime, bytes), ...]，只有能看圖的模型才會帶進去。"""
        mode = self.mode
        if mode == "offline":
            return None, "offline"
        t0 = time.time()
        try:
            if mode == "gemini":
                text = self._gemini(SYSTEM, prompt, temperature, images=images)
            else:
                text = self._ollama(SYSTEM, prompt, temperature, max_tokens, images=images)
            data = extract_json(text)
            if not isinstance(data, dict):
                raise AIError("AI 輸出不是 JSON 物件")
            if mode == "ollama":
                data = self._to_tw(data)
            self.last_error = ""
            self.last_ok = time.time()
            self.log(f"[AI] {task}：完成（{MODE_LABELS[mode]}，{time.time() - t0:.1f} 秒）")
            return data, mode
        except (AIError, ValueError, KeyError, TypeError) as e:
            self.last_error = f"{task}：{e}"
            self.log(f"[AI] {task}：失敗，改用離線規則。原因：{e}")
            return None, "offline"

    def test(self):
        data, src = self.run_json("連線測試", '請輸出 JSON：{"ok": true, "reply": "一句 10 字內的問候"}', 60)
        if data is None:
            return {"ok": False, "source": src, "message": self.last_error or "目前是離線規則模式，沒有使用 AI"}
        return {"ok": True, "source": src, "message": _clean(data.get("reply") or "連線成功", 60)}

    def warmup_async(self):
        """本機模型第一次載入約需 30–60 秒，啟動時先在背景載入。"""
        if self.mode != "ollama":
            return

        def run():
            try:
                self._ollama(SYSTEM, '請輸出 JSON：{"ok": true}', 0.0, 10)
                self.log("[AI] 本機模型已預先載入")
            except AIError as e:
                self.log(f"[AI] 本機模型預先載入失敗：{e}")

        threading.Thread(target=run, daemon=True).start()

    # ---------- 共用資料 ----------
    @staticmethod
    def _who(visit):
        o, v = visit["owner"], visit["vehicle"]
        depth = "懂車、可以用專業用語" if o.get("depth") == "technical" else "偏好簡單白話的說明"
        return (f"車主：{o['name']}（{o.get('persona', '')}；{depth}）\n"
                f"車輛：{v['model']}，{v['year']} 年，{POWERTRAIN_TEXT.get(v['powertrain'], '')}，"
                f"目前里程 {v['odometer']:,} 公里，交車日 {v['purchase_date']}")

    # ---------- 任務 1：今日保養說明頁 ----------
    def explanation(self, visit, now=None):
        now = now or dt.datetime.now()
        base = offline.explanation(visit, self.kb, now)
        base["source"] = "offline"
        if self.mode == "offline":
            return base
        items = []
        for it in visit["items"]:
            k = self.kb.get(it["code"]) or {}
            items.append({"code": it["code"], "name": it["name"], "price": it["price"],
                          "one_liner": k.get("one_liner", ""), "interval": k.get("interval_text", "")})
        history = [{"date": h["date"], "km": h["km"], "type": h["type"],
                    "items": [self.kb.name(c) for c in h["items"]]} for h in visit.get("history", [])[-5:]]
        prompt = (
            "請為這位車主撰寫「今日保養說明頁」的個人化內容。\n"
            f"今天日期：{now:%Y-%m-%d}\n{self._who(visit)}\n"
            f"今日工單項目（含原廠說明）：\n{json.dumps(items, ensure_ascii=False)}\n"
            f"這台車的保養紀錄（由舊到新）：\n{json.dumps(history, ensure_ascii=False)}\n\n"
            "輸出 JSON，格式：\n"
            '{"greeting": "一句問候，說明今天在做什麼（40 字內）",\n'
            ' "summary": "今天的重點，讓車主一眼看懂（80 字內）",\n'
            ' "items": [{"code": "項目代碼（照上面原樣）", "personal_note": "結合這台車的紀錄說明為什麼這次要做（50 字內；只能根據上面的紀錄與週期推論，不要編造日期或里程）"}]}'
        )
        data, src = self.run_json("今日說明頁", prompt, 900)
        if not data:
            return base
        out = {"greeting": _clean(data.get("greeting")) or base["greeting"],
               "summary": _clean(data.get("summary")) or base["summary"], "items": {}, "source": src}
        ai_notes = {}
        for it in data.get("items") or []:
            if isinstance(it, dict) and it.get("code") in base["items"] and it.get("personal_note"):
                ai_notes[it["code"]] = _clean(it["personal_note"], 200)
        for code, v in base["items"].items():
            out["items"][code] = {"personal_note": ai_notes.get(code, v["personal_note"])}
        return out

    # ---------- 任務 2：追加項目說明草稿 ----------
    def addon_draft(self, visit, code, value, tech_note, price, minutes):
        base = offline.addon_draft(visit, code, value, tech_note, self.kb)
        base["source"] = "offline"
        if self.mode == "offline":
            return base
        item = self.kb.get(code) or {}
        rule_tier = base.get("rule_tier")
        tier_hint = (f"{TIERS[rule_tier]['label']}（{rule_tier}）" if rule_tier
                     else "沒有量測門檻，請依原廠資料與技師紀錄判斷")
        facts = {k: item.get(k) for k in ("name", "one_liner", "detail", "skip", "rule_text") if item.get(k)}
        measure = f"{value:g} {item.get('unit', '')}" if value is not None else "無"
        prompt = (
            "技師在檢查時發現一個需要處理的項目，請幫服務專員擬一份「給車主看」的說明草稿。\n"
            f"{self._who(visit)}\n"
            f"技師紀錄（可能是行話）：「{tech_note or '（無）'}」\n"
            f"量測值：{measure}\n"
            f"項目原廠資料：{json.dumps(facts, ensure_ascii=False)}\n"
            f"依原廠門檻判定的分級：{tier_hint}\n"
            "分級代碼：safety＝安全必要、recommend＝建議處理、can_wait＝可以下次\n"
            f"費用：NT$ {price:,}（示意），追加工時約 {minutes} 分鐘\n\n"
            "輸出 JSON，格式：\n"
            '{"title": "給車主看的標題（15 字內）",\n'
            ' "plain": "白話說明技師看到什麼、這個零件是做什麼的（80 字內）",\n'
            ' "tier": "safety、recommend、can_wait 其中之一（有原廠門檻判定時必須相同）",\n'
            ' "tier_reason": "分級理由（40 字內）",\n'
            ' "consequence": "如果先不處理可能會怎樣（50 字內，不誇大）",\n'
            ' "suggestion": "給車主的建議（40 字內，可以延後就誠實說）"}'
        )
        data, src = self.run_json("追加說明草稿", prompt, 500)
        if not data:
            return base
        out = dict(base)
        for k in ("title", "plain", "tier_reason", "consequence", "suggestion"):
            if _clean(data.get(k)):
                out[k] = _clean(data.get(k))
        ai_tier = data.get("tier") if data.get("tier") in TIERS else None
        if rule_tier:
            out["tier"] = rule_tier
            if ai_tier != rule_tier:  # 模型和門檻不一致時，理由與建議改回規則版，避免前後矛盾
                out["tier_reason"], out["suggestion"] = base["tier_reason"], base["suggestion"]
        elif ai_tier:
            out["tier"] = ai_tier
        # 建議和分級互相矛盾時（例如「安全必要」卻說可以延後），改用規則版建議
        sug = out.get("suggestion", "")
        if out["tier"] == "safety" and any(w in sug for w in ("延後", "下次", "觀察", "再說", "不急", "可以等")):
            out["suggestion"] = offline.SUGGESTIONS["safety"]
        elif out["tier"] == "can_wait" and any(w in sug for w in ("立即", "今天就", "馬上", "儘快", "盡快", "必須")):
            out["suggestion"] = offline.SUGGESTIONS["can_wait"]
        out["source"] = src
        return out

    # ---------- 任務 3：客休室問答 ----------
    def answer(self, visit, addons, question, eta_text, about=None):
        base = offline.answer(visit, addons, question, self.kb, eta_text, about=about)
        base["source"] = "offline"
        if self.mode == "offline":
            return base
        rule_topic = offline.classify_topic(question)
        advisor = visit["advisor"]
        work = [{"項目": i["name"], "費用": i["price"],
                 "來源": {"addon": "技師發現、車主同意的追加", "optional": "車主自選（非必要）"}.get(i.get("source"), "依里程的必要項目"),
                 "說明": (self.kb.get(i["code"]) or {}).get("one_liner", ""),
                 "週期": (self.kb.get(i["code"]) or {}).get("interval_text", "")} for i in visit["items"]]
        status_text = {"draft": "專員確認中", "sent": "等車主決定", "approved": "車主已同意", "deferred": "車主選擇下次"}
        extra = []
        for a in addons:
            if a["status"] == "draft":
                continue
            f = a.get("final") or a["draft"]
            extra.append({"項目": a["item_name"], "標題": f["title"], "分級": TIERS[f["tier"]]["label"],
                          "狀態": status_text.get(a["status"], a["status"]), "費用": a["price"],
                          "量測": (f"{a['measure']['value']:g} {a['measure']['unit']}" if a.get("measure") else "無"),
                          "說明": f["plain"], "不處理的後果": f["consequence"]})
        codes = [i["code"] for i in visit["items"]] + [a["item_code"] for a in addons]
        hit = self.kb.match_item(question, codes)
        if hit is None and about:
            hit = next((a["item_code"] for a in addons if a["id"] == about), None)
        facts = {}
        if hit:
            k = self.kb.get(hit) or {}
            facts[k.get("name", hit)] = {x: k.get(x) for x in ("detail", "analogy", "skip", "interval_text") if k.get(x)}
        ctx = {"車輛": f"{visit['vehicle']['model']}（{visit['plate']}）", "目前進度": eta_text,
               "今日工單": work, "追加項目": extra, "相關原廠資料": facts}
        recent = []
        for m in visit.get("chat", [])[-6:]:
            who = {"user": "車主", "ai": "助理", "advisor": "專員"}.get(m["role"])
            if who:
                recent.append(f"{who}：{m['text']}")
        about_line = ""
        if about:
            a = next((x for x in addons if x["id"] == about), None)
            if a:
                about_line = f"（車主是從追加項目「{a['item_name']}」的頁面點進來問的）\n"
        prompt = (
            "車主在客休室用手機提問，請根據資料回答。\n"
            f"資料：{json.dumps(ctx, ensure_ascii=False)}\n"
            f"最近的對話：\n{chr(10).join(recent) or '（無）'}\n"
            f"{about_line}車主的問題：「{question}」\n\n"
            "回答規則：\n"
            "- 只能用上面的資料回答；提到價格只能引用資料中的數字，並說明是示意價格、以結帳為準。\n"
            f"- 問到保固、延保、優惠、折扣、保險理賠等資格問題，或資料不足以回答時，escalate 設為 true，並在回答中說明已轉給{advisor}。\n"
            "- 追加項目的分級若是「可以下次」，要誠實告訴車主可以延後。\n"
            "- 回答 120 字內，口語、好懂。\n\n"
            '輸出 JSON：{"answer": "回答內容", "escalate": true 或 false, '
            '"topic": "necessity、price、timing、consequence、progress、policy、other 其中之一"}'
        )
        data, src = self.run_json("客休室問答", prompt, 350)
        if not data or not _clean(data.get("answer")):
            return base
        ans = _clean(data.get("answer"), 600)
        esc = as_bool(data.get("escalate"))
        topic = data.get("topic") if data.get("topic") in TOPICS else rule_topic
        if rule_topic == "policy":  # 資格類問題一律轉專員
            esc, topic = True, "policy"
        if esc and advisor not in ans:  # 一定講清楚「已經轉給哪位專員」，車主才不用自己再去找人
            ans += " " + offline._handoff(visit)
        return {"answer": ans, "escalate": esc, "topic": topic, "source": src}

    # ---------- 任務 4：整理車主關心的事（給專員） ----------
    def concerns(self, visit):
        base = offline.concerns(visit)
        base["source"] = "offline"
        pairs = []
        chat = visit.get("chat", [])
        for i, m in enumerate(chat):
            if m["role"] == "user":
                reply = next((x["text"] for x in chat[i + 1:] if x["role"] in ("ai", "advisor")), "")
                pairs.append(f"車主：{m['text']}\n回答：{reply}")
        if self.mode == "offline" or not pairs:
            return base
        prompt = (
            "以下是車主今天在客休室問 AI 助理的問題與回答，請幫服務專員整理交車前的溝通重點。\n"
            f"{self._who(visit)}\n問答紀錄：\n" + "\n\n".join(pairs[-8:]) + "\n\n"
            '輸出 JSON：{"summary": "一句話總結車主最在意什麼（40 字內）", '
            '"points": ["具體重點（25 字內）", "最多 4 點"], '
            '"suggest": "專員交車時建議怎麼說明（60 字內）"}'
        )
        data, src = self.run_json("車主關心的事", prompt, 350)
        if not data or not _clean(data.get("summary")):
            return base
        pts = [_clean(p, 80) for p in (data.get("points") or []) if _clean(p)][:4]
        return {"summary": _clean(data["summary"], 120), "points": pts or base["points"],
                "suggest": _clean(data.get("suggest"), 160) or base["suggest"], "source": src}

    # ---------- 任務 5：愛車健康報告的文字 ----------
    def report_text(self, visit, data):
        base = offline.report_text(visit, data)
        base["source"] = "offline"
        if self.mode == "offline":
            return base
        findings = [f"{f['name']}：{f['status_text']}" + (f"（量測 {f['measure']}）" if f.get("measure") else "")
                    for f in data["findings"]]
        prompt = (
            "車輛保養完工了，請為車主寫「愛車健康報告」的開頭與提醒。\n"
            f"{self._who(visit)}\n"
            f"里程碑：{data.get('milestone') or '無'}\n"
            f"今天完成：{'、'.join(d['name'] for d in data['done'])}\n"
            f"檢查發現：{'；'.join(findings) or '無'}\n"
            f"下次回廠：{data['next']['text']}\n\n"
            '輸出 JSON：{"headline": "報告標題（25 字內；有里程碑要祝賀）", '
            '"message": "給車主的一段話（100 字內，溫暖、不浮誇、不推銷）", '
            '"tips": ["和這台車有關的具體提醒（30 字內）", "最多 3 則"]}'
        )
        out, src = self.run_json("健康報告", prompt, 450)
        if not out or not _clean(out.get("headline")):
            return base
        tips = [_clean(t, 90) for t in (out.get("tips") or []) if _clean(t)][:3]
        return {"headline": _clean(out["headline"], 60), "message": _clean(out.get("message"), 300) or base["message"],
                "tips": tips or base["tips"], "source": src}

    # ---------- 任務 7：預約前的需求判斷（該去保修站還是服務廠） ----------
    def triage(self, owner, text, base):
        """規則先判；能用 AI 時再請 AI 依描述判斷，兩者取「需要較完整設備據點」的一方（安全優先）。"""
        import booking  # 只在這裡用到，避免模組互相匯入

        t = (text or "").strip()
        if self.mode == "offline" or not t:
            return base
        veh = owner["vehicle"]
        cats = "、".join(f"{k}＝{v['label']}" for k, v in booking.NEEDS.items())
        prompt = (
            "車主想預約 TOYOTA 回廠，請依描述判斷這次屬於哪一類需求，用來決定建議去社區型保修站還是服務廠。\n"
            f"車輛：{veh['model']}（{POWERTRAIN_TEXT.get(veh['powertrain'], '')}），{veh['odometer']:,} 公里。\n"
            f"車主描述：「{t}」\n類別：{cats}\n"
            "規則：保固、召回、重大維修→warranty；事故、鈑金烤漆→body；警示燈、油電／高壓、電子系統→warning；"
            "單一的小異音、小故障→noise；換胎、換電瓶→tire_battery；一般保養→maintenance。不確定就選比較需要完整設備的那一類。\n"
            '輸出 JSON：{"need": "類別代碼", "reason": "一句話理由（30 字內）"}'
        )
        data, src = self.run_json("需求判斷", prompt, 200)
        if not data or data.get("need") not in booking.NEEDS:
            return base
        order = booking.NEED_ORDER
        reason = _clean(data.get("reason"), 60)
        if order.index(data["need"]) < order.index(base["need"]):
            out = booking.triage(owner, data["need"], t)
            out["reasons"] = ([f"AI 判斷：{reason}"] if reason else []) + out["reasons"]
        else:
            out = dict(base)
            if reason and data["need"] == base["need"]:
                out["reasons"] = [f"AI 判斷：{reason}"] + list(base["reasons"])
        out["source"] = src
        return out

    # ---------- 任務 6：車主上傳照片的初步判讀 ----------
    def analyze_photo(self, visit, image_bytes, mime, text, candidates=None):
        """回傳 {summary, urgency, advice, related, matched, source, vision, note}。
        一律保底：先用文字描述做規則分級；能看圖時再請 AI 判讀，兩者取「較緊急」的分級（安全優先）。"""
        base = offline.photo_triage(text, self.kb, candidates)
        can_see, why = self.vision_status()
        base.update({"source": "offline", "vision": False, "note": why})
        if not can_see:
            return base
        img, img_mime = shrink_image(image_bytes, mime)
        where = ("目前車輛已在服務廠保養中" if 0 <= visit["stage"] < DONE_STAGE
                 else ("車輛已完工" if visit["stage"] >= DONE_STAGE else "車主尚未到廠（可能正在路上或在家）"))
        prompt = (
            "車主上傳了一張和愛車有關的照片，請幫服務專員做「初步判讀」，最後仍由技師實際確認。\n"
            f"{self._who(visit)}\n{where}。\n"
            f"車主的描述：「{text or '（沒有文字說明）'}」\n\n"
            "規則：\n"
            "- 只描述照片裡看得到的東西；看不清楚就說看不清楚，不要猜。\n"
            "- 不做最終診斷、不報價。\n"
            "- 和安全有關的狀況（煞車、轉向、輪胎鼓包或割傷、漏油冒煙、紅色警示燈）要提高緊急程度。\n"
            "- urgency：stop＝請盡快安全停車；soon＝盡快讓技師檢查；watch＝可以觀察；unknown＝看不出來、需技師確認。\n\n"
            '輸出 JSON：{"summary": "照片裡看到什麼、可能是什麼（60 字內）", '
            '"urgency": "stop、soon、watch、unknown 其中之一", '
            '"advice": "給車主的具體建議（60 字內）", '
            '"related": "可能相關的保養項目名稱，沒有就空字串"}'
        )
        data, src = self.run_json("照片判讀", prompt, 400, images=[(img_mime, img)])
        if not data or not _clean(data.get("summary")):
            base["note"] = "AI 看圖沒有成功，以下依您的文字描述整理"
            return base
        urgency = data.get("urgency") if data.get("urgency") in URGENCY else "unknown"
        advice = _clean(data.get("advice"), 200)
        # 安全優先：車主描述裡有危險關鍵字、而 AI 判得比較輕時，改用較緊急的規則分級與建議
        if base["matched"] and URGENCY[base["urgency"]]["rank"] > URGENCY[urgency]["rank"]:
            urgency = base["urgency"]
            advice = offline.PHOTO_ADVICE[urgency]
        return {"summary": _clean(data.get("summary"), 200), "urgency": urgency,
                "advice": advice or offline.PHOTO_ADVICE[urgency], "related": _clean(data.get("related"), 40),
                "matched": base["matched"], "source": src, "vision": True, "note": why}
