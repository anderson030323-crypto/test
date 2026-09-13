#!/usr/bin/env python3
"""Daily TPE -> DPS (Taipei -> Bali) fare monitor.

Queries Google Flights (via the ``fast-flights`` library, no API key needed)
for the cheapest round-trip fare in three cabins (Economy / Premium Economy /
Business) across a set of candidate departure dates, records the result in
``data/price_history.json`` and sends a notification whenever a cabin is
cheaper than the previous check or hits a new all-time low.

Configuration is via environment variables (see README.md).
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

ORIGIN = os.getenv("ORIGIN", "TPE")
DESTINATION = os.getenv("DESTINATION", "DPS")
CURRENCY = os.getenv("CURRENCY", "TWD")
ADULTS = int(os.getenv("ADULTS", "1"))
TRIP_NIGHTS = int(os.getenv("TRIP_NIGHTS", "5"))  # 0 = one-way
# Departure dates to sample, expressed as "days from today".
DAYS_AHEAD = [int(d) for d in os.getenv("DAYS_AHEAD", "30,45,60,90").split(",") if d.strip()]
# Explicit dates override DAYS_AHEAD, e.g. "2026-12-20,2026-12-27"
DEPARTURE_DATES = [d.strip() for d in os.getenv("DEPARTURE_DATES", "").split(",") if d.strip()]
MAX_STOPS = os.getenv("MAX_STOPS", "")  # "" = any, "0" = direct only, "1" = up to 1 stop
NOTIFY_ALWAYS = os.getenv("NOTIFY_ALWAYS", "false").lower() in {"1", "true", "yes"}
# Optional HTTP(S) proxy for reaching Google Flights.
GOOGLE_FLIGHTS_PROXY = os.getenv("GOOGLE_FLIGHTS_PROXY", "") or None
# If set, raw HTML of pages that yield no results is saved here for debugging.
DEBUG_DIR = os.getenv("DEBUG_DIR", "")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_STEP_SUMMARY = os.getenv("GITHUB_STEP_SUMMARY", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Email (primary channel). Defaults target Gmail SMTP; SMTP_USER is the Gmail
# address used to send, SMTP_PASSWORD is a Gmail App Password.
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO", "anderson030323@gmail.com")

HISTORY_PATH = Path(os.getenv("HISTORY_PATH", "data/price_history.json"))

CABINS = {
    "ECONOMY": "經濟艙",
    "PREMIUM_ECONOMY": "豪華經濟艙",
    "BUSINESS": "商務艙",
}
# Our cabin keys -> fast-flights seat names
SEAT_TYPES = {
    "ECONOMY": "economy",
    "PREMIUM_ECONOMY": "premium-economy",
    "BUSINESS": "business",
}

TAIPEI_TZ = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Fare:
    cabin: str
    price: float
    currency: str
    departure_date: str
    return_date: str | None
    carriers: list[str]
    stops_outbound: int
    stops_return: int | None
    checked_at: str

    def summary(self) -> str:
        route = f"{self.departure_date} 出發"
        if self.return_date:
            route += f" / {self.return_date} 回程"
        stops = "直飛" if self.stops_outbound == 0 else f"去程轉機 {self.stops_outbound} 次"
        if self.stops_return is not None:
            stops += f"，回程轉機 {self.stops_return} 次"
        return (
            f"{CABINS[self.cabin]}：{self.currency} {self.price:,.0f}"
            f"（{route}，{'/'.join(self.carriers) or '未知航空'}，{stops}）"
        )


# --------------------------------------------------------------------------- #
# Google Flights (fast-flights) client
# --------------------------------------------------------------------------- #


def google_flights_url(cabin: str, dep: str, ret: str | None) -> str:
    """Human-clickable Google Flights URL for the same search."""
    from fast_flights import FlightQuery, Passengers, create_query

    legs = [FlightQuery(date=dep, from_airport=ORIGIN, to_airport=DESTINATION)]
    if ret:
        legs.append(FlightQuery(date=ret, from_airport=DESTINATION, to_airport=ORIGIN))
    q = create_query(
        flights=legs,
        trip="round-trip" if ret else "one-way",
        seat=SEAT_TYPES[cabin],  # type: ignore[arg-type]
        passengers=Passengers(adults=ADULTS),
        language="zh-TW",
        currency=CURRENCY,
        max_stops=int(MAX_STOPS) if MAX_STOPS else None,
    )
    return q.url()


@dataclass
class Itinerary:
    """Minimal parsed Google Flights result (mirrors fast-flights' ``Flights``)."""

    price: float
    airlines: list[str]
    flights: list[Any]  # segments; only the count is used


def _dump_debug_html(html: str, tag: str) -> None:
    if DEBUG_DIR:
        Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)
        (Path(DEBUG_DIR) / f"{tag}.html").write_text(html, encoding="utf-8")


