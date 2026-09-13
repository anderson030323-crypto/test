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
def _csv(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


# Fixed travel dates: every departure date is paired with every return date.
DEPARTURE_DATES = _csv("DEPARTURE_DATES", "2027-02-02,2027-02-03")
RETURN_DATES = _csv("RETURN_DATES", "2027-02-08,2027-02-09")
# Fallbacks when RETURN_DATES / DEPARTURE_DATES are cleared:
TRIP_NIGHTS = int(os.getenv("TRIP_NIGHTS", "5"))  # nights when RETURN_DATES is empty (0 = one-way)
DAYS_AHEAD = [int(d) for d in _csv("DAYS_AHEAD", "30,45,60,90")]  # days from today when DEPARTURE_DATES is empty
# Only itineraries operated entirely by these airlines count. Empty = any airline.
AIRLINES = [a.upper() for a in _csv("AIRLINES", "BR,CI,CX,JX,EK")]
AIRLINE_NAMES = {
    "BR": "長榮航空",
    "CI": "中華航空",
    "CX": "國泰航空",
    "JX": "星宇航空",
    "EK": "阿聯酋航空",
}
# Names Google may show (zh-TW / en) -> IATA code, used when page metadata lacks a code.
AIRLINE_CODE_BY_NAME = {
    "長榮航空": "BR", "EVA Air": "BR", "EVA": "BR",
    "中華航空": "CI", "China Airlines": "CI",
    "國泰航空": "CX", "Cathay Pacific": "CX",
    "星宇航空": "JX", "STARLUX": "JX", "Starlux Airlines": "JX", "STARLUX Airlines": "JX",
    "阿聯酋航空": "EK", "Emirates": "EK",
}
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
CSV_PATH = Path(os.getenv("CSV_PATH", "data/prices.csv"))
CHART_PATH = Path(os.getenv("CHART_PATH", "charts/price_trend.png"))
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "")

# Make sibling modules (plot_history) importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

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

    airlines = AIRLINES or None
    legs = [FlightQuery(date=dep, from_airport=ORIGIN, to_airport=DESTINATION, airlines=airlines)]
    if ret:
        legs.append(FlightQuery(date=ret, from_airport=DESTINATION, to_airport=ORIGIN, airlines=airlines))
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
    airlines: list[str]  # display names
    codes: list[str]  # IATA codes (best effort)
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

    # Airline code <-> name lookup (best effort)
    names: dict[str, str] = {}
    codes_by_name: dict[str, str] = dict(AIRLINE_CODE_BY_NAME)
    try:
        for code, name in payload[7][1][1]:
            names[str(code)] = str(name)
            codes_by_name[str(name)] = str(code)
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
                raw = [str(a) for a in (flight[1] or [])]
                airlines = [names.get(a, a) for a in raw]
                codes = [codes_by_name.get(a, a).upper() for a in raw]
                segments = list(flight[2] or [])
            except (IndexError, TypeError, ValueError, KeyError):
                continue  # unpriced or unexpected entry -> skip
            if price > 0:
                results.append(Itinerary(price=price, airlines=airlines, codes=codes, flights=segments))
    if not results:
        _dump_debug_html(html, f"{tag}-empty")
        _print_payload_diagnostics(payload, tag)
    return results


def _shape(obj: Any, depth: int = 0) -> str:
    """Compact description of nested list structure for log diagnostics."""
    if isinstance(obj, list):
        if depth >= 2:
            return f"list[{len(obj)}]"
        inner = ", ".join(_shape(x, depth + 1) for x in obj[:6])
        more = f", …+{len(obj) - 6}" if len(obj) > 6 else ""
        return f"[{inner}{more}]"
    if obj is None:
        return "None"
    if isinstance(obj, str):
        return f"str({len(obj)})"
    return type(obj).__name__


def _print_payload_diagnostics(payload: Any, tag: str) -> None:
    print(f"  [diag] {tag}: 頁面無可用報價。payload 結構：{_shape(payload)}")
    for idx in (2, 3):
        try:
            grp = payload[idx][0]
        except (IndexError, TypeError):
            continue
        if isinstance(grp, list) and grp:
            sample = json.dumps(grp[0], ensure_ascii=False)
            print(f"  [diag] payload[{idx}][0] 有 {len(grp)} 筆，第一筆前 400 字：{sample[:400]}")
        else:
            print(f"  [diag] payload[{idx}][0] = {_shape(grp)}")


