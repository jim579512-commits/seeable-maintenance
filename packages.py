"""TOYOTA 官方定期保養套餐（T30／T50／T60）：使用時機、基本更換項目、週期性零件、詳細檢查項目。

資料來源：TOYOTA 官網「愛車養護與保證」
- 汽油／柴油車 https://www.toyota.com.tw/owner_maintenance.aspx
- HYBRID       https://www.toyota.com.tw/owner_maintenance.aspx?ishybrid=Y
- 純電車       https://www.toyota.com.tw/electric_guarantee.aspx
啟動時讀內建快照（toyota_packages.json），再在背景向官網更新（每天一次）；連不上時繼續用快照。
官網沒有公開套餐價格（請洽服務廠），網站上的金額仍是示意。

更新內建快照：py -3 packages.py --update-snapshot
"""
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import urllib.request
from html.parser import HTMLParser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.path.join(BASE_DIR, "toyota_packages.json")
PAGES = {
    "gas": "https://www.toyota.com.tw/owner_maintenance.aspx",
    "hybrid": "https://www.toyota.com.tw/owner_maintenance.aspx?ishybrid=Y",
    "ev": "https://www.toyota.com.tw/electric_guarantee.aspx",
}
USER_AGENT = "Mozilla/5.0 (concept prototype for 2026 Hotai AI Hackathon; refresh daily)"
REFRESH_SECONDS = 24 * 3600
RETRY_SECONDS = 60 * 60
CODES = ("T30", "T50", "T60")
RULES = {"T30": "1、3、5 萬公里等單數萬公里（或每半年）", "T50": "2 萬公里起每 4 萬公里（或每單數年）",
         "T60": "4 萬公里起每 4 萬公里（或每雙數年）"}
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "area", "base", "col", "embed", "param", "track", "wbr"}


# ---------------- 簡單的 HTML 解析（標準函式庫，不用另外安裝套件） ----------------
class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.children, self.parent = tag, dict(attrs), [], parent

    def classes(self):
        return (self.attrs.get("class") or "").split()

    def walk(self):
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.walk()

    def text(self):
        out = []
        for c in self.children:
            if isinstance(c, str):
                out.append(c)
            elif c.tag == "br":
                out.append("\n")
            elif c.tag not in ("script", "style"):
                out.append(c.text())
        return "".join(out)

    def next_element(self):
        sib = self.parent.children if self.parent else []
        i = sib.index(self)
        return next((c for c in sib[i + 1:] if isinstance(c, Node)), None)


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", [], None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_html(html):
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root


def _clean(s):
    lines = [re.sub(r"\s+", " ", x).strip() for x in (s or "").split("\n")]
    return "\n".join(x for x in lines if x)


def _one_line(s):
    return re.sub(r"\s*\n\s*", " ", _clean(s)).strip()


def parse_page(html):
    """一頁 → {'T30': {...}, 'T50': {...}, 'T60': {...}}。"""
    root = parse_html(html)
    details = {}
    for d in root.walk():
        did = d.attrs.get("id") or ""
        if not did.startswith("packageDetails"):
            continue
        groups, note = [], ""
        for h in (n for n in d.walk() if n.tag == "h6"):
            name = _one_line(h.text())
            ul = h.next_element()
            items = [_one_line(li.text()) for li in ul.walk() if li.tag == "li"] if ul is not None and ul.tag == "ul" else []
            if items:
                groups.append({"name": name, "items": [x for x in items if x]})
            elif name.startswith("*") or name.startswith("＊"):
                note = name
        details[did] = {"groups": groups, "note": note}
    out = {}
    for it in (n for n in root.walk() if "spec-item" in n.classes()):
        title_n = next((n for n in it.walk() if "spec-title" in n.classes()), None)
        m = re.match(r"(T\d0)", _one_line(title_n.text()) if title_n else "")
        if not m or m.group(1) not in CODES:
            continue
        secs = {}
        for h in (n for n in it.walk() if n.tag == "h6"):
            box = h.parent
            parts = [n for n in box.walk() if n.tag in ("p", "li")]
            secs[_one_line(h.text())] = _clean("\n".join(p.text() for p in parts))
        link = next((n for n in it.walk() if (n.attrs.get("data-src") or "").startswith("#packageDetails")), None)
        det = details.get(link.attrs["data-src"][1:], {}) if link is not None else {}
        groups = det.get("groups", [])
        out[m.group(1)] = {
            "code": m.group(1), "when": secs.get("使用時機", ""), "basic": _one_line(secs.get("基本更換項目", "")),
            "periodic": _one_line(secs.get("週期性零件更換項目", "")), "checks": _one_line(secs.get("檢查項目", "")),
            "groups": groups, "count": sum(len(g["items"]) for g in groups), "note": det.get("note", ""),
        }
    return out