def parse_results_html(html: str, tag: str = "page") -> list[Itinerary]:
    """Tolerant version of fast-flights' parser.

    Google occasionally lists itineraries without a price (or with a
    different nesting) and the upstream parser raises IndexError for the
    whole page. Here each itinerary is parsed independently and unpriced
    ones are skipped, so one odd entry never hides the rest.
    """
    from selectolax.lexbor import LexborHTMLParser

    parser = LexborHTMLParser(html)
    script = parser.css_first(r"script.ds\:1")
    if script is None:
        _dump_debug_html(html, f"{tag}-noscript")
        return []
    js = script.text()
    if "data:" not in js:
        _dump_debug_html(html, f"{tag}-nodata")
        return []
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        return []  # Google says: no flights
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        _dump_debug_html(html, f"{tag}-badjson")
        return []

    # Airline code -> name lookup (best effort)
    names: dict[str, str] = {}
    try:
        for code, name in payload[7][1][1]:
            names[str(code)] = str(name)
    except (IndexError, TypeError, ValueError):
        pass

    # Google splits results into "best" and "other" groups; scan every group.
    groups: list[Any] = []
    for idx in (3, 2):
        try:
            grp = payload[idx][0]
        except (IndexError, TypeError):
            continue
        if isinstance(grp, list):
            groups.append(grp)

    results: list[Itinerary] = []
    for grp in groups:
        for entry in grp:
            try:
                flight = entry[0]
                price = float(entry[1][0][1])
                airlines = [names.get(str(a), str(a)) for a in (flight[1] or [])]
                segments = list(flight[2] or [])
            except (IndexError, TypeError, ValueError, KeyError):
                continue  # unpriced or unexpected entry -> skip
            if price > 0:
                results.append(Itinerary(price=price, airlines=airlines, flights=segments))
    if not results and groups:
        _dump_debug_html(html, f"{tag}-empty")
    return results


def search_google_flights(cabin: str, dep: str, ret: str | None) -> list[Any]:
    """Return parsed itineraries for one cabin / date pair."""
    from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html

    legs = [FlightQuery(date=dep, from_airport=ORIGIN, to_airport=DESTINATION)]
    if ret:
        legs.append(FlightQuery(date=ret, from_airport=DESTINATION, to_airport=ORIGIN))
    query = create_query(
        flights=legs,
        trip="round-trip" if ret else "one-way",
        seat=SEAT_TYPES[cabin],  # type: ignore[arg-type]
        passengers=Passengers(adults=ADULTS),
        language="zh-TW",
        currency=CURRENCY,
        max_stops=int(MAX_STOPS) if MAX_STOPS else None,
    )
    tag = f"{cabin}-{dep}"
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            html = fetch_flights_html(query, proxy=GOOGLE_FLIGHTS_PROXY)
        except Exception as exc:  # network hiccup: retry with backoff
            last_exc = exc
            time.sleep(3 * (attempt + 1))
            continue
        try:
            return parse_results_html(html, tag)
        except Exception as exc:  # unexpected page shape: keep the evidence
            _dump_debug_html(html, f"{tag}-parseerror")
            print(f"  [warn] 解析失敗 {cabin} {dep}: {type(exc).__name__}: {exc}")
            return []
    print(f"  [warn] Google Flights 連線失敗 {cabin} {dep}: {last_exc}")
    return []


