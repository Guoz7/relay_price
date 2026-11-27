"""Consumer-facing station metrics (stdlib only).

Plain-language price anchors for ordinary AI users: every priced station gets
- `mkt_idx`: 10%-trimmed geometric mean of (station standard-lane input price / market median) x 100
- `off_pct`: the same index against the official list price (`in_usd * usd_cny_rate`)
- `cheaper_pct`: 0-100, share of other priced stations that are strictly more expensive
- per-family indices (openai / claude / gemini / deepseek)
plus B_hybrid peer matching with brand / mirror dedup and two actionable alternatives.
"""
from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from typing import Any, Iterable

TRIM = 0.10
MIN_MARKET_STATIONS = 3
FAMILY_KEYS = {"openai": "gpt", "claude": "claude", "gemini": "gemini", "deepseek": "deepseek"}


def trimmed_geomean(vals: Iterable[float], trim: float = TRIM) -> float | None:
    """Geometric mean after dropping the lowest and highest `trim` share (at least one value always survives)."""
    xs = sorted(float(v) for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 and math.isfinite(v))
    if not xs:
        return None
    k = int(len(xs) * trim)
    if k and len(xs) - 2 * k >= 1:
        xs = xs[k:len(xs) - k]
    return math.exp(sum(math.log(v) for v in xs) / len(xs))


def market_index(prices: dict[str, float], ref: dict[str, float]) -> float | None:
    """Trimmed geometric mean of price / reference x 100 over the models both sides know (None when no overlap)."""
    ratios = [p / ref[m] * 100 for m, p in prices.items()
              if isinstance(p, (int, float)) and p > 0 and isinstance(ref.get(m), (int, float)) and ref[m] > 0]
    g = trimmed_geomean(ratios)
    return round(g, 2) if g is not None else None


def model_medians(station_prices: dict[str, dict[str, float]], min_n: int = MIN_MARKET_STATIONS) -> dict[str, float]:
    """Market median per model across stations (one price per station); models carried by < min_n stations are dropped."""
    by_model: dict[str, list[float]] = defaultdict(list)
    for prices in station_prices.values():
        for m, p in prices.items():
            if isinstance(p, (int, float)) and p > 0:
                by_model[m].append(float(p))
    return {m: statistics.median(v) for m, v in by_model.items() if len(v) >= min_n}


# ---------------------------------------------------------------- per-station price metrics
def station_std_prices(qs: Iterable[Any]) -> dict[str, float]:
    """Cheapest standard-lane token input price (¥ / 1M) per canonical model; free, welfare and anomalous rows ignored."""
    out: dict[str, float] = {}
    for q in qs:
        if q.kind != "token" or q.lane != "standard" or q.free or q.welfare or q.anomaly:
            continue
        v = q.input_cny
        if not isinstance(v, (int, float)) or v <= 0 or not math.isfinite(v):
            continue
        if q.canonical not in out or v < out[q.canonical]:
            out[q.canonical] = float(v)
    return out


def official_ref(specs: dict[str, dict[str, Any]], rate: float, field: str = "in_usd") -> dict[str, float]:
    """Official list price in ¥ / 1M tokens (`spec[field] * rate`) for every model with a positive spec price."""
    out: dict[str, float] = {}
    for m, sp in (specs or {}).items():
        v = (sp or {}).get(field)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out[m] = float(v) * rate
    return out


def family_of(canon: str) -> str:
    import pricing as P  # local import keeps this module importable without tools/ on sys.path for pure-math callers

    return P.classify_family(canon)


def _family_key(fam: str) -> str | None:
    for k, v in FAMILY_KEYS.items():
        if v == fam:
            return k
    return None


def cheaper_percentiles(idx: dict[str, float | None]) -> dict[str, float | None]:
    """0-100: share of the OTHER priced stations whose index is strictly higher (more expensive)."""
    import bisect

    vals = sorted(v for v in idx.values() if v is not None)
    n = len(vals)
    out: dict[str, float | None] = {}
    for dom, v in idx.items():
        if v is None:
            out[dom] = None
        elif n <= 1:
            out[dom] = 50.0
        else:
            higher = n - bisect.bisect_right(vals, v)
            out[dom] = round(100.0 * higher / (n - 1), 1)
    return out


