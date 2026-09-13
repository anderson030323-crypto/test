#!/usr/bin/env python3
"""Draw the daily fare trend chart from ``data/price_history.json``.

One small panel per cabin, one line per airline (fixed colour per airline),
the all-time low annotated. Output is a PNG that is committed to the repo,
shown in the README and embedded in the daily e-mail.

Usage: python monitor/plot_history.py [history.json] [out.png]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

CABINS = {"ECONOMY": "經濟艙", "PREMIUM_ECONOMY": "豪華經濟艙", "BUSINESS": "商務艙"}

# Fixed categorical slot per airline (order is the CVD-safe order of the
# reference palette; never reassigned when a series disappears).
AIRLINE_COLORS = {
    "國泰航空": "#2a78d6",
    "長榮航空": "#eb6834",
    "中華航空": "#1baf7a",
    "星宇航空": "#eda100",
    "阿聯酋航空": "#e87ba4",
}
MIXED_LABEL = "混合航班（多家航空）"
MIXED_COLOR = "#4a3aa7"

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
LOW_COLOR = "#d03b3b"  # status "critical" red for the single annotated low


def pick_cjk_font() -> None:
    """Use the first installed CJK-capable font so Chinese labels render."""
    preferred = [
        "Noto Sans CJK TC",
        "Noto Sans CJK JP",
        "Noto Sans CJK SC",
        "Noto Sans TC",
        "WenQuanYi Zen Hei",
        "Microsoft JhengHei",
        "PingFang TC",
    ]
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in preferred:
        if name in installed:
            plt.rcParams["font.family"] = [name, "sans-serif"]
            break
    plt.rcParams["axes.unicode_minus"] = False


def daily_series(history: dict[str, Any]) -> dict[str, dict[str, dict[date, float]]]:
    """cabin -> airline label -> {date: price}. The last run of a day wins."""
    series: dict[str, dict[str, dict[date, float]]] = defaultdict(lambda: defaultdict(dict))
    for run in sorted(history.get("runs", []), key=lambda r: r.get("checked_at", "")):
        day = date.fromisoformat(run["date"])
        by_airline = run.get("by_airline")
        if by_airline:
            for cabin, entries in by_airline.items():
                # Reset this day's entries for the cabin so the last run wins.
                for label in list(series[cabin]):
                    series[cabin][label].pop(day, None)
                for label, rec in entries.items():
                    series[cabin][_normalize(label)][day] = float(rec["price"])
        else:  # older records: only the cheapest fare, attributed to its carrier
            for cabin, rec in run.get("prices", {}).items():
                label = _normalize("/".join(rec.get("carriers", [])) or "未知航空")
                series[cabin][label][day] = float(rec["price"])
    return series


def _normalize(label: str) -> str:
    return MIXED_LABEL if "/" in label else label


def _color(label: str) -> str:
    return AIRLINE_COLORS.get(label, MIXED_COLOR)


def _style_axes(ax: plt.Axes) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.grid(axis="y", color=GRID, linewidth=1, linestyle="-")
    ax.set_axisbelow(True)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))


def draw(history: dict[str, Any], out_path: Path, currency: str = "TWD", title_note: str = "") -> bool:
    pick_cjk_font()
    series = daily_series(history)
    cabins = [c for c in CABINS if series.get(c)]
    if not cabins:
        return False

    n = len(cabins)
    fig, axes = plt.subplots(n, 1, figsize=(11, 3.6 * n), dpi=150, squeeze=False, sharex=True)
    fig.patch.set_facecolor(SURFACE)
    all_days: set[date] = set()

    for ax, cabin in zip(axes[:, 0], cabins):
        _style_axes(ax)
        lines = series[cabin]
        # Fixed legend order: palette order first, mixed last.
        order = [a for a in AIRLINE_COLORS if a in lines] + [a for a in lines if a not in AIRLINE_COLORS]
        low_val, low_day, low_label = None, None, None
        ends: list[tuple[float, date, str]] = []
        for label in order:
            pts = sorted(lines[label].items())
            xs = [d for d, _ in pts]
            ys = [p for _, p in pts]
            all_days.update(xs)
            ax.plot(
                xs, ys, color=_color(label), linewidth=2, solid_joinstyle="round", solid_capstyle="round",
                marker="o", markersize=6, markerfacecolor=_color(label), markeredgecolor=SURFACE, markeredgewidth=2,
                label=label, zorder=3,
            )
            ends.append((ys[-1], xs[-1], label))
            for d, p in pts:
                if low_val is None or p < low_val:
                    low_val, low_day, low_label = p, d, label

        ymin = min(min(v.values()) for v in lines.values())
        ymax = max(max(v.values()) for v in lines.values())
        pad = max((ymax - ymin) * 0.25, ymax * 0.06)
        ax.set_ylim(max(0, ymin - pad), ymax + pad)
        yspan = (ymax + pad) - max(0, ymin - pad)

        # Selective direct labels at the line ends: cheapest first, and skip any
        # label that would sit on top of one already placed (legend + table carry it).
        placed: list[float] = []
        for val, day, label in sorted(ends):
            if any(abs(val - p) < yspan * 0.06 for p in placed):
                continue
            placed.append(val)
            ax.annotate(f"{val:,.0f}", (day, val), xytext=(8, 0), textcoords="offset points",
                        va="center", fontsize=9, color=INK_2)

        if low_val is not None:
            ax.scatter([low_day], [low_val], s=140, facecolors="none", edgecolors=LOW_COLOR, linewidths=2, zorder=4)
            ax.annotate(
                f"▼ 歷史最低 {currency} {low_val:,.0f}（{low_label}，{low_day:%m/%d}）",
                (low_day, low_val), xytext=(6, -22), textcoords="offset points",
                ha="right", fontsize=9, color=LOW_COLOR, fontweight="bold",
            )
        ax.set_title(f"{CABINS[cabin]}", loc="left", fontsize=12, color=INK, fontweight="bold", pad=26)
        ax.set_ylabel(currency, color=MUTED, fontsize=9)
        if len(order) >= 2:  # legend sits in the band between title and plot, off the grid
            ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=min(len(order), 6), frameon=False,
                      fontsize=9, labelcolor=INK_2, handlelength=2.5, borderaxespad=0.2)

    ax_last = axes[-1, 0]
    ax_last.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    if len(all_days) <= 14:
        ax_last.xaxis.set_major_locator(mdates.DayLocator())
    else:
        ax_last.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=12))
    if len(all_days) == 1:  # a single day: give the lone points some room
        d = mdates.date2num(next(iter(all_days)))
        ax_last.set_xlim(d - 1, d + 1)
    for ax in axes[:, 0]:
        ax.margins(x=0.08)

    fig.suptitle("台北 (TPE) → 峇里島 (DPS) 來回票價走勢" + (f"　{title_note}" if title_note else ""),
                 x=0.06, ha="left", fontsize=14, color=INK, fontweight="bold")
    fig.text(0.06, 0.005, "每日 09:00 查詢 Google Flights，取各航空當日最低價；資料：data/price_history.json",
             fontsize=8, color=MUTED)
    fig.tight_layout(rect=(0, 0.02, 1, 0.96))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor=SURFACE)
    plt.close(fig)
    return True


def main() -> int:
    hist_path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/price_history.json")
    out_path = Path(sys.argv[2] if len(sys.argv) > 2 else "charts/price_trend.png")
    history = json.loads(hist_path.read_text(encoding="utf-8"))
    note = ""
    crit = history.get("criteria", "")
    if "|" in crit:
        dates = crit.split("|")[1]
        deps = sorted({p.split(">")[0] for p in dates.split(";") if ">" in p})
        rets = sorted({p.split(">")[1] for p in dates.split(";") if ">" in p})
        if deps and rets:
            note = f"（{' / '.join(d[5:] for d in deps)} 出發，{' / '.join(r[5:] for r in rets)} 回程）"
    ok = draw(history, out_path, title_note=note)
    print(f"chart written to {out_path}" if ok else "no data to plot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