def search_google_flights(cabin: str, dep: str, ret: str | None) -> list[Any]:
    """Return parsed itineraries for one cabin / date pair."""
    from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html

    airlines = AIRLINES or None
    legs = [FlightQuery(date=dep, from_airport=ORIGIN, to_airport=DESTINATION, airlines=airlines)]
    if ret:
        legs.append(FlightQuery(date=ret, from_airport=DESTINATION, to_airport=ORIGIN, airlines=airlines))
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


def target_fares(results: list[Any], cabin: str, dep: str, ret: str | None) -> list[Fare]:
    """Convert parsed itineraries to Fares, keeping only target-airline ones.

    For a round trip Google Flights lists outbound options priced as the full
    round-trip total, so ``price`` is already the total fare.
    """
    fares: list[Fare] = []
    wanted = set(AIRLINES)
    for item in results:
        try:
            price = float(item.price)
        except (TypeError, ValueError, AttributeError):
            continue
        if price <= 0:
            continue
        codes = [c.upper() for c in (getattr(item, "codes", None) or [])]
        if wanted and (not codes or any(c not in wanted for c in codes)):
            continue  # operated (partly) by an airline outside the target list
        segments = list(getattr(item, "flights", []) or [])
        carriers = []
        for name in getattr(item, "airlines", []) or []:
            if name and name not in carriers:
                carriers.append(str(name))
        fares.append(
            Fare(
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
        )
    return fares


def cheapest_fare(results: list[Any], cabin: str, dep: str, ret: str | None) -> Fare | None:
    fares = target_fares(results, cabin, dep, ret)
    return min(fares, key=lambda f: f.price) if fares else None


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def criteria_fingerprint() -> str:
    """Identifies the search criteria; when it changes, old prices are not comparable."""
    dates = ";".join(f"{d}>{r or ''}" for d, r in candidate_dates()) if (DEPARTURE_DATES and RETURN_DATES) else (
        f"nights={TRIP_NIGHTS};ahead={','.join(map(str, DAYS_AHEAD))}" if not DEPARTURE_DATES else f"deps={','.join(DEPARTURE_DATES)};nights={TRIP_NIGHTS}"
    )
    return f"{ORIGIN}-{DESTINATION}|{dates}|airlines={','.join(AIRLINES)}|adults={ADULTS}|stops={MAX_STOPS}|{CURRENCY}"


def load_history() -> dict[str, Any]:
    fresh = {"route": f"{ORIGIN}-{DESTINATION}", "criteria": criteria_fingerprint(), "lowest": {}, "runs": []}
    if not HISTORY_PATH.exists():
        return fresh
    with HISTORY_PATH.open(encoding="utf-8") as fh:
        history = json.load(fh)
    if history.get("criteria") != fresh["criteria"]:
        print("搜尋條件已變更，歷史最低價重新計算。")
        fresh["previous_criteria"] = history.get("criteria")
        return fresh
    return history


def save_history(history: dict[str, Any]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def append_csv(now: datetime, by_airline: dict[str, dict[str, "Fare"]]) -> None:
    """Flat, spreadsheet-friendly log: one row per cabin x airline per run."""
    import csv

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_PATH.exists()
    with CSV_PATH.open("a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["date", "checked_at", "cabin", "airline", "departure", "return", "stops_outbound", "currency", "price"])
        for cabin, fares in by_airline.items():
            for label, f in sorted(fares.items(), key=lambda kv: kv[1].price):
                w.writerow([now.date().isoformat(), now.isoformat(timespec="seconds"), CABINS[cabin], label,
                            f.departure_date, f.return_date or "", f.stops_outbound, f.currency, f"{f.price:.0f}"])


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


def report_to_html(report: str, alert: bool, chart_cid: str | None = None) -> str:
    """Render the plain-text report as HTML with loud highlighting on price drops."""
    import html as _html

    out: list[str] = []
    for raw in report.split("\n"):
        line = _html.escape(raw)
        # Make URLs clickable.
        if "https://" in raw:
            url = raw[raw.index("https://"):].strip()
            text = "Google Flights 連結" if "google.com" in url else "開啟"
            line = line.replace(_html.escape(url), f'<a href="{_html.escape(url)}">{text}</a>')
        if raw.startswith("═"):
            continue  # the HTML banner box replaces the text rule
        if "歷史新低" in raw or "價格下跌通知" in raw or raw.startswith("  【"):
            line = f'<div style="background:#ffe9e9;color:#c00000;font-weight:bold;font-size:1.15em;padding:4px 8px;border-left:6px solid #c00000">{line}</div>'
        elif "比上次便宜" in raw or "👉" in raw:
            line = f'<div style="background:#fff6d5;font-weight:bold;padding:2px 8px">{line}</div>'
        elif raw.startswith("各") or raw.startswith("📅"):
            line = f'<div style="font-weight:bold;margin-top:10px">{line}</div>'
        else:
            line = f"<div>{line if line else '&nbsp;'}</div>"
        out.append(line)
    banner = ""
    if alert:
        banner = (
            '<div style="background:#c00000;color:#fff;font-size:1.6em;font-weight:bold;'
            'padding:14px 18px;margin-bottom:14px;border-radius:6px">🔥 機票降價了！請看下方紅色標記</div>'
        )
    chart = ""
    if chart_cid:
        chart = (
            '<div style="margin:16px 0 6px;font-weight:bold">📈 每日價格走勢</div>'
            f'<img src="cid:{chart_cid}" alt="價格走勢圖" style="max-width:100%;border:1px solid #e1e0d9;border-radius:6px">'
        )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Noto Sans TC,sans-serif;'
        'font-size:14px;line-height:1.6;white-space:pre-wrap;max-width:900px">'
        f"{banner}{''.join(out)}{chart}</div>"
    )


def notify_email(subject: str, text: str, html: str | None = None, image_path: Path | None = None) -> bool:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and NOTIFY_EMAIL_TO):
        return False
    if html:
        from email.mime.image import MIMEImage
        from email.mime.multipart import MIMEMultipart

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(text, "plain", "utf-8"))
        alt.attach(MIMEText(html, "html", "utf-8"))
        if image_path and image_path.exists():
            msg: Any = MIMEMultipart("related")
            msg.attach(alt)
            img = MIMEImage(image_path.read_bytes(), _subtype="png")
            img.add_header("Content-ID", "<trend>")
            img.add_header("Content-Disposition", "inline", filename=image_path.name)
            msg.attach(img)
        else:
            msg = alt
    else:
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
    """(departure, return) pairs to search.

    Fixed DEPARTURE_DATES x RETURN_DATES when both are set; otherwise fall back
    to DEPARTURE_DATES + TRIP_NIGHTS, or to DAYS_AHEAD from today.
    """
    today = date.today()
    deps = DEPARTURE_DATES or [(today + timedelta(days=n)).isoformat() for n in DAYS_AHEAD]
    out: list[tuple[str, str | None]] = []
    for dep in deps:
        if RETURN_DATES:
            for ret in RETURN_DATES:
                if date.fromisoformat(ret) > date.fromisoformat(dep):
                    out.append((dep, ret))
        elif TRIP_NIGHTS > 0:
            out.append((dep, (date.fromisoformat(dep) + timedelta(days=TRIP_NIGHTS)).isoformat()))
        else:
            out.append((dep, None))
    return out


