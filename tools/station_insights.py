"""Pure analytics helpers for Station Advanced Details:
1. Price Stability Analytics: 30-day change events, a deterministic 0–100 `stability_score` and a plain
   Chinese `volatility_summary` that separates benign drops / <2% rounding from frequent or sharp hikes.
2. Domain Longevity & Infrastructure Profile: age formatting, 0→3-year longevity meter, expiry risk.
3. Structured Community Reviews: data/reviews.json loader, star summary and evidence signal tags.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROUNDING_PCT = 2.0
SHARP_PCT = 20.0
EXTREME_PCT = 50.0
LONGEVITY_FULL_DAYS = 3 * 365
EXPIRY_WARN_DAYS = 60

REVIEW_SIGNALS: dict[str, tuple[str, str]] = {
    "verified": ("验真通过", "ok"),
    "billing_match": ("扣费一致", "ok"),
    "price_hike": ("暗涨预警", "danger"),
    "spoof": ("疑似套壳", "danger"),
    "rate_limit": ("限流 / 超时", "warn"),
}
_SIGNAL_ALIASES = {
    "验真通过": "verified", "verify": "verified", "verify_pass": "verified",
    "扣费一致": "billing_match", "billing": "billing_match",
    "暗涨预警": "price_hike", "暗涨": "price_hike", "hike": "price_hike",
    "疑似套壳": "spoof", "套壳": "spoof", "fake": "spoof",
    "限流": "rate_limit", "限流 / 超时": "rate_limit", "429": "rate_limit", "timeout": "rate_limit",
}


def _entry_price(v: list[Any] | None) -> float | None:
    if not v or not isinstance(v, list) or len(v) < 7:
        return None
    kind = v[0]
    cny = v[1] if kind == "token" else v[3]
    if isinstance(cny, (int, float)) and cny > 0:
        return float(cny)
    usd = v[4] if kind == "token" else v[6]
    if isinstance(usd, (int, float)) and usd > 0:
        return float(usd)
    return None


def classify_move(change: dict[str, Any]) -> tuple[str, float | None]:
    """Return (kind, pct) for one change: rounding / drop / minor_hike / sharp_hike / extreme_hike / other."""
    t = change.get("type")
    if t not in ("up", "down"):
        return "other", None
    old_p, new_p = _entry_price(change.get("old")), _entry_price(change.get("new"))
    pct = (new_p - old_p) / old_p * 100.0 if (old_p and new_p) else None
    if pct is not None and abs(pct) < ROUNDING_PCT:
        return "rounding", pct
    if t == "down" or (pct is not None and pct < 0):
        return "drop", pct
    if pct is None or pct < SHARP_PCT:
        return "minor_hike", pct
    return ("extreme_hike" if pct >= EXTREME_PCT else "sharp_hike"), pct


def stability_score(*, minor_events: int, sharp_events: int, max_up_pct: float, hike_models: int,
                    outs: int = 0, removed: int = 0) -> int:
    """Deterministic 0–100 score. Hikes are counted per refresh run (event), not per model row."""
    hike_events = minor_events + sharp_events
    penalty = min(30, 6 * minor_events) + min(45, 15 * sharp_events)
    if max_up_pct >= EXTREME_PCT:
        penalty += 12
    if hike_models >= 10:
        penalty += 8
    if hike_events >= 3:
        penalty += 6
    penalty += min(6, 2 * outs) + min(6, removed // 5)
    return max(0, min(100, 100 - penalty))


def default_stability() -> dict[str, Any]:
    return {
        "total": 0, "price_moves": 0, "ups": 0, "downs": 0, "outs": 0, "news": 0, "removed": 0,
        "rounding": 0, "minor_hikes": 0, "sharp_hikes": 0, "hike_events": 0, "sharp_events": 0, "hike_models": 0,
        "max_up_pct": 0.0, "max_down_pct": 0.0, "last_up_at": None,
        "stability_score": 100, "label": "价格平稳", "cls": "ok",
        "volatility_summary": "近 30 天未监测到公开报价变动",
        "impulses": [],
    }


def _volatility_summary(s: dict[str, Any]) -> str:
    parts: list[str] = []
    if s["hike_events"]:
        sharp = f"，其中 {s['sharp_events']} 次 ≥{SHARP_PCT:g}% 急涨" if s["sharp_events"] else ""
        parts.append(f"{s['hike_events']} 次涨价调整{sharp}（涉及 {s['hike_models']} 个型号，最大 +{s['max_up_pct']:g}%）")
    if s["downs"]:
        parts.append(f"{s['downs']} 条降价（最大 -{s['max_down_pct']:g}%）")
    if s["rounding"]:
        parts.append(f"{s['rounding']} 条 <{ROUNDING_PCT:g}% 舍入微调（不计扣分）")
    if s["news"] or s["removed"]:
        parts.append(f"上新 {s['news']} · 下架 {s['removed']}")
    if not parts:
        return "近 30 天未监测到公开报价变动"
    lead = "近 30 天：" if s["hike_events"] else "近 30 天无涨价："
    return lead + "；".join(parts)


def compute_price_stability(changes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate 30-day price change logs per station into stability score, label and summary."""
    by_dom: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in changes or []:
        dom = c.get("domain")
        if dom:
            by_dom[dom].append(c)

    out: dict[str, dict[str, Any]] = {}
    for dom, items in by_dom.items():
        s = default_stability()
        s["total"] = len(items)
        run_sev: dict[str, float] = {}
        day_moves: dict[str, list[float]] = {}
        hike_models: set[str] = set()
        max_up = max_down = 0.0
        for c in items:
            t = c.get("type")
            if t == "up":
                s["ups"] += 1
            elif t == "down":
                s["downs"] += 1
            elif t == "out":
                s["outs"] += 1
            elif t == "new":
                s["news"] += 1
            elif t == "removed":
                s["removed"] += 1
            kind, pct = classify_move(c)
            if pct is not None:
                max_up = max(max_up, pct)
                max_down = min(max_down, pct)
            day = str(c.get("run_at") or "")[:10]
            if day and pct is not None and kind in ("drop", "minor_hike", "sharp_hike", "extreme_hike"):
                dm = day_moves.setdefault(day, [0.0, 0.0])
                dm[0], dm[1] = max(dm[0], pct), min(dm[1], pct)
            if kind == "rounding":
                s["rounding"] += 1
            elif kind.endswith("_hike"):
                s["sharp_hikes" if kind != "minor_hike" else "minor_hikes"] += 1
                run = str(c.get("run_at") or "")
                run_sev[run] = max(run_sev.get(run, 0.0), pct if pct is not None else ROUNDING_PCT)
                if run > (s["last_up_at"] or ""):
                    s["last_up_at"] = run
                hike_models.add(str(c.get("model") or ""))
        s["impulses"] = [[d, round(v[0], 1), round(v[1], 1)] for d, v in sorted(day_moves.items())]
        s["price_moves"] = s["ups"] + s["downs"] + s["outs"]
        s["sharp_events"] = sum(1 for v in run_sev.values() if v >= SHARP_PCT)
        s["hike_events"] = len(run_sev)
        s["hike_models"] = len(hike_models)
        s["max_up_pct"] = round(max_up, 1)
        s["max_down_pct"] = round(abs(max_down), 1)
        s["stability_score"] = stability_score(
            minor_events=s["hike_events"] - s["sharp_events"], sharp_events=s["sharp_events"],
            max_up_pct=max_up, hike_models=s["hike_models"], outs=s["outs"], removed=s["removed"],
        )
        score = s["stability_score"]
        if not s["hike_events"]:
            s["label"] = "平稳 · 近期降价" if s["downs"] > s["rounding"] else ("价格平稳 · 仅微调" if s["rounding"] else "价格平稳")
            s["cls"] = "ok" if score >= 85 else "warn"
        elif max_up >= EXTREME_PCT or score < 60 or (s["hike_events"] >= 3 and s["sharp_events"]):
            s["label"] = "高频暗涨预警" if s["hike_events"] >= 3 else "近期有大幅涨价"
            s["cls"] = "danger"
        else:
            s["label"] = "频繁小幅上调" if s["hike_events"] >= 3 else "近期小幅上调"
            s["cls"] = "warn"
        s["volatility_summary"] = _volatility_summary(s)
        out[dom] = s
    return out