def compute_metrics(stations: dict[str, dict[str, Any]], by_station: dict[str, list[Any]], specs: dict[str, dict[str, Any]],
                    rate: float = 7.25, top_n: int = 30) -> dict[str, dict[str, Any]]:
    """Per-station consumer metrics (see module docstring). Scores are read from `st["score"].total` when present."""
    prices = {dom: station_std_prices(by_station.get(dom, [])) for dom in stations}
    medians = model_medians(prices)
    official = official_ref(specs, rate)
    carried: dict[str, int] = defaultdict(int)
    for p in prices.values():
        for m in p:
            carried[m] += 1
    top = frozenset(m for m, _ in sorted(carried.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n])
    out: dict[str, dict[str, Any]] = {}
    for dom, st in stations.items():
        p = prices[dom]
        fam_count: dict[str, int] = defaultdict(int)
        fam_prices: dict[str, dict[str, float]] = defaultdict(dict)
        for m, v in p.items():
            f = family_of(m)
            fam_count[f] += 1
            k = _family_key(f)
            if k:
                fam_prices[k][m] = v
        primary = sorted(fam_count.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if fam_count else None
        sc = st.get("score")
        out[dom] = {
            "prices": p,
            "models": frozenset(p),
            "top": frozenset(p) & top,
            "n_models": len(p),
            "primary": primary,
            "mkt_idx": market_index(p, medians),
            "off_pct": market_index(p, official),
            "fam": {k: market_index(fam_prices[k], medians) for k in FAMILY_KEYS if fam_prices.get(k)},
            "score": float(getattr(sc, "total", sc) or 0.0) if sc is not None else 0.0,
            "aliases": frozenset(st.get("aliases") or []),
        }
    cp = cheaper_percentiles({d: m["mkt_idx"] for d, m in out.items()})
    for d, m in out.items():
        m["cheaper_pct"] = cp[d]
    return out


# ---------------------------------------------------------------- plain-language price anchors
def fmt_official_discount(off_pct: float | None) -> str:
    """Official-price anchor in everyday Chinese ("折" = tenths of the list price)."""
    if not isinstance(off_pct, (int, float)) or isinstance(off_pct, bool) or not math.isfinite(off_pct) or off_pct <= 0:
        return "暂无统一比价基线"
    if off_pct < 10:
        return "不到官网 1 折"
    if off_pct < 95:
        return f"约官网 {off_pct / 10:.1f} 折"
    if off_pct <= 105:
        return "约等于官网价"
    return f"高于官网（约 {off_pct / 100:.1f} 倍）"


def fmt_discount_short(off_pct: float | None) -> str:
    """Compact pill text for tables: `约 1.8 折` / `<1 折` / `≈官网价` / `官网 2.5 倍` / `—`."""
    if not isinstance(off_pct, (int, float)) or isinstance(off_pct, bool) or not math.isfinite(off_pct) or off_pct <= 0:
        return "—"
    if off_pct < 10:
        return "不到 1 折"
    if off_pct < 95:
        return f"约 {off_pct / 10:.1f} 折"
    if off_pct <= 105:
        return "≈官网价"
    return f"官网 {off_pct / 100:.1f} 倍"


def fmt_cheaper(cheaper_pct: float | None) -> str:
    if cheaper_pct is None:
        return ""
    return f"比全网 {cheaper_pct:.0f}% 同行便宜"


def price_headline(m: dict[str, Any] | None) -> str:
    """「约官网 X.X 折 · 比全网 XX% 同行便宜」 (degrades gracefully when either anchor is unknown)."""
    m = m or {}
    off, cp = m.get("off_pct"), m.get("cheaper_pct")
    parts = []
    if off is not None:
        parts.append(fmt_official_discount(off))
    if cp is not None:
        parts.append(fmt_cheaper(cp))
    return " · ".join(parts) if parts else "暂无统一比价基线"


def pct_delta(mine: float | None, other: float | None) -> tuple[str, str]:
    """Peer price vs this station: ("比本站便宜 12%", "ok") / ("比本站贵 30%", "warn") / ("和本站差不多", "")."""
    if not mine or not other or mine <= 0 or other <= 0:
        return "暂无可比价格", "muted"
    d = (other / mine - 1) * 100
    if abs(d) < 3:
        return "和本站差不多", ""
    if d < 0:
        return f"比本站便宜 {-d:.0f}%", "ok"
    return f"比本站贵 {d:.0f}%", "warn"


# ---------------------------------------------------------------- peer matching (B_hybrid + brand / mirror dedup)
_MULTI_SUFFIX = {"com.cn", "net.cn", "org.cn", "gov.cn", "ac.cn", "com.hk", "com.tw", "co.uk", "co.jp", "com.au", "co.kr", "com.sg"}
_AFFIXES = ("api", "ai")


def root_label(dom: str) -> str:
    parts = [p for p in str(dom).lower().strip(".").split(".") if p]
    if not parts:
        return ""
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_SUFFIX:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


def brand_stem(dom: str) -> str:
    """Root-domain label without punctuation and trailing/leading `api` / `ai` (bianxieai.com ≡ api.bianxie.ai)."""
    s = re.sub(r"[^a-z0-9]", "", root_label(dom))
    changed = True
    while changed:
        changed = False
        for a in _AFFIXES:
            if s.endswith(a) and len(s) - len(a) >= 4:
                s, changed = s[: -len(a)], True
            elif s.startswith(a) and len(s) - len(a) >= 4:
                s, changed = s[len(a):], True
    return s


def is_clone(a: str, b: str, metrics: dict[str, dict[str, Any]]) -> bool:
    """Same brand / mirror: alias link, identical brand stem, or identical (model count, index) fingerprint."""
    if a == b:
        return True
    ma, mb = metrics.get(a) or {}, metrics.get(b) or {}
    if b in (ma.get("aliases") or ()) or a in (mb.get("aliases") or ()):
        return True
    sa, sb = brand_stem(a), brand_stem(b)
    if sa and sa == sb:
        return True
    ia, ib = ma.get("mkt_idx"), mb.get("mkt_idx")
    return (ia is not None and ib is not None and ma.get("n_models", 0) >= 3
            and ma.get("n_models") == mb.get("n_models") and round(ia, 1) == round(ib, 1))


def _jac(a: frozenset, b: frozenset) -> tuple[float, int]:
    if not a and not b:
        return 0.0, 0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter), inter


