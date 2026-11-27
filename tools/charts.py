"""Zero-dependency inline SVG charts (stdlib only, no JS).

Every chart is a single <svg class="ch ..."> with role="img", aria-label, <title> and a viewBox so it scales
fluidly from 375px to 1280px. Colours come from CSS classes in assets/style.css that resolve to theme
variables (var(--accent) / --ok / --warn / --danger / --ink2 / --ink3 / --line), so light and dark mode
both render crisply. Each builder returns "" when there is nothing meaningful to draw (except the price
impulse chart, which renders an explicit empty state).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from html import escape
from typing import Any, Iterable, Sequence

GRADE_BANDS = ((85, "S"), (75, "A"), (65, "B"), (50, "C"), (0, "D"))


def _e(s: Any) -> str:
    return escape(str(s), quote=True)


def _n(x: float) -> str:
    """Compact coordinate: one decimal, trailing .0 stripped."""
    v = round(float(x), 1)
    return str(int(v)) if v == int(v) else f"{v:g}"


def fmt_price(v: float | None, unit: str = "¥") -> str:
    if v is None:
        return "—"
    a = abs(v)
    if a >= 100:
        return f"{unit}{v:.0f}"
    if a >= 10:
        return f"{unit}{v:.1f}"
    if a >= 1:
        return f"{unit}{v:.2f}"
    return f"{unit}{v:.3g}"


def quantile(vals: Sequence[float], q: float) -> float | None:
    """Linear-interpolated quantile of a pre-sorted or unsorted sequence."""
    xs = sorted(vals)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def price_stats(vals: Iterable[float]) -> dict[str, float] | None:
    xs = sorted(v for v in vals if isinstance(v, (int, float)) and v > 0)
    if not xs:
        return None
    return {"min": xs[0], "p10": quantile(xs, .1), "p25": quantile(xs, .25), "p50": quantile(xs, .5),
            "p75": quantile(xs, .75), "p90": quantile(xs, .9), "max": xs[-1], "n": len(xs)}


def _svg(cls: str, w: int, h: int, label: str, body: str, detail: str = "") -> str:
    """Short accessible name in aria-label; the full data summary lives once in <title>."""
    return (f'<svg class="ch {cls}" viewBox="0 0 {w} {h}" role="img" aria-label="{_e(label)}">'
            f'<title>{_e(detail or label)}</title>{body}</svg>')


def grade_of(score: float) -> str:
    for lo, g in GRADE_BANDS:
        if score >= lo:
            return g
    return "D"


# ---------------------------------------------------------------- 1. radar
def station_radar_svg(
    dims: dict[str, float],
    market: dict[str, float] | None = None,
    *,
    labels: dict[str, str],
    order: Sequence[str],
    penalized: Iterable[str] = (),
    name: str = "本站",
) -> str:
    """7-axis trust profile: this station (filled polygon) vs market median (dashed polygon)."""
    keys = [k for k in order if isinstance(dims.get(k), (int, float))]
    if len(keys) < 3:
        return ""
    market = market or {}
    pen = set(penalized)
    cx, cy, rad = 130, 96, 62
    n = len(keys)

    def pt(i: int, v: float, r: float = rad) -> tuple[float, float]:
        a = -math.pi / 2 + 2 * math.pi * i / n
        rr = r * max(0.0, min(100.0, v)) / 100
        return cx + rr * math.cos(a), cy + rr * math.sin(a)

    def poly(vals: list[float]) -> str:
        return " ".join(f"{_n(x)},{_n(y)}" for x, y in (pt(i, v) for i, v in enumerate(vals)))

    rings = "".join(f'<polygon class="gl" points="{poly([lv] * n)}"/>' for lv in (50, 100))
    spokes = "".join(f"M{cx} {cy}L{_n(x)} {_n(y)}" for x, y in (pt(i, 100) for i in range(n)))
    body = rings + f'<path class="gl" d="{spokes}"/>'
    if all(isinstance(market.get(k), (int, float)) for k in keys):
        body += f'<polygon class="mk" points="{poly([market[k] for k in keys])}"/>'
    body += f'<polygon class="me" points="{poly([dims[k] for k in keys])}"/>'
    dots, texts = [], []
    for i, k in enumerate(keys):
        x, y = pt(i, dims[k])
        dots.append(f'<circle class="{"dw" if k in pen else "da"}" cx="{_n(x)}" cy="{_n(y)}" r="3"/>')
        lx, ly = pt(i, 100, rad + 13)
        anchor = "middle" if abs(lx - cx) < 8 else ("start" if lx > cx else "end")
        tcls = ' class="tw"' if k in pen else ""
        texts.append(f'<text x="{_n(lx)}" y="{_n(ly + 3)}" text-anchor="{anchor}"{tcls}>'
                     f'{_e(labels.get(k, k))} {dims[k]:.0f}</text>')
    legend = (f'<text class="t2" x="6" y="190">━ {_e(name)}</text>'
              f'<text class="t2" x="254" y="190" text-anchor="end">┅ 全网中位</text>')
    summary = "，".join(f"{labels.get(k, k)} {dims[k]:.0f}" + (f"（全网中位 {market[k]:.0f}）" if k in market else "") for k in keys)
    return _svg("radar", 260, 196, "七维信任画像雷达图（本站 vs 全网中位）", body + "".join(dots) + "".join(texts) + legend, summary)


# ---------------------------------------------------------------- 2. flagship price strip
def station_price_strip_svg(rows: list[dict[str, Any]], unit: str = "¥") -> str:
    """Per-model market band (P10–P90, IQR, median), official tick and this station's standard vs promo dumbbell."""
    rows = [r for r in rows if r.get("p10") and r.get("p90") and (r.get("std") or r.get("alt"))][:6]
    if not rows:
        return ""
    x0, x1, rh, top = 132, 386, 26, 6
    parts: list[str] = []
    aria: list[str] = []
    for i, r in enumerate(rows):
        y = top + i * rh + 12
        lo_b, hi_b = r["p10"] / 6, r["p90"] * 6
        cand = [v for v in (r["p10"], r["p90"], r.get("std"), r.get("alt"), r.get("official")) if v]
        lo = max(lo_b, min(cand)) / 1.25
        hi = min(hi_b, max(cand)) * 1.25

        def X(v: float, lo=lo, hi=hi) -> float:
            v = max(lo, min(hi, v))
            return x0 + (x1 - x0) * (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo))

        name = str(r["model"])[:28]
        parts.append(f'<text x="4" y="{y + 4}">{_e(name[:20])}</text>')
        bx, bx2 = X(r["p10"]), X(r["p90"])
        parts.append(f'<path class="bd" d="M{_n(bx)} {y - 4}h{_n(max(1, bx2 - bx))}v8H{_n(bx)}z"/>')
        if r.get("p25") and r.get("p75"):
            qx, qx2 = X(r["p25"]), X(r["p75"])
            parts.append(f'<path class="iq" d="M{_n(qx)} {y - 4}h{_n(max(1, qx2 - qx))}v8H{_n(qx)}z"/>')
        if r.get("p50"):
            parts.append(f'<path class="md" d="M{_n(X(r["p50"]))} {y - 7}v14"/>')
        off = r.get("official")
        if off:
            ox = _n(X(off))
            parts.append(f'<path class="of" d="M{ox} {y - 9}v18"/>')
            if off > hi or off < lo:
                parts.append(f'<text class="tof" x="{ox}" y="{y - 10}" text-anchor="middle">{"»" if off > hi else "«"}</text>')
        s_v, a_v = r.get("std"), r.get("alt")
        if s_v and a_v:
            parts.append(f'<path class="lk" d="M{_n(X(s_v))} {y}H{_n(X(a_v))}"/>')
        if a_v:
            parts.append(f'<circle class="dw" cx="{_n(X(a_v))}" cy="{y}" r="3.5"/>')
        if s_v:
            parts.append(f'<circle class="da" cx="{_n(X(s_v))}" cy="{y}" r="4"/>')
        parts.append(f'<text x="{x1 + 8}" y="{y + 4}">{fmt_price(s_v or a_v, unit)}</text>')
        txt = f"{name} 常规{fmt_price(s_v, unit)}"
        if a_v:
            txt += f"/特惠{fmt_price(a_v, unit)}"
        txt += f"/中位{fmt_price(r.get('p50'), unit)}"
        if off:
            txt += f"/官方{fmt_price(off, unit)}"
        aria.append(txt)
    h = top + len(rows) * rh + 14
    legend = (f'<text class="t2" x="4" y="{h - 4}">▬ 全网 P10–P90 · ▮ P25–P75 · │中位 · '
              f'<tspan class="tof">┃官方</tspan> · <tspan class="tac">● 本站常规</tspan> · <tspan class="tw">● 特惠/逆向</tspan>（对数轴）</text>')
    return _svg("strip", 454, h, "旗舰模型输入价定位图（每百万 token）", "".join(parts) + legend, "；".join(aria))