def cheapest_fare(results: list[Any], cabin: str, dep: str, ret: str | None) -> Fare | None:
    """Pick the cheapest itinerary out of fast-flights results.

    For a round trip Google Flights lists outbound options priced as the full
    round-trip total, so ``price`` is already the total fare.
    """
    best: Fare | None = None
    for item in results:
        try:
            price = float(item.price)
        except (TypeError, ValueError, AttributeError):
            continue
        if price <= 0:
            continue
        segments = list(getattr(item, "flights", []) or [])
        carriers = []
        for name in getattr(item, "airlines", []) or []:
            if name and name not in carriers:
                carriers.append(str(name))
        fare = Fare(
            cabin=cabin,
            price=price,
            currency=CURRENCY,
            departure_date=dep,
            return_date=ret,
            carriers=carriers,
            stops_outbound=max(len(segments) - 1, 0),
            stops_return=None,
            checked_at=datetime.now(TAIPEI_TZ).isoformat(timespec="seconds"),
        )
        if best is None or fare.price < best.price:
            best = fare
    return best


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def load_history() -> dict[str, Any]:
    if HISTORY_PATH.exists():
        with HISTORY_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    return {"route": f"{ORIGIN}-{DESTINATION}", "lowest": {}, "runs": []}


def save_history(history: dict[str, Any]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #


def notify_github_issue(title: str, body: str) -> bool:
    if not (GITHUB_TOKEN and GITHUB_REPOSITORY):
        return False
    resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        json={"title": title, "body": body, "labels": ["flight-deal"]},
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"  [warn] GitHub issue failed: {resp.status_code} {resp.text[:200]}")
        return False
    print(f"  GitHub issue created: {resp.json().get('html_url')}")
    return True


def notify_telegram(text: str) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=30,
    )
    ok = resp.status_code < 300
    print("  Telegram sent" if ok else f"  [warn] Telegram failed: {resp.text[:200]}")
    return ok


def notify_email(subject: str, text: str) -> bool:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and NOTIFY_EMAIL_TO):
        return False
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL_TO
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
        print(f"  Email sent to {NOTIFY_EMAIL_TO}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] Email failed: {exc}")
        return False