def similarity(ma: dict[str, Any], mb: dict[str, Any]) -> tuple[float, int]:
    """B_hybrid similarity of station b as a peer of station a, plus the shared-model count."""
    j30, _ = _jac(ma["top"], mb["top"])
    jall, shared = _jac(ma["models"], mb["models"])
    pa, pb = ma.get("mkt_idx"), mb.get("mkt_idx")
    price = math.exp(-abs(math.log(pa / pb)) / 0.6) if (pa and pb and pa > 0 and pb > 0) else 0.0
    score = max(0.0, 1 - abs(ma.get("score", 0) - mb.get("score", 0)) / 35)
    sim = 0.40 * j30 + 0.20 * jall + 0.25 * price + 0.15 * score
    if ma.get("primary") != mb.get("primary"):
        sim *= 0.75
    if shared < 2 and ma.get("n_models", 0) >= 2:
        sim *= 0.20
    return sim, shared


def compute_peers(metrics: dict[str, dict[str, Any]], k: int = 5, *, cheaper_gap: float = 0.05,
                  safer_gap: float = 2.0) -> dict[str, dict[str, Any]]:
    """For every station: up to `k` distinct peers (brand / mirror clones collapsed) + 「同类更省钱 / 更稳」 alternatives.

    cheaper: shares >= 2 models, index at least `cheaper_gap` lower, score not more than 5 points worse.
    safer:   shares >= 2 models, score at least `safer_gap` higher, index at most 1.5x this station's.
    """
    doms = sorted(metrics)
    out: dict[str, dict[str, Any]] = {}
    for a in doms:
        ma = metrics[a]
        cands = []
        for b in doms:
            if b == a:
                continue
            sim, shared = similarity(ma, metrics[b])
            cands.append((sim, shared, b))
        cands.sort(key=lambda x: (-x[0], x[2]))
        peers: list[str] = []
        sims: dict[str, float] = {}
        cheaper = safer = None
        ia, sa = ma.get("mkt_idx"), ma.get("score", 0)
        for sim, shared, b in cands:
            mb = metrics[b]
            if is_clone(a, b, metrics):
                continue
            if len(peers) < k and not any(is_clone(b, p, metrics) for p in peers):
                peers.append(b)
                sims[b] = round(sim, 3)
            if shared >= 2:
                ib, sb = mb.get("mkt_idx"), mb.get("score", 0)
                if cheaper is None and ia and ib and ib <= ia * (1 - cheaper_gap) and sb >= sa - 5:
                    cheaper = b
                if safer is None and sb >= sa + safer_gap and (not ia or not ib or ib <= ia * 1.5):
                    safer = b
            if len(peers) >= k and cheaper is not None and safer is not None:
                break
        out[a] = {"peers": peers, "sims": sims, "cheaper": cheaper, "safer": safer}
    return out


# ---------------------------------------------------------------- everyday client compatibility & verdict tags
CLIENT_CHIPS = (
    ("anthropic", "Claude Code / Cursor", "支持 Claude 原生接口，可直接填进 Claude Code、Cursor 等编程工具"),
    ("openai", "ChatBox / Cherry Studio", "支持最常见的 OpenAI 兼容接口，ChatBox、Cherry Studio、沉浸式翻译等都能用"),
    ("openai-response", "Codex 等新版客户端", "支持 OpenAI 新版接口，Codex 等新版客户端可直接使用"),
    ("gemini", "Gemini 客户端", "支持 Gemini 原生接口"),
    ("image", "AI 生图", "提供图片生成模型"),
    ("video", "AI 视频", "提供视频生成模型"),
)