# ---------------------------------------------------------------- 3. price impulses
def price_impulse_svg(impulses: Sequence[Sequence[Any]], now: datetime | None = None, days: int = 30) -> str:
    """Daily impulses over the last `days`: hikes point up (scaled by %), drops point down."""
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    w, h, base, x0, x1 = 300, 64, 32, 8, 292
    hk, hm, dp = [], [], []
    n_up = n_dn = 0
    for item in impulses or []:
        try:
            d = date.fromisoformat(str(item[0])[:10])
        except ValueError:
            continue
        off = (today - d).days
        if off < 0 or off >= days:
            continue
        x = _n(x1 - (x1 - x0) * off / (days - 1))
        up, dn = float(item[1] or 0), float(item[2] or 0)
        if up > 0:
            n_up += 1
            ln = 4 + 22 * math.sqrt(min(up, 150) / 150)
            (hk if up >= 20 else hm).append(f"M{x} {base}V{_n(base - ln)}")
        if dn < 0:
            n_dn += 1
            ln = 4 + 22 * math.sqrt(min(-dn, 100) / 100)
            dp.append(f"M{x} {base}V{_n(base + ln)}")
    body = f'<path class="b0" d="M{x0} {base}H{x1}"/>'
    for cls, segs in (("hk", hk), ("hm", hm), ("dp", dp)):
        if segs:
            body += f'<path class="{cls}" d="{"".join(segs)}"/>'
    if not (n_up or n_dn):
        body += f'<text class="t2" x="150" y="{base - 6}" text-anchor="middle">近 {days} 天无调价事件</text>'
    body += (f'<text class="t2" x="{x0}" y="{h - 2}">{days} 天前</text>'
             f'<text class="t2" x="{x1}" y="{h - 2}" text-anchor="end">今天</text>')
    label = f"近 {days} 天调价脉冲：{n_up} 天涨价（向上，红色为 ≥20% 急涨），{n_dn} 天降价（向下）"
    return _svg("imp", w, h, label, body)