def fetch_official(timeout=15):
    data = {}
    for pt, url in PAGES.items():
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", errors="replace")
        pk = parse_page(html)
        if set(pk) != set(CODES) or not all(p["groups"] for p in pk.values()):
            raise ValueError(f"{url} 的保養套餐格式和預期不同")
        data[pt] = pk
    return data


# ---------------- 依里程選套餐 ----------------
def package_code(bucket_km):
    """官網規則：1、3、5 萬等單數萬公里＝T30；2 萬起每 4 萬＝T50；4 萬起每 4 萬＝T60（SIENNA、SUPRA 週期不同）。"""
    n = int(bucket_km) // 10000
    if n <= 0 or n % 2 == 1:
        return "T30"
    return "T60" if n % 4 == 0 else "T50"


def pt_key(powertrain):
    return {"ev": "ev", "hybrid": "hybrid", "phev": "hybrid"}.get(powertrain, "gas")


class Packages:
    def __init__(self, mode="auto", snapshot_path=SNAPSHOT_PATH, cache_path=None, fetch=None):
        self.mode = mode
        self.snapshot_path, self.cache_path = snapshot_path, cache_path
        self.fetch = fetch or fetch_official
        self.data = None
        self.origin = ""
        self.error = ""
        self.last_try = 0.0
        self.fetching = False
        self.lock = threading.Lock()
        best = None
        for path, origin in ((cache_path, "cache"), (snapshot_path, "snapshot")):
            d = self._read(path) if path else None
            if d and (best is None or d.get("fetched_at", "") > best[0].get("fetched_at", "")):
                best = (d, origin)
        if best:
            self.data, self.origin = best

    @staticmethod
    def _read(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d.get("packages"), dict) and d["packages"] else None
        except (OSError, ValueError, AttributeError):
            return None

    def fetched_text(self):
        try:
            return dt.datetime.fromisoformat(self.data["fetched_at"]).strftime("%Y/%m/%d")
        except (TypeError, KeyError, ValueError):
            return ""

    def for_vehicle(self, bucket_km, powertrain):
        """這台車這次里程適用的官方套餐；沒有資料時回傳 None。"""
        if not self.data:
            return None
        pt = pt_key(powertrain)
        code = package_code(bucket_km)
        p = (self.data["packages"].get(pt) or {}).get(code)
        if not p:
            return None
        when = re.sub(r"\s*\n\s*或\s*\n\s*", "；或", p.get("when", "")).replace("\n", " ")
        return dict(p, pt=pt, rule=RULES[code], url=PAGES[pt], fetched=self.fetched_text(), when_text=when,
                    origin={"live": "即時", "cache": "上次抓到的資料", "snapshot": "內建快照"}.get(self.origin, ""))

    def refresh(self, wait=False):
        if self.mode != "auto":
            return False
        with self.lock:
            if self.fetching:
                return False
            self.fetching = True
            self.last_try = time.time()

        def run():
            try:
                pk = self.fetch()
                data = {"source": "TOYOTA 官網「愛車養護與保證」", "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                        "urls": PAGES, "packages": pk}
                self.data, self.origin, self.error = data, "live", ""
                if self.cache_path:
                    try:
                        tmp = self.cache_path + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False)
                        os.replace(tmp, self.cache_path)
                    except OSError:
                        pass
            except Exception as e:  # noqa: BLE001 — 官網連不上時繼續用快照
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
        try:
            age = time.time() - dt.datetime.fromisoformat(self.data["fetched_at"]).timestamp()
        except (TypeError, KeyError, ValueError):
            age = REFRESH_SECONDS + 1
        if self.origin != "live" or age > REFRESH_SECONDS:
            self.refresh()


def update_snapshot(path=SNAPSHOT_PATH):
    pk = fetch_official()
    data = {"source": "TOYOTA 官網「愛車養護與保證」", "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "urls": PAGES, "packages": pk}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return data


if __name__ == "__main__":
    if "--update-snapshot" in sys.argv:
        d = update_snapshot()
        for pt, pk in d["packages"].items():
            print(pt, {k: v["count"] for k, v in pk.items()})
        print(f"已更新 {SNAPSHOT_PATH}")
    else:
        print(__doc__)
