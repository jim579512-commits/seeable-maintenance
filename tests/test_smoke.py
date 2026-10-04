"""整條示範流程的自動測試（離線規則模式＋假的 Gemini／Ollama 回應，不需要網路或顯卡）。

執行：py -3 -m unittest discover -s tests -v
"""
import io
import json
import os
import datetime as dt
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402


def make_client(mode="offline", locations="demo"):
    cfg = appmod.load_config()
    cfg["ai"] = dict(cfg["ai"], mode=mode, gemini_api_key="")
    cfg["locations"] = {"source": locations}  # 測試不連官網：demo＝示意據點、snapshot＝內建的官網快照
    tmp = tempfile.mkdtemp(prefix="hotai_test_")
    app = appmod.create_app(cfg, data_dir=tmp)
    app.testing = True
    return app, app.test_client(), tmp


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.app, self.c, self.tmp = make_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, url, body=None):
        r = self.c.post(url, json=body or {})
        self.assertLess(r.status_code, 400, (url, r.status_code, r.get_data(as_text=True)[:300]))
        return r.get_json()

    def test_pages_render(self):
        for url in ["/", "/c/V001", "/c/V001/chat", "/c/V001/report", "/c/V003/report", "/c/V002/addon/A001",
                    "/c/V002/chat?about=A001", "/c/V006", "/sa", "/sa/visit/V001", "/sa/visit/V004", "/sa/visit/V005",
                    "/sa/addon/A005", "/sa/addon/A001", "/tech", "/dashboard", "/lounge", "/static/img/samples/brake_pad.svg"]:
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
        self.assertEqual(self.c.get("/c/NOPE").status_code, 404)
        self.assertEqual(self.c.get("/api/visit/NOPE/state").status_code, 404)

    def test_full_demo_flow(self):
        # ⑥ 專員產生並送出說明頁
        d = self.post("/api/visit/V004/explanation/generate")
        self.assertEqual(len(d["items"]), 4)
        self.assertTrue(all(i["note"] for i in d["items"]))
        self.post("/api/visit/V004/explanation/send", {"greeting": "張先生您好，測試問候", "notes": {"engine_oil": "專員改過的說明"}})
        html = self.c.get("/c/V004").get_data(as_text=True)
        self.assertIn("張先生您好，測試問候", html)
        self.assertIn("專員改過的說明", html)
        self.assertIn("修改並", html)

        # ⑧ 技師回報（範例圖）
        r = self.c.post("/api/tech/addon", data={"visit_id": "V001", "item_code": "brake_pad", "measure": "2.5",
                                                  "note": "前輪來令片剩2.5mm", "use_sample": "1"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        aid = r.get_json()["id"]
        a = self.c.get(f"/api/addon/{aid}").get_json()
        self.assertEqual(a["draft"]["tier"], "safety")  # 2.5 mm < 3 mm 門檻
        self.assertEqual(a["draft"]["title"], "前輪煞車來令片變薄了")
        self.assertEqual(a["addon"]["media"][0]["url"], "/static/img/samples/brake_pad.svg")
        # 車主在專員核可前看不到
        self.assertEqual(self.c.get(f"/c/V001/addon/{aid}").status_code, 404)
        self.assertEqual(self.c.get("/api/visit/V001/state").get_json()["pending"], [])

        # ⑦ 專員核可（改標題、改價格）
        s = self.post(f"/api/addon/{aid}/send", {"title": "前輪煞車片該換了", "tier": "safety", "price": 3300, "minutes": 40})
        self.assertEqual(s["addon"]["status"], "sent")
        st = self.c.get("/api/visit/V001/state").get_json()
        self.assertEqual([p["id"] for p in st["pending"]], [aid])
        self.assertIn("前輪煞車片該換了", self.c.get(f"/c/V001/addon/{aid}").get_data(as_text=True))

        # ③ 車主提問
        m = self.post("/api/visit/V001/chat", {"question": "不換會怎樣？", "about": aid})["messages"]
        self.assertEqual(m[0]["role"], "user")
        self.assertIn("來令片", m[1]["text"])
        self.assertFalse(m[1]["escalated"])
        m = self.post("/api/visit/V001/chat", {"question": "還要等多久？"})["messages"]
        self.assertIn("預計", m[1]["text"])
        m = self.post("/api/visit/V001/chat", {"question": "延長保固要怎麼辦？"})["messages"]
        self.assertTrue(m[1]["escalated"])
        board = self.c.get("/api/board").get_json()
        mine = lambda b: [t for t in b["todo"] if t["text"].startswith("BKR-5821") and "延長保固" in t["text"]]  # noqa: E731
        self.assertEqual(len(mine(board)), 1)

        # ② 車主決定
        before = self.c.get("/api/visit/V001/state").get_json()["items_sig"]
        dd = self.post(f"/api/addon/{aid}/decide", {"decision": "approve"})
        self.assertEqual(dd["addon"]["status"], "approved")
        after = self.c.get("/api/visit/V001/state").get_json()["items_sig"]
        self.assertNotEqual(before, after)
        self.assertEqual(self.c.post(f"/api/addon/{aid}/decide", json={"decision": "defer"}).status_code, 409)

        # 專員回覆被轉過來的問題
        self.post("/api/visit/V001/reply", {"text": "林小姐您好，延長保固的資格我交車時跟您說明。"})
        board = self.c.get("/api/board").get_json()
        self.assertEqual(mine(board), [])
        self.assertTrue(any(t["text"].startswith("RCF-6618") for t in board["todo"]))  # 黃小姐那則仍待回覆

        # 進度推到完工 → ④ 健康報告
        for _ in range(4):
            self.post("/api/visit/V001/stage", {"action": "next"})
        st = self.c.get("/api/visit/V001/state").get_json()
        self.assertEqual(st["stage"], 5)
        self.assertTrue(st["report_ready"])
        rep = self.post("/api/visit/V001/report")["report"]
        self.assertTrue(rep["headline"])
        html = self.c.get("/c/V001/report").get_data(as_text=True)
        self.assertIn("下次回廠", html)
        self.assertIn("煞車來令片", html)
        self.post("/api/visit/V001/rating", {"stars": 5})

        # ⑨ 儀表板
        dash = self.c.get("/api/dashboard").get_json()
        self.assertGreaterEqual(dash["live"]["questions"], 3)
        self.assertEqual(dash["live"]["rating"], 5.0)
        self.assertTrue(dash["insights"])

        # 報到、重置
        self.post("/api/visit/V006/checkin")
        self.assertEqual(self.c.get("/api/visit/V006/state").get_json()["stage"], 0)
        self.post("/api/reset")
        self.assertEqual(self.c.get("/api/visit/V006/state").get_json()["stage"], -1)

    def test_milestone_report(self):
        rep = self.post("/api/visit/V003/report")["report"]
        self.assertIn("10 萬公里", rep["headline"])

    def test_upload_real_file(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        r = self.c.post("/api/tech/addon", data={"visit_id": "V002", "item_code": "tire", "measure": "1.2",
                                                  "media": (io.BytesIO(png), "photo.png")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        a = self.c.get(f"/api/addon/{r.get_json()['id']}").get_json()
        self.assertEqual(a["draft"]["tier"], "safety")  # 1.2 mm < 1.6 mm
        url = a["addon"]["media"][0]["url"]
        self.assertTrue(url.startswith("/uploads/"))
        self.assertEqual(self.c.get(url).status_code, 200)
        bad = self.c.post("/api/tech/addon", data={"visit_id": "V002", "item_code": "tire", "media": (io.BytesIO(b"x"), "a.exe")},
                          content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 400)
        bad = self.c.post("/api/tech/addon", data={"visit_id": "V002", "item_code": "tire", "measure": "abc"})
        self.assertEqual(bad.status_code, 400)


class GateAndNetworkTest(unittest.TestCase):
    def test_access_code(self):
        cfg = appmod.load_config()
        cfg["ai"] = dict(cfg["ai"], mode="offline")
        cfg["access_code"] = "abc123"
        cfg["_share"] = {"url": "https://demo.trycloudflare.com", "code": "abc123", "status": "ready", "generated": False}
        tmp = tempfile.mkdtemp(prefix="hotai_test_")
        # 模擬經由 Cloudflare 通道進來的外部請求
        R = {"base_url": "https://demo.trycloudflare.com", "headers": {"Cf-Ray": "8c1", "Cf-Connecting-Ip": "203.0.113.5"}}
        try:
            c = appmod.create_app(cfg, data_dir=tmp).test_client()
            r = c.get("/c/V001", **R)
            self.assertEqual(r.status_code, 302)
            self.assertIn("/enter?next=/c/V001", r.headers["Location"])
            self.assertEqual(c.get("/api/board", **R).status_code, 401)
            self.assertEqual(c.post("/api/reset", **R).status_code, 401)
            self.assertEqual(c.get("/api/share", **R).status_code, 401)
            self.assertEqual(c.get("/static/css/app.css", **R).status_code, 200)
            self.assertIn("密碼不對", c.post("/enter", data={"code": "nope", "next": "/c/V001"}, **R).get_data(as_text=True))
            r = c.post("/enter", data={"code": "abc123", "next": "/c/V001"}, **R)
            self.assertEqual(r.headers["Location"], "/c/V001")
            self.assertEqual(c.get("/c/V001", **R).status_code, 200)
            self.assertEqual(c.get("/api/board", **R).status_code, 200)
            # 外面的人登入後也不能重置、不能看分享密碼、看不到重置按鈕
            self.assertEqual(c.post("/api/reset", **R).status_code, 403)
            self.assertEqual(c.post("/api/ai/test", **R).status_code, 403)
            self.assertEqual(c.get("/api/share", **R).status_code, 404)
            hub = c.get("/", **R).get_data(as_text=True)
            self.assertNotIn('id="btnReset"', hub)
            self.assertNotIn("abc123", hub)
            r = c.post("/enter", data={"code": "abc123", "next": "//evil.example/"}, **R)
            self.assertEqual(r.headers["Location"], "/")  # 不會被導到外部網站
            # 主持人自己用 localhost 開：不用密碼、可以重置、看得到分享卡片
            local = appmod.create_app(cfg, data_dir=tmp).test_client()
            self.assertEqual(local.get("/c/V001").status_code, 200)
            self.assertEqual(local.post("/api/reset").status_code, 200)
            self.assertEqual(local.get("/api/share").get_json()["code"], "abc123")
            self.assertIn('id="shareCard"', local.get("/").get_data(as_text=True))
            # 從 localhost 進來但帶有轉送標頭（例如其他通道工具）→ 仍然要密碼
            self.assertEqual(local.get("/c/V001", headers={"X-Forwarded-For": "198.51.100.7"}).status_code, 302)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_host_code_gives_teammates_full_rights(self):
        cfg = appmod.load_config()
        cfg["ai"] = dict(cfg["ai"], mode="offline")
        cfg["access_code"] = "toyota1234"
        cfg["host_code"] = "host-abc234"
        cfg["_share"] = {"url": "https://demo-link-test.trycloudflare.com", "code": "toyota1234", "status": "ready",
                         "generated": True, "host_code": "host-abc234", "host_generated": True}
        tmp = tempfile.mkdtemp(prefix="hotai_test_")
        R = {"base_url": "https://demo-link-test.trycloudflare.com", "headers": {"Cf-Ray": "8c1", "Cf-Connecting-Ip": "203.0.113.9"}}
        try:
            app = appmod.create_app(cfg, data_dir=tmp)
            guest, mate = app.test_client(), app.test_client()
            guest.post("/enter", data={"code": "toyota1234", "next": "/"}, **R)
            self.assertEqual(guest.post("/api/reset", **R).status_code, 403)
            self.assertEqual(guest.get("/api/share", **R).status_code, 404)
            r = mate.post("/enter", data={"code": "host-abc234", "next": "/sa"}, **R)
            self.assertEqual(r.headers["Location"], "/sa")
            self.assertEqual(mate.get("/sa", **R).status_code, 200)
            self.assertEqual(mate.post("/api/reset", **R).status_code, 200)
            self.assertEqual(mate.get("/api/share", **R).get_json()["host_code"], "host-abc234")
            hub = mate.get("/", **R).get_data(as_text=True)
            self.assertIn('id="btnReset"', hub)
            self.assertIn('id="shareCard"', hub)
            self.assertIn("主持人密碼」登入的隊友", hub)
            # 同一個人改用展示密碼重新登入 → 降回一般權限
            mate.post("/enter", data={"code": "toyota1234", "next": "/"}, **R)
            self.assertEqual(mate.post("/api/reset", **R).status_code, 403)
            # 兩組密碼設成一樣 → 主持人密碼停用，不會讓所有試用者都變主持人
            cfg2 = dict(cfg, host_code="toyota1234")
            same = appmod.create_app(cfg2, data_dir=tmp).test_client()
            same.post("/enter", data={"code": "toyota1234", "next": "/"}, **R)
            self.assertEqual(same.post("/api/reset", **R).status_code, 403)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pack_for_team_strips_secrets(self):
        import subprocess
        import zipfile

        tmp = tempfile.mkdtemp(prefix="hotai_pack_")
        out = os.path.join(tmp, "team.zip")
        try:
            proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            subprocess.run([sys.executable, os.path.join(proj, "pack_for_team.py"), out], check=True,
                           capture_output=True, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
                self.assertFalse(any("/data/" in n.replace("\\", "/") for n in names))
                self.assertFalse(any("__pycache__" in n for n in names))
                cfg_name = next(n for n in names if n.replace("\\", "/").endswith("看得見的保養/config.json"))
                packed = json.loads(z.read(cfg_name).decode("utf-8"))
                self.assertEqual(packed["ai"]["gemini_api_key"], "")
                self.assertEqual(packed["access_code"], "")
                self.assertEqual(packed["host_code"], "")
                self.assertTrue(any(n.replace("\\", "/").endswith("app.py") for n in names))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_code_blocks_tunnel_traffic(self):
        # 一般模式（沒設密碼）：本機、區網照常；從外部通道轉進來的一律拒絕
        app, c, tmp = make_client()
        try:
            self.assertEqual(c.get("/c/V001").status_code, 200)
            self.assertEqual(c.get("/c/V001", environ_base={"REMOTE_ADDR": "192.168.1.23"}).status_code, 200)
            R = {"base_url": "https://old-link-demo.trycloudflare.com", "headers": {"Cf-Ray": "8c1", "Cf-Connecting-Ip": "203.0.113.5"}}
            self.assertEqual(c.get("/c/V001", **R).status_code, 403)
            self.assertEqual(c.post("/api/reset", **R).status_code, 403)
            self.assertEqual(c.get("/static/css/app.css", **R).status_code, 200)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_share_url_pattern(self):
        import share

        line = "2026-09-27T09:10:11Z INF |  https://smooth-river-tiger-demo.trycloudflare.com                    |"
        self.assertEqual(share.URL_RE.search(line).group(0), "https://smooth-river-tiger-demo.trycloudflare.com")
        self.assertIsNone(share.URL_RE.search("https://api.trycloudflare.com.evil.example/x x"))
        self.assertIsNone(share.URL_RE.search("Requesting new quick Tunnel on https://api.trycloudflare.com/tunnel"))

    def test_never_binds_public_ip(self):
        from unittest import mock

        fake = [(2, 1, 6, "", (ip, 0)) for ip in ("218.173.34.151", "169.254.1.2", "26.241.92.44", "172.20.10.6")]

        class NoRoute:
            def __init__(self, *a, **k):
                pass

            def connect(self, *_):
                raise OSError("no route")

            def close(self):
                pass

        with mock.patch.object(appmod.socket, "getaddrinfo", return_value=fake), mock.patch.object(appmod.socket, "socket", NoRoute):
            self.assertEqual(appmod.private_ips(), ["172.20.10.6"])
        with mock.patch.object(appmod, "private_ips", return_value=[]):
            hosts, lan, note = appmod.resolve_hosts("lan")
            self.assertEqual(hosts, ["127.0.0.1"])
            self.assertIsNone(lan)
            self.assertTrue(note)
        with mock.patch.object(appmod, "private_ips", return_value=["192.168.1.5"]):
            self.assertEqual(appmod.resolve_hosts("lan")[0], ["127.0.0.1", "192.168.1.5"])


class FakeAITest(unittest.TestCase):
    """模擬 Gemini／Ollama 的回應，檢查解析、規則優先與出錯時退回離線規則。"""

    def setUp(self):
        self.app, self.c, self.tmp = make_client()
        self.eng = self.app.engine
        self.state = self.app.store.snapshot()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake(self, mode, text):
        self.eng.mode = mode
        if mode == "gemini":
            self.eng._gemini = lambda system, prompt, temperature, images=None: text
        else:
            self.eng._ollama = lambda system, prompt, temperature, max_tokens, images=None: text

    def test_policy_always_escalates(self):
        self.fake("gemini", json.dumps({"answer": "延保可以直接加購喔", "escalate": "false", "topic": "price"}, ensure_ascii=False))
        v = self.state["visits"]["V005"]
        r = self.eng.answer(v, [], "延長保固要怎麼申請？", "目前進度…")
        self.assertTrue(r["escalate"])
        self.assertEqual(r["topic"], "policy")
        self.assertEqual(r["source"], "gemini")
        self.assertIn("專員", r["answer"])

    def test_string_false_is_false(self):
        self.fake("gemini", '```json\n{"answer": "煞車油會吸水，要定期換。", "escalate": "false", "topic": "necessity"}\n```')
        v = self.state["visits"]["V001"]
        r = self.eng.answer(v, [], "為什麼要換煞車油？", "目前進度…")
        self.assertFalse(r["escalate"])
        self.assertEqual(r["answer"], "煞車油會吸水，要定期換。")

    def test_rule_tier_wins(self):
        self.fake("ollama", json.dumps({"title": "来令片变薄", "plain": "说明", "tier": "can_wait", "tier_reason": "还可以",
                                         "consequence": "后果", "suggestion": "下次再换"}, ensure_ascii=False))
        v = self.state["visits"]["V001"]
        d = self.eng.addon_draft(v, "brake_pad", 2.0, "剩2mm", 3200, 40)
        self.assertEqual(d["tier"], "safety")  # 門檻優先於模型
        self.assertIn("依原廠門檻", d["tier_reason"])  # 分級不一致時理由改回規則版
        self.assertEqual(d["source"], "ollama")
        if self.eng._cc is not None:
            self.assertEqual(d["title"], "來令片變薄")  # 簡體轉台灣繁體

    def test_contradicting_suggestion_is_fixed(self):
        self.fake("ollama", json.dumps({"title": "前輪煞車來令片需更換", "plain": "剩 2.8mm", "tier": "safety",
                                         "tier_reason": "影響煞車", "consequence": "煞車距離變長",
                                         "suggestion": "建議儘快處理，但若無法立即更換，可稍延後觀察。"}, ensure_ascii=False))
        d = self.eng.addon_draft(self.state["visits"]["V002"], "brake_pad", 2.8, "剩2.8mm", 3200, 40)
        self.assertEqual(d["tier"], "safety")
        self.assertNotIn("延後", d["suggestion"])
        self.assertEqual(d["title"], "前輪煞車來令片需更換")  # 其他 AI 內容保留

    def test_escalation_names_advisor(self):
        self.fake("ollama", json.dumps({"answer": "請直接洽詢服務專員。", "escalate": True, "topic": "policy"}, ensure_ascii=False))
        v = self.state["visits"]["V002"]
        r = self.eng.answer(v, [], "延長保固還可以買嗎？", "目前進度…")
        self.assertIn(v["advisor"], r["answer"])

    def test_bad_output_falls_back(self):
        self.fake("gemini", "抱歉我無法回答")
        v = self.state["visits"]["V001"]
        exp = self.eng.explanation(v)
        self.assertEqual(exp["source"], "offline")
        self.assertEqual(set(exp["items"].keys()), {i["code"] for i in v["items"]})

    def test_explanation_merge(self):
        codes = [i["code"] for i in self.state["visits"]["V001"]["items"]]
        payload = {"greeting": "林小姐您好", "summary": "今天重點", "items": [{"code": codes[0], "personal_note": "AI 的說明"},
                                                                          {"code": "not_exist", "personal_note": "不該出現"}]}
        self.fake("gemini", json.dumps(payload, ensure_ascii=False))
        exp = self.eng.explanation(self.state["visits"]["V001"])
        self.assertEqual(exp["source"], "gemini")
        self.assertEqual(exp["items"][codes[0]]["personal_note"], "AI 的說明")
        self.assertNotIn("not_exist", exp["items"])
        self.assertTrue(exp["items"][codes[1]]["personal_note"])  # 沒給的項目用規則版補上


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class NewFeatureTest(unittest.TestCase):
    """花費透明（價目表、零件／工資）、自選項目、指定技師與獎金、車主拍照詢問。"""

    def setUp(self):
        self.app, self.c, self.tmp = make_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, url, body=None):
        r = self.c.post(url, json=body or {})
        self.assertLess(r.status_code, 400, (url, r.status_code, r.get_data(as_text=True)[:300]))
        return r.get_json()

    def photo(self, vid, text, name="p.png", data=PNG):
        return self.c.post(f"/api/visit/{vid}/photo", data={"text": text, "photo": (io.BytesIO(data), name)},
                           content_type="multipart/form-data")

    def perf(self, tid):
        return next(r for r in self.c.get("/api/tech/performance").get_json()["rows"] if r["id"] == tid)

    def test_pages_render(self):
        for url in ["/prices", "/prices?pt=ev", "/prices?pt=gas", "/prices?pt=nope", "/c/V001/prices", "/c/V003/prices",
                    "/c/V007/prices", "/c/V007", "/c/V002", "/c/V005/chat", "/static/img/samples/warning_light.svg"]:
            self.assertEqual(self.c.get(url).status_code, 200, url)
        html = self.c.get("/c/V001/prices").get_data(as_text=True)
        self.assertIn("本次", html)
        self.assertIn("下次", html)
        self.assertIn("工資", html)
        # 10 萬公里以上的車：價目表延伸到「下次」那一級
        self.assertIn("110,000 公里", self.c.get("/c/V003/prices").get_data(as_text=True))
        sa = self.c.get("/sa/visit/V002").get_data(as_text=True)
        self.assertIn('id="techSel"', sa)
        self.assertIn('id="optList"', sa)
        tech = self.c.get("/tech").get_data(as_text=True)
        self.assertIn('id="me"', tech)
        self.assertIn('data-tech="T01"', tech)

    def test_price_breakdown_adds_up(self):
        kb = self.app.kb
        for pt in ("gas", "hybrid", "ev"):
            for row in kb.price_table(pt):
                self.assertEqual(row["parts"] + row["labor"], row["total"], (pt, row["km"]))
                for it in row["items"]:
                    self.assertGreaterEqual(it["parts"], 0)
                    self.assertEqual(it["labor"] % 10, 0)
        bd = kb.breakdown("brake_pad", 3300, 40)
        self.assertEqual(bd["labor"], 800)  # 40 分鐘 × 每小時 1,200（示意）
        self.assertEqual(bd["parts"], 2500)
        self.assertTrue(kb.breakdown("inspection")["included"])  # 免費項目顯示「含」

    def test_optional_items(self):
        r = self.post("/api/visit/V006/optional", {"code": "ac_clean", "on": True})
        self.assertEqual(r["totals"]["opt"]["count"], 1)
        self.assertEqual(r["totals"]["optional"], 1800)
        t = r["totals"]
        self.assertEqual(t["total"], t["planned"] + t["optional"] + t["added"])
        self.assertEqual(t["parts"] + t["labor"], t["total"])
        self.post("/api/visit/V006/optional", {"code": "ac_clean", "on": True})  # 重複加選不會多一筆
        items = self.app.store.snapshot()["visits"]["V006"]["items"]
        self.assertEqual(sum(1 for i in items if i["code"] == "ac_clean"), 1)
        self.assertIn("已加選", self.c.get("/c/V006").get_data(as_text=True))
        r = self.post("/api/visit/V006/optional", {"code": "ac_clean", "on": False})
        self.assertEqual(r["totals"]["opt"]["count"], 0)
        # 不能自選的項目、不適用的車型、太晚加選
        self.assertEqual(self.c.post("/api/visit/V006/optional", json={"code": "brake_pad", "on": True}).status_code, 400)
        self.assertEqual(self.c.post("/api/visit/V007/optional", json={"code": "fuel_system_clean", "on": True}).status_code, 400)
        self.assertEqual(self.c.post("/api/visit/V005/optional", json={"code": "ac_clean", "on": True}).status_code, 409)
        # 專員改工單時，車主自選的項目會保留
        codes = [i["code"] for i in self.app.store.snapshot()["visits"]["V002"]["items"] if i.get("source", "planned") == "planned"]
        self.post("/api/visit/V002/items", {"codes": codes[:-1]})
        items = self.app.store.snapshot()["visits"]["V002"]["items"]
        self.assertTrue(any(i["code"] == "ac_clean" and i["source"] == "optional" for i in items))
        # 自選項目不會混進專員的必要項目清單
        self.assertEqual(self.c.post("/api/visit/V002/items", json={"codes": ["ac_clean"]}).status_code, 400)

    def test_customer_designates_technician(self):
        before = self.c.get("/api/visit/V006/state").get_json()["items_sig"]
        r = self.post("/api/visit/V006/technician", {"technician_id": "T03"})
        self.assertEqual(r["tech"]["name"], "林技師")
        self.assertEqual(r["requested"], "T03")
        self.assertNotEqual(before, self.c.get("/api/visit/V006/state").get_json()["items_sig"])
        self.assertIn("您指定的技師", self.c.get("/c/V006").get_data(as_text=True))
        appt = next(a for a in self.c.get("/api/board").get_json()["appointments"] if a["id"] == "V006")
        self.assertEqual(appt["requested"], "林技師")
        self.post("/api/visit/V006/technician", {"technician_id": ""})  # 取消指定
        self.assertIsNone(self.app.store.snapshot()["visits"]["V006"]["requested_tech_id"])
        self.assertEqual(self.c.post("/api/visit/V006/technician", json={"technician_id": "T99"}).status_code, 400)
        # 開始作業後車主不能換；專員可以（完工前）
        self.assertEqual(self.c.post("/api/visit/V005/technician", json={"technician_id": "T01"}).status_code, 409)
        self.post("/api/visit/V005/technician", {"technician_id": "T01", "by": "advisor"})
        self.assertEqual(self.app.store.snapshot()["visits"]["V005"]["technician"], "周技師")
        self.assertEqual(self.c.post("/api/visit/V003/technician", json={"technician_id": "T02", "by": "advisor"}).status_code, 409)
        row = next(x for x in self.c.get("/api/board").get_json()["rows"] if x["id"] == "V001")
        self.assertEqual((row["technician"], row["requested"]), ("周技師", "周技師"))
        # 被指名算在技師本月表現裡（V001 指名周技師、已報到）
        self.assertGreaterEqual(self.perf("T01")["live"]["designated"], 1)

    def test_report_is_credited_to_technician(self):
        b0 = self.perf("T03")
        r = self.c.post("/api/tech/addon", data={"visit_id": "V001", "item_code": "brake_pad", "measure": "2.5",
                                                  "note": "前輪來令片剩2.5mm", "use_sample": "1", "technician_id": "T03"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        aid = r.get_json()["id"]
        a = self.app.store.snapshot()["addons"][aid]
        self.assertEqual((a["technician_id"], a["technician"]), ("T03", "林技師"))
        b1 = self.perf("T03")
        self.assertEqual(b1["live"]["reports"], b0["live"]["reports"] + 1)
        self.assertEqual(b1["bonus"], b0["bonus"] + 30)  # 附照片＋量測值＝完整回報
        self.post(f"/api/addon/{aid}/send")  # 專員沒修改就送出
        self.assertEqual(self.perf("T03")["bonus"], b0["bonus"] + 40)
        html = self.c.get(f"/c/V001/addon/{aid}").get_data(as_text=True)
        self.assertIn("林技師", html)
        self.assertIn("零件 NT$", html)
        # 沒選「我是」或亂填：記在這台車的負責技師名下
        for extra in ({}, {"technician_id": "T99"}):
            r = self.c.post("/api/tech/addon", data=dict({"visit_id": "V001", "item_code": "wiper"}, **extra))
            self.assertEqual(self.app.store.snapshot()["addons"][r.get_json()["id"]]["technician_id"], "T01")

    def test_bonus_ignores_upsell(self):
        perf = self.c.get("/api/tech/performance").get_json()
        self.assertEqual({r["key"] for r in perf["rules"]}, {"designated", "quality", "unedited", "five_star"})
        self.assertIn("不看追加金額", perf["note"])
        b0 = self.perf("T02")["bonus"]
        # V002（吳技師）的追加項目被車主同意：獎金不會因此增加
        aid = next(a["id"] for a in self.app.store.snapshot()["addons"].values()
                   if a["visit_id"] == "V002" and a["status"] == "sent")
        self.post(f"/api/addon/{aid}/decide", {"decision": "approve"})
        self.assertEqual(self.perf("T02")["bonus"], b0)

    def test_photo_upload_offline(self):
        r = self.photo("V001", "儀表板亮紅色的燈")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        um, am = d["messages"]
        self.assertEqual((um["kind"], am["kind"]), ("photo", "photo_analysis"))
        self.assertEqual(am["urgency"], "stop")
        self.assertTrue(am["escalated"])
        self.assertFalse(d["analysis"]["vision"])
        self.assertIn("陳專員", am["text"])  # 一律轉給專員與技師
        self.assertEqual(self.c.get(um["image"]).status_code, 200)
        board = self.c.get("/api/board").get_json()
        self.assertTrue(any(t["kind"] == "photo" and t["text"].startswith("BKR-5821") for t in board["todo"]))
        self.assertEqual(next(x for x in board["rows"] if x["id"] == "V001")["photos_pending"], 1)
        photos = self.c.get("/api/tech/photos").get_json()["photos"]
        pid = next(p["id"] for p in photos if p["visit_id"] == "V001")
        self.assertEqual(photos[0]["status"], "new")  # 待查看的排最前面
        # 技師按「已查看」→ 車主收到技師回覆、待辦消失
        self.post(f"/api/visit/V001/photo/{pid}/check", {"technician_id": "T01"})
        self.assertTrue(self.post(f"/api/visit/V001/photo/{pid}/check", {"technician_id": "T01"})["already"])
        chat = self.c.get("/api/visit/V001/chat").get_json()["messages"]
        self.assertEqual(chat[-1]["role"], "tech")
        self.assertIn("周技師", chat[-1]["text"])
        board = self.c.get("/api/board").get_json()
        self.assertFalse(any(t["kind"] == "photo" and t["text"].startswith("BKR-5821") for t in board["todo"]))
        self.assertEqual(self.perf("T01")["live"]["photos_checked"], 1)
        self.assertEqual(self.c.post("/api/visit/V001/photo/P999/check", json={}).status_code, 404)
        # 其他分級與錯誤輸入
        self.assertEqual(self.photo("V001", "車門有一點刮痕").get_json()["analysis"]["urgency"], "watch")
        self.assertEqual(self.photo("V001", "").get_json()["analysis"]["urgency"], "unknown")
        self.assertEqual(self.photo("V001", "看看", name="a.exe").status_code, 400)
        self.assertEqual(self.c.post("/api/visit/V001/photo", data={"text": "沒有照片"}).status_code, 400)
        # 到廠前也能先拍照（預約中的車）
        self.assertEqual(self.photo("V006", "右後輪胎壓好像不足").status_code, 200)
        appt = next(a for a in self.c.get("/api/board").get_json()["appointments"] if a["id"] == "V006")
        self.assertEqual(appt["photos"], 1)
        dash = self.c.get("/api/dashboard").get_json()
        self.assertGreaterEqual(next(t for t in dash["topics"] if t["key"] == "photo")["live"], 1)

    def test_seeded_photo_and_tech_rating(self):
        chat = self.c.get("/api/visit/V005/chat").get_json()["messages"]
        photo = next(m for m in chat if m.get("kind") == "photo")
        self.assertEqual(self.c.get(photo["image"]).status_code, 200)
        self.assertEqual(self.c.get("/api/tech/photos").get_json()["photos"][0]["plate"], "RCF-6618")
        self.assertEqual(self.c.post("/api/visit/V003/tech_rating", json={"stars": 0}).status_code, 400)
        before = self.perf("T01")
        self.post("/api/visit/V003/tech_rating", {"stars": 5})
        after = self.perf("T01")
        self.assertEqual(after["live"]["five_star"], before["live"]["five_star"] + 1)
        self.assertEqual(after["bonus"], before["bonus"] + 50)
        self.post("/api/visit/V003/report")
        html = self.c.get("/c/V003/report").get_data(as_text=True)
        self.assertIn('id="techStars"', html)
        self.assertIn("謝謝！您的評分會回饋給技師。", html)
        dash = self.c.get("/api/dashboard").get_json()
        self.assertEqual(len(dash["techs"]["rows"]), 4)
        self.assertTrue(any("指名最多" in s for s in dash["insights"]))

    def test_old_data_is_reseeded(self):
        tmp = tempfile.mkdtemp(prefix="hotai_test_")
        try:
            with open(os.path.join(tmp, "state.json"), "w", encoding="utf-8") as f:
                json.dump({"schema": 1, "visits": {}, "addons": {}}, f)
            cfg = appmod.load_config()
            cfg["ai"] = dict(cfg["ai"], mode="offline", gemini_api_key="")
            app = appmod.create_app(cfg, data_dir=tmp)
            st = app.store.snapshot()
            self.assertEqual(st["schema"], appmod.SCHEMA)
            self.assertIn("T01", st["technicians"])
            self.assertEqual(app.test_client().get("/c/V001").status_code, 200)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PhotoAITest(unittest.TestCase):
    """照片判讀：能看圖時才把照片交給 AI；AI 判得比車主描述輕時，採用較緊急的分級（安全優先）。"""

    def setUp(self):
        self.app, self.c, self.tmp = make_client()
        self.eng = self.app.engine
        self.v = self.app.store.snapshot()["visits"]["V001"]
        self.seen = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def gemini_says(self, payload):
        def fake(system, prompt, temperature, images=None):
            self.seen.append(images)
            return json.dumps(payload, ensure_ascii=False)
        self.eng.mode = "gemini"
        self.eng._gemini = fake

    def test_ai_sees_the_photo(self):
        self.gemini_says({"summary": "儀表板上亮著黃色的胎壓警示燈", "urgency": "soon", "advice": "請盡快檢查胎壓", "related": "輪胎"})
        r = self.eng.analyze_photo(self.v, PNG, "image/png", "這個燈是什麼？")
        self.assertEqual((r["source"], r["vision"], r["urgency"]), ("gemini", True, "soon"))
        self.assertEqual(len(self.seen[0]), 1)  # 照片有交給 AI

    def test_safety_first(self):
        self.gemini_says({"summary": "看起來是一般的儀表板", "urgency": "watch", "advice": "可以先觀察", "related": ""})
        r = self.eng.analyze_photo(self.v, PNG, "image/png", "踩煞車的時候煞車變軟")
        self.assertEqual(r["urgency"], "stop")
        self.assertIn("安全的地方停車", r["advice"])
        self.gemini_says({"summary": "輪胎側面有明顯鼓包", "urgency": "stop", "advice": "請勿繼續行駛", "related": "輪胎"})
        self.assertEqual(self.eng.analyze_photo(self.v, PNG, "image/png", "有點刮痕")["urgency"], "stop")  # AI 較緊急也保留

    def test_bad_ai_output_falls_back(self):
        self.gemini_says({"urgency": "maybe"})
        r = self.eng.analyze_photo(self.v, PNG, "image/png", "車底在漏油")
        self.assertEqual((r["source"], r["urgency"]), ("offline", "soon"))
        self.assertIn("沒有成功", r["note"])

    def test_local_model_without_vision_never_gets_the_photo(self):
        def boom(*a, **k):
            raise AssertionError("不應該把照片送給不能看圖的模型")
        self.eng.mode = "ollama"
        self.eng.cfg["ollama_vision_model"] = ""
        self.eng._ollama = boom
        r = self.eng.analyze_photo(self.v, PNG, "image/png", "引擎燈亮了")
        self.assertEqual((r["source"], r["vision"], r["urgency"]), ("offline", False, "soon"))
        self.assertFalse(self.eng.status()["vision"])
        self.eng.cfg["ollama_vision_model"] = "llava:7b"
        self.eng._ollama = lambda system, prompt, temperature, max_tokens, images=None: json.dumps(
            {"summary": "看到引擎故障燈", "urgency": "soon", "advice": "盡快檢查", "related": ""}, ensure_ascii=False)
        r = self.eng.analyze_photo(self.v, PNG, "image/png", "引擎燈亮了")
        self.assertEqual((r["source"], r["vision"]), ("ollama", True))


class CloudModeTest(unittest.TestCase):
    """雲端版（Render）：密碼從環境變數讀、雲端上沒有「主持電腦」、重置一律要主持人密碼。"""

    SITE = "https://seeable-maintenance.onrender.com"
    H = {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "seeable-maintenance.onrender.com"}

    def make(self, **env):
        cfg = appmod.cloud_config(dict({"SECRET_KEY": "test-secret-key-123"}, **env))
        tmp = tempfile.mkdtemp(prefix="hotai_cloud_")
        self.addCleanup(shutil.rmtree, tmp, True)
        app = appmod.create_app(cfg, data_dir=tmp)
        return app, app.test_client()

    def get(self, c, url, headers=None):
        return c.get(url, base_url=self.SITE, headers=headers or self.H)

    def post(self, c, url, headers=None, **kw):
        return c.post(url, base_url=self.SITE, headers=headers or self.H, **kw)

    def test_config_from_env(self):
        cfg = appmod.cloud_config({"ACCESS_CODE": " demo2026 ", "HOST_CODE": "team-secret-99", "AI_MODE": "weird"})
        self.assertTrue(cfg["cloud"])
        self.assertEqual((cfg["access_code"], cfg["host_code"]), ("demo2026", "team-secret-99"))
        self.assertEqual(cfg["ai"]["mode"], "auto")
        app, _ = self.make()
        self.assertEqual(app.engine.mode, "offline")  # 沒有金鑰；雲端沒有本機 Ollama，也不去偵測
        self.assertIn("GEMINI_API_KEY", app.engine.detail)
        self.assertEqual(app.secret_key, b"test-secret-key-123")
        app, _ = self.make(GEMINI_API_KEY="fake-key-for-test")
        self.assertEqual(app.engine.mode, "gemini")
        cfg = appmod.cloud_config({"ACCESS_CODE": "-", "GEMINI_API_KEY": "－"})  # 減號＝不設定
        self.assertEqual((cfg["access_code"], cfg["ai"]["gemini_api_key"]), ("", ""))

    def test_public_mode(self):
        _, c = self.make(HOST_CODE="team-secret-99")
        self.assertEqual(self.get(c, "/c/V001").status_code, 200)
        self.assertEqual(self.get(c, "/sa").status_code, 200)
        self.assertEqual(self.post(c, "/api/reset").status_code, 403)
        self.assertEqual(self.post(c, "/api/ai/test").status_code, 403)
        hub = self.get(c, "/").get_data(as_text=True)
        self.assertIn("主持人登入", hub)
        self.assertIn(self.SITE + "/c/V001", hub)  # 固定網址以 https 顯示
        self.assertNotIn('id="btnReset"', hub)
        self.assertEqual(c.post("/api/reset").status_code, 403)  # 主機內部直接連線也不算「主持電腦」
        r = self.post(c, "/enter", data={"code": "team-secret-99", "next": "/"})
        self.assertEqual(r.headers["Location"], "/")
        self.assertIn("Secure", r.headers.get("Set-Cookie", ""))  # 登入狀態只走 https
        self.assertEqual(self.post(c, "/api/reset").status_code, 200)
        self.assertIn('id="btnReset"', self.get(c, "/").get_data(as_text=True))

    def test_access_code_mode(self):
        _, c = self.make(ACCESS_CODE="demo2026", HOST_CODE="team-secret-99")
        r = self.get(c, "/c/V001")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/enter", r.headers["Location"])
        self.assertEqual(self.get(c, "/healthz").status_code, 200)  # 雲端主機的健康檢查不用密碼
        self.assertIn("Disallow: /", self.get(c, "/robots.txt").get_data(as_text=True))
        self.post(c, "/enter", data={"code": "demo2026", "next": "/c/V001"})
        r = self.get(c, "/c/V001")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(self.post(c, "/api/reset").status_code, 403)
        self.assertNotIn("demo2026", self.get(c, "/").get_data(as_text=True))  # 一般試用者看不到密碼
        self.post(c, "/enter", data={"code": "team-secret-99", "next": "/"})
        self.assertIn("demo2026", self.get(c, "/").get_data(as_text=True))  # 主持人看得到，方便複製邀請訊息
        self.assertEqual(self.post(c, "/api/reset").status_code, 200)

    def test_login_throttle(self):
        _, c = self.make(HOST_CODE="team-secret-99")
        for _ in range(10):
            self.assertIn("密碼不對", self.post(c, "/enter", data={"code": "0000"}).get_data(as_text=True))
        r = self.post(c, "/enter", data={"code": "team-secret-99"})
        self.assertIn("嘗試太多次", r.get_data(as_text=True))  # 猜太多次：連正確的也先擋 10 分鐘
        other = dict(self.H, **{"X-Forwarded-For": "198.51.100.20"})
        self.assertEqual(self.post(c, "/enter", headers=other, data={"code": "team-secret-99"}).status_code, 302)

    def test_short_host_code_warning(self):
        _, c = self.make(HOST_CODE="1234")
        self.post(c, "/enter", data={"code": "1234", "next": "/"})
        self.assertIn("少於 8 碼", self.get(c, "/").get_data(as_text=True))


class ErpLayoutTest(unittest.TestCase):
    """內部系統的 ERP 版面：左側選單的數字、追加審核／車主提問清單、工單表單、技師績效。"""

    def setUp(self):
        self.app, self.c, self.tmp = make_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pages_render(self):
        for url in ["/", "/sa", "/sa/addons", "/sa/questions", "/sa/visit/V002", "/tech", "/dashboard", "/dashboard/techs"]:
            html = self.c.get(url).get_data(as_text=True)
            self.assertIn('class="e-side"', html, url)  # 每一頁都有左側模組選單
        html = self.c.get("/sa/visit/V002").get_data(as_text=True)
        self.assertIn('id="visitTabs"', html)
        self.assertIn("冷氣系統清潔除菌", html)  # 自選項目列在工單明細
        self.assertIn('class="e-statusbar"', html)
        home = self.c.get("/c/V001").get_data(as_text=True)
        for text in ("負責技師", "可自選的保養項目", "目前合計"):
            self.assertIn(text, home)

    def test_nav_counts(self):
        nav = self.c.get("/api/nav").get_json()
        self.assertEqual((nav["cars"], nav["appointments"], nav["photos"]), (4, 2, 1))
        self.assertGreaterEqual(nav["review"], 1)
        self.assertGreaterEqual(nav["questions"], 2)  # 黃小姐的保固問題＋警示燈照片
        self.c.post("/api/visit/V005/reply", json={"text": "黃小姐您好，交車時我帶資料跟您說明。"})
        self.assertLess(self.c.get("/api/nav").get_json()["questions"], nav["questions"])

    def test_question_and_addon_lists(self):
        qs = self.c.get("/api/questions").get_json()["questions"]
        self.assertEqual(qs[0]["status"], "pending")  # 待回覆的排最前面
        self.assertTrue(any(q["kind"] == "photo" and q["image"] for q in qs))
        self.assertTrue(any(q["status"] == "ai" for q in qs))
        ads = self.c.get("/api/addons").get_json()["addons"]
        self.assertTrue(ads and all(a["plate"] and a["owner"] for a in ads))
        self.assertEqual([a["id"] for a in ads], sorted([a["id"] for a in ads], key=lambda x: -self.app.store.snapshot()["addons"][x]["created_at"]))


class BookingTest(unittest.TestCase):
    """車主端線上預約：透明報價、比較 TOYOTA 各服務廠、選時段、預約後接到服務廠的預約清單。"""

    def setUp(self):
        self.app, self.c, self.tmp = make_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def best_slot(self, cid, oid="O01", opt=""):
        rows = self.c.get(f"/api/owners/{oid}/compare?opt={opt}").get_json()["rows"]
        return next(r for r in rows if r["center_id"] == cid)["best"]["iso"]

    def test_pages_render(self):
        for url in ["/o/O01", "/o/O02", "/o/O03", "/o/O01/quote", "/o/O01/quote?opt=ac_clean", "/o/O02/centers",
                    "/o/O03/centers", "/o/O01/centers/C04", "/o/O01/centers/C01?opt=ac_clean"]:
            self.assertEqual(self.c.get(url).status_code, 200, url)
        self.assertEqual(self.c.get("/o/NOPE").status_code, 404)
        self.assertEqual(self.c.get("/o/O01/centers/C99").status_code, 404)
        slot = self.best_slot("C01")
        self.assertEqual(self.c.get(f"/o/O01/confirm?center=C01&slot={slot}").status_code, 200)
        self.assertEqual(self.c.get("/o/O01/confirm?center=C01&slot=2020-01-01T09:00").status_code, 302)  # 過期時段 → 回選時段
        html = self.c.get("/o/O01/quote").get_data(as_text=True)
        for text in ("同一個價格", "和上次比較", "建議", "到廠後可能會多花的錢"):
            self.assertIn(text, html)
        self.assertIn("推薦給您", self.c.get("/o/O01/centers").get_data(as_text=True))

    def test_quote_is_transparent(self):
        import booking

        kb = self.app.kb
        codes = kb.scheduled_items(40000, "hybrid")
        q1 = booking.quote(kb, codes, booking.CENTERS["C01"])
        q3 = booking.quote(kb, codes, booking.CENTERS["S01"])
        off = booking.quote(kb, codes, booking.CENTERS["C01"], True)
        # TOYOTA 官網：社區型保修站與服務廠的零件價格、工資費率相同 → 同一份工單同一個價格
        self.assertEqual((q1["parts"], q1["labor"], q1["total"]), (q3["parts"], q3["labor"], q3["total"]))
        self.assertGreater(off["discount"], 0)
        for q in (q1, q3, off):
            self.assertEqual(q["parts"] + q["labor"] - q["discount"], q["total"])
        d = self.c.get("/api/owners/O01/compare").get_json()
        self.assertEqual(len(d["rows"]), len(booking.CENTERS))
        self.assertTrue(d["recommended"] and d["reasons"])
        d2 = self.c.get("/api/owners/O01/compare?opt=ac_clean").get_json()
        self.assertIn("ac_clean", d2["codes"])
        a = next(r for r in d["rows"] if r["center_id"] == "C01")["normal_total"]
        b = next(r for r in d2["rows"] if r["center_id"] == "C01")["normal_total"]
        self.assertGreater(b, a)
        # 不適用的自選項目、不是自選的項目都會被忽略（電動車沒有燃油系統清潔）
        d3 = self.c.get("/api/owners/O03/compare?opt=fuel_system_clean,brake_pad").get_json()
        self.assertFalse({"fuel_system_clean", "brake_pad"} & set(d3["codes"]))

    def test_booking_reaches_service_center(self):
        slot = self.best_slot("C01", opt="ac_clean")
        r = self.c.post("/api/owners/O01/book", json={"center_id": "C01", "slot": slot, "opt": ["ac_clean"],
                                                       "wait_mode": "leave", "note": "冷氣有異味"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        vid = r.get_json()["vid"]
        v = self.app.store.snapshot()["visits"][vid]
        self.assertEqual((v["stage"], v["center_id"], v["owner_id"]), (-1, "C01", "O01"))
        self.assertTrue(any(i["code"] == "ac_clean" and i["source"] == "optional" for i in v["items"]))
        self.assertEqual(sum(i["price"] for i in v["items"]), v["booking"]["total"])
        self.assertTrue(v["technician_id"])  # 本廠預約先派技師，車主到廠前還能改指定
        appt = next(a for a in self.c.get("/api/board").get_json()["appointments"] if a["id"] == vid)
        self.assertTrue(appt["online"])
        html = self.c.get(f"/c/{vid}?booked=1").get_data(as_text=True)
        for text in ("預約完成", "內湖服務廠", "冷氣有異味"):
            self.assertIn(text, html)
        self.assertIn(vid, self.c.get("/o/O01").get_data(as_text=True))
        self.c.post(f"/api/visit/{vid}/checkin")  # 報到後就是一般的在廠工單
        self.assertEqual(self.c.get(f"/api/visit/{vid}/state").get_json()["stage"], 0)
        self.assertEqual(self.c.get(f"/sa/visit/{vid}").status_code, 200)

    def test_other_center_full_slot_and_pricing(self):
        import booking

        slot = self.best_slot("C04", oid="O02")
        r = self.c.post("/api/owners/O02/book", json={"center_id": "C04", "slot": slot})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        vid = r.get_json()["vid"]
        self.assertFalse(any(a["id"] == vid for a in self.c.get("/api/board").get_json()["appointments"]))  # 別間廠的預約
        self.assertEqual(self.c.get(f"/c/{vid}").status_code, 200)
        # 預約後再加選：用汐止的費率與當時的離峰條件
        self.c.post(f"/api/visit/{vid}/optional", json={"code": "wheel_alignment", "on": True})
        v = self.app.store.snapshot()["visits"][vid]
        item = next(i for i in v["items"] if i["code"] == "wheel_alignment")
        expect = booking.quote(self.app.kb, ["wheel_alignment"], booking.CENTERS["C04"], v["booking"]["offpeak"])["total"]
        self.assertEqual(item["price"], expect)
        statuses = []
        for _ in range(12):  # 同一個時段一直訂到額滿
            statuses.append(self.c.post("/api/owners/O02/book", json={"center_id": "C04", "slot": slot}).status_code)
            if statuses[-1] == 409:
                break
        self.assertEqual(statuses[-1], 409)
        self.assertEqual(self.c.post("/api/owners/O02/book", json={"center_id": "C99", "slot": slot}).status_code, 400)
        self.assertEqual(self.c.post("/api/owners/O02/book", json={"center_id": "C04", "slot": "2020-01-01T09:00"}).status_code, 400)


class BookingLikeLineTest(unittest.TestCase):
    """和官方 LINE 預約流程對齊：表單欄位、上次回廠據點、更改／取消預約、加入行事曆。"""

    def setUp(self):
        self.app, self.c, self.tmp = make_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def slots(self, cid, oid="O01"):
        html_rows = self.c.get(f"/api/owners/{oid}/compare").get_json()["rows"]
        return next(r for r in html_rows if r["center_id"] == cid)["best"]["iso"]

    def book(self, **body):
        body.setdefault("center_id", "C01")
        body.setdefault("slot", self.slots(body["center_id"]))
        r = self.c.post("/api/owners/O01/book", json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def test_form_fields_and_last_center(self):
        d = self.c.get("/api/owners/O01/compare").get_json()
        self.assertIn("上次回廠", next(r for r in d["rows"] if r["center_id"] == "C01")["tags"])
        self.assertIn("上次回廠：內湖服務廠", self.c.get("/o/O01").get_data(as_text=True))
        res = self.book(remind="sms", contact="pm", advisor="陳專員", wait_mode="leave", note="x" * 100)
        v = self.app.store.snapshot()["visits"][res["vid"]]
        self.assertEqual((v["booking"]["remind"], v["booking"]["contact"], v["advisor"], v["advisor_requested"]),
                         ("sms", "pm", "陳專員", True))
        self.assertEqual(len(v["booking"]["note"]), 60)  # 和官方表單一樣限 60 字
        res2 = self.book(advisor="不存在的專員", remind="fax")  # 不合法的值改用預設
        v2 = self.app.store.snapshot()["visits"][res2["vid"]]
        self.assertEqual((v2["advisor"], v2["advisor_requested"], v2["booking"]["remind"]), ("李專員", False, "line"))
        html = self.c.get(f"/c/{res['vid']}").get_data(as_text=True)
        for text in ("預約保養成功通知", "加入行事曆", "取消預約", "更改預約", "簡訊", "您指定"):
            self.assertIn(text, html)

    def test_calendar_file(self):
        vid = self.book()["vid"]
        r = self.c.get(f"/c/{vid}/calendar.ics")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/calendar", r.headers["Content-Type"])
        body = r.get_data(as_text=True)
        for text in ("BEGIN:VEVENT", "DTSTART;TZID=Asia/Taipei:", "TRIGGER:-PT30M", "內湖服務廠"):
            self.assertIn(text, body)

    def test_change_booking_keeps_number(self):
        vid = self.book(advisor="黃專員")["vid"]
        old = self.app.store.snapshot()["visits"][vid]
        new_slot = self.slots("C04")
        self.assertEqual(self.c.get(f"/o/O01/centers?change={vid}").status_code, 200)
        self.assertIn("正在更改預約", self.c.get(f"/o/O01/confirm?center=C04&slot={new_slot}&change={vid}").get_data(as_text=True))
        r = self.c.post("/api/owners/O01/book", json={"center_id": "C04", "slot": new_slot, "change": vid})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual((r.get_json()["vid"], r.get_json()["changed"]), (vid, True))
        st = self.app.store.snapshot()
        v = st["visits"][vid]
        self.assertEqual((v["center_id"], v["booking"]["slot"]), ("C04", new_slot))
        self.assertTrue(v["booking"]["changed_at"])
        self.assertEqual(v["booking"]["created_at"], old["booking"]["created_at"])
        self.assertEqual(st["owners"]["O01"]["bookings"].count(vid), 1)
        self.assertFalse(any(a["id"] == vid for a in self.c.get("/api/board").get_json()["appointments"]))  # 改到別間廠
        self.assertIn("預約保養更改通知", self.c.get(f"/c/{vid}?changed=1").get_data(as_text=True))
        # 已報到的預約不能再線上更改
        self.c.post("/api/owners/O01/book", json={"center_id": "C01", "slot": self.slots("C01"), "change": vid})
        vid2 = self.book()["vid"]
        self.c.post(f"/api/visit/{vid2}/checkin")
        r = self.c.post("/api/owners/O01/book", json={"center_id": "C01", "slot": self.slots("C01"), "change": vid2})
        self.assertEqual(r.status_code, 409)

    def test_cancel_booking(self):
        vid = self.book()["vid"]
        r = self.c.post(f"/api/visit/{vid}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/o/O01?cancelled=", r.get_json()["href"])
        st = self.app.store.snapshot()
        self.assertNotIn(vid, st["visits"])
        self.assertNotIn(vid, st["owners"]["O01"]["bookings"])
        self.assertIn("已取消預約", self.c.get(r.get_json()["href"]).get_data(as_text=True))
        self.assertEqual(self.c.post(f"/api/visit/{vid}/cancel").status_code, 404)
        self.assertEqual(self.c.post("/api/visit/V001/cancel").status_code, 409)  # 不是線上預約／已在廠


class NeedTriageTest(unittest.TestCase):
    """預約前的需求判斷：該去社區型保修站還是服務廠（安全優先），以及據點能處理的範圍。"""

    def setUp(self):
        self.app, self.c, self.tmp = make_client()
        self.owners = self.app.store.snapshot()["owners"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rules(self):
        import booking

        o1, o2 = self.owners["O01"], self.owners["O02"]  # 油電車、汽油車
        r = booking.triage(o1, "maintenance", "")
        self.assertEqual((r["need"], r["station_ok"]), ("maintenance", True))
        r = booking.triage(o1, "maintenance", "上週收到召回通知")  # 描述比選項嚴重 → 改用服務廠
        self.assertEqual((r["need"], r["station_ok"]), ("warranty", False))
        self.assertTrue(any("召回" in x for x in r["reasons"]))
        self.assertEqual(booking.triage(o1, "noise", "")["need"], "warning")  # 油電車的異音 → 有設備的服務廠
        self.assertEqual(booking.triage(o2, "noise", "")["need"], "noise")
        r = booking.triage(o2, None, "踩煞車變軟，引擎室還在冒煙")
        self.assertEqual(r["urgency"], "stop")
        self.assertIn("安全的地方停車", r["tips"][0])
        self.assertEqual(booking.triage(o2, None, "前保桿擦撞凹陷")["need"], "body")
        r = self.c.post("/api/owners/O01/triage", json={"choice": "tire_battery"}).get_json()
        self.assertEqual((r["need"], r["station_ok"]), ("tire_battery", True))

    def test_locations_by_need(self):
        import booking

        rows = lambda q: self.c.get("/api/owners/" + q).get_json()  # noqa: E731
        d = rows("O01/compare")
        kinds = {r["kind"] for r in d["rows"]}
        self.assertEqual(kinds, {"plant", "station"})  # 定期保養：保修站和服務廠都列
        d = rows("O02/compare?need=warranty")
        self.assertTrue(d["rows"] and all(r["kind"] == "plant" for r in d["rows"]))
        self.assertTrue(any(e["center_id"].startswith("S") and "不處理" in e["reason"] for e in d["excluded"]))
        d = rows("O02/compare?need=body")
        self.assertFalse({"C02", "C05"} & {r["center_id"] for r in d["rows"]})  # 沒有鈑噴設備的服務廠
        d = rows("O03/compare")  # 電動車：只列有高壓作業資格的據點
        self.assertTrue(all(r["kind"] == "plant" for r in d["rows"]))
        self.assertTrue(any("電動車" in e["reason"] for e in d["excluded"]))
        import datetime as dt
        monday = dt.date(2026, 10, 5)
        times = [s["hm"] for s in booking.day_slots(booking.CENTERS["S01"], monday, dt.datetime(2026, 10, 4, 12, 0), {}, 90, True,
                                                         self.app.eta_model)]
        self.assertIn("19:00", times)  # 有些保修站平日晚上也能保養

    def test_slots_finish_before_closing(self):
        import datetime as dt
        import booking
        import eta

        model = getattr(self.app, "eta_model", None) or eta.EtaModel()
        now = dt.datetime(2026, 10, 4, 12, 0)
        for c in booking.CENTERS.values():
            for i in range(1, 8):
                d = now.date() + dt.timedelta(days=i)
                for s in booking.day_slots(c, d, now, {}, 90, True, model):
                    if s["status"] in ("free", "normal", "busy"):
                        self.assertLessEqual(s["done_hm"], booking.close_time(c, d), (c["id"], s["iso"]))
        monday = dt.date(2026, 10, 5)
        long_job = booking.day_slots(booking.CENTERS["S01"], monday, now, {}, 90, True, model)
        self.assertEqual(next(s for s in long_job if s["hm"] == "20:00")["status"], "late")  # 2 小時的保養，晚上 8 點來不及
        short_job = booking.day_slots(booking.CENTERS["S01"], monday, now, {}, 15, False, model)
        self.assertIn(next(s for s in short_job if s["hm"] == "19:30")["status"], ("free", "normal", "busy"))  # 換電瓶這類短項目可以

    def test_booking_by_need(self):
        slot = next(r for r in self.c.get("/api/owners/O02/compare?need=warranty").get_json()["rows"] if r["center_id"] == "C04")["best"]["iso"]
        r = self.c.post("/api/owners/O02/book", json={"center_id": "C04", "slot": slot, "need": "warranty"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        v = self.app.store.snapshot()["visits"][r.get_json()["vid"]]
        self.assertEqual((v["service_type"], [i["code"] for i in v["items"]], v["booking"]["need"]),
                         ("保固／召回檢查", ["warranty_check"], "warranty"))
        self.assertIn("need=warranty", self.c.get("/c/" + v["id"]).get_data(as_text=True))  # 更改預約時不會變回定期保養
        st_slot = next(r for r in self.c.get("/api/owners/O02/compare").get_json()["rows"] if r["center_id"] == "S03")["best"]["iso"]
        bad = self.c.post("/api/owners/O02/book", json={"center_id": "S03", "slot": st_slot, "need": "warranty"})
        self.assertEqual(bad.status_code, 400)  # 保修站不處理保固召回
        html = self.c.get("/o/O02/need?n=warranty").get_data(as_text=True)
        self.assertIn("請到原廠服務廠", html)
        self.assertEqual(self.c.get("/o/O01/quote?need=warranty").status_code, 302)  # 報價頁只給定期保養
        self.assertIn("社區型保修站", self.c.get("/o/O01/centers").get_data(as_text=True))
        hub = self.c.get("/").get_data(as_text=True)
        self.assertIn('href="/o/O02/need"', hub)  # 系統首頁找得到需求判斷
        self.assertIn('href="/o/O03/need"', hub)  # 每位示範車主都有入口
        self.assertIn('href="/o/O02/need"', self.c.get("/sa").get_data(as_text=True))  # 左側選單也有

    def test_ai_triage_is_safety_first(self):
        eng = self.app.engine
        eng.mode = "gemini"
        o1 = self.owners["O01"]
        import booking

        eng._gemini = lambda system, prompt, temperature, images=None: json.dumps({"need": "warranty", "reason": "疑似召回範圍"}, ensure_ascii=False)
        r = eng.triage(o1, "車子最近怪怪的", booking.triage(o1, "maintenance", "車子最近怪怪的"))
        self.assertEqual((r["need"], r["source"]), ("warranty", "gemini"))  # AI 判得更需要完整設備 → 採用
        eng._gemini = lambda system, prompt, temperature, images=None: json.dumps({"need": "maintenance", "reason": "一般保養"}, ensure_ascii=False)
        r = eng.triage(o1, "收到召回通知", booking.triage(o1, None, "收到召回通知"))
        self.assertEqual(r["need"], "warranty")  # AI 判得比較輕 → 維持較安全的判斷


class OfficialLocationsTest(unittest.TestCase):
    """據點改用 TOYOTA 官網資料（測試用內建快照，不連網）：營業時間、國定假日、附近據點、預約接回本廠、官網連不上時的備援。"""

    def setUp(self):
        self.app, self.c, self.tmp = make_client(locations="snapshot")

    def tearDown(self):
        import booking

        shutil.rmtree(self.tmp, ignore_errors=True)
        booking.install(None)  # 換回示意據點，不影響其他測試

    def test_snapshot_installed(self):
        import booking

        self.assertTrue(booking.SOURCE["official"])
        self.assertGreater(len(booking.CENTERS), 200)
        home = booking.CENTERS[booking.HOME_CENTER]
        self.assertEqual((booking.HOME_CENTER, home["name"], home["kind"], home["dealer"]), ("TB08", "內湖服務廠", "plant", "北都汽車"))
        self.assertIn("body", home["caps"])  # 官網標示有鈑噴
        self.assertEqual(booking.get_center("C01")["id"], "TB08")  # 舊的示意代碼也對得到
        self.assertNotIn("body", booking.CENTERS["TB4D"]["caps"])  # 保修站不做鈑噴
        self.assertTrue(all(c["id"].startswith("T") for c in booking.CENTERS.values()))  # 官網的 C01 是新竹，不能和示意代碼撞名

    def test_hours_and_holidays(self):
        import booking
        import locations as L

        b08, b4d, a46 = (booking.CENTERS[k]["official"] for k in ("TB08", "TB4D", "TA46"))
        at = lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M")  # noqa: E731
        self.assertTrue(L.status(b08, at("2026-10-11 10:00"))["open"])  # 內湖服務廠：週日有開
        self.assertEqual(L.status(b4d, at("2026-10-11 10:00"))["state"], "closed")  # 東湖保修站：週日公休
        self.assertEqual(L.status(b4d, at("2026-10-09 10:00"))["state"], "closed")  # 國慶日補假
        self.assertIn("國慶日", L.status(b4d, at("2026-10-09 10:00"))["label"])
        self.assertTrue(L.status(b08, at("2026-10-10 10:00"))["open"])  # 國定假日有開
        self.assertEqual(L.status(b4d, at("2026-10-12 07:50"))["detail"], "今天 08:30 開始營業")
        self.assertFalse(L.status(b08, at("2026-10-12 17:45"))["open"])
        self.assertIn("18:30", L.slot_times(a46, dt.date(2026, 10, 12)))  # 長安保修站平日晚上到 19:00
        self.assertNotIn("18:30", L.slot_times(b4d, dt.date(2026, 10, 12)))
        self.assertNotIn("12:00", L.slot_times(b4d, dt.date(2026, 10, 12)))  # 午休不排
        self.assertEqual(booking.slot_times(booking.CENTERS["TB4D"], dt.date(2026, 10, 10)), [])
        self.assertFalse(booking.is_offpeak(booking.CENTERS["TB08"], dt.date(2026, 10, 9), "14:00"))  # 假日沒有離峰優惠

    def test_compare_lists_nearby_official_locations(self):
        import booking

        d = self.c.get("/api/owners/O01/compare").get_json()
        ids = {r["center_id"] for r in d["rows"]} | {e["center_id"] for e in d["excluded"]}
        self.assertIn("TB08", ids)
        self.assertLessEqual(len(ids), booking.NEARBY_MAX + 1)  # 只比車主附近的據點
        self.assertTrue({r["kind"] for r in d["rows"]} <= {"plant", "station"})
        d = self.c.get("/api/owners/O02/compare?need=body").get_json()
        self.assertTrue(d["rows"] and all(booking.CENTERS[r["center_id"]]["official"]["body"] for r in d["rows"]))
        d = self.c.get("/api/owners/O03/compare").get_json()  # 電動車只列服務廠（示意）
        self.assertTrue(d["rows"] and all(r["kind"] == "plant" for r in d["rows"]))
        html = self.c.get("/o/O01/centers").get_data(as_text=True)
        for text in ("北都汽車", "TOYOTA 官網", "open-tag"):
            self.assertIn(text, html)
        html = self.c.get("/o/O01/centers/TB08").get_data(as_text=True)
        for text in ("內湖路一段", "營業時間與服務項目", "國定假日"):
            self.assertIn(text, html)
        self.assertEqual(self.c.get("/o/O01/centers/C01").status_code, 200)  # 舊連結還能用

    def test_booking_at_home_plant_reaches_erp(self):
        row = next(r for r in self.c.get("/api/owners/O01/compare").get_json()["rows"] if r["center_id"] == "TB08")
        r = self.c.post("/api/owners/O01/book", json={"center_id": "TB08", "slot": row["best"]["iso"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        vid = r.get_json()["vid"]
        self.assertTrue(any(a["id"] == vid for a in self.c.get("/api/board").get_json()["appointments"]))  # 本廠的預約接到今日工單
        v = self.app.store.snapshot()["visits"][vid]
        self.assertEqual(v["booking"]["center_name"], "北都汽車 內湖服務廠")
        html = self.c.get(f"/c/{vid}").get_data(as_text=True)
        self.assertIn("內湖路一段", html)
        self.assertIn("不會傳送到據點", html)
        other = next(r for r in self.c.get("/api/owners/O01/compare").get_json()["rows"] if r["center_id"] != "TB08")
        r = self.c.post("/api/owners/O01/book", json={"center_id": other["center_id"], "slot": other["best"]["iso"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(any(a["id"] == r.get_json()["vid"] for a in self.c.get("/api/board").get_json()["appointments"]))

    def test_locations_page_and_api(self):
        html = self.c.get("/locations").get_data(as_text=True)
        for text in ("內湖服務廠", "東湖保修站", "TOYOTA 官網"):
            self.assertIn(text, html)
        d = self.c.get("/api/locations?near=O01").get_json()
        self.assertTrue(d["rows"] and all("status" in r and "distance_km" in r for r in d["rows"]))
        self.assertTrue(d["source"]["official"])
        self.assertIn('href="/locations"', self.c.get("/sa").get_data(as_text=True))  # 左側選單有入口

    def test_provider_fallback_and_live_update(self):
        import locations as L

        snap = L.Provider(mode="snapshot")
        main = [{"TYPE": "2", "KEY": "Z01", "TITLE": "測試服務廠", "DEALER": "測試", "CITY": "台北市", "AREA": "內湖區",
                 "ADDR": "台北市內湖區測試路1號", "TEL": "02-0000000", "LAT": "25.08", "LNG": "121.58", "TIME": "08:30~17:30",
                 "AED": "1", "BPISFLAG": "Y", "NTISFLAG": "N",
                 "DATEINFO": {"ODTIME": "08:30~17:30", "SDISFLAG": "N", "SDTIME": "~", "HDISFLAG": "N", "HDTIME": "~",
                              "Wflags": ["Y"] * 6}},
                {"TYPE": "1", "KEY": "Z02", "TITLE": "測試營業所", "LAT": "25", "LNG": "121"}]  # 營業所（賣車）不列
        locs = L.normalize(main, [], "2026-10-04")
        self.assertEqual(list(locs), ["Z01"])
        self.assertEqual((locs["Z01"]["kind"], locs["Z01"]["body"], locs["Z01"]["sunday"]), ("plant", True, None))

        def boom():
            raise OSError("network down")

        p = L.Provider(mode="auto", snapshot_path=L.SNAPSHOT_PATH, cache_path=os.path.join(self.tmp, "c.json"), fetch=boom)
        p.refresh(wait=True)
        self.assertTrue(p.official)  # 官網連不上：繼續用內建快照
        self.assertEqual(p.origin, "snapshot")
        self.assertIn("network down", p.info()["error"])
        many = [dict(main[0], KEY=f"Z{i:02d}") for i in range(30)]
        got = []
        p2 = L.Provider(mode="auto", cache_path=os.path.join(self.tmp, "c2.json"), fetch=lambda: (many, []),
                        on_update=lambda pp: got.append(pp))
        p2.refresh(wait=True)
        self.assertEqual((p2.origin, len(p2.data["locations"]), len(got)), ("live", 30, 1))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "c2.json")))  # 存一份，下次啟動先用
        self.assertEqual(snap.info()["origin"], "snapshot")


class OfficialPackagesTest(unittest.TestCase):
    """TOYOTA 官方定保套餐（T30／T50／T60）：依里程選套餐、詳細檢查項目、報價頁與價目表顯示、官網連不上時用快照。"""

    def setUp(self):
        self.app, self.c, self.tmp = make_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rule_and_snapshot(self):
        import packages as P

        self.assertEqual([P.package_code(k * 10000) for k in (1, 2, 3, 4, 5, 6, 8, 10, 12)],
                         ["T30", "T50", "T30", "T60", "T30", "T50", "T60", "T50", "T60"])
        pk = P.Packages(mode="snapshot")
        hy = pk.for_vehicle(40000, "hybrid")
        self.assertEqual(hy["code"], "T60")
        self.assertIn("機油濾清器", hy["basic"])
        self.assertIn("空氣濾芯", hy["periodic"])
        names = [g["name"] for g in hy["groups"]]
        self.assertIn("輪胎", names)
        self.assertGreater(hy["count"], 50)
        self.assertTrue(any("HV" in i for g in hy["groups"] for i in g["items"]))  # 油電車版本有 HV 系統檢查
        ev = pk.for_vehicle(20000, "ev")
        self.assertEqual((ev["code"], ev["basic"]), ("T50", ""))  # 電動車沒有機油
        self.assertIn("動力室", [g["name"] for g in ev["groups"]])

    def test_parser_on_page_structure(self):
        import packages as P

        html = """<div class="spec-item"><h3 class="spec-title price">T30<span class="name">定保套餐</span></h3>
          <main class="spec-body"><div><h6>使用時機</h6><p>車齡：每半年實施<br>或<br>里程數：1萬公里</p></div>
          <div><h6>基本更換項目</h6><p>機油、機油濾清器</p></div>
          <a data-src="#packageDetails30">詳細檢查項目</a></main></div>
          <div id="packageDetails30"><h6>燈光</h6><ul><li><span>頭燈作動檢查</span></li><li>煞車燈作動檢查</li></ul>
          <h6>*該項目費用為獨立收費</h6></div>"""
        got = P.parse_page(html)["T30"]
        self.assertEqual((got["basic"], got["count"], got["note"]), ("機油、機油濾清器", 2, "*該項目費用為獨立收費"))
        self.assertEqual(got["groups"][0]["items"], ["頭燈作動檢查", "煞車燈作動檢查"])
        self.assertIn("\n或\n", got["when"])

    def test_pages_show_official_package(self):
        html = self.c.get("/o/O01/quote").get_data(as_text=True)  # 王小姐 4 萬公里油電車 → T60
        for text in ("T60 定保套餐", "詳細檢查項目", "愛車養護與保證", "放油塞墊片"):
            self.assertIn(text, html)
        self.assertIn("T50 定保套餐", self.c.get("/o/O02/quote").get_data(as_text=True))  # 6 萬公里 → T50
        self.assertNotIn("定保套餐", self.c.get("/o/O01/need?n=warranty").get_data(as_text=True))
        prices = self.c.get("/prices").get_data(as_text=True)
        self.assertIn('class="pkg-tag">T60', prices)
        self.assertIn("定保套餐", self.c.get("/c/V001").get_data(as_text=True))  # 到廠後的車主頁也看得到

    def test_fallback_when_site_unreachable(self):
        import packages as P

        def boom():
            raise OSError("offline")

        pk = P.Packages(mode="auto", cache_path=os.path.join(self.tmp, "p.json"), fetch=boom)
        pk.refresh(wait=True)
        self.assertEqual(pk.origin, "snapshot")
        self.assertIn("offline", pk.error)
        self.assertEqual(pk.for_vehicle(10000, "gas")["code"], "T30")


if __name__ == "__main__":
    unittest.main()
