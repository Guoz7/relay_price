"""Consumer dashboard HTML for ordinary AI users (stdlib only, no JS libraries).

Layer 1 of every station page (verdict strip, 30-day heartbeat + price trend, peer comparison, flagship price bars)
and the /stations/ 「省钱 vs 靠谱」 bubble map. All interactivity (tabs, hover tips, bubble map) lives in assets/app.js.
"""
from __future__ import annotations

import json
import math
import statistics
from html import escape
from typing import Any, Iterable, Sequence

import charts as CH
import consumer as C

MIN_CURVE_POINTS = 5


def E(s: Any) -> str:
    return escape(str(s), quote=True)


def _fin(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def money(v: float | None) -> str:
    return CH.fmt_price(v) if _fin(v) else "—"


def inline_json(obj: Any) -> str:
    """JSON safe to embed inside <script type="application/json"> (no `</script>` break-out)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def tabs(prefix: str, items: Sequence[tuple[str, str]], label: str) -> str:
    """Accessible tab strip + panels. items = [(tab label, panel html)]; behaviour wired by app.js `[data-tabs]`."""
    if not items:
        return ""
    btns, panels = [], []
    for i, (lbl, html) in enumerate(items):
        tid, pid = f"{prefix}-t{i}", f"{prefix}-p{i}"
        on = i == 0
        btns.append(f'<button type="button" role="tab" id="{tid}" aria-controls="{pid}" aria-selected="{"true" if on else "false"}"'
                    f' tabindex="{0 if on else -1}"{" class=\"on\"" if on else ""}>{E(lbl)}</button>')
        panels.append(f'<div role="tabpanel" id="{pid}" aria-labelledby="{tid}" class="tab-panel"{"" if on else " hidden"}>{html}</div>')
    return (f'<div class="tabs" data-tabs><div class="tab-list" role="tablist" aria-label="{E(label)}">{"".join(btns)}</div>'
            + "".join(panels) + "</div>")


# ---------------------------------------------------------------- 1. verdict strip
def verdict_card(*, grade_html: str, score: float, headline: str, greens: Iterable[str], warns: Iterable[tuple[str, str]],
                 chips: Iterable[tuple[str, str]], cap_note: str = "") -> str:
    g_html = "".join(f'<span class="lt ok">{E(t)}</span>' for t in greens)
    w_html = "".join(f'<li class="lt {E(cls)}">{E(t)}</li>' for t, cls in warns)
    c_html = "".join(f'<span class="app-chip" data-tip="{E(tip)}" tabindex="0">{E(lbl)}</span>' for lbl, tip in chips)
    return (
        '<section class="verdict" aria-label="体检结论">'
        f'<div class="vd-main">{grade_html}<b class="vd-score">{score:.0f}</b><span class="vd-sub">综合靠谱分</span>'
        + (f'<small class="vd-cap">{E(cap_note)}</small>' if cap_note else "") + '</div>'
        '<div class="vd-body">'
        f'<p class="vd-price">{E(headline)}</p>'
        + (f'<div class="vd-tags">{g_html}</div>' if g_html else "")
        + (f'<ul class="vd-warn">{w_html}</ul>' if w_html else "")
        + (f'<div class="vd-apps"><span class="muted">能直接用在</span>{c_html}</div>' if c_html else
           '<div class="vd-apps"><span class="muted">站点未声明支持的接口类型，接入前请先看站点文档</span></div>')
        + '</div></section>'
    )


# ---------------------------------------------------------------- 2a. heartbeat & response card
def heartbeat_card(series: Sequence[Sequence[Any]]) -> str:
    checks = [(p[0], p[1], p[2]) for p in series if len(p) > 2 and p[1] in (0, 1)]
    n = len(checks)
    head = ('<div class="dash-h"><h3>站点在线心跳与接口响应</h3>'
            '<p class="muted">站内每 6 小时自动巡检公开接口（反映站点是否常打不开，不等同于模型回复速度）</p></div>')
    if not n:
        return (f'<div class="dash-card hb-card">{head}<p class="dash-note">还没有巡检记录，下一次定时巡检后会自动出现。</p></div>')
    n_ok = sum(1 for c in checks if c[1] == 1)
    ms = [float(c[2]) for c in checks if c[1] == 1 and _fin(c[2])]
    speed = ""
    if ms:
        p90 = CH.quantile(ms, .9) or max(ms)
        speed = f" · 平时中位 {statistics.median(ms):.0f}ms · 偏慢时 {p90:.0f}ms"
    summary = f'<p class="dash-kpi"><b>{n_ok / n:.0%}</b> 在线（{n_ok}/{n} 次巡检）{E(speed)}</p>'
    if n < MIN_CURVE_POINTS:
        pills = "".join(
            f'<span class="pulse {"ok" if ok == 1 else "bad"}" tabindex="0" data-tip="{E(CH.ts_label(ts) + (" · 在线" if ok == 1 else " · 打不开") + (f" · 响应 {int(m)}ms" if (ok == 1 and _fin(m)) else ""))}">'
            f'{E(CH.ts_label(ts))} {"在线" if ok == 1 else "打不开"}</span>'
            for ts, ok, m in checks[-8:]
        )
        return (f'<div class="dash-card hb-card">{head}{summary}<div class="pulse-strip">{pills}</div>'
                f'<p class="dash-note">已连续记录 {n} 次巡检，正随每 6 小时定时巡检自动累积 30 天曲线</p></div>')
    return (f'<div class="dash-card hb-card">{head}{summary}'
            f'<div class="chart-box" data-tipbox>{CH.heartbeat_svg(checks)}</div>'
            f'<div class="chart-box" data-tipbox>{CH.latency_sparkline_svg(checks)}</div>'
            '<p class="dash-note">绿色 = 在线，红色 = 打不开；把鼠标放在色条上可看具体时间和响应毫秒数。</p></div>')


# ---------------------------------------------------------------- 2b. tracked-model price trend card
def _cmp_rows(mine: float | None, peer: float | None, off: float | None, peer_label: str) -> str:
    """Three short horizontal bars: 本站 / 同行均价 / 官方原价 (winsorized at 2x the anchor)."""
    anchor = off or peer or mine
    if not _fin(anchor) or anchor <= 0:
        return '<p class="muted">暂无可比价格</p>'
    axis = anchor * 2
    out = []
    for lbl, v, cls in (("本站", mine, "me"), (peer_label, peer, "peer"), ("官方原价", off, "off")):
        if not _fin(v) or v <= 0:
            continue
        over = v > axis
        wpct = 100.0 if over else max(2.0, 100.0 * v / axis)
        tip = f"{lbl} {money(v)} / 百万 token" + (f" · {C.fmt_official_discount(v / off * 100)}" if (off and lbl != "官方原价") else "")
        tag = (f' <span class="badge danger">高于官网 {v / off:.1f} 倍</span>' if (over and off) else
               (' <span class="badge danger">&gt;2× 同行价</span>' if over else ""))
        out.append(f'<div class="pbar" data-tip="{E(tip)}"><span class="pb-l">{E(lbl)}</span>'
                   f'<span class="pb-track"><i class="pb-fill {cls}" style="width:{wpct:.0f}%"></i></span>'
                   f'<span class="pb-v">{E(money(v))}{tag}</span></div>')
    return '<div class="pbars">' + "".join(out) + "</div>"


def price_trend_card(models: Sequence[dict[str, Any]], tracked_labels: Iterable[str] = ()) -> str:
    """models = [{model, label, points: [(ts, price)], ref: [(ts, price)], ref_label, mine, peer, official, events: [(cls, text)]}]."""
    head = ('<div class="dash-h"><h3>热门模型历史价格走势</h3>'
            '<p class="muted">阶梯线表示价格一直保持到下一次调整；虚线是同行价格参考</p></div>')
    if not models:
        names = "、".join(tracked_labels)
        return (f'<div class="dash-card pt-card">{head}<p class="dash-note">本站暂未上架热门追踪模型'
                f'{("（" + E(names) + "）") if names else ""}，可在下方完整价格表查看全部型号。</p></div>')
    items = []
    for m in models:
        pts = m.get("points") or []
        evs = m.get("events") or []
        ev_html = ("".join(f'<span class="badge {E(c)}">{E(t)}</span>' for c, t in evs[:4]) if evs
                   else '<span class="badge ok">近 30 天暂无调价波动</span>')
        svg = CH.price_step_svg(pts, m.get("ref"), ref_label=m.get("ref_label") or "同行均价") if len(pts) >= MIN_CURVE_POINTS else ""
        if svg:
            pos = [v for _, v in pts if _fin(v) and v > 0]
            first, last = pos[0], pos[-1]
            pct = (last / first - 1) * 100
            moved = "持平" if abs(last - first) < 1e-9 else (f"下降 {-pct:.0f}%" if last < first else f"上涨 {pct:.0f}%")
            if not evs and abs(pct) >= 2:
                cls = "warn" if pct > 0 else "ok"
                ev_html = f'<span class="badge {cls}">时序监测：较期初{"上涨" if pct > 0 else "下降"} {abs(pct):.0f}%</span>'
            body = (f'<div class="chart-box" data-tipbox>{svg}</div>'
                    f'<p class="dash-kpi">近 {len(pts)} 次快照：{E(money(first))} → <b>{E(money(last))}</b>（{E(moved)}）</p>'
                    f'<div class="bd">{ev_html}</div>')
        else:
            body = (_cmp_rows(m.get("mine"), m.get("peer"), m.get("official"), m.get("ref_label") or "同行均价")
                    + f'<div class="bd">{ev_html}</div>'
                    + (f'<p class="dash-note">已记录 {len(pts)} 次快照，满 {MIN_CURVE_POINTS} 次自动展开 30 天阶梯曲线</p>'
                       if len(pts) < MIN_CURVE_POINTS else '<p class="dash-note">该型号近期价格为免费或未公开，暂不画走势曲线</p>'))
        items.append((m["label"], body))
    return f'<div class="dash-card pt-card">{head}{tabs("pt", items, "选择模型")}</div>'


def dashboard(heartbeat_html: str, trend_html: str) -> str:
    return (f'<section class="dash" aria-label="近 30 天动态"><h2 class="dash-title">近 30 天动态</h2>'
            f'<div class="dash-grid">{heartbeat_html}{trend_html}</div></section>')


# ---------------------------------------------------------------- 3. peer comparison
def _bar(pct: float | None, cls: str, text: str, tip: str = "") -> str:
    v = 0.0 if not _fin(pct) else max(0.0, min(100.0, float(pct)))
    tip_attr = f' data-tip="{E(tip)}"' if tip else ""
    return (f'<div class="pk-m"{tip_attr}><span class="pk-track"><i class="{cls}" style="width:{v:.0f}%"></i></span>'
            f'<span class="pk-v">{text}</span></div>')


def peer_pk(rows: Sequence[dict[str, Any]], *, cheaper: dict[str, Any] | None = None, safer: dict[str, Any] | None = None,
            compare_href: str = "/compare/") -> str:
    """rows[0] is this station. Each row: dom, name, grade_html, score, disc, delta, delta_cls, age_txt, age_pct, stab, uptime."""
    if len(rows) < 2:
        return ""
    heads = ('<div class="pk-row pk-head" aria-hidden="true"><span>站点</span><span>综合靠谱分</span><span>价格（约官网几折）</span>'
             '<span>开站时长</span><span>防暗涨 / 在线率</span></div>')
    out = []
    for r in rows:
        me = r.get("me")
        up = r.get("uptime")
        up_txt = f"在线 {up:.0f}%" if _fin(up) else "在线率累积中"
        name = (f'<b>{E(r["name"])}</b><span class="badge info">本站</span>' if me
                else f'<a href="/site/{E(r["dom"])}/">{E(r["name"])}</a>')
        out.append(
            f'<div class="pk-row{" me" if me else ""}">'
            f'<div class="pk-name">{name}<small>{E(r["dom"])}</small></div>'
            + _bar(r.get("score"), "sc", f'{r.get("grade_html", "")} <b>{r.get("score", 0):.0f}</b>', f'综合靠谱分 {r.get("score", 0):.1f} / 100')
            + f'<div class="pk-m pk-price"><span class="badge">{E(r.get("disc") or "—")}</span>'
            + ("" if me else f'<small class="{E(r.get("delta_cls") or "")}">{E(r.get("delta") or "")}</small>') + '</div>'
            + _bar(r.get("age_pct"), "age", E(r.get("age_txt") or "未知"), "满格 = 开站 3 年以上")
            + _bar(r.get("stab"), "stab", f'防暗涨 {int(r.get("stab") or 0)} · {E(up_txt)}', "防暗涨分 0–100：近 30 天涨价次数越多、幅度越大，分越低")
            + '</div>'
        )
    alts = []
    for kind, a in (("同类更省钱备选", cheaper), ("同类更稳备选", safer)):
        if a:
            alts.append(f'<a class="pk-alt" href="/site/{E(a["dom"])}/"><span class="pk-alt-k">{E(kind)}</span>'
                        f'<b>{E(a["name"])}</b><small>{E(a["why"])}</small></a>')
    return (
        '<section class="peer-pk" aria-labelledby="pk-h"><h2 id="pk-h">同级别同行直接对比 <small>帮你看清这家在同行里的位置</small></h2>'
        '<p class="muted">按“卖的型号重合度、价格水平、靠谱分”自动挑出 4–5 家相近的站，同一品牌的镜像站只保留一家。</p>'
        f'<div class="pk-list">{heads}{"".join(out)}</div>'
        + (f'<div class="pk-alts">{"".join(alts)}</div>' if alts else "")
        + f'<p><a class="btn sm primary" href="{E(compare_href)}">去对比页深度 PK →</a></p></section>'
    )


# ---------------------------------------------------------------- 4. flagship consumer price bars
def price_bars(rows: Sequence[dict[str, Any]]) -> str:
    """rows = [{label, chat: (mine, peer, official), cache: (mine, peer, official), peer_label}]."""
    if not rows:
        return ""

    def block(key: str) -> str:
        parts = []
        for r in rows:
            mine, peer, off = r.get(key) or (None, None, None)
            if not _fin(mine):
                continue
            badge = ""
            if _fin(off) and off > 0:
                pct = mine / off * 100
                cls = "ok" if pct < 95 else ("" if pct <= 105 else "danger")
                badge = f'<span class="badge {cls}">{E(C.fmt_official_discount(pct))}</span>'
            elif mine == 0:
                badge = '<span class="badge ok">免费</span>'
            parts.append(f'<div class="pbar-row"><div class="pbar-h"><b>{E(r["label"])}</b>{badge}</div>'
                         f'{_cmp_rows(mine, peer, off, r.get("peer_label") or "同行均价")}</div>')
        return ("".join(parts) or '<p class="muted">本站热门模型暂未公开这项价格。</p>')

    chat = block("chat")
    cache = block("cache")
    return (
        '<section class="chart-card price-bars" aria-labelledby="pb-h"><h2 id="pb-h">热门模型一眼比价 <small>本站价 vs 同行均价 vs 官方原价（¥ / 百万 token）</small></h2>'
        + tabs("pb", [("日常聊天价", chat), ("长对话缓存价", cache)], "价格类型")
        + '<p class="muted">日常聊天价按“输入 3 : 输出 1”混合计算；长对话缓存价是重复内容命中缓存后的价格，写代码、长对话时最省钱。条形超过官方价 2 倍时截断并标出倍数。</p></section>'
    )


# ---------------------------------------------------------------- /stations/ bubble map
BUBBLE_COLS = ["d", "n", "g", "s", "x", "o", "a", "u", "m", "f"]


def bubble_payload(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """rows: dom, name, grade, score, cheaper_pct, off_pct, age_days, uptime, n_models, facets — positional for size."""
    out = []
    for r in rows:
        if r.get("cheaper_pct") is None:
            continue
        out.append([r["dom"], str(r.get("name") or r["dom"])[:24], r.get("grade") or "—", round(float(r.get("score") or 0), 1),
                    round(float(r["cheaper_pct"]), 1), (round(float(r["off_pct"]), 1) if _fin(r.get("off_pct")) else None),
                    (int(r["age_days"]) if _fin(r.get("age_days")) and r["age_days"] >= 0 else None),
                    (int(round(r["uptime"])) if _fin(r.get("uptime")) else None), int(r.get("n_models") or 0), r.get("facets") or ""])
    return {"cols": BUBBLE_COLS, "rows": out}


def bubble_section(payload: dict[str, Any], n_total: int) -> str:
    n = len(payload.get("rows") or [])
    return (
        '<section class="bubble-card" aria-labelledby="bb-h">'
        '<h2 id="bb-h">省钱 vs 靠谱 分布图 <small>每个点是一家站，右上角最值得先看</small></h2>'
        '<div class="bubble-wrap" id="bubble-wrap">'
        '<svg id="bubble-chart" class="bubble" role="img" tabindex="0" viewBox="0 0 800 460" '
        f'aria-label="{n} 家有统一比价基线的站点：横轴是比全网多少同行便宜，纵轴是综合靠谱分。聚焦后用方向键浏览，回车打开站点"></svg>'
        '<div class="bb-card" id="bb-card" role="status" aria-live="polite" hidden></div></div>'
        f'<p class="muted bb-legend">横轴：比全网多少比例的同行便宜，越靠右越省钱；纵轴：综合靠谱分，越高越靠谱；点越大，卖的型号越多。'
        f'共画出 {n} 家，另有 {max(0, n_total - n)} 家没有统一比价基线未画入。点一下圆点可打开站点档案。</p>'
        f'<noscript><p class="muted">分布图需要启用 JavaScript，下方排行表不受影响。</p></noscript>'
        f'<script type="application/json" id="bubble-data">{inline_json(payload)}</script>'
        '</section>'
    )