def client_chips(endpoints: Iterable[str] | None) -> list[tuple[str, str]]:
    eps = set(endpoints or [])
    return [(label, tip) for key, label, tip in CLIENT_CHIPS if key in eps]


def verdict_tags(*, stage: int, failed: bool, age_days: Any, ups: int, sharp: int, monitored: bool,
                 cache_cov: Any, attempts: int, successes: int, caveat_groups: int,
                 consecutive_failures: int = 0) -> tuple[list[str], list[tuple[str, str]]]:
    """(green highlights <= 3, [(warning text, "warn" | "danger"), ...]) in plain Chinese."""
    greens: list[str] = []
    warns: list[tuple[str, str]] = []
    up = (successes / attempts) if attempts else None
    if stage == 2 and not failed:
        greens.append("已通过真实调用实测")
    if isinstance(age_days, (int, float)) and age_days >= 365:
        greens.append(f"开站 {age_days / 365:.1f} 年老站")
    if monitored and not ups:
        greens.append("近 30 天未涨价")
    if up is not None and attempts >= 3 and up >= 0.95:
        greens.append("巡检持续在线")
    if isinstance(cache_cov, (int, float)) and cache_cov > 0:
        greens.append("支持长对话省钱缓存")
    if stage == 2 and failed:
        warns.append(("实测未达标，最高只给 C 级", "danger"))
    elif stage < 2:
        warns.append(("仅公开报价，未人工实测（最高 B 级）", "warn"))
    if isinstance(age_days, (int, float)) and 0 <= age_days < 182:
        warns.append(("建站未满半年，建议先小额试水", "warn"))
    if caveat_groups:
        warns.append(("含低价特惠/逆向分组，主力号慎选", "warn"))
    if sharp:
        warns.append(("近 30 天有大幅涨价", "danger"))
    elif ups:
        warns.append(("近 30 天有涨价波动", "warn"))
    if up is not None and attempts >= 3 and up < 0.8:
        warns.append(("巡检时常打不开", "danger"))
    if consecutive_failures:
        warns.append((f"最近 {consecutive_failures} 次巡检没打开，价格可能不是最新", "warn"))
    return greens[:3], warns


# ---------------------------------------------------------------- flagship lanes for the consumer price bars
def model_lanes(by_station: dict[str, list[Any]], models: Iterable[str], wi: float = 3.0, wo: float = 1.0) -> dict[str, dict[str, dict[str, float | None]]]:
    """{canon: {dom: {"in", "chat" (3:1 input:output mix), "cache"}}} from each station's cheapest standard-lane token quote."""
    want = set(models)
    out: dict[str, dict[str, dict[str, float | None]]] = {m: {} for m in want}
    for dom, qs in by_station.items():
        best: dict[str, Any] = {}
        for q in qs:
            if q.canonical not in want or q.kind != "token" or q.lane != "standard" or q.free or q.welfare or q.anomaly:
                continue
            if not isinstance(q.input_cny, (int, float)) or q.input_cny <= 0:
                continue
            if q.canonical not in best or q.input_cny < best[q.canonical].input_cny:
                best[q.canonical] = q
        for m, q in best.items():
            o = q.output_cny if isinstance(q.output_cny, (int, float)) else q.input_cny
            cache = q.cache_cny if (q.cache_status in ("priced", "free") and isinstance(q.cache_cny, (int, float))) else None
            out[m][dom] = {"in": float(q.input_cny), "chat": (q.input_cny * wi + o * wo) / (wi + wo), "cache": cache}
    return out


def official_lane(spec: dict[str, Any] | None, rate: float, wi: float = 3.0, wo: float = 1.0) -> dict[str, float | None]:
    sp = spec or {}
    i, o, c = sp.get("in_usd"), sp.get("out_usd"), sp.get("cache_usd")
    ok = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0  # noqa: E731
    return {
        "in": i * rate if ok(i) else None,
        "chat": ((i * wi + (o if ok(o) else i) * wo) / (wi + wo)) * rate if ok(i) else None,
        "cache": c * rate if ok(c) else None,
    }


def peer_reference(lanes: dict[str, dict[str, float | None]], peers: Iterable[str], key: str,
                   min_peers: int = 2) -> tuple[float | None, str]:
    """Mean of matched peers' price (label 同行均价); falls back to the whole-market median (label 全网中位价)."""
    vals = [lanes[p][key] for p in peers if p in lanes and isinstance(lanes[p].get(key), (int, float)) and lanes[p][key] > 0]
    if len(vals) >= min_peers:
        return statistics.fmean(vals), "同行均价"
    allv = [v[key] for v in lanes.values() if isinstance(v.get(key), (int, float)) and v[key] > 0]
    if len(allv) >= 3:
        return statistics.median(allv), "全网中位价"
    return None, "同行均价"
