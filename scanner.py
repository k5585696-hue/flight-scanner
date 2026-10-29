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
from fast_flights.parser import parse_js
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
        sys.exit(f"設定錯誤：「{name}」是來回票，要寫 stay_days（玩幾天）")
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


def segment_numbers(item) -> list[tuple[str, str]]:
    """每一段的 (航空公司代碼, 航班號碼)，例如 ("IT", "IT210")。"""
    try:
        return [(seg[22][0], f"{seg[22][0]}{seg[22][1]}") for seg in item[0][2]]
    except (IndexError, TypeError):
        return []


def parse_all(html: str) -> list:
    """fast-flights 3.1 只讀 Google 的「其他航班」清單，會漏掉「最佳航班」（常常就是最便宜的），這裡兩個都讀。"""
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise RuntimeError("Google 回傳的頁面沒有航班資料（可能被暫時擋住或網頁改版）")
    data = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise FlightsNotFound("no flights found")
    payload = json.loads(data)
    flights = []
    for idx in (2, 3):  # 2 = 最佳航班, 3 = 其他航班
        if len(payload) > idx and payload[idx] and payload[idx][0]:
            p = list(payload)
            p[3] = payload[idx]  # parse_js 只讀 payload[3]
            parsed = parse_js("data:" + json.dumps(p) + ",")
            for f, item in zip(parsed, payload[idx][0]):
                f.segments = segment_numbers(item)
            flights += parsed
    return flights


def fetch(query) -> list:
    for attempt in range(3):
        try:
            return [f for f in parse_all(fetch_flights_html(query)) if f.price and f.price > 0]
        except FlightsNotFound:
            return []
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    return []


def summarize(f, route: Route, bag_fees: dict[str, int]) -> dict:
    first, last = f.flights[0], f.flights[-1]
    codes = {code for code, _ in getattr(f, "segments", [])} or {f.type}
    lcc = sorted(c for c in codes if c in bag_fees)
    legs = 2 if route.trip == "round-trip" else 1
    bag_fee = sum(bag_fees[c] for c in lcc) * route.checked_bags * legs
    return {
        "price": f.price + bag_fee,   # 拿來比較的價格（含估計行李費）
        "fare": f.price,              # Google 上的票價
        "bag_fee": bag_fee,
        "lcc": bool(lcc),
        "airline": "、".join(f.airlines) or f.type,
        "nums": [num for _, num in getattr(f, "segments", [])],
        "stops": len(f.flights) - 1,
        "via": [s.to_airport.code for s in f.flights[:-1]],
        "from": first.from_airport.code,
        "to": last.to_airport.code,
        "dep": "%02d:%02d" % first.departure.time,
        "arr": "%02d:%02d" % last.arrival.time,
        "arr_plus": (date(*last.arrival.date) - date(*first.departure.date)).days,
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
    sys.stdout.reconfigure(encoding="utf-8")
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
        print("⚠ 查詢數太多，可能被 Google 限制，建議減少日期或玩幾天的選項")

    new_rows: list[dict] = []
    route_combos: dict[str, list[dict]] = defaultdict(list)
    to_mail: list[dict] = []
    ok = 0
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
            flights = fetch(query)
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