def main() -> int:
    now = datetime.now(TAIPEI_TZ)
    print(f"=== {ORIGIN} -> {DESTINATION} fare check @ {now:%Y-%m-%d %H:%M} (Asia/Taipei) ===")
    dates = candidate_dates()
    print(f"Searching {len(dates)} date pair(s): " + ", ".join(f"{d}→{r}" if r else d for d, r in dates))
    if AIRLINES:
        print("Airlines: " + ", ".join(f"{c}({AIRLINE_NAMES.get(c, c)})" for c in AIRLINES))

    today_best: dict[str, Fare] = {}
    # cabin -> airline label -> cheapest fare across all date pairs
    by_airline: dict[str, dict[str, Fare]] = {cabin: {} for cabin in CABINS}
    # cabin -> (dep, ret) -> airline label -> cheapest fare for that pair
    by_pair: dict[str, dict[tuple[str, str | None], dict[str, Fare]]] = {cabin: {} for cabin in CABINS}
    for cabin in CABINS:
        for dep, ret in dates:
            results = search_google_flights(cabin, dep, ret)
            fares = target_fares(results, cabin, dep, ret)
            fare = min(fares, key=lambda f: f.price) if fares else None
            print(
                f"    {CABINS[cabin]} {dep}→{ret}: {len(results)} 筆"
                + (f"，目標航空最低 {fare.price:,.0f}（{'/'.join(fare.carriers)}）" if fare else "，目標航空無報價")
            )
            if fare and (cabin not in today_best or fare.price < today_best[cabin].price):
                today_best[cabin] = fare
            pair_best = by_pair[cabin].setdefault((dep, ret), {})
            for f in fares:
                label = "/".join(f.carriers) or "未知航空"
                if label not in by_airline[cabin] or f.price < by_airline[cabin][label].price:
                    by_airline[cabin][label] = f
                if label not in pair_best or f.price < pair_best[label].price:
                    pair_best[label] = f
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
            # Per-airline cheapest fares, so the trend chart can draw one line per airline.
            "by_airline": {cabin: {label: asdict(f) for label, f in fares.items()} for cabin, fares in by_airline.items() if fares},
        }
    )
    history["runs"] = history["runs"][-365:]
    save_history(history)
    append_csv(now, by_airline)

    chart_ok = False
    try:
        from plot_history import draw as draw_chart  # same directory

        note = f"（{' / '.join(d[5:] for d in DEPARTURE_DATES)} 出發，{' / '.join(r[5:] for r in RETURN_DATES)} 回程）" if DEPARTURE_DATES and RETURN_DATES else ""
        chart_ok = draw_chart(history, CHART_PATH, currency=CURRENCY, title_note=note)
        print(f"走勢圖已更新：{CHART_PATH}" if chart_ok else "走勢圖：尚無資料")
    except Exception as exc:  # charting must never block the alert
        print(f"[warn] 走勢圖產生失敗：{type(exc).__name__}: {exc}")

    # Build report
    lines = [f"📅 {now:%Y-%m-%d} 台北(TPE) → 峇里島(DPS) 機票價格"]
    lines.append(
        "條件：" + " 或 ".join(DEPARTURE_DATES) + " 出發，" + " 或 ".join(RETURN_DATES) + " 回程"
        if DEPARTURE_DATES and RETURN_DATES
        else "條件：" + "、".join(f"{d}→{r}" if r else d for d, r in dates)
    )
    if AIRLINES:
        lines.append("航空公司：" + "、".join(AIRLINE_NAMES.get(c, c) for c in AIRLINES))
    lines.append("")

    # Loud banner at the very top whenever something got cheaper.
    if alert_cabins:
        bar = "═" * 46
        lines += [bar, "🔥🔥🔥  價格下跌通知  🔥🔥🔥"]
        for cabin in alert_cabins:
            f = today_best[cabin]
            what = "【歷史新低】" if cabin in new_lows else "【比上次便宜】"
            line = f"  {what} {CABINS[cabin]} {f.currency} {f.price:,.0f}  {'/'.join(f.carriers)}，{f.departure_date} → {f.return_date or '單程'}"
            if cabin in drops:
                line += f"（↓ {drops[cabin] - f.price:,.0f}，上次 {drops[cabin]:,.0f}）"
            lines.append(line)
        lines += [bar, ""]

    for cabin in CABINS:
        if cabin not in today_best:
            lines.append(f"• {CABINS[cabin]}：Google Flights 目前沒有目標航空的{CABINS[cabin]}報價（該航線可能未提供此艙等）")
            continue
        fare = today_best[cabin]
        tags = []
        if cabin in new_lows:
            tags.append("🔥🔥🔥 歷史新低！")
        if cabin in drops:
            diff = drops[cabin] - fare.price
            tags.append(f"📉 比上次便宜 {fare.currency} {diff:,.0f}（上次 {drops[cabin]:,.0f}）")
        prefix = "👉 " if cabin in alert_cabins else "• "
        lines.append(prefix + fare.summary() + (("  ◀◀ " + "；".join(tags)) if tags else ""))
        low = history["lowest"].get(cabin, {})
        if low and cabin not in new_lows:
            lines.append(f"   （歷史最低 {low['currency']} {float(low['price']):,.0f}，{low['checked_at'][:10]}）")
        last = last_run_prices.get(cabin)
        if last is not None and cabin not in drops:
            lines.append(f"   （上次查價 {fare.currency} {last:,.0f}）")
        lines.append(f"   查看/訂票：{google_flights_url(cabin, fare.departure_date, fare.return_date)}")
    # Per-airline breakdown so the reader can see how the others compare.
    lines += ["", "各航空最低價（跨所有日期組合）："]
    for cabin in CABINS:
        entries = sorted(by_airline[cabin].values(), key=lambda f: f.price)
        if not entries:
            lines.append(f"  {CABINS[cabin]}：目標航空皆無報價")
            continue
        lines.append(f"  {CABINS[cabin]}：")
        for i, f in enumerate(entries):
            stops = "直飛" if f.stops_outbound == 0 else f"轉機 {f.stops_outbound} 次"
            mark = "👉 " if i == 0 else "- "
            tail = "  ◀ 最低" if i == 0 else ""
            lines.append(
                f"    {mark}{'/'.join(f.carriers)}：{f.currency} {f.price:,.0f}"
                f"（{f.departure_date} 出發 / {f.return_date} 回程，{stops}）{tail}"
            )
        missing = [AIRLINE_NAMES.get(c, c) for c in AIRLINES if not any(AIRLINE_NAMES.get(c, c) in k for k in by_airline[cabin])]
        if missing:
            lines.append(f"    - 無報價：{'、'.join(missing)}")

    # Per date-pair breakdown (one compact line per pair).
    if len(dates) > 1:
        lines += ["", "各日期組合明細（各航空最低，括號為去程轉機次數）："]
        for cabin in CABINS:
            pairs = by_pair[cabin]
            if not any(pairs.values()):
                continue
            lines.append(f"  {CABINS[cabin]}：")
            for dep, ret in dates:
                entries = sorted(pairs.get((dep, ret), {}).values(), key=lambda f: f.price)
                label = f"{dep[5:].replace('-', '/')}→{ret[5:].replace('-', '/')}" if ret else dep
                if not entries:
                    lines.append(f"    {label}：目標航空無報價")
                    continue
                best_price = today_best[cabin].price if cabin in today_best else None
                cells = []
                for f in entries:
                    cell = f"{'/'.join(f.carriers)} {f.price:,.0f}" + ("" if f.stops_outbound == 0 else f"（轉{f.stops_outbound}）")
                    if best_price is not None and f.price == best_price:
                        cell = f"👉 {cell} ◀ 最低"
                    cells.append(cell)
                lines.append(f"    {label}：" + "｜".join(cells))
    lines += ["", f"資料來源：Google Flights（{len(dates)} 組日期，每艙等取目標航空最低）"]
    if chart_ok and GITHUB_REPOSITORY:
        ref = GITHUB_REF_NAME or "main"
        lines.append(f"📈 每日價格走勢圖：https://github.com/{GITHUB_REPOSITORY}/blob/{ref}/{CHART_PATH.as_posix()}")
        lines.append(f"📄 完整價格紀錄（CSV）：https://github.com/{GITHUB_REPOSITORY}/blob/{ref}/{CSV_PATH.as_posix()}")
    report = "\n".join(lines)
    print("\n" + report)

    write_step_summary("## 今日查價結果\n\n```\n" + report + "\n```")

    if alert_cabins or NOTIFY_ALWAYS:
        if alert_cabins:
            # Subject leads with the loudest fact: which cabin, how cheap.
            parts = [f"{CABINS[c]} {today_best[c].price:,.0f}" for c in alert_cabins]
            kind = "🔥🔥 歷史新低" if any(c in new_lows for c in alert_cabins) else "📉 降價"
            title = f"{kind}｜台北→峇里島 {'、'.join(parts)}（{now:%m/%d}）"
        else:
            title = f"✈️ 台北→峇里島 每日機票價格（{now:%m/%d}）"
        email_ok = notify_email(
            title, report,
            html=report_to_html(report, alert=bool(alert_cabins), chart_cid="trend" if chart_ok else None),
            image_path=CHART_PATH if chart_ok else None,
        )
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