def stability_band(score: int | float | None) -> str:
    if score is None:
        return ""
    return "ok" if score >= 85 else ("warn" if score >= 60 else "danger")


def fmt_domain_age(days: int | None) -> tuple[str, str, str]:
    """Return (short_label, long_label, badge_cls) for domain registration age in days."""
    if days is None or days < 0:
        return "未知", "暂未查到公开 WHOIS/RDAP 注册日期", ""
    if days >= 365:
        yrs = days // 365
        rem_m = (days % 365) // 30
        short = f"{days / 365:.1f} 年"
        long_s = f"{yrs} 年 {rem_m} 个月（{days} 天）" if rem_m else f"{yrs} 年（{days} 天）"
        return short, long_s, "ok" if days >= 540 else "info"
    if days >= 180:
        return f"{days // 30} 个月", f"{days // 30} 个月（{days} 天）", ""
    return f"{days} 天", f"{days} 天（新站预警）", "warn"


def longevity_pct(days: int | float | None) -> int | None:
    """Domain age mapped onto a 0→3-year meter (100 = 3 years or more)."""
    if not isinstance(days, (int, float)) or isinstance(days, bool) or days < 0:
        return None
    return int(min(100, round(days / LONGEVITY_FULL_DAYS * 100)))


def _parse_day(s: Any) -> date | None:
    if not s:
        return None
    txt = str(s).strip()
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        return (dt.astimezone(timezone.utc) if dt.tzinfo else dt).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(txt[:10])
    except ValueError:
        return None