# ---------------------------------------------------------------- 4. histogram
def score_histogram_svg(totals: Sequence[float], *, median: float | None = None, cap: float = 74.0, bin_w: int = 5) -> str:
    """Market score distribution coloured by grade band with median and Stage 1 cap markers."""
    vals = [float(t) for t in totals if isinstance(t, (int, float))]
    if not vals:
        return ""
    nb = 100 // bin_w
    counts = [0] * nb
    for v in vals:
        counts[min(nb - 1, max(0, int(v // bin_w)))] += 1
    w, h, x0, x1, yb, ht = 480, 160, 24, 470, 128, 92
    peak = max(counts) or 1

    def X(s: float) -> float:
        return x0 + (x1 - x0) * s / 100

    bars: dict[str, list[str]] = {}
    labels = []
    bw = (x1 - x0) / nb
    for i, c in enumerate(counts):
        if not c:
            continue
        bh = max(1.5, ht * c / peak)
        x = X(i * bin_w) + 1
        bars.setdefault(grade_of(i * bin_w), []).append(f"M{_n(x)} {yb}v{_n(-bh)}h{_n(bw - 2)}v{_n(bh)}z")
        labels.append(f'<text x="{_n(x + bw / 2 - 1)}" y="{_n(yb - bh - 3)}">{c}</text>')
    body = f'<path class="b0" d="M{x0} {yb}H{x1}"/>'
    body += "".join(f'<path class="g{g}" d="{"".join(p)}"/>' for g, p in bars.items())
    ticks = "".join(f'<text x="{_n(X(t))}" y="{yb + 13}">{t}</text>' for t in (0, 50, 65, 75, 85, 100))
    body += f'<g class="t2" text-anchor="middle">{"".join(labels)}{ticks}</g>'
    cx = _n(X(cap))
    body += f'<path class="cl" d="M{cx} 12V{yb}"/><text class="tcl" x="{cx}" y="10" text-anchor="middle">Stage 1 封顶 {cap:g}</text>'
    if median is not None:
        mx = _n(X(median))
        body += f'<path class="ml" d="M{mx} 24V{yb}"/><text x="{mx}" y="22" text-anchor="end">中位 {median:.0f}</text>'
    body += f'<text class="t2" x="{x1}" y="{h - 3}" text-anchor="end">D &lt;50 · C 50–64 · B 65–74 · A 75–84 · S ≥85</text>'
    label = (f"全网 {len(vals)} 家站点信誉分分布（每 {bin_w} 分一档）" + (f"，中位 {median:.1f}" if median is not None else "")
             + f"，Stage 1 未实测封顶 {cap:g}")
    return _svg("hist", w, h, label, body)


# ---------------------------------------------------------------- 5. model page spread
def model_price_dist_svg(
    stats: dict[str, float] | None,
    *,
    official: float | None = None,
    rec: float | None = None,
    alt_median: float | None = None,
    unit: str = "¥",
    what: str = "输入价 / 百万 token",
) -> str:
    """Ultra-compact standard-lane spread (min · IQR · median · P90) with official, recommended and alt-lane markers."""
    if not stats or not stats.get("n") or stats["n"] < 3:
        return ""
    x0, x1, y = 10, 390, 22
    cand = [v for v in (stats["min"], stats["p90"], official, rec, alt_median) if v]
    lo = max(stats["min"] / 1.15, min(cand) / 1.15, stats["p10"] / 8)
    hi = min(max(cand) * 1.15, stats["p90"] * 8)
    if hi <= lo:
        hi = lo * 2

    def X(v: float) -> str:
        v = max(lo, min(hi, v))
        return _n(x0 + (x1 - x0) * (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo)))

    body = (f'<path class="wk" d="M{X(stats["min"])} {y}H{X(stats["p90"])}"/>'
            f'<rect class="iq" x="{X(stats["p25"])}" y="{y - 6}" width="{_n(max(1.0, float(X(stats["p75"])) - float(X(stats["p25"]))))}" height="12" rx="2"/>'
            f'<path class="md" d="M{X(stats["p50"])} {y - 9}V{y + 9}"/>')
    if official:
        body += f'<path class="of" d="M{X(official)} {y - 11}V{y + 11}"/>'
    if alt_median:
        body += f'<circle class="dw" cx="{X(alt_median)}" cy="{y}" r="3.5"/>'
    if rec:
        body += f'<circle class="do" cx="{X(rec)}" cy="{y}" r="4.5"/>'
    p = lambda v: fmt_price(v, unit)  # noqa: E731
    line = f'最低 {p(stats["min"])} · P25 {p(stats["p25"])} · 中位 {p(stats["p50"])} · P75 {p(stats["p75"])} · P90 {p(stats["p90"])}'
    body += f'<text x="{x0}" y="48">{_e(line)}</text>'
    leg = []
    if official:
        leg.append(f'<tspan class="tof">┃官方 {p(official)}</tspan>')
    if rec:
        leg.append(f'<tspan class="tok">● 推荐 {p(rec)}</tspan>')
    if alt_median:
        leg.append(f'<tspan class="tw">● 特惠/逆向中位 {p(alt_median)}</tspan>')
    leg.append(f'{stats["n"]} 站 · 对数轴')
    body += f'<text class="t2" x="{x0}" y="62">{" · ".join(leg)}</text>'
    label = f"常规按量{what}分布：{line}" + (f"；官方 {p(official)}" if official else "") + (f"；推荐 {p(rec)}" if rec else "") + (
        f"；特惠/逆向中位 {p(alt_median)}" if alt_median else "")
    return _svg("mdist", 400, 66, label, body)


# ---------------------------------------------------------------- 6. consumer dashboard (heartbeat / latency / step price)
_CST_OFFSET_H = 8


def ts_label(ts: str) -> str:
    """Ledger hour stamp "2026-11-26T12:00Z" -> Beijing time "11-26 20:00"."""
    try:
        dt = datetime.strptime(str(ts)[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return str(ts)
    from datetime import timedelta

    return (dt + timedelta(hours=_CST_OFFSET_H)).strftime("%m-%d %H:%M")


def _epoch(ts: str) -> float | None:
    try:
        return datetime.strptime(str(ts)[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _fin(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def heartbeat_svg(points: Sequence[Sequence[Any]]) -> str:
    """One bar per scheduled check: green = 在线, red = 异常, grey = 未巡检. points = [(ts, ok_01|None, ms|None), ...]."""
    pts = [p for p in points if p and p[0]]
    if not pts:
        return ""
    w, h, top, bh = 600, 46, 2, 30
    n = len(pts)
    bw = w / n
    gap = 1.0 if bw >= 3 else 0.0
    groups: dict[str, list[str]] = {"hb-ok": [], "hb-bad": [], "hb-na": []}
    n_ok = n_bad = 0
    for i, (ts, ok, ms) in enumerate((p[0], p[1] if len(p) > 1 else None, p[2] if len(p) > 2 else None) for p in pts):
        cls = "hb-ok" if ok == 1 else ("hb-bad" if ok == 0 else "hb-na")
        n_ok += ok == 1
        n_bad += ok == 0
        state = "在线" if ok == 1 else ("打不开" if ok == 0 else "未巡检")
        tip = f"{ts_label(ts)} · {state}" + (f" · 响应 {int(ms)}ms" if (ok == 1 and _fin(ms)) else "")
        groups[cls].append(f'<rect x="{_n(i * bw)}" y="{top}" width="{_n(max(0.6, bw - gap))}" height="{bh}" rx="1" data-tip="{_e(tip)}"/>')
    body = "".join(f'<g class="{c}">{"".join(r)}</g>' for c, r in groups.items() if r)
    body += (f'<text class="t2" x="0" y="{h - 2}">{_e(ts_label(pts[0][0]))}</text>'
             f'<text class="t2" x="{w}" y="{h - 2}" text-anchor="end">最近一次 {_e(ts_label(pts[-1][0]))}</text>')
    label = f"近 {n} 次巡检：{n_ok} 次在线，{n_bad} 次打不开"
    return _svg("hb", w, h, label, body)


def latency_sparkline_svg(points: Sequence[Sequence[Any]]) -> str:
    """Response-time sparkline over successful checks (values winsorized at 1.25x P90 so one stall never flattens the line)."""
    pts = [(p[0], float(p[2])) for p in points if len(p) > 2 and p[1] == 1 and _fin(p[2]) and p[2] >= 0]
    if len(pts) < 2:
        return ""
    w, h, y0, y1 = 600, 54, 6, 40
    vals = [v for _, v in pts]
    p90 = quantile(vals, .9) or max(vals)
    hi = max(1.0, p90 * 1.25)
    lo = 0.0
    med = quantile(vals, .5) or 0.0
    n = len(pts)

    def X(i: int) -> float:
        return w * i / (n - 1)

    def Y(v: float) -> float:
        return y1 - (y1 - y0) * (min(v, hi) - lo) / (hi - lo)

    line = " ".join(f"{_n(X(i))},{_n(Y(v))}" for i, (_, v) in enumerate(pts))
    dots = "".join(
        f'<circle cx="{_n(X(i))}" cy="{_n(Y(v))}" r="5" data-tip="{_e(f"{ts_label(ts)} · 响应 {int(v)}ms" + ("（偏慢）" if v > p90 else ""))}"/>'
        for i, (ts, v) in enumerate(pts)
    )
    my = _n(Y(med))
    body = (f'<path class="lt-md" d="M0 {my}H{w}"/><polyline class="lt" points="{line}"/><g class="lt-hit">{dots}</g>'
            f'<text class="t2" x="0" y="{h - 2}">平时中位 {int(med)}ms</text>'
            f'<text class="t2" x="{w}" y="{h - 2}" text-anchor="end">偏慢时 {int(p90)}ms</text>')
    return _svg("lat", w, h, f"接口响应：平时中位 {int(med)}ms，偏慢时 {int(p90)}ms（{n} 次成功巡检）", body)


def price_step_svg(points: Sequence[Sequence[Any]], ref: Sequence[Sequence[Any]] | None = None, *,
                   ref_label: str = "同行均价", unit: str = "¥", what: str = "输入价 / 百万 token") -> str:
    """Step-after price line (price holds until the next snapshot) with a dashed reference line and hover hit zones."""
    pts = sorted(((ts, float(v)) for ts, v in points if _fin(v) and v > 0 and _epoch(ts) is not None), key=lambda x: x[0])
    if len(pts) < 2:
        return ""
    refs = sorted(((ts, float(v)) for ts, v in (ref or []) if _fin(v) and v > 0 and _epoch(ts) is not None), key=lambda x: x[0])
    w, h, x0, x1, y0, y1 = 600, 150, 46, 594, 10, 122
    t0 = min(_epoch(pts[0][0]), _epoch(refs[0][0]) if refs else _epoch(pts[0][0]))
    t1 = max(_epoch(pts[-1][0]), _epoch(refs[-1][0]) if refs else _epoch(pts[-1][0]))
    if t1 <= t0:
        t1 = t0 + 3600
    vals = [v for _, v in pts] + [v for _, v in refs]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.12 if hi > lo else max(hi * 0.1, 1e-6)
    lo, hi = max(0.0, lo - pad), hi + pad

    def X(ts: str) -> float:
        return x0 + (x1 - x0) * ((_epoch(ts) or t0) - t0) / (t1 - t0)

    def Y(v: float) -> float:
        return y1 - (y1 - y0) * (v - lo) / (hi - lo)

    def step(series: list[tuple[str, float]]) -> str:
        d = f"M{_n(X(series[0][0]))} {_n(Y(series[0][1]))}"
        for ts, v in series[1:]:
            d += f"H{_n(X(ts))}V{_n(Y(v))}"
        return d + f"H{x1}"

    ref_at = dict(refs)
    body = "".join(f'<path class="gl" d="M{x0} {_n(Y(v))}H{x1}"/><text class="t2" x="{x0 - 4}" y="{_n(Y(v) + 3)}" text-anchor="end">{_e(fmt_price(v, unit))}</text>'
                   for v in (lo + (hi - lo) * f for f in (0.15, 0.5, 0.85)))
    if refs:
        body += f'<path class="ps-ref" d="{step(refs)}"/>'
    body += f'<path class="ps" d="{step(pts)}"/>'
    hits = []
    last_ref = None
    for i, (ts, v) in enumerate(pts):
        nx = X(pts[i + 1][0]) if i + 1 < len(pts) else x1
        r = ref_at.get(ts, last_ref)
        last_ref = r
        tip = f"{ts_label(ts)} · 本站 {fmt_price(v, unit)}"
        if r:
            d = (v / r - 1) * 100
            tip += f" · {ref_label} {fmt_price(r, unit)} · " + ("和同行差不多" if abs(d) < 3 else (f"比同行便宜 {-d:.0f}%" if d < 0 else f"比同行贵 {d:.0f}%"))
        hits.append(f'<rect x="{_n(X(ts))}" y="{y0}" width="{_n(max(2.0, nx - X(ts)))}" height="{y1 - y0}" fill="transparent" data-tip="{_e(tip)}"/>')
        if i == 0 or v != pts[i - 1][1]:
            hits.append(f'<circle class="ps-pt" cx="{_n(X(ts))}" cy="{_n(Y(v))}" r="3"/>')
    body += f'<g class="ps-hit">{"".join(hits)}</g>'
    body += (f'<text class="t2" x="{x0}" y="{h - 12}">{_e(ts_label(pts[0][0]))}</text>'
             f'<text class="t2" x="{x1}" y="{h - 12}" text-anchor="end">{_e(ts_label(pts[-1][0]))}</text>'
             f'<text class="t2" x="{x0}" y="{h - 1}"><tspan class="tac">━ 本站</tspan>'
             + (f' · <tspan class="tref">┅ {_e(ref_label)}</tspan>' if refs else "") + f' · {_e(what)}</text>')
    first, last = pts[0][1], pts[-1][1]
    trend = "持平" if abs(last - first) < 1e-9 else ("下降" if last < first else "上涨")
    label = f"本站价格走势（{len(pts)} 次快照）：{fmt_price(first, unit)} → {fmt_price(last, unit)}，整体{trend}"
    return _svg("pstep", w, h, label, body)
