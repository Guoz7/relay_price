"""Pure analytics helpers for Station Advanced Details:
1. Price Stability Analytics (30-day change count, hikes, drops, max hike %, stability badge)
2. Domain Longevity & Infrastructure Profile (exact age formatting, registrar, expiry, global ranks)
3. Structured Community Reviews Loader (data/reviews.json, GitHub-authenticated evidence reviews)
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


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


def compute_price_stability(changes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate 30-day price change logs per station into clear stability signals."""
    by_dom: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in changes or []:
        dom = c.get("domain")
        if dom:
            by_dom[dom].append(c)

    out: dict[str, dict[str, Any]] = {}
    for dom, items in by_dom.items():
        ups = downs = outs = news = removed = 0
        max_up_pct = 0.0
        max_down_pct = 0.0
        last_up_at = ""
        for c in items:
            t = c.get("type")
            if t == "up":
                ups += 1
                ra = str(c.get("run_at") or "")
                if ra > last_up_at:
                    last_up_at = ra
            elif t == "down":
                downs += 1
            elif t == "out":
                outs += 1
            elif t == "new":
                news += 1
            elif t == "removed":
                removed += 1
            old_p = _entry_price(c.get("old"))
            new_p = _entry_price(c.get("new"))
            if old_p and new_p and old_p > 0:
                pct = (new_p - old_p) / old_p * 100.0
                if pct > max_up_pct:
                    max_up_pct = pct
                if pct < max_down_pct:
                    max_down_pct = pct

        price_moves = ups + downs + outs
        if ups >= 5 or max_up_pct >= 50.0:
            label, cls = "近期有大幅涨价", "danger"
        elif ups >= 2 or max_up_pct >= 20.0:
            label, cls = "近期小幅上调", "warn"
        elif downs > 0 and ups == 0:
            label, cls = "平稳 · 近期降价", "ok"
        else:
            label, cls = "价格平稳", "ok"

        out[dom] = {
            "total": len(items),
            "price_moves": price_moves,
            "ups": ups,
            "downs": downs,
            "outs": outs,
            "news": news,
            "removed": removed,
            "max_up_pct": round(max_up_pct, 1),
            "max_down_pct": round(abs(max_down_pct), 1),
            "last_up_at": last_up_at or None,
            "label": label,
            "cls": cls,
        }
    return out


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
    for r in items:
        if not isinstance(r, dict):
            continue
        dom = str(r.get("domain") or "").strip().lower()
        if not dom:
            continue
        by_dom[dom].append(r)
    for dom in by_dom:
        by_dom[dom].sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    return dict(by_dom)
