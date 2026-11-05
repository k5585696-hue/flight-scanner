"""機票雷達：查 Google Flights → 存價格紀錄 → 比平常便宜就寄信 → 產生網頁。

本機試跑：python scanner.py
GitHub Actions 會每小時自動執行一次（見 .github/workflows/scan.yml）。
航線設定：網頁上設定的存在 routes.json；沒有 routes.json 時改讀 config.toml 的 [[routes]]。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import smtplib
import statistics
import sys
import time
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, fetch_flights_html
from selectolax.lexbor import LexborHTMLParser

ROOT = Path(__file__).resolve().parent
HISTORY_DIR = ROOT / "data" / "history"
STATE_FILE = ROOT / "data" / "state.json"
SITE_TEMPLATE = ROOT / "site" / "index.html"
SITE_OUT = ROOT / "_site"
OUT_DIR = ROOT / "out"

TW = timezone(timedelta(hours=8))  # 台灣沒有日光節約時間
HISTORY_FIELDS = ["time", "route", "depart", "return", "price", "airline", "stops"]
KEEP_DAYS = 90                      # 網頁走勢圖最多顯示幾天
FAIL_ALERT_AFTER = timedelta(hours=6)
DEFAULT_ALERT = {"drop_percent": 15, "usual_days": 14, "min_samples": 24, "realert_percent": 3}
WEEKDAYS = "一二三四五六日"

# 廉航托運行李費估計（每件、單程、新台幣，約 20 公斤、訂票時一起買）。
# Google Flights 沒有亞洲廉航的行李費資料，只能用估計值；可以在 config.toml 的 [bag_fees] 改成官網實際價格。
# 不在這個表裡的航空公司視為票價已含托運行李。
DEFAULT_BAG_FEES = {
    "IT": 900,   # 台灣虎航
    "TR": 1100,  # 酷航
    "MM": 800,   # 樂桃
    "GK": 900, "JQ": 900, "3K": 900,  # 捷星
    "IJ": 900,   # 春秋航空日本
    "ZG": 1200,  # ZIPAIR
    "UO": 800,   # 香港快運
    "9C": 800,   # 春秋航空
    "7C": 900, "TW": 900, "LJ": 900, "ZE": 900, "RS": 900, "BX": 900,  # 濟州、德威、真航空、易斯達、首爾、釜山
    "AK": 1000, "D7": 1000, "FD": 1000, "XJ": 1000, "Z2": 1000, "QZ": 1000,  # 亞洲航空系列
    "SL": 800,   # 泰國獅航
    "VJ": 800, "VZ": 800,  # 越捷
    "5J": 900, "DG": 900,  # 宿霧太平洋
}


# ---------- 設定 ----------

@dataclass
class Route:
    id: str
    name: str
    frm: str
    to: str
    trip: str
    depart: list[str]          # 原本的寫法（網頁編輯用）
    departs: list[date]
    stay_days: list[int]
    adults: int
    children: int
    infants: int               # 未滿 2 歲、坐大人腿上
    seat: str
    max_stops: int | None
    checked_bags: int          # 整團總共幾件托運行李
    depart_hours: tuple[int, int] | None
    alert_below: int
    paused: bool

    @property
    def key(self) -> str:
        """價格紀錄用的代號；會影響價格的條件都放進來，改條件就會重新累積。"""
        stops = "any" if self.max_stops is None else self.max_stops
        key = f"{self.frm}-{self.to}-{self.trip}-{self.seat}-{self.adults}p-{stops}stop"
        if self.children:
            key += f"-{self.children}c"
        if self.infants:
            key += f"-{self.infants}i"
        if self.checked_bags:
            key += f"-{self.checked_bags}bag"
        if self.depart_hours:
            key += "-h%d-%d" % self.depart_hours
        if self.adults + self.children + self.infants > 1:
            key += "-v2"  # 2026-11 起多人改用瀏覽器查價；之前的紀錄不完整，平常價重新累積
        return key

    def combos(self, today: date) -> list[tuple[date, date | None]]:
        out = []
        for d in self.departs:
            if d < today:
                continue
            if self.trip == "one-way":
                out.append((d, None))
            else:
                out.extend((d, d + timedelta(days=n)) for n in self.stay_days)
        return out

    def config(self) -> dict:
        """網頁表單用的設定，格式跟 routes.json 一樣。"""
        return {
            "id": self.id, "name": self.name, "from": self.frm, "to": self.to, "trip": self.trip,
            "depart": self.depart, "stay_days": self.stay_days,
            "adults": self.adults, "children": self.children, "infants": self.infants,
            "seat": self.seat, "max_stops": self.max_stops, "checked_bags": self.checked_bags,
            "depart_hours": list(self.depart_hours) if self.depart_hours else None,
            "alert_below": self.alert_below or None, "paused": self.paused,
        }


def combo_key(route: Route, dep: date, ret: date | None) -> str:
    return f"{route.key}|{dep.isoformat()}|{ret.isoformat() if ret else ''}"


def expand_dates(items: list[str]) -> list[date]:
    out: set[date] = set()
    for item in items:
        if ".." in item:
            a, b = (s.strip() for s in item.split("..", 1))
            d, end = date.fromisoformat(a), date.fromisoformat(b)
            while d <= end:
                out.add(d)
                d += timedelta(days=1)
        else:
            out.add(date.fromisoformat(item.strip()))
    return sorted(out)


def parse_route(r: dict, i: int) -> Route:
    name = r.get("name") or f"{r['from']} → {r['to']}"
    trip = r.get("trip", "round-trip")
    if trip not in ("round-trip", "one-way"):
        sys.exit(f"設定錯誤：「{name}」的 trip 只能是 round-trip 或 one-way")
    if trip == "round-trip" and not r.get("stay_days"):
        sys.exit(f"設定錯誤：「{name}」是來回票，要寫 stay_days（住幾晚）")
    adults, children, infants = int(r.get("adults", 1)), int(r.get("children", 0)), int(r.get("infants", 0))
    if adults < 1 or adults + children + infants > 9 or infants > adults:
        sys.exit(f"設定錯誤：「{name}」人數不對（至少 1 位大人、總共最多 9 人、嬰兒不能比大人多）")
    depart = [r["depart"]] if isinstance(r["depart"], str) else list(r["depart"])
    hours = r.get("depart_hours")
    return Route(
        id=str(r.get("id") or f"r{i + 1}"),
        name=name,
        frm=r["from"].strip().upper(),
        to=r["to"].strip().upper(),
        trip=trip,
        depart=depart,
        departs=expand_dates(depart),
        stay_days=[int(n) for n in r.get("stay_days") or []],
        adults=adults,
        children=children,
        infants=infants,
        seat=r.get("seat") or "economy",
        max_stops=r.get("max_stops"),
        checked_bags=int(r.get("checked_bags") or 0),
        depart_hours=(int(hours[0]), int(hours[1])) if hours else None,
        alert_below=int(r.get("alert_below") or 0),
        paused=bool(r.get("paused")),
    )


def load_config(path: Path) -> tuple[dict, list[Route], bool]:
    """回傳 (設定, 航線, 是否全部暫停)。"""
    # utf-8-sig：Windows 記事本存檔時可能在開頭加 BOM
    cfg = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    routes_file = path.parent / "routes.json"
    if routes_file.exists():
        doc = json.loads(routes_file.read_text(encoding="utf-8-sig"))
        raw, paused = doc.get("routes", []), bool(doc.get("paused"))
        print("航線設定來源：routes.json（網頁上設定的）")
    else:
        raw, paused = cfg.get("routes", []), False
    return cfg, [parse_route(r, i) for i, r in enumerate(raw)], paused


# ---------- 查價 ----------

def build_query(cfg: dict, route: Route, dep: date, ret: date | None):
    earliest, latest = route.depart_hours or (None, None)
    legs = [FlightQuery(date=dep.isoformat(), from_airport=route.frm, to_airport=route.to,
                        earliest_departure_hour=earliest, latest_departure_hour=latest)]
    if ret:
        legs.append(FlightQuery(date=ret.isoformat(), from_airport=route.to, to_airport=route.frm))
    return create_query(
        flights=legs,
        trip=route.trip,
        seat=route.seat,
        passengers=Passengers(adults=route.adults, children=route.children, infants_on_lap=route.infants),
        currency=cfg.get("currency", "TWD"),
        language=cfg.get("language", "zh-TW"),
        max_stops=route.max_stops,
    )


@dataclass
class Segment:
    frm: str
    to: str
    dep: tuple[int, int]        # (時, 分)
    arr: tuple[int, int]
    dep_date: tuple[int, int, int]
    arr_date: tuple[int, int, int]
    num: str                    # 航班號碼，例如 IT210
    code: str                   # 航空公司代碼，例如 IT


@dataclass
class Itinerary:
    price: int | None
    type: str                   # 航空公司代碼；不同航空轉機時是 "multi"
    airlines: list[str]
    segs: list[Segment]


def _hm(v) -> tuple[int, int]:
    """Google 省略 0：[8] = 08:00、[None, 31] = 00:31。"""
    padded = [*(v or []), None, None]
    return padded[0] or 0, padded[1] or 0


def parse_items(payload: list) -> list[Itinerary]:
    """Google 的航班資料 → 航班清單。2 = 最佳航班、3 = 其他航班
    （fast-flights 3.1 只讀 3，會漏掉常常最便宜的「最佳航班」，所以自己讀）。"""
    out = []
    for idx in (2, 3):
        block = payload[idx] if len(payload) > idx else None
        for k in (block[0] if isinstance(block, list) and block and block[0] else []):
            try:
                f = k[0]
                segs = [Segment(s[3], s[6], _hm(s[8]), _hm(s[10]), tuple(s[20]), tuple(s[21]),
                                f"{s[22][0]}{s[22][1]}", s[22][0]) for s in f[2]]
                out.append(Itinerary(k[1][0][1], f[0], f[1] or [], segs))
            except (IndexError, TypeError):
                continue  # 這一筆格式不一樣（例如沒有價格），跳過
    return out


def parse_html(html: str) -> list[Itinerary]:
    """直接下載的網頁：航班資料在 ds:1 這段 script 裡。"""
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise RuntimeError("Google 回傳的頁面沒有航班資料（可能被暫時擋住或網頁改版）")
    data = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise FlightsNotFound("no flights found")
    return parse_items(json.loads(data))


def parse_rpc(text: str) -> tuple[list[Itinerary], bool]:
    """瀏覽器裡 GetShoppingResults 的回應：一段一段串流送來，每段都是更完整的清單。
    回傳 (航班, 是否完整)；Google 搜尋完成後送的那幾段，最後多了 [31] = true。"""
    decoder, size_line = json.JSONDecoder(), re.compile(r"\s*\d+\s*\n")
    pos, payloads = (text.find("\n") + 1 if text.startswith(")]}'") else 0), []
    while m := size_line.match(text, pos):
        try:
            chunk, pos = decoder.raw_decode(text, m.end())
        except json.JSONDecodeError:
            break  # 最後一段還沒傳完
        for e in chunk:
            if not (isinstance(e, list) and len(e) > 2 and e[0] == "wrb.fr"):
                continue
            if isinstance(e[2], str):
                payloads.append(json.loads(e[2]))
            elif len(e) > 5 and e[5]:  # Google 回錯誤（例如查太快時的代碼 13），不是「沒有航班」
                raise RuntimeError(f"Google 回傳錯誤（代碼 {e[5][0]}）")
    complete = [p for p in payloads if len(p) > 31 and p[31] is True]
    if complete:
        return parse_items(complete[-1]), True
    for p in reversed(payloads):
        if items := parse_items(p):
            return items, False
    return [], False


# 在網頁裡攔 GetShoppingResults，邊收邊存到 window.__gsr（瀏覽器本身的回應暫存不可靠，串流也可能很久才結束）
GSR_HOOK = """
(() => {
  const list = window.__gsr = [];
  const f = window.fetch;
  window.fetch = async function (input) {
    const res = await f.apply(this, arguments);
    const u = typeof input === "string" ? input : (input && input.url) || "";
    if (String(u).includes("GetShoppingResults") && res.body) {
      const rec = { text: "", done: false };
      list.push(rec);
      const reader = res.clone().body.getReader(), dec = new TextDecoder();
      (async () => {
        try { for (;;) { const { value, done } = await reader.read(); if (done) break; rec.text += dec.decode(value, { stream: true }); } }
        catch (e) {}
        rec.done = true;
      })();
    }
    return res;
  };
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__gsr = String(u).includes("GetShoppingResults"); return open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    if (this.__gsr) {
      const xhr = this, rec = { text: "", done: false };
      list.push(rec);
      const grab = () => { try { rec.text = xhr.responseText || rec.text; } catch (e) {} };
      xhr.addEventListener("progress", grab);
      xhr.addEventListener("loadend", () => { grab(); rec.done = true; });
    }
    return send.apply(this, arguments);
  };
})();
"""


class Browser:
    """用無頭瀏覽器開 Google Flights。
    多人（大人＋小孩＋嬰兒）時，Google 直接回傳的網頁常常還沒有航班，要等網頁裡的 JavaScript
    再去要一次（GetShoppingResults）才拿得到；所以用真的瀏覽器開，讀那次回應。"""

    CHANNELS = ("chrome", "msedge", None)  # GitHub 主機有 Chrome、Windows 有 Edge；None = Playwright 自帶的 Chromium

    def __init__(self, language: str):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        channels = [os.environ["BROWSER_CHANNEL"]] if os.environ.get("BROWSER_CHANNEL") else self.CHANNELS
        err = None
        for ch in channels:
            try:
                self._browser = self._pw.chromium.launch(channel=ch, headless=True)
                break
            except Exception as e:
                err = e
        else:
            self._pw.stop()
            raise RuntimeError(f"找不到可以用的瀏覽器：{err}")
        # 不要用 page.route 擋圖片：攔截請求會讓 GetShoppingResults 讀不到
        self._page = self._browser.new_page(locale=language)
        self._page.add_init_script(GSR_HOOK)

    def fetch(self, url: str, timeout: float = 40) -> tuple[list[Itinerary], bool]:
        """等到 Google 說搜尋完成（或串流結束）。時間到就用目前收到最完整的一段。回傳 (航班, 是否完整)。"""
        self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
        start = changed = parsed_time = time.time()
        seen = parsed_at = -1
        flights, complete = [], False
        while time.time() - start < timeout:
            info = self._page.evaluate("window.__gsr.length ? [window.__gsr[0].done, window.__gsr[0].text.length] : null")
            if info:
                done, n = info
                if n != seen:
                    seen, changed = n, time.time()
                # 有新資料時，每 3 秒或停下來 1 秒就檢查一次完成了沒（每次都解析很耗時）
                if n != parsed_at and (done or time.time() - changed > 1 or time.time() - parsed_time > 3):
                    flights, complete = parse_rpc(self._page.evaluate("window.__gsr[0].text"))
                    parsed_at, parsed_time = n, time.time()
                    if complete:
                        return flights, True
                    if done:  # 串流結束了卻沒有「搜尋完成」：當作不完整
                        return flights, False
            self._page.wait_for_timeout(300)
        if seen <= 0:
            raise RuntimeError("等不到 Google 的查詢結果")
        if parsed_at != seen:
            flights, complete = parse_rpc(self._page.evaluate("window.__gsr[0].text"))
        return flights, complete

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()


class Fetcher:
    """查一筆：先用瀏覽器（結果完整）；瀏覽器開不起來或出錯，就改用直接下載網頁（舊方法，多人時可能不完整）。"""

    def __init__(self, language: str):
        self.language, self.browser, self.broken = language, None, False

    def _get_browser(self) -> Browser | None:
        if self.browser is None and not self.broken:
            try:
                self.browser = Browser(self.language)
            except Exception as e:
                self.broken = True
                print(f"⚠ 無法啟動瀏覽器，改用直接下載網頁（多人查詢的結果可能不完整）：{e}")
        return self.browser

    def fetch(self, query) -> list[Itinerary]:
        valid = lambda fl: [f for f in fl if f.price and f.price > 0]
        flights, complete, browser_used = [], False, False
        if browser := self._get_browser():
            browser_used = True
            try:
                flights, complete = browser.fetch(query.url())
            except Exception as e:
                # Google 有時會拒絕自動化的查詢（代碼 13）；不重試硬查，直接改用另一種方法
                print(f"    瀏覽器這次沒拿到結果（{type(e).__name__}: {str(e)[:60]}），改用直接下載")
        if complete and flights:
            return valid(flights)
        # 瀏覽器出錯、沒找到或沒等到搜尋完成：用直接下載的結果補上（兩邊都是同一個搜尋條件的真實航班）
        try:
            extra = self._static(query)
        except Exception:
            if flights or complete:
                return valid(flights)
            raise
        if browser_used and not (flights or extra or complete):
            # 分不出是真的沒航班、還是 Google 拒絕了這次查詢 → 當作查詢失敗，不要顯示「沒有航班」
            raise RuntimeError("Google 這次沒有回傳結果（可能查詢太頻繁），下次整點會再試")
        if flights and not complete:
            print(f"    （Google 沒在時間內搜尋完，用目前收到的 {len(flights)} 個航班＋直接下載的 {len(extra)} 個）")
        merged: dict[tuple, Itinerary] = {}
        for f in valid(flights) + valid(extra):
            key = tuple((s.num, s.dep_date, s.dep) for s in f.segs)
            if key not in merged or f.price < merged[key].price:
                merged[key] = f
        return list(merged.values())

    @staticmethod
    def _static(query) -> list[Itinerary]:
        for attempt in range(3):
            try:
                return parse_html(fetch_flights_html(query))
            except FlightsNotFound:
                return []
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
        return []

    def close(self) -> None:
        if self.browser:
            self.browser.close()


def summarize(f: Itinerary, route: Route, bag_fees: dict[str, int]) -> dict:
    first, last = f.segs[0], f.segs[-1]
    codes = {s.code for s in f.segs} or {f.type}
    lcc = sorted(c for c in codes if c in bag_fees)
    legs = 2 if route.trip == "round-trip" else 1
    bag_fee = sum(bag_fees[c] for c in lcc) * route.checked_bags * legs
    return {
        "price": f.price + bag_fee,   # 拿來比較的價格（含估計行李費）
        "fare": f.price,              # Google 上的票價
        "bag_fee": bag_fee,
        "lcc": bool(lcc),
        "airline": "、".join(f.airlines) or f.type,
        "nums": [s.num for s in f.segs],
        "stops": len(f.segs) - 1,
        "via": [s.to for s in f.segs[:-1]],
        "from": first.frm,
        "to": last.to,
        "dep": "%02d:%02d" % first.dep,
        "arr": "%02d:%02d" % last.arr,
        "arr_plus": (date(*last.arr_date) - date(*first.dep_date)).days,
    }


# ---------- 紀錄 ----------

def fmt_time(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


def parse_time(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def load_history(now: datetime) -> list[dict]:
    cutoff = now - timedelta(days=KEEP_DAYS)
    rows = []
    for path in sorted(HISTORY_DIR.glob("*.csv")):
        if path.stem < f"{cutoff:%Y-%m}":
            continue
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                row["t"] = parse_time(row["time"])
                row["price"] = int(row["price"])
                if row["t"] >= cutoff:
                    rows.append(row)
    return rows


def append_history(rows: list[dict], now: datetime) -> None:
    if not rows:
        return
    path = HISTORY_DIR / f"{now:%Y-%m}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, HISTORY_FIELDS, extrasaction="ignore")
        if is_new:
            w.writeheader()
        w.writerows(rows)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


# ---------- 判斷便宜 ----------

def money(p: int | None, cur: str) -> str:
    if p is None:
        return "—"
    return f"NT${p:,}" if cur == "TWD" else f"{cur} {p:,}"


def day_label(s: str | None) -> str:
    if not s:
        return ""
    d = date.fromisoformat(s)
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"


def deal_reasons(price: int, usual: int | None, samples: int, route: Route, alert: dict, cur: str) -> list[str]:
    reasons = []
    if route.alert_below and price <= route.alert_below:
        reasons.append(f"低於你設定的 {money(route.alert_below, cur)}")
    if usual and samples >= alert["min_samples"]:
        drop = (usual - price) / usual * 100
        if drop >= alert["drop_percent"]:
            reasons.append(f"比平常價 {money(usual, cur)} 便宜 {drop:.0f}%")
    return reasons


# ---------- Email ----------

def send_mail(subject: str, html: str, text: str) -> bool:
    """有設定 SMTP 就寄出；沒設定就存到 out/ 資料夾讓你在本機預覽。"""
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").replace(" ", "")
    to = os.environ.get("MAIL_TO", "").strip() or user
    if not (user and password):
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / "last_email.html").write_text(f"<h3>{escape(subject)}</h3>{html}", encoding="utf-8")
        print(f"[信件] 沒有設定 SMTP_USER / SMTP_PASSWORD，只存成 out/last_email.html：{subject}")
        return True

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"機票雷達 <{user}>"
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    try:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    except Exception as e:
        print(f"[信件] 寄送失敗：{type(e).__name__}: {e}")
        return False
    print(f"[信件] 已寄出：{subject}")
    return True


def mail_frame(title: str, body: str, site_url: str) -> str:
    link = f'<p><a href="{escape(site_url)}">打開機票雷達網頁 →</a></p>' if site_url else ""
    return (
        '<div style="font-family:system-ui,\'Microsoft JhengHei\',sans-serif;font-size:15px;color:#0b0b0b">'
        f'<h2 style="font-size:18px;margin:0 0 12px">{escape(title)}</h2>{body}{link}'
        '<p style="color:#898781;font-size:12px">價格為所有乘客總價；廉航的托運行李費是估計值。'
        '資料來自 Google Flights，實際以訂票頁面為準。</p></div>'
    )


def deals_email(items: list[dict], cur: str, site_url: str) -> tuple[str, str, str]:
    if len(items) == 1:
        it = items[0]
        subject = f"✈️ 便宜機票：{it['route']} {day_label(it['depart'])} {money(it['price'], cur)}"
    else:
        best = min(items, key=lambda it: it["price"])
        subject = f"✈️ 發現 {len(items)} 筆便宜機票（最低 {best['route']} {money(best['price'], cur)}）"

    cell = 'style="padding:8px 10px;border-bottom:1px solid #e1e0d9;text-align:left;vertical-align:top"'
    rows, lines = [], []
    for it in items:
        when = day_label(it["depart"]) + (f" → {day_label(it['return'])}" if it["return"] else "")
        f = it["flight"]
        detail = f"{f['airline']} {f['dep']}→{f['arr']}{'+1' if f['arr_plus'] else ''}，" + (
            "直飛" if not f["stops"] else f"轉機 {f['stops']} 次（{'、'.join(f['via'])}）")
        split = (f"票價 {money(f['fare'], cur)}＋行李估計 {money(f['bag_fee'], cur)}" if f["bag_fee"] else "")
        rows.append(
            f"<tr><td {cell}><b>{escape(it['route'])}</b><br>{escape(when)}</td>"
            f"<td {cell}><b style=\"font-size:17px\">{money(it['price'], cur)}</b><br>"
            + (f"<span style=\"color:#52514e;font-size:13px\">{split}</span><br>" if split else "")
            + f"<span style=\"color:#006300\">{escape('；'.join(it['reasons']))}</span></td>"
            f"<td {cell}>{escape(detail)}<br><a href=\"{escape(it['url'])}\">在 Google Flights 查看 →</a></td></tr>"
        )
        lines.append(f"{it['route']} {when} {money(it['price'], cur)} {split}（{'；'.join(it['reasons'])}）\n"
                     f"{detail}\n{it['url']}\n")
    table = f'<table style="border-collapse:collapse;width:100%;max-width:720px">{"".join(rows)}</table>'
    html = mail_frame("發現比平常便宜的機票", table, site_url)
    return subject, html, "\n".join(lines) + (f"\n網頁：{site_url}" if site_url else "")


# ---------- 主流程 ----------

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    ap = argparse.ArgumentParser(description="機票雷達：掃一次機票價格")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--test-email", action="store_true", help="另外寄一封測試信，確認 Email 設定正確")
    args = ap.parse_args()
    test_email = args.test_email or os.environ.get("TEST_EMAIL", "").lower() == "true"

    cfg, routes, all_paused = load_config(Path(args.config))
    alert = {**DEFAULT_ALERT, **cfg.get("alert", {})}
    bag_fees = {**DEFAULT_BAG_FEES, **{k.upper(): int(v) for k, v in cfg.get("bag_fees", {}).items()}}
    cur = cfg.get("currency", "TWD")
    pause = float(cfg.get("pause_seconds", 1.5))
    site_url = os.environ.get("SITE_URL", "")
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    today = now.astimezone(TW).date()
    usual_since = now - timedelta(days=alert["usual_days"])

    history = load_history(now)
    state = load_state()
    alerted: dict[str, int] = state.setdefault("alerted", {})
    by_combo: dict[tuple, list[dict]] = defaultdict(list)
    for row in history:
        by_combo[(row["route"], row["depart"], row["return"])].append(row)

    # 暫停中的航線不查價，但保留它的價格紀錄和通知狀態
    active_keys = {combo_key(r, d, rt) for r in routes for d, rt in r.combos(today)}
    plan = [] if all_paused else [(r, d, rt) for r in routes if not r.paused for d, rt in r.combos(today)]
    if all_paused:
        print("⏸ 全部暫停中，這次不查價")
    print(f"本次要查 {len(plan)} 筆（{now.astimezone(TW):%Y-%m-%d %H:%M} 台灣時間）")
    if len(plan) > 80:
        print("⚠ 查詢數太多，可能被 Google 限制，建議減少日期或住幾晚的選項")

    new_rows: list[dict] = []
    route_combos: dict[str, list[dict]] = defaultdict(list)
    to_mail: list[dict] = []
    ok = 0
    fetcher = Fetcher(cfg.get("language", "zh-TW"))
    for i, (route, dep, ret) in enumerate(plan):
        dep_s, ret_s = dep.isoformat(), ret.isoformat() if ret else ""
        ckey = combo_key(route, dep, ret)
        past = by_combo[(route.key, dep_s, ret_s)]
        recent = [r for r in past if r["t"] >= usual_since]
        usual = round(statistics.median(r["price"] for r in recent)) if recent else None
        query = build_query(cfg, route, dep, ret)
        combo = {
            "depart": dep_s, "return": ret_s or None, "url": query.url(),
            "usual": usual, "samples": len(recent),
            "spark": [[int(r["t"].timestamp()), r["price"]] for r in recent],
        }
        label = f"{route.name} {dep_s}{' → ' + ret_s if ret_s else ''}"
        try:
            flights = fetcher.fetch(query)
            ok += 1
        except Exception as e:
            combo.update(status="error", error=f"{type(e).__name__}: {e}"[:200])
            print(f"  ✗ {label}：查詢失敗 {combo['error']}")
            route_combos[route.id].append(combo)
            continue
        finally:
            if i < len(plan) - 1:
                time.sleep(pause + random.random())

        if not flights:
            combo["status"] = "none"
            print(f"  - {label}：沒有航班")
            route_combos[route.id].append(combo)
            continue

        options = sorted((summarize(f, route, bag_fees) for f in flights), key=lambda o: o["price"])
        best = options[0]
        price = best["price"]
        reasons = deal_reasons(price, usual, len(recent), route, alert, cur)
        combo.update(
            status="ok", price=price, deal=bool(reasons), reasons=reasons,
            drop=round((usual - price) / usual * 100, 1) if usual else None,
            low=min([price, *(r["price"] for r in past)]),
            flights=options[:5],
        )
        combo["spark"].append([int(now.timestamp()), price])
        route_combos[route.id].append(combo)
        new_rows.append({"time": fmt_time(now), "route": route.key, "depart": dep_s, "return": ret_s,
                         "price": price, "airline": best["airline"], "stops": best["stops"], "t": now})
        vs = f"，平常 {money(usual, cur)}（{'便宜' if combo['drop'] >= 0 else '貴'} {abs(combo['drop']):.0f}%）" if usual else ""
        bag = f"（含行李估計 {money(best['bag_fee'], cur)}）" if best["bag_fee"] else ""
        print(f"  {'★' if reasons else '✓'} {label}：{money(price, cur)}{bag}{vs}")

        # 同一個日期寄過信後，要再降 realert_percent 才再寄；價格回升 5% 以上就重新計算
        if reasons:
            prev = alerted.get(ckey)
            if prev is None or price <= prev * (1 - alert["realert_percent"] / 100):
                alerted[ckey] = price
                to_mail.append({"key": ckey, "route": route.name, "depart": dep_s, "return": ret_s or None, "price": price,
                                "reasons": reasons, "url": combo["url"], "flight": best})
        elif ckey in alerted and price >= alerted[ckey] * 1.05:
            del alerted[ckey]

    fetcher.close()
    for k in list(alerted):
        if k not in active_keys:
            del alerted[k]

    append_history(new_rows, now)
    all_rows = history + new_rows

    # ---- 異常通知：連續 6 小時全部查詢失敗就寄一封 ----
    mail_ok = True
    state.setdefault("last_ok", fmt_time(now))
    if plan and ok == 0:
        down_for = now - parse_time(state["last_ok"])
        print(f"⚠ 全部查詢失敗（已連續 {down_for}）")
        if down_for >= FAIL_ALERT_AFTER and not state.get("fail_mailed"):
            err = next((c.get("error") for cs in route_combos.values() for c in cs if c.get("error")), "")
            body = (f"<p>機票雷達已經連續 {down_for.total_seconds() / 3600:.0f} 小時查不到任何價格，"
                    f"可能是 Google 暫時擋住或網頁改版。</p><p style=\"color:#52514e\">錯誤訊息：{escape(err)}</p>"
                    "<p>如果持續發生，請更新 requirements.txt 裡 fast-flights 的版本。</p>")
            mail_ok = send_mail("⚠️ 機票雷達：查詢連續失敗", mail_frame("查詢連續失敗", body, site_url), err)
            state["fail_mailed"] = mail_ok
    else:  # 有查成功，或是暫停中沒有要查的
        state["last_ok"] = fmt_time(now)
        state["fail_mailed"] = False

    if to_mail:
        mail_ok = send_mail(*deals_email(to_mail, cur, site_url)) and mail_ok
        if not mail_ok:  # 沒寄出去就不要記成「已通知」，下次再試
            for it in to_mail:
                alerted.pop(it["key"], None)

    if test_email:
        lines = []
        for route in routes:
            okc = [c for c in route_combos[route.id] if c.get("status") == "ok"]
            if okc:
                c = min(okc, key=lambda c: c["price"])
                lines.append(f"<li>{escape(route.name)}：目前最低 {money(c['price'], cur)}（{day_label(c['depart'])} 出發）</li>")
        body = f"<p>Email 設定成功！之後發現便宜機票就會寄到這個信箱。</p><ul>{''.join(lines)}</ul>"
        mail_ok = send_mail("✅ 機票雷達測試信", mail_frame("Email 設定成功", body, site_url), "Email 設定成功！") and mail_ok

    save_state(state)
    build_site(routes, route_combos, all_rows, active_keys, alert, bag_fees, cur, now, ok, len(plan), all_paused)
    print(f"完成：成功 {ok}/{len(plan)}，寄出通知 {len(to_mail)} 筆")
    return 0 if mail_ok else 1


# ---------- 網頁 ----------

def build_site(routes, route_combos, all_rows, active_keys, alert, bag_fees, cur, now, ok, total, all_paused) -> None:
    usual_since = now - timedelta(days=alert["usual_days"])
    today = now.astimezone(TW).date()
    out_routes = []
    for route in routes:
        combos = route_combos[route.id]
        # 走勢圖：每次掃描時，目前追蹤的所有日期中最便宜的價格
        cheapest_at: dict[datetime, int] = {}
        for r in all_rows:
            if r["route"] == route.key and f"{route.key}|{r['depart']}|{r['return']}" in active_keys:
                cheapest_at[r["t"]] = min(r["price"], cheapest_at.get(r["t"], r["price"]))
        series = sorted(cheapest_at.items())
        earlier = [p for t, p in series if usual_since <= t < now]
        priced = [c for c in combos if c.get("status") == "ok"]
        out_routes.append({
            "id": route.id,
            "config": route.config(),
            "has_dates": bool(route.combos(today)),
            "cheapest": min(priced, key=lambda c: c["price"]) if priced else None,
            "usual": round(statistics.median(earlier)) if earlier else None,
            "series": [[int(t.timestamp()), p] for t, p in series],
            "combos": combos,
        })

    payload = {
        "generated": int(now.timestamp()), "ok": ok, "total": total, "currency": cur,
        "paused": all_paused, "repo": os.environ.get("GITHUB_REPOSITORY", ""),
        "alert": alert, "bag_fees": bag_fees, "routes": out_routes,
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    SITE_OUT.mkdir(exist_ok=True)
    html = SITE_TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", data.replace("</", "<\\/"))
    (SITE_OUT / "index.html").write_text(html, encoding="utf-8")
    (SITE_OUT / "data.json").write_text(data, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