def write_step_summary(markdown: str) -> None:
    if GITHUB_STEP_SUMMARY:
        with open(GITHUB_STEP_SUMMARY, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def candidate_dates() -> list[tuple[str, str | None]]:
    today = date.today()
    deps = DEPARTURE_DATES or [(today + timedelta(days=n)).isoformat() for n in DAYS_AHEAD]
    out: list[tuple[str, str | None]] = []
    for dep in deps:
        ret = None
        if TRIP_NIGHTS > 0:
            ret = (date.fromisoformat(dep) + timedelta(days=TRIP_NIGHTS)).isoformat()
        out.append((dep, ret))
    return out


def main() -> int:
    now = datetime.now(TAIPEI_TZ)
    print(f"=== {ORIGIN} -> {DESTINATION} fare check @ {now:%Y-%m-%d %H:%M} (Asia/Taipei) ===")
    dates = candidate_dates()
    print(f"Sampling {len(dates)} departure date(s): {', '.join(d for d, _ in dates)}")

    today_best: dict[str, Fare] = {}
    for cabin in CABINS:
        for dep, ret in dates:
            results = search_google_flights(cabin, dep, ret)
            fare = cheapest_fare(results, cabin, dep, ret)
            print(f"    {CABINS[cabin]} {dep}: {len(results)} 筆" + (f"，最低 {fare.price:,.0f}" if fare else ""))
            if fare and (cabin not in today_best or fare.price < today_best[cabin].price):
                today_best[cabin] = fare
            time.sleep(1.5)  # be gentle: avoid Google rate limiting
        if cabin in today_best:
            print("  " + today_best[cabin].summary())
        else:
            print(f"  {CABINS[cabin]}：今日查無報價")

    history = load_history()
    # Baseline 1: the most recent previous check ("today's" price before this run).
    last_run_prices: dict[str, float] = {}
    if history["runs"]:
        for cabin, rec in history["runs"][-1].get("prices", {}).items():
            last_run_prices[cabin] = float(rec["price"])

    # Baseline 2: the all-time low since monitoring started.
    new_lows: set[str] = set()  # cabins below the historical low
    drops: dict[str, float] = {}  # cabin -> previous price it dropped below
    for cabin, fare in today_best.items():
        prev = history["lowest"].get(cabin)
        prev_low = float(prev["price"]) if prev else None
        if prev_low is None or fare.price < prev_low:
            new_lows.add(cabin)
            history["lowest"][cabin] = asdict(fare)
        last = last_run_prices.get(cabin)
        if last is not None and fare.price < last:
            drops[cabin] = last
    alert_cabins = [c for c in CABINS if c in new_lows or c in drops]

    history["runs"].append(
        {
            "date": now.date().isoformat(),
            "checked_at": now.isoformat(timespec="seconds"),
            "prices": {cabin: asdict(f) for cabin, f in today_best.items()},
        }
    )
    history["runs"] = history["runs"][-365:]
    save_history(history)

    # Build report
    lines = [f"📅 {now:%Y-%m-%d} 台北(TPE) → 峇里島(DPS) 機票價格", ""]
    for cabin in CABINS:
        if cabin not in today_best:
            lines.append(f"• {CABINS[cabin]}：今日查無報價")
            continue
        fare = today_best[cabin]
        tags = []
        if cabin in new_lows:
            tags.append("🔥 歷史新低")
        if cabin in drops:
            diff = drops[cabin] - fare.price
            tags.append(f"📉 比上次便宜 {fare.currency} {diff:,.0f}（上次 {drops[cabin]:,.0f}）")
        lines.append("• " + fare.summary() + (("  " + "；".join(tags)) if tags else ""))
        low = history["lowest"].get(cabin, {})
        if low and cabin not in new_lows:
            lines.append(f"   （歷史最低 {low['currency']} {float(low['price']):,.0f}，{low['checked_at'][:10]}）")
        last = last_run_prices.get(cabin)
        if last is not None and cabin not in drops:
            lines.append(f"   （上次查價 {fare.currency} {last:,.0f}）")
        lines.append(f"   查看/訂票：{google_flights_url(cabin, fare.departure_date, fare.return_date)}")
    lines += ["", f"資料來源：Google Flights（{len(dates)} 個出發日取樣，每艙等取最低）"]
    report = "\n".join(lines)
    print("\n" + report)

    write_step_summary("## 今日查價結果\n\n```\n" + report + "\n```")

    if alert_cabins or NOTIFY_ALWAYS:
        if alert_cabins:
            cabins_txt = "、".join(CABINS[c] for c in alert_cabins)
            kind = "歷史新低" if any(c in new_lows for c in alert_cabins) else "降價"
            title = f"✈️ 台北→峇里島 {kind}：{cabins_txt}（{now:%m/%d}）"
        else:
            title = f"✈️ 台北→峇里島 每日機票價格（{now:%m/%d}）"
        email_ok = notify_email(title, report)
        telegram_ok = notify_telegram(f"{title}\n\n{report}")
        # GitHub Issue is the fallback so an alert is never silently lost.
        issue_ok = notify_github_issue(title, report) if not email_ok else False
        if not (email_ok or telegram_ok or issue_ok):
            print("[warn] 沒有任何通知管道成功送出，請檢查 secrets 設定。")
    else:
        print("\n今日價格沒有低於上次查價或歷史最低，不發送通知。")

    if not today_best:
        print("[error] 三種艙等都查無報價，可能是 Google Flights 暫時封鎖或頁面格式改變。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