def expiry_risk(expires_at: Any, now: datetime | None) -> dict[str, Any] | None:
    """Days until domain expiry with a level: expired / soon (≤60 days) / ok. None when unparseable."""
    d = _parse_day(expires_at)
    if d is None or now is None:
        return None
    today = (now.astimezone(timezone.utc) if now.tzinfo else now).date()
    left = (d - today).days
    level = "expired" if left < 0 else ("soon" if left <= EXPIRY_WARN_DAYS else "ok")
    return {"days_left": left, "level": level}


def load_reviews(data_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Load verified structured station reviews grouped by domain from data/reviews.json."""
    p = data_dir / "reviews.json"
    if not p.exists():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    items = doc.get("reviews") if isinstance(doc, dict) else (doc if isinstance(doc, list) else [])
    by_dom: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in items or []:
        if not isinstance(r, dict):
            continue
        dom = str(r.get("domain") or "").strip().lower()
        if not dom:
            continue
        by_dom[dom].append(r)
    for dom in by_dom:
        by_dom[dom].sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    return dict(by_dom)


def review_rating(r: dict[str, Any]) -> float | None:
    try:
        v = float(r.get("rating"))
    except (TypeError, ValueError):
        return None
    return v if 1.0 <= v <= 5.0 else None


def review_signals(r: dict[str, Any]) -> list[str]:
    """Canonical signal keys present on a review (order follows REVIEW_SIGNALS, unknown tags dropped)."""
    raw = r.get("signals") or []
    if isinstance(raw, str):
        raw = [raw]
    keys = set()
    for x in raw if isinstance(raw, list) else []:
        k = str(x).strip()
        if k not in REVIEW_SIGNALS and k not in _SIGNAL_ALIASES:
            k = k.replace("：", ":").split(":", 1)[0].strip()
        k = k if k in REVIEW_SIGNALS else _SIGNAL_ALIASES.get(k, _SIGNAL_ALIASES.get(k.lower(), ""))
        if k:
            keys.add(k)
    return [k for k in REVIEW_SIGNALS if k in keys]


def review_summary(revs: list[dict[str, Any]]) -> dict[str, Any]:
    ratings = [v for v in (review_rating(r) for r in revs) if v is not None]
    counts: dict[str, int] = {}
    for r in revs:
        for k in review_signals(r):
            counts[k] = counts.get(k, 0) + 1
    avg = round(sum(ratings) / len(ratings), 1) if ratings else None
    return {
        "count": len(revs),
        "rated": len(ratings),
        "avg": avg,
        "pct": int(round(avg / 5 * 100)) if avg is not None else 0,
        "signals": [(k, REVIEW_SIGNALS[k][0], REVIEW_SIGNALS[k][1], counts[k]) for k in REVIEW_SIGNALS if k in counts],
    }
