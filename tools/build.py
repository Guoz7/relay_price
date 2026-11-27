"""Static site builder: data/raw/*.json.gz (+ intel, fetch history, probes) -> dist/ (SEO-friendly multi-page HTML).

V1.0 + V2A Architecture:
- Staged certification (Stage 0 archived 401/403, Stage 1 public-verified capped at B/74,
  Stage 2 real-call probed unlocking A/S with non-compensatory reliability/billing vetoes).
- Multi-group expansion per station + Standard vs Promo/Welfare lane separation.
- Verifiable protocol (`endpoints`) filtering + Dialogue (3:1) / Coding Cache (7:2:1) toggle.
- 4 high-intent Scenario Landing Pages (/scenes/...) + SEO `noindex,follow` on <10-station thin pages.
- Auto-exported Awesome-style GITHUB_README.md + optional outbound telemetry hook.

Usage:
  python tools/build.py                 # build dist/
  python tools/build.py --serve 8790    # build then preview at http://127.0.0.1:8790
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import re
import shutil
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import calc_page  # noqa: E402
import charts as CH  # noqa: E402
import compare_page as CP  # noqa: E402
import consumer as CM  # noqa: E402
import dashboard as DB  # noqa: E402
import en_page  # noqa: E402
import export_readme  # noqa: E402
import model_specs as MS  # noqa: E402
import pricing as P  # noqa: E402
import probes  # noqa: E402
import psl  # noqa: E402
import scenes  # noqa: E402
import scoring as S  # noqa: E402
import station_insights as SI  # noqa: E402
import submit_page  # noqa: E402
import timeseries as TS  # noqa: E402
import verify_page  # noqa: E402

ROOT = TOOLS.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
HIST = DATA / "history"
RUNS = DATA / "fetch-runs"
DIST = ROOT / "dist"
ASSETS = ROOT / "assets"
CST = timezone(timedelta(hours=8))

E = html.escape


# ---------------------------------------------------------------- helpers
def load_json(p: Path, default: Any) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_cst(dt: datetime | None, with_time: bool = True) -> str:
    if not dt:
        return "—"
    return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def ago(dt: datetime | None, now: datetime) -> str:
    if not dt:
        return "未知"
    s = (now - dt).total_seconds()
    if s < 3600:
        return f"{max(1, int(s // 60))} 分钟前"
    if s < 86400:
        return f"{int(s // 3600)} 小时前"
    return f"{int(s // 86400)} 天前"


def money(v: float | None, cur: str = "¥") -> str:
    if v is None:
        return "—"
    if v == 0:
        return f"{cur}0"
    if v >= 100:
        return f"{cur}{v:,.0f}"
    if v >= 1:
        return f"{cur}{v:.2f}".rstrip("0").rstrip(".")
    if v >= 0.01:
        return f"{cur}{v:.4f}".rstrip("0").rstrip(".")
    return f"{cur}{v:.6f}".rstrip("0").rstrip(".")


def jsonld(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/ld+json">{s}</script>'


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def display_name(name: Any, domain: str) -> str:
    """Station-supplied names may contain QQ numbers / ads; fall back to domain when noisy."""
    n = re.sub(r"\s+", " ", str(name or "").strip())
    if not n or len(n) > 24 or re.search(r"\d{6,}", n) or re.search(r"(?:群|加我|微信|qq|vx|wx)", n, re.I):
        return domain
    return n


def grade_badge(score: float | None) -> str:
    g = S.grade(score)
    return f'<b class="gr gr-{g if g != "—" else "x"}">{g}</b>'


def stage_badge(stage: int, failed_veto: bool = False) -> str:
    if stage == 2 and not failed_veto:
        return '<span class="badge ok">已通过真实调用实测</span>'
    if stage == 2 and failed_veto:
        return '<span class="badge danger">实测未达标（最高 C 级）</span>'
    if stage == 0:
        return '<span class="badge warn">仅建档（未公开报价）</span>'
    return '<span class="badge">仅公开报价（未人工实测，最高 B 级）</span>'


def plain_stage(sc: Any) -> str:
    if sc.stage == 2 and not sc.cap_reason:
        return "已通过真实调用实测"
    if sc.stage == 2:
        return "实测未达标（最高 C 级）"
    return "仅抓取公开报价（未人工实测，最高 B 级）"


PLAIN_EP = {**P.ENDPOINT_LABELS, "anthropic": "Claude 原生接口", "openai": "OpenAI 通用接口", "openai-response": "Codex 新版接口",
            "gemini": "Gemini 原生接口", "image": "AI 生图", "video": "AI 视频", "embeddings": "知识库向量"}
PLAIN_DIM_SHORT = {"stability": "在线", "longevity": "资历", "transparency": "透明", "reputation": "口碑", "latency": "速度",
                   "price_stability": "防暗涨", "purity_protocol": "干净", "probe": "实测"}
PLAIN_DIM_LONG = {"stability": "接口稳定", "longevity": "开站资历", "transparency": "价格透明", "reputation": "社区口碑",
                  "latency": "响应速度", "price_stability": "防暗涨", "purity_protocol": "分组干净", "probe": "真实实测"}


# ---------------------------------------------------------------- data load
def seed_order() -> dict[str, int]:
    """Position of each domain in seeds.txt (earlier = listed longer)."""
    out: dict[str, int] = {}
    p = ROOT / "seeds.txt"
    for line in (p.read_text(encoding="utf-8").splitlines() if p.exists() else []):
        d = line.split("#", 1)[0].strip().lower()
        if d and d not in out:
            out[d] = len(out)
    return out


def snapshot_files() -> list[Path]:
    """Raw snapshots, longest-listed first, so a byte-identical mirror found later never replaces an established page."""
    order = seed_order()
    return sorted(RAW.glob("*.json.gz"), key=lambda f: (order.get(f.name[:-8], len(order)), f.name))


def load_snapshots(cfg: dict[str, Any], now: datetime) -> tuple[list[P.Quote], dict[str, dict[str, Any]]]:
    exclude = set(cfg.get("exclude_domains") or [])
    hide_after = timedelta(days=int(cfg.get("hide_after_days", 30)))
    state = load_json(DATA / "stations-state.json", {})
    files = snapshot_files()

    def _read_one(f: Path) -> dict[str, Any] | None:
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        snaps = list(pool.map(_read_one, files))

    stations: dict[str, dict[str, Any]] = {}
    quotes: list[P.Quote] = []
    by_hash: dict[str, str] = {}
    for snap in snaps:
        if not snap:
            continue
        dom = snap.get("domain")
        if not dom or dom in exclude:
            continue
        fetched = parse_dt(snap.get("fetched_at"))
        if not fetched or now - fetched > hide_after:
            continue
        pr = snap.get("pricing") or {}
        h = hashlib.sha1(json.dumps(pr, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if h in by_hash:  # mirror domain with byte-identical pricing -> alias
            stations[by_hash[h]].setdefault("aliases", []).append(dom)
            continue
        qs, meta = P.normalize_station(dom, pr, snap.get("status"))
        if not qs:
            continue
        by_hash[h] = dom
        st = state.get(dom, {})
        meta.update(
            {
                "display_name": display_name(meta.get("system_name"), dom),
                "fetched_at": fetched,
                "last_attempt_at": parse_dt(st.get("last_attempt_at")),
                "consecutive_failures": int(st.get("consecutive_failures") or 0),
                "last_error": st.get("last_error"),
                "aliases": [],
                "status_sha256": snap.get("status_sha256"),
                "pricing_sha256": snap.get("pricing_sha256"),
            }
        )
        stations[dom] = meta
        quotes.extend(qs)
    return quotes, stations


def load_closed_stations(cfg: dict[str, Any], priced_stations: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Stage 0 archive entries for 401/403 login-gated or WAF-protected stations (no pricing, no rank)."""
    exclude = set(cfg.get("exclude_domains") or [])
    taken = set(priced_stations.keys())
    for st in priced_stations.values():
        taken.update(st.get("aliases") or [])
    state = load_json(DATA / "stations-state.json", {})
    out: dict[str, dict[str, Any]] = {}
    for dom, st in sorted(state.items()):
        if dom in taken or dom in exclude:
            continue
        code = st.get("last_pricing_http")
        if code not in (401, 403):
            continue
        out[dom] = {
            "domain": dom,
            "display_name": dom,
            "http_status": code,
            "status_http": st.get("last_status_http"),
            "last_error": st.get("last_error") or f"HTTP {code}",
            "last_attempt_at": parse_dt(st.get("last_attempt_at")),
        }
    return out


def load_run_stats(now: datetime, days: int = 30) -> dict[str, dict[str, Any]]:
    """Per-domain attempts / successes / latencies from fetch-run logs in the last `days` days."""
    out: dict[str, dict[str, Any]] = defaultdict(lambda: {"attempts": 0, "successes": 0, "ms": []})
    cutoff = now - timedelta(days=days)
    for f in sorted(RUNS.glob("*.json")):
        run = load_json(f, {})
        ra = parse_dt(run.get("run_at"))
        if not ra or ra < cutoff:
            continue
        for e in run.get("log") or []:
            d = e.get("domain")
            if not d:
                continue
            r = out[d]
            r["attempts"] += 1
            if e.get("ok"):
                r["successes"] += 1
                if isinstance(e.get("elapsed_ms"), (int, float)):
                    r["ms"].append(float(e["elapsed_ms"]))
    return out


# ---------------------------------------------------------------- price helpers
def mix(q: P.Quote, cfg: dict[str, Any], cur: str = "cny") -> float | None:
    wi, wo = float(cfg.get("mix_input_weight", 3)), float(cfg.get("mix_output_weight", 1))
    i, o = (q.input_cny, q.output_cny) if cur == "cny" else (q.input_usd, q.output_usd)
    if i is None and o is None:
        return None
    return ((i or 0) * wi + (o if o is not None else (i or 0)) * wo) / (wi + wo)


def cache_mix_cny(q: P.Quote, cfg: dict[str, Any]) -> float | None:
    """Coding / Agent cache-heavy blended price (default 7:2:1 cache_read:input:output)."""
    if q.kind != "token" or q.input_cny is None or q.cache_cny is None:
        return None
    cw = cfg.get("cache_mix_weights") or {"cache": 7, "input": 2, "output": 1}
    wc, wi, wo = float(cw.get("cache", 7)), float(cw.get("input", 2)), float(cw.get("output", 1))
    o = q.output_cny if q.output_cny is not None else q.input_cny
    return (q.cache_cny * wc + q.input_cny * wi + o * wo) / (wc + wi + wo)


def main_cny(q: P.Quote, cfg: dict[str, Any]) -> float | None:
    return mix(q, cfg, "cny") if q.kind == "token" else q.call_cny


def price_key(q: P.Quote, cfg: dict[str, Any]) -> tuple:
    """Cheapest-first key."""
    v_cny = main_cny(q, cfg)
    v_usd = mix(q, cfg, "usd") if q.kind == "token" else q.call_usd
    lane_ord = {"standard": 0, "promo": 1, "welfare": 2}.get(q.lane, 1)
    return (
        lane_ord,
        1 if q.anomaly else 0,
        0 if v_cny is not None else 1,
        v_cny if v_cny is not None else (v_usd if v_usd is not None else 9e9),
        q.domain,
    )


def is_stale(st: dict[str, Any], cfg: dict[str, Any], now: datetime) -> bool:
    f = st.get("fetched_at")
    return bool(f and now - f > timedelta(days=int(cfg.get("stale_days", 7))))


def is_reliable_rec(q: P.Quote, st: dict[str, Any], cfg: dict[str, Any], now: datetime) -> bool:
    """Strict eligibility gate for Reliable Top 3 recommendations (validated in Experiment 2)."""
    if q.lane != "standard" or q.free or q.welfare or q.anomaly or q.low_outlier:
        return False
    if main_cny(q, cfg) is None or is_stale(st, cfg, now):
        return False
    sc: S.StationScore = st["score"]
    if sc.stage == 2 and sc.cap_reason:  # failed Stage 2 health/billing gate
        return False
    if sc.dims["longevity"].score < 50 or sc.dims["transparency"].score < 80:
        return False
    return True


def rank_key(q: P.Quote, stations: dict[str, dict[str, Any]], cfg: dict[str, Any], now: datetime) -> tuple:
    """Default display order: lane -> freshness -> Stage 2 verified -> capped score -> raw score tie-breaker."""
    st = stations[q.domain]
    sc: S.StationScore = st["score"]
    lane_ord = {"standard": 0, "promo": 1, "welfare": 2}.get(q.lane, 1)
    stage2_ok = 0 if (sc.stage == 2 and not sc.cap_reason) else 1
    return (
        lane_ord,
        1 if is_stale(st, cfg, now) else 0,
        1 if (q.anomaly or q.low_outlier) else 0,
        stage2_ok,
        -(q.score if q.score is not None else -1),
        -(q.raw_score if q.raw_score is not None else -1),
        price_key(q, cfg),
    )


def mark_anomalies(by_model: dict[str, list[P.Quote]], cfg: dict[str, Any]) -> None:
    """Flag extreme misconfig anomalies (< med / factor) using Standard-lane median."""
    factor = float(cfg.get("anomaly_factor", 30))
    for qs in by_model.values():
        for kind in ("token", "call"):
            std_vals = [
                (main_cny(q, cfg), q) for q in qs
                if q.kind == kind and q.lane == "standard" and not q.free and not q.welfare
            ]
            std_vals = [(v, q) for v, q in std_vals if v is not None and v > 0]
            all_vals = [
                (main_cny(q, cfg), q) for q in qs
                if q.kind == kind and not q.free and not q.welfare
            ]
            all_vals = [(v, q) for v, q in all_vals if v is not None and v > 0]
            base = std_vals if len(std_vals) >= 4 else all_vals
            if len(base) < 4:
                continue
            xs = sorted(v for v, _ in base)
            med = xs[len(xs) // 2]
            for v, q in all_vals:
                q.median_ratio = round(v / med, 2) if med > 0 else None
                if v < med / factor:
                    q.anomaly = True
                    q.flags.append(f"远低于中位数（{money(med)}），疑似配置错误或限量")


def merge_mirrors(quotes: list[P.Quote], stations: dict[str, dict[str, Any]], intel: dict[str, Any]) -> list[P.Quote]:
    """Same root domain + same station name = one operator; keep the domain with most quotes.

    Tenants of shared hosting platforms (*.pages.dev, *.vercel.app, ...) are keyed by their own full host,
    so two unrelated stations on the same platform are never merged.
    """
    order = seed_order()
    counts: dict[str, int] = defaultdict(int)
    for q in quotes:
        counts[q.domain] += 1
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for dom, st in stations.items():
        if psl.shared_host_of(dom):
            root = psl.registrable_root(dom)
        else:
            root = (intel.get(dom) or {}).get("root") or psl.registrable_root(dom)
        if st["display_name"] == dom:
            continue
        groups[(root, st["display_name"].lower())].append(dom)
    drop: set[str] = set()
    for doms in groups.values():
        if len(doms) < 2:
            continue
        main = max(doms, key=lambda d: (counts[d], -order.get(d, len(order)), -len(d)))
        for d in doms:
            if d != main:
                stations[main].setdefault("aliases", []).append(d)
                stations[main]["aliases"] += stations[d].get("aliases") or []
                drop.add(d)
    for d in drop:
        stations.pop(d, None)
    return [q for q in quotes if q.domain not in drop]


# ---------------------------------------------------------------- scoring
FLAGSHIP_CACHE_FAMS = {"claude", "gpt", "gemini", "deepseek"}
GPT_RESP_RE = re.compile(r"^gpt-(?:5|6)")
EXTREME_SPREAD = 4.0


def _lane_price(q: P.Quote, cfg: dict[str, Any]) -> float | None:
    """Currency-agnostic main price (ratios within one station are identical in CNY and USD)."""
    v = mix(q, cfg, "usd") if q.kind == "token" else q.call_usd
    if v is None:
        v = main_cny(q, cfg)
    return v if (v is not None and v > 0) else None


def purity_inputs(st: dict[str, Any], qs: list[P.Quote], cfg: dict[str, Any]) -> dict[str, Any]:
    """Raw counts for scoring.score_purity_protocol: caveat lanes, std-vs-promo spreads, protocol & cache coverage."""
    groups = [g for g in (st.get("groups") or []) if g.get("selectable") and not g.get("welfare")]
    paid = [q for q in qs if not (q.free or q.welfare) and q.lane != "welfare"]
    lanes: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for q in paid:
        v = _lane_price(q, cfg)
        if v is None:
            continue
        cur = lanes[(q.canonical, q.kind)].get(q.lane)
        if cur is None or v < cur:
            lanes[(q.canonical, q.kind)][q.lane] = v
    spreads = [max(d["standard"], d["promo"]) / min(d["standard"], d["promo"]) for d in lanes.values() if "standard" in d and "promo" in d]
    std = [q for q in paid if q.lane == "standard"]
    claude = [q for q in std if q.family == "claude" and q.kind == "token"]
    gpt = [q for q in std if q.kind == "token" and GPT_RESP_RE.match(q.canonical)]
    cache_el = [q for q in std if q.kind == "token" and q.family in FLAGSHIP_CACHE_FAMS]
    return {
        "groups_paid": len(groups),
        "groups_caveat": sum(1 for g in groups if g.get("caveats")),
        "quotes_paid": len(paid),
        "quotes_promo": sum(1 for q in paid if q.lane == "promo"),
        "quotes_anomaly": sum(1 for q in paid if q.anomaly),
        "dual_lane_models": len(spreads),
        "extreme_spread_models": sum(1 for x in spreads if x > EXTREME_SPREAD),
        "max_spread": round(max(spreads), 2) if spreads else None,
        "claude_quotes": len(claude),
        "claude_native": sum(1 for q in claude if "anthropic" in P.endpoint_tags(q.endpoints)),
        "gpt_quotes": len(gpt),
        "gpt_responses": sum(1 for q in gpt if "openai-response" in P.endpoint_tags(q.endpoints)),
        "cache_eligible": len(cache_el),
        "cache_disclosed": sum(1 for q in cache_el if q.cache_usd is not None or q.cache_cny is not None),
    }


def merged_run_stats(st: dict[str, Any], dom: str, run_stats: dict[str, Any]) -> dict[str, Any]:
    rs = run_stats.get(dom) or {"attempts": 0, "successes": 0, "ms": []}
    for a in st.get("aliases") or []:
        ra = run_stats.get(a)
        if ra:
            rs = {"attempts": rs["attempts"] + ra["attempts"], "successes": rs["successes"] + ra["successes"], "ms": rs["ms"] + ra["ms"]}
    return rs


def station_dims(
    dom: str, st: dict[str, Any], qs: list[P.Quote], it: dict[str, Any], rs: dict[str, Any],
    stab: dict[str, Any] | None, cfg: dict[str, Any], now: datetime | None,
    *, monitored: bool = True, drop_sources: int = 0,
) -> list[S.Dim]:
    """All 7 public-signal dimensions for one station (shared by build and tools/eval_scoring.py)."""
    groups = [g for g in (st.get("groups") or []) if g.get("selectable")]
    risk = SI.expiry_risk(it.get("domain_expires_at"), now)
    n_src = max(0, len(it.get("sources") or []) - drop_sources)
    return [
        S.score_stability(rs["attempts"], rs["successes"], st.get("consecutive_failures") or 0),
        S.score_longevity(it.get("domain_age_days"), it.get("domain_registered_at"), risk["days_left"] if risk else None),
        S.score_transparency(
            topup_known=st.get("topup_price") is not None,
            priced=st.get("parsed") or 0,
            placeholders=st.get("dropped_placeholder") or 0,
            groups_selectable=len(groups),
            groups_with_desc=sum(1 for g in groups if g.get("desc")),
            quotes=len(qs),
            anomalies=sum(1 for q in qs if q.anomaly),
        ),
        S.score_reputation(n_src, it.get("tranco_rank"), it.get("umbrella_rank"), it.get("majestic_rank")),
        S.score_latency(statistics.median(rs["ms"]) if rs["ms"] else None),
        S.score_price_stability(stab, monitored=monitored),
        S.score_purity_protocol(**purity_inputs(st, qs, cfg)),
    ]


def score_params(cfg: dict[str, Any]) -> dict[str, Any]:
    wl = cfg.get("weak_link")
    sc = cfg.get("stage1_soft_cap")
    return {
        "soft": {k: float(v) for k, v in sc.items()} if isinstance(sc, dict) else None,
        "weak_link": {k: (float(v[0]), float(v[1])) for k, v in wl.items()} if isinstance(wl, dict) else None,
    }


def compute_station_scores(stations, by_station, intel, run_stats, cfg, probe_map: dict[str, Any] | None = None,
                           stability_map: dict[str, Any] | None = None, now: datetime | None = None,
                           monitored: bool = True) -> dict[str, float]:
    """Score every priced station, then assign market percentiles. Returns the market median per dimension."""
    weights = cfg.get("station_weights") or S.DEFAULT_STATION_WEIGHTS
    probe_map = probe_map or {}
    stability_map = stability_map or {}
    params = score_params(cfg)
    for dom, st in stations.items():
        rs = merged_run_stats(st, dom, run_stats)
        dims = station_dims(dom, st, by_station.get(dom, []), intel.get(dom) or {}, rs, stability_map.get(dom), cfg, now, monitored=monitored)
        st["score"] = S.station_score(dims, weights, attempts=rs["attempts"], probe=probe_map.get(dom), **params)
    scores = [st["score"] for st in stations.values()]
    S.assign_percentiles(scores)
    return S.market_medians(scores)


SEARCH_INDEX = "search-index.json"
FLAGSHIP_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "gpt-6-astra", "gpt-6.1-sol", "deepseek-v4.1-flash", "gemini-4-carbon")


def _lane_min_input(qs: list[P.Quote], lane: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for q in qs:
        if q.kind != "token" or q.lane != lane or q.free or q.welfare or q.input_cny is None or q.input_cny <= 0:
            continue
        if lane == "standard" and q.anomaly:
            continue
        if q.domain not in out or q.input_cny < out[q.domain]:
            out[q.domain] = q.input_cny
    return out


def flagship_market(by_model: dict[str, list[P.Quote]], specs: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Full-market standard-lane input price distribution (one price per station) for each flagship model."""
    rate = float(cfg.get("usd_cny_rate", 7.25))
    out: dict[str, dict[str, Any]] = {}
    for canon in FLAGSHIP_MODELS:
        qs = by_model.get(canon) or []
        stats = CH.price_stats(_lane_min_input(qs, "standard").values())
        if not stats or stats["n"] < 4:
            continue
        alt = sorted(_lane_min_input(qs, "promo").values())
        sp = specs.get(canon) or {}
        off = sp.get("in_usd")
        out[canon] = {**stats, "alt_median": CH.quantile(alt, .5) if alt else None,
                      "official": off * rate if isinstance(off, (int, float)) and off > 0 else None}
    return out


def station_price_rows(qs: list[P.Quote], market_px: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for canon in FLAGSHIP_MODELS:
        mk = market_px.get(canon)
        mine = [q for q in qs if q.canonical == canon]
        if not mk or not mine:
            continue
        std = _lane_min_input(mine, "standard").get(mine[0].domain) or min(
            (q.input_cny for q in mine if q.lane == "standard" and q.kind == "token" and q.input_cny), default=None)
        alt = _lane_min_input(mine, "promo").get(mine[0].domain)
        if std or alt:
            rows.append({**mk, "model": canon, "std": std, "alt": alt})
    return rows


def compute_quote_scores(by_model: dict[str, list[P.Quote]], stations, cfg) -> None:
    qw = cfg.get("quote_weights") or S.DEFAULT_QUOTE_WEIGHTS
    floor = float(cfg.get("reliable_discount_floor", 0.25))
    for qs in by_model.values():
        for kind in ("token", "call"):
            std_vals = sorted(
                v for v in (main_cny(q, cfg) for q in qs if q.kind == kind and q.lane == "standard" and not (q.free or q.welfare or q.anomaly))
                if v is not None and v > 0
            )
            all_vals = sorted(
                v for v in (main_cny(q, cfg) for q in qs if q.kind == kind and not (q.free or q.welfare or q.anomaly))
                if v is not None and v > 0
            )
            vals = std_vals if len(std_vals) >= 4 else all_vals
            med = vals[len(vals) // 2] if vals else None
            for q in qs:
                if q.kind != kind:
                    continue
                if q.free or q.welfare:
                    q.price_score, q.score, q.raw_score = None, None, None
                    continue
                sc: S.StationScore = stations[q.domain]["score"]
                v = main_cny(q, cfg)
                if med is not None and v is not None and len(vals) >= 4:
                    q.median_ratio = round(v / med, 2)
                    if q.lane == "standard" and not q.anomaly and v < med * floor and sc.stage < 2:
                        q.low_outlier = True
                        note = f"低于中位价（{q.median_ratio:.2f}×），未实测暂不进常规推荐"
                        if note not in q.flags:
                            q.flags.append(note)
                q.price_score = S.price_score(v, med, q.anomaly or q.low_outlier)
                q.raw_score = S.quote_score(q.price_score, sc.raw_total, qw)
                cap = S.STAGE2_FAIL_CAP if (sc.stage == 2 and sc.cap_reason) else (S.STAGE1_CAP if sc.stage < 2 else None)
                q.score = S.quote_score(q.price_score, sc.total, qw, cap=cap)


# ---------------------------------------------------------------- layout
class Site:
    def __init__(self, cfg: dict[str, Any], now: datetime, asset_ver: str):
        self.cfg = cfg
        self.now = now
        self.base = cfg["base_url"].rstrip("/")
        self.name = cfg["site_name"]
        self.asset_ver = asset_ver
        self.urls: list[tuple[str, str, float]] = []  # (path, lastmod, priority)
        self._pending: list[tuple[Path, str]] = []

    def flush(self) -> None:
        if not self._pending:
            return
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda item: write(item[0], item[1]), self._pending))
        self._pending.clear()

    def url(self, path: str) -> str:
        return self.base + path

    def ext_link(self, domain: str, text: str | None = None) -> str:
        aff = (self.cfg.get("affiliate_links") or {}).get(domain)
        href = aff or f"https://{domain}/"
        rel = "sponsored nofollow noopener" if aff else "nofollow noopener"
        tip = ' title="含推广返佣链接，不影响评分与排序"' if aff else ""
        suffix = ' <small class="muted">(含返佣)</small>' if aff else ""
        return f'<a class="ext" href="{E(href)}" rel="{rel}" target="_blank" data-out-domain="{E(domain)}"{tip}>{E(text or domain)}</a>{suffix}'

    def _verification_tags(self) -> str:
        mapping = [
            ("google_site_verification", "google-site-verification"),
            ("bing_site_verification", "msvalidate.01"),
            ("baidu_site_verification", "baidu-site-verification"),
            ("so360_site_verification", "360-site-verification"),
            ("sogou_site_verification", "sogou_site_verification"),
            ("toutiao_site_verification", "bytedance-verification-code"),
        ]
        tags: list[str] = []
        for cfg_key, meta_name in mapping:
            v = str(self.cfg.get(cfg_key) or "").strip()
            if v:
                tags.append(f'<meta name="{meta_name}" content="{E(v)}">\n')
        return "".join(tags)

    def _analytics_tag(self) -> str:
        tags: list[str] = []
        cf_tok = str(self.cfg.get("cf_beacon_token") or "").strip()
        if cf_tok:
            tags.append(
                f'<script defer src="https://static.cloudflareinsights.com/beacon.min.js" '
                f'data-cf-beacon=\'{{"token":"{E(cf_tok)}"}}\'></script>\n'
            )
        sc = str(self.cfg.get("analytics_script") or "").strip()
        if sc:
            dom = str(self.cfg.get("analytics_domain") or "").strip()
            dom_attr = f' data-domain="{E(dom)}"' if dom else ""
            tags.append(f'<script defer{dom_attr} src="{E(sc)}"></script>\n')
        return "".join(tags)

    def page(self, path: str, title: str, desc: str, body: str, *, crumbs=None, ld=None,
             lastmod: datetime | None = None, priority: float = 0.5, noindex: bool = False,
             lang: str = "zh-CN") -> None:
        canonical = self.url(path)
        crumbs_html = ""
        ld = list(ld or [])
        is_en = lang.lower().startswith("en")
        if crumbs:
            items = [("Home" if is_en else "首页", "/en/" if is_en else "/")] + crumbs
            crumbs_html = f'<nav class="crumbs" aria-label="{"Breadcrumb" if is_en else "面包屑"}">' + " / ".join(
                f'<a href="{E(u)}">{E(t)}</a>' if i < len(items) - 1 else f"<span>{E(t)}</span>"
                for i, (t, u) in enumerate(items)
            ) + "</nav>"
            ld.append({
                "@context": "https://schema.org", "@type": "BreadcrumbList",
                "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": t, "item": self.url(u)} for i, (t, u) in enumerate(items)],
            })
        brand_lbl = "API Price Radar" if is_en else self.name
        full_title = (title if (path == "/" or brand_lbl in title) else f"{title} | {brand_lbl}")
        robots = '<meta name="robots" content="noindex,follow">' if noindex else ""
        nav_html = (
            '<a href="/en/">Overview</a><a href="/calc/">TCO Calc</a><a href="/verify/">Verify Model</a>'
            '<a href="/scenes/claude-code/">Claude Code</a><a href="/stations/">Relays</a>'
            '<a href="/about/">Method</a><a href="/submit/">Submit</a><a href="/">中文</a>'
            if is_en else
            '<a href="/scenes/">场景</a><a href="/calc/">算账</a><a href="/verify/">验真</a>'
            '<a href="/stations/">排行</a><a href="/compare/">对比</a><a href="/models/">模型</a><a href="/changes/">调价</a>'
            '<a href="/about/">方法</a><a href="/submit/">收录</a><a href="/en/" title="English Overview">EN</a>'
        )
        foot_html = (
            f'<p>Unprobed relays are hard-capped at Stage 1 (Grade B / 74). Only relays passing 7-day real-call probes unlock Stage 2 (A/S). Prices come from read-only public APIs and official models.dev / LiteLLM baselines; verify with small top-ups first.</p>'
            f'<p>Built {fmt_cst(self.now)} (UTC+8) · <a href="/calc/">TCO Calculator</a> · <a href="/verify/">Model Verifier</a> · <a href="/about/">Methodology &amp; Disclosure</a> · <a href="/submit/">Submit / Correct</a> · <a href="/">中文主站</a> · <a href="/sitemap.xml">Sitemap</a></p>'
            if is_en else
            f'<p>没有经过真实调用实测的站点最高只给 B 级（74 分），实测达标才能拿到 A / S 级。价格按站点公开只读接口、分组倍率与公开充值比例换算，未计入卡密折扣与活动；请以站点实时页面为准，大额充值前先小额测试。</p>'
            f'<p>本页生成于 {fmt_cst(self.now)}（北京时间）· <a href="/scenes/">场景选站</a> · <a href="/calc/">月度算账器</a> · <a href="/verify/">模型验真</a> · <a href="/about/">评分方法与利益披露</a> · <a href="/submit/">提交收录 / 纠错</a> · <a href="/en/">English</a> · <a href="/changes.xml">RSS</a> · <a href="/sitemap.xml">Sitemap</a></p>'
        )
        search_ph = "Search models, e.g. claude-sonnet-5-5 / gpt-6-astra" if is_en else "搜模型，如 deepseek-v4.1-flash / claude-sonnet-5-5"
        doc = f"""<!doctype html>
<html lang="{E(lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(full_title)}</title>
<meta name="description" content="{E(desc)}">
<link rel="canonical" href="{E(canonical)}">
{robots}
{self._verification_tags()}<meta property="og:type" content="website">
<meta property="og:site_name" content="{E(self.name)}">
<meta property="og:title" content="{E(full_title)}">
<meta property="og:description" content="{E(desc)}">
<meta property="og:url" content="{E(canonical)}">
<meta name="twitter:card" content="summary">
<link rel="alternate" type="application/rss+xml" title="{E(self.name)} 价格变动" href="/changes.xml">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="preload" href="/assets/fonts/inter-latin-var.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/assets/style.css?v={self.asset_ver}">
<script>try{{var t=localStorage.getItem('theme');if(t)document.documentElement.dataset.theme=t;}}catch(e){{}}</script>
{self._analytics_tag()}{''.join(jsonld(x) for x in ld)}
</head>
<body>
<header class="top">
  <div class="wrap top-in">
    <a class="brand" href="{'/' if not is_en else '/en/'}"><span class="logo" aria-hidden="true">◎</span>{E(brand_lbl)}</a>
    <div class="search" role="search">
      <input id="q" type="search" placeholder="{E(search_ph)}" autocomplete="off" aria-label="搜索模型">
      <div id="sug" class="sug" hidden></div>
    </div>
    <nav class="nav">
      {nav_html}
      <button id="theme" class="ghost" type="button" title="切换深浅色" aria-label="切换深浅色">◐</button>
    </nav>
  </div>
</header>
<main class="wrap">
{crumbs_html}
{body}
</main>
<footer class="foot">
  <div class="wrap">
    {foot_html}
  </div>
</footer>
<script src="/assets/app.js?v={self.asset_ver}" defer></script>
</body>
</html>
"""
        out = DIST / path.lstrip("/")
        if path.endswith("/"):
            out = out / "index.html"
        self._pending.append((out, doc))
        if not noindex:
            self.urls.append((path, (lastmod or self.now).astimezone(timezone.utc).strftime("%Y-%m-%d"), priority))


# ---------------------------------------------------------------- tables
def filter_bar(tid: str, *, show_mode: bool = True, show_ep: bool = True, show_view: bool = False) -> str:
    view_html = (
        '<span class="mode-bar view-bar" role="group" aria-label="表格视角">'
        '<button type="button" class="on" data-view="cny" aria-pressed="true" title="输入 / 输出 / 缓存读 / 缓存写 / 综合价（人民币每百万 token）">人民币价格视角</button>'
        '<button type="button" data-view="ratio" aria-pressed="false" title="模型倍率 / 补全倍率 / 缓存倍率 / 写缓存倍率 / 分组倍率 / 计费模式 + 公式溯源">原始倍率与公式视角</button>'
        '</span>'
    ) if show_view else ""
    mode_html = (
        '<span class="mode-bar" role="group" aria-label="计费视角">'
        '<button type="button" class="on" data-mode="mix" aria-pressed="true">对话 3:1</button>'
        '<button type="button" data-mode="cache" aria-pressed="false" title="70%缓存读 + 20%输入 + 10%输出">编程缓存 7:2:1</button>'
        '</span>'
    ) if show_mode else ""
    ep_html = (
        '<label>协议 <select data-ep="1">'
        '<option value="">全部协议</option>'
        '<option value="anthropic">Anthropic</option>'
        '<option value="openai-response">OpenAI 新版接口（Codex 等）</option>'
        '<option value="openai">OpenAI Chat</option>'
        '<option value="gemini">Gemini</option>'
        '<option value="image">生图</option>'
        '<option value="video">视频</option>'
        '</select></label>'
    ) if show_ep else ""
    return (
        f'<div class="filters" data-for="{tid}">{view_html}{mode_html}{ep_html}'
        '<label><input type="checkbox" data-hide="stale-row"> 隐藏 7 天未更新</label>'
        '<label><input type="checkbox" data-hide="unk-row"> 只看可换算人民币</label>'
        '<label><input type="checkbox" data-hide="lowsc-row"> 只看 B 级及以上</label>'
        '<label><input type="checkbox" data-hide="free-row" checked> 隐藏免费 / 福利 / 测试分组</label>'
        '</div>'
    )


def score_cell(q: P.Quote, st: dict[str, Any]) -> str:
    if q.score is None:
        return '<td class="sc" data-v="-1"><span class="muted" title="免费 / 公益 / 体验 / 测试分组不参与评分">不评分</span></td>'
    ps = "—" if q.price_score is None else f"{q.price_score:.0f}"
    sc: S.StationScore = st["score"]
    st_hint = "实测" if (sc.stage == 2 and not sc.cap_reason) else "未实测封顶"
    return (
        f'<td class="sc" data-v="{q.score}">{grade_badge(q.score)} <b>{q.score:.0f}</b>'
        f'<small title="{E(st_hint)} · 价格分相对常规中位价；站点分 {sc.total:.0f}">价 {ps} · 站 {sc.total:.0f}</small></td>'
    )


def _group_btn(q: P.Quote) -> str:
    n = len(q.all_groups or [])
    if n <= 1:
        return ""
    return f' <button type="button" class="gbtn" data-gdom="{E(q.domain)}">+{n - 1} 组</button>'


def _model_groups_json(rows: list[P.Quote]) -> str:
    """Compact per-model JSON map {domain: [[group, ratio_str, price_str, caveats, desc], ...]} for on-demand expansion."""
    data: dict[str, list[list[Any]]] = {}
    for q in rows:
        if q.domain in data or len(q.all_groups or []) <= 1:
            continue
        items = []
        for g in (q.all_groups or [])[:12]:
            ratio = "" if g.get("ratio") is None else f"×{g['ratio']:g}"
            if q.kind == "token":
                p = f"{money(g['in_cny'])}/{money(g['out_cny'])}" if g.get("in_cny") is not None else f"{money(g.get('in_usd'), '$')}/{money(g.get('out_usd'), '$')}"
                if g.get("cache_cny") is not None:
                    p += f" 缓{money(g['cache_cny'])}"
            else:
                p = f"{money(g['call_cny'])}/次" if g.get("call_cny") is not None else f"{money(g.get('call_usd'), '$')}/次"
            items.append([
                str(g.get("group") or "—")[:28],
                ratio,
                p,
                g.get("caveats") or [],
                (g.get("desc") or "")[:60],
            ])
        data[q.domain] = items
    if not data:
        return ""
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/json" id="m-groups">{raw}</script>'


TOKEN_RATIO_HEADS = (("模型倍率", "n"), ("补全倍率", "n"), ("缓存倍率", "n"), ("写缓存倍率", "n"),
                     ("分组倍率", "n"), ("计费模式", "s"), ("公式溯源", "s"))
THREE_LABELS = {
    "free": ("免费", "badge free", "站点明确配置 ×0（不是未填写的默认 0）"),
    "unknown": ("未公开", "muted", "站点未公开该价格；未公开不等于免费"),
    "na": ("不适用", "muted", "该型号不涉及此项计费（生图 / 向量 / 按次计费或不支持缓存）"),
}


def three_valued(q: P.Quote, cny: float | None, usd: float | None) -> str:
    """Strict three-valued cache semantics: priced / free (explicit ×0) / unknown (未公开) / na (不适用)."""
    v = cny if cny is not None else usd
    if v is not None:
        return "free" if v == 0 else "priced"
    return "na" if (q.cache_status == "na" or q.kind != "token") else "unknown"


def three_cell(status: str, cls: str = "") -> str:
    txt, span_cls, tip = THREE_LABELS[status]
    sortv = "0" if status == "free" else "9e9"
    return f'<td class="n{cls}" data-v="{sortv}"><span class="{span_cls}" title="{E(tip)}">{txt}</span></td>'


def quote_table(
    site: Site,
    rows: list[P.Quote],
    stations,
    *,
    show_model: bool,
    show_station: bool,
    table_id: str,
    title_prefix: str = "",
) -> str:
    cfg, now = site.cfg, site.now
    cx = "" if show_station else " cny-x"
    tok = [q for q in rows if q.kind == "token"]
    call = [q for q in rows if q.kind == "call"]
    parts = []

    def head(extra: list[tuple[str, str, str]]) -> str:
        cells: list[tuple[str, str, str]] = []
        if show_station:
            cells.append(("站点", "s", ""))
        if show_model:
            cells.append(("型号", "s", ""))
        cells.append(("综合评分", "n", ""))
        cells += extra
        cells += [("分组 / 协议", "s", ""), ("充值比例", "n", ""), ("更新", "n", "")]
        return "".join(
            f'<th data-type="{t}" class="{cls.strip()}" scope="col"{" aria-sort=descending" if h == "综合评分" else ""}><button type="button">{E(h)}</button></th>'
            for h, t, cls in cells
        )

    def lead(q: P.Quote, st: dict[str, Any]) -> str:
        c = []
        if show_station:
            sc: S.StationScore = st["score"]
            s_tag = ' <span class="badge ok">实测</span>' if (sc.stage == 2 and not sc.cap_reason) else ""
            c.append(
                f'<td class="st" data-v="{E(q.domain)}"><a href="/site/{E(q.domain)}/">{E(st["display_name"])}</a>{s_tag}'
                f'<small>{E(q.domain)}</small></td>'
            )
        if show_model:
            c.append(f'<td class="mn" data-v="{E(q.canonical)}"><code>{E(q.model)}</code></td>')
        c.append(score_cell(q, st))
        return "".join(c)

    def tail(q: P.Quote, st: dict[str, Any]) -> str:
        g = q.group or "—"
        ratio = "" if q.group_ratio is None else f"×{q.group_ratio:g}"
        desc = f' title="{E(q.group_desc[:200])}"' if q.group_desc else ""
        eps = P.endpoint_tags(q.endpoints)
        ep_pills = "".join(f'<span class="badge ep">{E(PLAIN_EP.get(x, x))}</span>' for x in eps[:3])
        cav_pills = "".join(f'<span class="badge caveat">{E(c)}</span>' for c in q.caveats)
        pills = f'<div class="bd">{ep_pills}{cav_pills}</div>' if (ep_pills or cav_pills) else ""
        more = _group_btn(q) if show_station else ""
        tp = st.get("topup_price")
        f = st.get("fetched_at")
        stale = is_stale(st, cfg, now)
        return (
            f'<td class="g" data-v="{E(g)}"{desc}><span>{E(g[:28])}</span> <small>{E(ratio)}</small>{more}{pills}</td>'
            f'<td class="n" data-v="{tp if tp is not None else 9e9}">'
            + (f"¥{tp:g}/$1" if tp is not None else f'<span class="muted" title="{E(st.get("topup_note") or "")}">未知</span>')
            + "</td>"
            f'<td class="t {"stale" if stale else "fresh"}" data-v="{int(f.timestamp()) if f else 0}" title="{E(fmt_cst(f))}">'
            f'{E(ago(f, now))}</td>'
        )

    def badges(q: P.Quote) -> str:
        b = ['<span class="badge free">免费</span>'] if q.free else []
        if q.median_ratio is not None and not q.free and not q.welfare:
            if q.median_ratio < 0.25:
                b.append(f'<span class="badge warn">中位价 {q.median_ratio:.2f}×</span>')
        b += [f'<span class="badge">{E(x)}</span>' for x in q.flags]
        return ("<div class='bd'>" + "".join(b) + "</div>") if b else ""

    def price_cell(cny: float | None, usd: float | None, strong: bool = False, cls: str = "") -> str:
        if cny is None and usd is None:
            return f'<td class="n{cls}" data-v="9e9">—</td>'
        sortv = cny if cny is not None else 9e8 + usd
        main = money(cny) if cny is not None else f'<span class="usd" title="站点未公开充值比例，显示美元额度">{money(usd, "$")}</span>'
        sub = f"<small>{money(usd, '$')}</small>" if (cny is not None and usd is not None and abs(cny - usd) > 1e-12) else ""
        return f'<td class="n{" hl" if strong else ""}{cls}" data-v="{sortv}">{main}{sub}</td>'

    def cache_cell(q: P.Quote, cny: float | None, usd: float | None, cls: str = "") -> str:
        return price_cell(cny, usd, cls=cls) if three_valued(q, cny, usd) == "priced" else three_cell(three_valued(q, cny, usd), cls)

    def ratio_cell(v: float | None) -> str:
        if v is None:
            return '<td class="n ratio-col" data-v="na">—</td>'
        return f'<td class="n ratio-col" data-v="{v:.10g}">×{v:.6g}</td>'

    def ratio_cells(q: P.Quote, st: dict[str, Any]) -> str:
        if show_station:
            return ""
        mode = q.billing_mode or ("倍率" if q.model_ratio is not None else ("按次" if q.kind == "call" else "—"))
        fml = P.format_formula(q, st.get("topup_price"))
        head_f, _, rest = fml.partition("；")
        f_html = (E(head_f) + (f"<small>{E(rest)}</small>" if rest else "")) if fml else "无可复核公式"
        cells = "" if q.kind != "token" else (
            ratio_cell(q.model_ratio) + ratio_cell(q.completion_ratio) + ratio_cell(q.cache_ratio) + ratio_cell(q.cache_write_ratio))
        return (
            cells + ratio_cell(q.group_ratio)
            + f'<td class="ratio-col">{E(mode)}</td>'
            + f'<td class="ratio-col fml">{f_html}</td>'
        )

    def mix_cell(q: P.Quote) -> str:
        mv = mix(q, cfg, "cny")
        cv = cache_mix_cny(q, cfg)
        l_mix = money(mv) if mv is not None else '<span class="muted">—</span>'
        vm = f"{mv:.6g}" if mv is not None else "9e9"
        vc = f"{cv:.6g}" if cv is not None else ""
        return f'<td class="n mix-col{cx}" data-v="{vm}" data-m="{vm}" data-c="{vc}">{l_mix}</td>'

    def row_cls(q: P.Quote, st: dict[str, Any], unknown: bool) -> str:
        return " ".join(x for x in (
            "stale-row" if is_stale(st, cfg, now) else "",
            "free-row" if (q.free or q.welfare) else "",
            "unk-row" if unknown else "",
            "lowsc-row" if (q.score is None or q.score < 65) else "",
        ) if x)

    def sub_row(q: P.Quote, cls: str, eps_csv: str) -> str:
        if not (q.flags or q.free):
            return ""
        return f'<tr class="sub {cls}" data-eps="{E(eps_csv)}"><td colspan="20">{badges(q)}</td></tr>'

    if tok:
        tok.sort(key=lambda q: rank_key(q, stations, cfg, now))
        body = []
        for q in tok:
            st = stations[q.domain]
            cls = row_cls(q, st, q.input_cny is None)
            eps_csv = ",".join(P.endpoint_tags(q.endpoints))
            body.append(
                f'<tr class="{cls}" data-eps="{E(eps_csv)}">{lead(q, st)}'
                f'{price_cell(q.input_cny, q.input_usd, strong=True)}{price_cell(q.output_cny, q.output_usd, cls=cx)}'
                f'{cache_cell(q, q.cache_cny, q.cache_usd, cx)}'
                + ("" if show_station else cache_cell(q, q.cache_write_cny, q.cache_write_usd, cx))
                + f'{mix_cell(q)}{ratio_cells(q, st)}'
                f'{tail(q, st)}</tr>{sub_row(q, cls, eps_csv)}'
            )
        parts.append(
            f'<h3 class="qt-h" id="{table_id}-token">{E(title_prefix)}按 Token 计费 <small>每百万 token 人民币（小字为站内美元额度）· 可切 7:2:1 缓存视角</small></h3>'
            f'<div class="tbl-wrap"><table class="qt sortable" id="{table_id}-t"><thead><tr>'
            + head([("输入", "n", ""), ("输出", "n", cx), ("缓存读", "n", cx)]
                   + ([] if show_station else [("缓存写", "n", cx)])
                   + [("3:1 综合价", "n", "mix-th" + cx)]
                   + ([] if show_station else [(h, t, "ratio-col") for h, t in TOKEN_RATIO_HEADS]))
            + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
        )
    if call:
        call.sort(key=lambda q: rank_key(q, stations, cfg, now))
        body = []
        for q in call:
            st = stations[q.domain]
            cls = row_cls(q, st, q.call_cny is None)
            eps_csv = ",".join(P.endpoint_tags(q.endpoints))
            body.append(
                f'<tr class="{cls}" data-eps="{E(eps_csv)}">{lead(q, st)}'
                f'{price_cell(q.call_cny, q.call_usd, strong=True)}{ratio_cells(q, st)}{tail(q, st)}</tr>{sub_row(q, cls, eps_csv)}'
            )
        parts.append(
            f'<h3 class="qt-h" id="{table_id}-call">{E(title_prefix)}按次计费 <small>每次调用人民币</small></h3>'
            f'<div class="tbl-wrap"><table class="qt sortable" id="{table_id}-c"><thead><tr>'
            + head([("每次", "n", "")] + ([] if show_station else [(h, t, "ratio-col") for h, t in TOKEN_RATIO_HEADS[4:]]))
            + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
        )
    return "".join(parts)


def probe_panel(sc: S.StationScore) -> str:
    pr = sc.probe
    if not pr:
        return ""
    bars = []
    for day, ok_n, tot in pr.get("daily") or []:
        rate = ok_n / tot if tot else 0
        h = max(4, int(rate * 26))
        cls = "bad" if rate < 0.8 else ("warn" if rate < 0.95 else "")
        bars.append(f'<i class="{cls}" style="height:{h}px" title="{E(day)}: {ok_n}/{tot} ({rate:.0%})"></i>')
    drift = pr.get("billing_drift_pct")
    drift_txt = f"{drift:+.1f}%" if drift is not None else "暂无对账样本"
    ttft = f"{pr['avg_ttft_ms']:.0f}ms" if pr.get("avg_ttft_ms") is not None else "—"
    tpot = f"{pr['avg_tpot_ms']:.1f}ms/tok" if pr.get("avg_tpot_ms") is not None else "—"
    tool_r = f"{pr['tool_call_pass_rate']:.0%}" if pr.get("tool_call_pass_rate") is not None else "—"
    cache_r = f"{pr['cache_hit_rate']:.0%}" if pr.get("cache_hit_rate") is not None else "—"
    return (
        f'<section class="probe-card">'
        f'<h3>真实调用实测证据 <small>近 {pr.get("window_days", 7)} 天 {pr.get("total_samples", 0)} 次实测 · 最后探测 {E(fmt_cst(parse_dt(pr.get("last_probed_at"))))}</small></h3>'
        f'<div class="kpis">'
        f'<div><b>{pr.get("success_rate", 0):.1%}</b><span>真实成功率</span></div>'
        f'<div><b>{ttft}</b><span>中位首字 TTFT（TPOT {tpot}）</span></div>'
        f'<div><b>{tool_r} / {cache_r}</b><span>工具调用 / 缓存命中</span></div>'
        f'<div><b>{drift_txt}</b><span>余额实扣 vs 公开标价偏差</span></div>'
        f'</div>'
        + (f'<div class="probe-bars" aria-label="近 7 日每日成功率">{"".join(bars)}</div>' if bars else "")
        + fidelity_kpis(pr)
        + "</section>"
    )


def _pct(v: Any) -> str:
    return f"{float(v):.0%}" if isinstance(v, (int, float)) else "—"


def fidelity_kpis(pr: dict[str, Any]) -> str:
    """Stage 2 authenticity / IQ evidence row: verified tier, pass rates, tamper count and 24h downgrade alert."""
    if not any(pr.get(k) is not None for k in ("auth_pass_rate", "iq_pass_rate", "context_pass_rate")):
        return ""
    tier = str(pr.get("verified_tier") or "L0")
    tamper = int(pr.get("tamper_accepted_count") or 0)
    alert = ""
    if pr.get("downgrade_alert"):
        alert = f'<p class="badge danger">近 24h 降级告警：{E("；".join(str(x) for x in pr.get("downgrade_reasons") or []))}</p>'
    return (
        '<div class="kpis">'
        f'<div><b>{E(tier)}</b><span>已验证档位（L1 真伪 · L2 智商 · L3 长文）</span></div>'
        f'<div><b>{_pct(pr.get("auth_pass_rate"))}</b><span>真伪通过率（{int(pr.get("auth_samples") or 0)} 次，否决线 85%）</span></div>'
        f'<div><b>{_pct(pr.get("iq_pass_rate"))} / {_pct(pr.get("context_pass_rate"))}</b><span>智商抽检 / 长文多跳通过率（否决线 65%）</span></div>'
        f'<div><b>{tamper}</b><span>篡改签名被接受次数（大于 0 直接否决）</span></div>'
        '</div>'
        + alert
    )


def score_panel(st: dict[str, Any], cfg: dict[str, Any], market_dims: dict[str, float] | None = None) -> str:
    """Section 03 card A 「七维评分拆解」: radar + per-dimension evidence. The grade and big score live only in the hero verdict."""
    sc: S.StationScore = st["score"]
    w = cfg.get("station_weights") or S.DEFAULT_STATION_WEIGHTS
    keys = [k for k in S.DIM_ORDER if k in sc.dims]
    pen = set(sc.penalized or [])
    lis = []
    for key in keys:
        d = sc.dims[key]
        wl_tag = '<b class="badge warn">短板拖累</b> ' if key in pen else ""
        li_cls = ' class="wl"' if key in pen else ""
        lis.append(
            f'<li{li_cls}><span class="dl">{PLAIN_DIM_LONG.get(key, S.DIM_LABELS[key])}<small>{w.get(key, 0)}%</small></span>'
            f'<span class="bar"><i style="width:{d.score:.0f}%"></i></span><b>{d.score:.0f}</b>'
            f'<small class="ev">{wl_tag}{E(d.evidence)}{"（缺数据，按中性值计）" if (not d.known and "样本不足" not in d.evidence and "未" not in d.evidence and "无" not in d.evidence and "尚" not in d.evidence) else ""}</small></li>'
        )
    pd = sc.dims["probe"]
    p_cls = "" if pd.known else "pending"
    p_wt = f"{int(S.PROBE_BLEND_WEIGHT * 100)}%" if sc.stage == 2 else "未计"
    lis.append(
        f'<li class="{p_cls}"><span class="dl">真实实测<small>{p_wt}</small></span>'
        f'<span class="bar"><i style="width:{pd.score if pd.known else 0:.0f}%"></i></span>'
        f'<b>{f"{pd.score:.0f}" if pd.known else "—"}</b>'
        f'<small class="ev">{E(pd.evidence)}</small></li>'
    )
    cap_line = ""
    formula = f"加权均值 {sc.linear_total:.1f}" + (f" − 短板拖累 {sc.penalty:.1f}" if sc.penalty else "")
    if sc.stage == 1 and sc.raw_total - sc.total >= 0.05:
        cap_line = f'<small class="muted">{formula} = 公开信号分 {sc.raw_total:.1f} → 还没人工实测，按规则压到 {sc.total:.1f}（最高 74 分）</small>'
    elif sc.cap_reason:
        cap_line = f'<small class="badge danger">{E(sc.cap_reason)}</small>'
    elif sc.penalty:
        cap_line = f'<small class="muted">{formula} = {sc.raw_total:.1f}</small>'
    pct_line = ""
    if sc.percentile is not None:
        pcls = "ok" if sc.percentile >= 75 else ("info" if sc.percentile >= 40 else "warn")
        pct_line = (f'<span class="pct-line"><span class="badge {pcls}">超过全网 {sc.percentile:.0f}% 站点</span>'
                    f'<small class="muted" title="按缺失维度与证据样本估算的分数不确定度">±{sc.uncertainty:.1f} · 证据完整度 {sc.completeness:.0%}</small></span>')
    radar = CH.station_radar_svg(
        {k: sc.dims[k].score for k in keys}, market_dims, labels=PLAIN_DIM_SHORT, order=S.DIM_ORDER, penalized=pen,
        name=str(st.get("display_name") or "本站")[:16],
    )
    return (
        '<section class="deep-card score-break" aria-labelledby="sb-h">'
        f'<div class="deep-h"><h3 id="sb-h">七维评分拆解</h3>{pct_line}</div>'
        f'<p class="sb-note">综合 {sc.total:.1f} 分 · 证据可信度{E(sc.confidence)}{(" · " + cap_line) if cap_line else ""}</p>'
        f'<div class="sb-grid"><div class="sb-radar">{radar}</div><ul class="dims">{"".join(lis)}</ul></div>'
        '</section>'
    )


# ---------------------------------------------------------------- build helpers
def pick(qs: list[P.Quote], stations, cfg, now, by: str) -> P.Quote | None:
    """by='score' -> reliable Standard-lane recommended; by='price' -> cheapest Standard-lane."""
    n_tok = sum(1 for q in qs if q.kind == "token" and q.lane == "standard")
    primary = "token" if n_tok * 2 >= max(1, sum(1 for q in qs if q.lane == "standard")) else "call"
    strict = [q for q in qs if q.kind == primary and is_reliable_rec(q, stations[q.domain], cfg, now)]
    fallback = [
        q for q in qs
        if q.kind == primary and q.lane == "standard" and not (q.free or q.welfare or q.anomaly)
        and main_cny(q, cfg) is not None and not is_stale(stations[q.domain], cfg, now)
    ]
    cands = strict or fallback
    if not cands:
        return None
    if by == "score":
        return min(cands, key=lambda q: rank_key(q, stations, cfg, now))
    return min(cands, key=lambda q: price_key(q, cfg))


def price_txt(q: P.Quote) -> str:
    if q.kind == "token":
        return f"输入 {money(q.input_cny)} / 输出 {money(q.output_cny)}（每百万 token）"
    return f"{money(q.call_cny)}/次"


def short_price(q: P.Quote) -> str:
    if q.kind == "token":
        s = f"输入 {money(q.input_cny)} · 输出 {money(q.output_cny)} /百万"
        if q.cache_cny is not None:
            s += f" · 缓存 {money(q.cache_cny)}"
        return s
    return f"{money(q.call_cny)}/次"


def who(q: P.Quote, stations) -> str:
    st = stations[q.domain]
    name = st["display_name"]
    where = f"{q.domain}，" if name != q.domain else ""
    return f"{name}（{where}{q.group or 'default'} 分组）"


def asset_tree(root: Path | None = None) -> list[Path]:
    """Every file under assets/ (recursively, e.g. assets/fonts/*.woff2), in a stable order."""
    root = root or ASSETS
    return [f for f in sorted(root.rglob("*")) if f.is_file()]


def asset_version(files: list[Path], root: Path | None = None) -> str:
    """Cache-busting hash over relative paths + contents, so renames and nested files both bump the version."""
    root = root or ASSETS
    h = hashlib.sha1()
    for f in files:
        h.update(f.relative_to(root).as_posix().encode("utf-8") + b"\0" + f.read_bytes())
    return h.hexdigest()[:8]


def copy_assets(dist: Path, root: Path | None = None) -> list[Path]:
    """Mirror assets/ into dist/assets/ keeping relative paths (fonts/ etc.); returns the source files copied."""
    root = root or ASSETS
    files = asset_tree(root)
    for f in files:
        out = dist / "assets" / f.relative_to(root)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, out)
    return files


def build(cfg: dict[str, Any]) -> dict[str, Any]:
    import time as _time
    _t0 = _time.perf_counter()
    def _log(msg: str) -> None:
        print(f"[build +{_time.perf_counter() - _t0:.1f}s] {msg}", file=sys.stderr, flush=True)

    now = datetime.now(timezone.utc)
    quotes, stations = load_snapshots(cfg, now)
    _log(f"loaded {len(stations)} stations, {len(quotes)} quotes")
    if not quotes:
        sys.exit("no snapshots in data/raw — run tools/refresh.py fetch first")
    global DIST
    final_dist = DIST
    DIST = ROOT / "dist.tmp"
    if DIST.exists():
        shutil.rmtree(DIST, ignore_errors=True)
        _log("cleaned old dist.tmp")
    (DIST / "assets").mkdir(parents=True, exist_ok=True)
    asset_files = copy_assets(DIST)
    asset_ver = asset_version(asset_files)
    site = Site(cfg, now, asset_ver)
    min_st = int(cfg.get("min_stations_for_model_page", 3))
    index_min_st = int(cfg.get("index_min_stations", 10))

    intel = psl.sanitize_intel(load_json(DATA / "intel.json", {}))
    probe_map = probes.load_probe_summary(DATA)
    specs = MS.load_model_specs(DATA)
    quotes = merge_mirrors(quotes, stations, intel)
    closed_stations = load_closed_stations(cfg, stations)

    by_model: dict[str, list[P.Quote]] = defaultdict(list)
    by_station: dict[str, list[P.Quote]] = defaultdict(list)
    for q in quotes:
        by_model[q.canonical].append(q)
        by_station[q.domain].append(q)
    run_stats = load_run_stats(now)
    mark_anomalies(by_model, cfg)
    changes = update_history(quotes, stations, now)
    config_log = update_config_changes(stations, now)
    stability_map = SI.compute_price_stability(changes)
    monitored = len(list(HIST.glob("*.json.gz"))) >= 2
    market_dims = compute_station_scores(stations, by_station, intel, run_stats, cfg, probe_map,
                                         stability_map=stability_map, now=now, monitored=monitored)
    compute_quote_scores(by_model, stations, cfg)
    try:
        ledger = TS.update_ledger(DATA, now)
    except Exception as e:  # noqa: BLE001  (the 30-day ledger must never break a build)
        _log(f"timeseries ledger skipped: {type(e).__name__} {e}")
        ledger = TS.empty_ledger()
    cx = consumer_context(stations, by_station, specs, cfg, changes, ledger=ledger, intel=intel, run_stats=run_stats,
                          stability_map=stability_map, monitored=monitored)
    _log(f"consumer context ready ({len(ledger['s'])} stations in 30-day ledger)")

    def dedupe_per_lane(qs: list[P.Quote]) -> list[P.Quote]:
        """One representative row per (station, billing kind, lane): cheapest within that lane."""
        best: dict[tuple[str, str, str], P.Quote] = {}
        for q in qs:
            k = (q.domain, q.kind, q.lane)
            if k not in best or price_key(q, cfg) < price_key(best[k], cfg):
                best[k] = q
        return list(best.values())

    model_pages: dict[str, dict[str, Any]] = {}
    slug_used: dict[str, str] = {}
    for canon, qs in by_model.items():
        rows = dedupe_per_lane(qs)
        n_st = len({q.domain for q in rows})
        if n_st < min_st:
            continue
        slug = P.slugify(canon)
        if slug in slug_used and slug_used[slug] != canon:
            slug = f"{slug}-{hashlib.sha1(canon.encode()).hexdigest()[:6]}"
        slug_used[slug] = canon
        std_ins = sorted(q.input_cny for q in rows if q.kind == "token" and q.lane == "standard" and not q.anomaly and q.input_cny is not None)
        model_pages[canon] = {
            "slug": slug, "rows": rows, "n": n_st, "family": P.classify_family(canon),
            "rec": pick(rows, stations, cfg, now, "score"), "cheap": pick(rows, stations, cfg, now, "price"),
            "median_in": std_ins[len(std_ins) // 2] if std_ins else None,
            "indexed": n_st >= index_min_st,
            "lastmod": max(stations[q.domain]["fetched_at"] for q in rows),
        }

    reviews_map = SI.load_reviews(DATA)
    _log(f"scored {len(model_pages)} models, starting page render")
    build_model_pages(site, model_pages, stations, cfg, specs)
    market_px = flagship_market(by_model, specs, cfg)
    build_station_pages(site, stations, by_station, intel, changes, cfg, run_stats=run_stats, stability_map=stability_map,
                        reviews_map=reviews_map, market_dims=market_dims, market_px=market_px, cx=cx)
    build_closed_station_pages(site, closed_stations)
    fam_order, fam_models = build_indexes(site, model_pages, stations, by_station, closed_stations, cfg, min_st, intel=intel, stability_map=stability_map, cx=cx)
    build_changes(site, changes, stations, model_pages)
    build_about(site, cfg)
    submit_page.build_submit(site, cfg, len(stations), load_json(DATA / "mine-last.json", None), n_stage0=len(closed_stations))

    helpers = {
        "money": money, "grade_badge": grade_badge, "rank_key": rank_key,
        "short_price": short_price, "cache_mix_cny": cache_mix_cny,
        "is_reliable_rec": is_reliable_rec, "is_stale": is_stale, "fmt_cst": fmt_cst,
    }
    scenes.build_scenes(site, model_pages, stations, helpers)
    calc_page.build_calc(site, model_pages, stations, specs, helpers, DIST)
    verify_page.build_verify(site)
    cmp_payload = CP.stations_payload(stations, by_station, intel, stability_map, now, FLAGSHIP_MODELS)
    _log(f"compare inline data {CP.gz_size(cmp_payload) / 1024:.1f} KB gzipped ({len(cmp_payload['rows'])} stations)")
    CP.build_compare(site, cmp_payload)
    en_page.build_en(site, model_pages, stations, specs, len(closed_stations), helpers)
    build_home(site, cfg, model_pages, stations, closed_stations, fam_order, fam_models, changes)
    site.page("/404.html", "页面不存在", "页面不存在",
              '<section class="hero sm"><h1>页面不存在</h1><p class="lead">试试顶部搜索框，或 <a href="/scenes/">按场景选站</a> / <a href="/models/">浏览全部模型</a>。</p></section>', noindex=True)
    _log(f"rendered {len(site._pending)} HTML pages in memory, flushing to disk")

    idx = []
    for canon, mp in model_pages.items():
        r, c = mp["rec"], mp["cheap"]
        idx.append({
            "m": canon, "s": mp["slug"], "f": mp["family"], "n": mp["n"],
            "p": (round(c.input_cny, 4) if c and c.kind == "token" and c.input_cny is not None else None),
            "o": (round(c.output_cny, 4) if c and c.kind == "token" and c.output_cny is not None else None),
            "c": (round(c.call_cny, 4) if c and c.kind == "call" and c.call_cny is not None else None),
            "r": (stations[r.domain]["display_name"] if r else None),
            "g": (S.grade(r.score) if r else None),
            "v": sorted({q.model for q in mp["rows"]} - {canon})[:8],
        })
    idx.sort(key=lambda x: -x["n"])
    write(DIST / SEARCH_INDEX, json.dumps(idx, ensure_ascii=False, separators=(",", ":")))
    _log(f"station config changes this run: {sum(len(v) for v in config_log.values())}")
    build_seo_files(site, model_pages, stations, len(closed_stations))
    export_readme.export_github_readme(
        site, model_pages, stations, len(closed_stations), helpers,
        [DIST / "GITHUB_README.md"],
        readme_path=ROOT / "README.md",
    )
    write(DIST / "favicon.svg", '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="28" fill="#1456d4"/><circle cx="32" cy="32" r="16" fill="none" stroke="#fff" stroke-width="5"/><circle cx="32" cy="32" r="5" fill="#fff"/></svg>')
    inow = str(cfg.get("indexnow_key") or "").strip()
    if inow:
        write(DIST / f"{inow}.txt", inow + "\n")
    write(DIST / "_headers", f"/assets/*\n  Cache-Control: public, max-age=31536000, immutable\n/{SEARCH_INDEX}\n  Cache-Control: public, max-age=600\n/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n")
    site.flush()
    _log("flushed HTML, swapping dist")
    swap_dist(DIST, final_dist)
    _log("done swap_dist")
    DIST = final_dist
    grades = defaultdict(int)
    stages = defaultdict(int)
    for st in stations.values():
        grades[st["score"].grade] += 1
        stages[f"stage{st['score'].stage}"] += 1
    stages["stage0"] = len(closed_stations)
    return {
        "stations": len(stations),
        "stage0_stations": len(closed_stations),
        "model_pages": len(model_pages),
        "indexed_models": sum(1 for mp in model_pages.values() if mp["indexed"]),
        "quotes": len(quotes),
        "urls": len(site.urls),
        "changes": len(changes),
        "station_grades": dict(sorted(grades.items())),
        "stages": dict(sorted(stages.items())),
    }


def swap_dist(tmp: Path, final: Path) -> None:
    """Replace `final` with `tmp`. Falls back to in-place sync when Windows locks the directory."""
    import time

    old = final.with_name(final.name + ".old")
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    for attempt in range(3):
        try:
            if final.exists():
                final.rename(old)
            break
        except OSError:
            time.sleep(0.5 * (attempt + 1))
    else:
        sync_tree(tmp, final)
        shutil.rmtree(tmp, ignore_errors=True)
        print("swap_dist: rename refused, synced dist/ in place", file=sys.stderr)
        return
    tmp.rename(final)
    shutil.rmtree(old, ignore_errors=True)


def sync_tree(src: Path, dst: Path) -> None:
    """Make dst identical to src: copy every file in parallel, then delete stale files/dirs."""
    want = {p.relative_to(src) for p in src.rglob("*")}
    files: list[tuple[Path, Path]] = []
    for rel in sorted(want):
        s, d = src / rel, dst / rel
        if s.is_dir():
            d.mkdir(parents=True, exist_ok=True)
        else:
            d.parent.mkdir(parents=True, exist_ok=True)
            files.append((s, d))
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda pair: shutil.copy2(pair[0], pair[1]), files))
    for p in sorted(dst.rglob("*"), key=lambda x: -len(x.parts)):
        if p.relative_to(dst) not in want:
            try:
                p.rmdir() if p.is_dir() else p.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- model pages
def _spec_bar(sp: dict[str, Any] | None, ref_quote: P.Quote | None, cfg: dict[str, Any]) -> str:
    if not sp:
        return ""
    usd_rate = float(cfg.get("usd_cny_rate", 7.25))
    pills: list[str] = []
    ctx_s = MS.fmt_ctx(sp.get("context"))
    if ctx_s:
        pills.append(f'<span class="badge ep">{E(ctx_s)} 上下文</span>')
    out_s = MS.fmt_ctx(sp.get("max_output"))
    if out_s:
        pills.append(f'<span class="badge">{E(out_s)} 最大输出</span>')
    if sp.get("reasoning"):
        pills.append('<span class="badge info">深度思考</span>')
    if sp.get("tool_call"):
        pills.append('<span class="badge">工具调用</span>')
    if sp.get("vision"):
        pills.append('<span class="badge">多模态</span>')
    if sp.get("long_tier"):
        pills.append('<span class="badge warn" title="超过 200K/272K 长上下文时官网按高档加价">超长上下文加价</span>')

    price_parts: list[str] = []
    in_u, out_u, cache_u = sp.get("in_usd"), sp.get("out_usd"), sp.get("cache_usd")
    if isinstance(in_u, (int, float)) and isinstance(out_u, (int, float)) and (in_u > 0 or out_u > 0):
        in_c, out_c = in_u * usd_rate, out_u * usd_rate
        p_txt = f'官网参考价 <b>{money(in_u, "$")} / {money(out_u, "$")}</b><small>（约 {money(in_c)} / {money(out_c)} 每百万 token）</small>'
        if isinstance(cache_u, (int, float)) and cache_u > 0:
            p_txt += f'<small>· 缓存读 {money(cache_u, "$")}</small>'
        price_parts.append(p_txt)
        if ref_quote and ref_quote.kind == "token":
            rel_mix = mix(ref_quote, cfg, "cny")
            wi, wo = float(cfg.get("mix_input_weight", 3)), float(cfg.get("mix_output_weight", 1))
            off_mix = (in_c * wi + out_c * wo) / (wi + wo)
            if rel_mix is not None and off_mix > 0:
                ratio = rel_mix / off_mix
                bcls = "warn" if (ratio < 0.15 or ratio > 1.05) else "ok"
                price_parts.append(
                    f'<span class="badge {bcls}" title="以 3:1 综合价对比官网直连估算（按 1 USD ≈ ¥{usd_rate:g}）">'
                    f'常规推荐约官网 {ratio * 100:.0f}%（{ratio * 10:.1f}折）</span>'
                )
    if not pills and not price_parts:
        return ""
    price_parts.append('<a href="/calc/">算月度总账 →</a>')
    return (
        '<div class="spec-bar">'
        f'<div class="spec-pills">{"".join(pills)}</div>'
        f'<div class="spec-price">{"".join(price_parts)} <small class="muted">来源: models.dev / LiteLLM</small></div>'
        '</div>'
    )


def model_dist_chart(canon: str, std_rows: list[P.Quote], alt_rows: list[P.Quote], rec: P.Quote | None,
                     sp: dict[str, Any] | None, cfg: dict[str, Any]) -> str:
    tok = [q.input_cny for q in std_rows if q.kind == "token" and not q.anomaly and q.input_cny]
    if len(tok) >= 3:
        off = (sp or {}).get("in_usd")
        alt = [q.input_cny for q in alt_rows if q.lane == "promo" and q.kind == "token" and q.input_cny]
        svg = CH.model_price_dist_svg(
            CH.price_stats(tok),
            official=off * float(cfg.get("usd_cny_rate", 7.25)) if isinstance(off, (int, float)) and off > 0 else None,
            rec=rec.input_cny if (rec and rec.kind == "token") else None,
            alt_median=CH.quantile(alt, .5) if alt else None,
        )
    else:
        calls = [q.call_cny for q in std_rows if q.kind == "call" and not q.anomaly and q.call_cny]
        alt = [q.call_cny for q in alt_rows if q.lane == "promo" and q.kind == "call" and q.call_cny]
        svg = CH.model_price_dist_svg(
            CH.price_stats(calls), rec=rec.call_cny if (rec and rec.kind == "call") else None,
            alt_median=CH.quantile(alt, .5) if alt else None, what="单次调用价",
        )
    return f'<div class="chart-card">{svg}</div>' if svg else ""


def build_model_pages(site: Site, model_pages, stations, cfg, specs: dict[str, dict[str, Any]] | None = None) -> None:
    specs = specs or {}
    now = site.now
    date = fmt_cst(now, False)
    for canon, mp in model_pages.items():
        rows, rec, cheap = mp["rows"], mp["rec"], mp["cheap"]
        std_rows = [q for q in rows if q.lane == "standard"]
        alt_rows = [q for q in rows if q.lane != "standard"]
        fam_label = P.FAMILY_LABELS.get(mp["family"], mp["family"])
        rec_txt = f"{who(rec, stations)}，综合 {S.grade(rec.score)} 级 {rec.score:.0f} 分，{price_txt(rec)}" if rec else "暂无符合常规推荐门槛的付费报价"
        cheap_txt = f"{who(cheap, stations)}，{price_txt(cheap)}" if cheap else "暂无可换算人民币的常规付费报价"
        title = f"{canon} API 中转推荐：{mp['n']} 家站点分层评分与价格对比"
        desc = f"{canon} 中转 API 哪家靠谱又便宜？截至 {date} 收录 {mp['n']} 家站点，常规按量推荐：{rec_txt}；常规最低价：{cheap_txt}。"

        strict_ranked = sorted(
            [q for q in std_rows if is_reliable_rec(q, stations[q.domain], cfg, now)],
            key=lambda q: rank_key(q, stations, cfg, now),
        )
        if not strict_ranked:
            strict_ranked = sorted(
                [q for q in std_rows if q.score is not None and not q.anomaly and main_cny(q, cfg) is not None and not is_stale(stations[q.domain], cfg, now)],
                key=lambda q: rank_key(q, stations, cfg, now),
            )
        seen, top = set(), []
        for q in strict_ranked:
            if q.domain in seen:
                continue
            seen.add(q.domain)
            top.append(q)
            if len(top) == 3:
                break
        top_cards = "".join(
            f'<a class="rec" href="/site/{E(q.domain)}/"><span class="rk">#{i + 1}</span>{grade_badge(q.score)}'
            f'<div><b>{E(stations[q.domain]["display_name"])}</b><small><span class="nw">{E((q.group or "default")[:24])} 分组</span> · <span class="nw">综合 {q.score:.0f}</span> <span class="nw">（价 {"—" if q.price_score is None else f"{q.price_score:.0f}"} · 站 {stations[q.domain]["score"].total:.0f}）</span></small>'
            f'<span class="pr">{E(short_price(q))}</span></div></a>'
            for i, q in enumerate(top)
        )
        normal_tok = [q for q in std_rows if q.kind == "token" and not q.anomaly and q.input_cny is not None]
        dist_html = model_dist_chart(canon, std_rows, alt_rows, rec, specs.get(canon), cfg)
        kpis = ""
        if normal_tok:
            ins = sorted(q.input_cny for q in normal_tok)
            caches = [q.cache_cny for q in normal_tok if q.cache_cny is not None]
            st2 = len({q.domain for q in std_rows if stations[q.domain]["score"].stage == 2 and not stations[q.domain]["score"].cap_reason})
            kpis = (
                '<div class="kpis">'
                f'<div><b>{money(ins[0])}</b><span>常规最低输入 / 百万</span></div>'
                f'<div><b>{money(ins[len(ins) // 2])}</b><span>常规输入中位数</span></div>'
                f'<div><b>{money(sorted(caches)[len(caches) // 2]) if caches else "—"}</b><span>缓存读中位数（{len(caches)} 站）</span></div>'
                f'<div><b>{mp["n"]}</b><span>收录站点（实测认证 {st2}）</span></div>'
                "</div>"
            )
        faq = [
            {"@type": "Question", "name": f"{canon} API 哪家中转站比较靠谱？",
             "acceptedAnswer": {"@type": "Answer", "text": f"截至 {date}（北京时间），在剔除自称 Kiro/Cursor/逆向/特惠及未核实极端低价分组后，常规按量综合评分最高的是 {rec_txt}。未实测站点评级封顶 B 级（74 分），大额充值前请先小额测试。"}},
            {"@type": "Question", "name": f"{canon} API 为什么同一站点有多个分组和价差？",
             "acceptedAnswer": {"@type": "Answer", "text": "许多中转站同时挂有高稳常规分组与低价逆向/特惠分组（价差可达 3–6 倍）。表格中点击「展开全部分组」可核对同站所有分组的原名、倍率与说明。"}},
            {"@type": "Question", "name": "为什么未实测站点最高只有 B 级？",
             "acceptedAnswer": {"@type": "Answer", "text": "仅凭公开价格接口与域名资历无法证明真实调用稳定性与模型保真度，因此未接入连续 7 天真金白银探针实测的站点统一封顶 B 级（74 分），只有实测达标后才解锁 A/S 认证。"}},
        ]
        std_tbl = quote_table(site, std_rows, stations, show_model=True, show_station=True, table_id="m", title_prefix="常规按量 · ")
        alt_section = ""
        if alt_rows:
            alt_tbl = quote_table(site, alt_rows, stations, show_model=True, show_station=True, table_id="ma", title_prefix="特惠 / 逆向 / 福利 · ")
            alt_section = (
                f'<details class="groups"><summary>特惠 / 逆向 / 福利分组对照（{len(alt_rows)} 条 · 已从常规推荐区隔离）</summary>'
                f'<p class="muted">以下条目在站点分组名或说明中自称 Kiro、Cursor、逆向/反代、特惠/限时或免费福利测试，单独展示供核对，不参与常规 Top 3 推荐。</p>'
                f'{alt_tbl}</details>'
            )
        variants = sorted({q.model for q in rows})
        spec_html = _spec_bar(specs.get(canon), rec or cheap, cfg)
        body = f"""
<section class="hero sm">
  <p class="eyebrow">{E(fam_label)} · {mp['n']} 家站点 · 更新于 {E(fmt_cst(mp['lastmod']))}</p>
  <h1>{E(canon)} API 中转推荐与价格对比</h1>
  <p class="lead">常规按量最低价：<strong>{E(cheap_txt)}</strong>。推荐区已自动隔离自称 Kiro/逆向/特惠及低于中位价 0.25× 的未实测极低价分组。</p>
  {spec_html}
  {kpis}
</section>
{('<section class="sec-block">' + DB.section_head("01", "常规按量推荐", "未经过真实调用实测的站最高 B 级；已隔离自称逆向、特惠与未核实极低价分组", "sec-rec") + '<div class="recs">' + top_cards + '</div>' + dist_html + '</section>') if top_cards else dist_html}
<section class="sec-block">
{DB.section_head("02" if top_cards else "01", "全部站点报价", "按接口、计费方式筛选，点表头排序；点分组列可展开同站全部分组与原文说明", "sec-quotes")}
{filter_bar("m", show_mode=bool(any(q.kind == "token" for q in rows)), show_ep=True)}
{std_tbl or '<p class="muted">该型号暂无常规按量报价，请展开下方特惠/福利分组查看。</p>'}
{alt_section}
</section>
<section class="note">
  <h2>怎么读这张表</h2>
  <ul>
    <li><b>Stage 1 / Stage 2 封顶机制</b>：未经过真金白银探针实测的站点与报价一律最高 <b>B 级（74 分）</b>；接入连续 7 天真实调用探针且可用率 ≥ 90%、账单实扣偏差 ≤ ±15% 才解锁 A / S 级。详见 <a href="/about/">评分方法</a>。</li>
    <li><b>多分组展开</b>：同一站点同时提供常规组与特惠/高稳组时，点击分组列的「+展开全部分组」可看该站本型号所有可选分组与原文说明。</li>
    <li><b>计费视角切换</b>：「对话 3:1」按 75% 输入 + 25% 输出核算；「编程缓存 7:2:1」按 70% 缓存读 + 20% 输入 + 10% 输出核算（适合 Claude Code / Cursor / Agent）。</li>
    <li>收录的站点原始型号名：{''.join(f'<code>{E(v)}</code> ' for v in variants[:30])}{'…' if len(variants) > 30 else ''}</li>
  </ul>
</section>
{_model_groups_json(rows)}"""
        site.page(
            f"/model/{mp['slug']}/", title, desc, body,
            crumbs=[("模型", "/models/"), (fam_label, f"/family/{mp['family']}/"), (canon, f"/model/{mp['slug']}/")],
            ld=[{"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": faq}],
            lastmod=mp["lastmod"], priority=0.8 if mp["indexed"] else 0.4,
            noindex=not mp["indexed"],
        )


# ---------------------------------------------------------------- station pages
def _meter(pct: int | None, cls: str, label: str) -> str:
    v = 0 if pct is None else max(0, min(100, int(pct)))
    return (
        f'<div class="meter {cls}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{v}" '
        f'aria-label="{E(label)}"><i style="width:{v}%"></i></div>'
    )


def station_deep_dive(
    dom: str,
    st: dict[str, Any],
    qs: list[P.Quote],
    it: dict[str, Any],
    rs: dict[str, Any],
    stab: dict[str, Any] | None,
    revs: list[dict[str, Any]],
    now: datetime | None = None,
) -> tuple[str, str]:
    """(Section 03 card B 「真伪抽检与社区口碑」, Section 04 raw 防暗涨 + 开站时长 evidence cards).

    Card B merges the authenticity / IQ self-test CTA, a one-line domain summary and community reviews. The two raw evidence
    cards keep every stability / registration detail but live in a collapsed drawer, so nothing duplicates the hero or peer table.
    """
    sc: S.StationScore = st["score"]
    # 1. Price Stability card
    stab = {**SI.default_stability(), **(stab or {})}
    score = int(stab.get("stability_score", 100))
    band = stab.get("cls") or SI.stability_band(score)
    up_txt = f"{stab['ups']} 条（{stab['hike_events']} 次调价，最大 +{stab['max_up_pct']:g}%）" if stab["ups"] else "0 条"
    dn_txt = f"{stab['downs']} 条（最大 -{stab['max_down_pct']:g}%）" if stab["downs"] else "0 条"
    chips = []
    if stab["sharp_events"]:
        chips.append(f'<span class="badge danger">急涨 ×{stab["sharp_events"]}</span>')
    if stab["minor_hikes"]:
        chips.append(f'<span class="badge warn">小幅上调 ×{stab["minor_hikes"]}</span>')
    if stab["downs"]:
        chips.append(f'<span class="badge ok">降价 ×{stab["downs"]}</span>')
    if stab["rounding"]:
        chips.append(f'<span class="badge">舍入微调 ×{stab["rounding"]}</span>')
    last_up = f'<dt>最近涨价</dt><dd>{E(str(stab["last_up_at"])[:10])}</dd>' if stab.get("last_up_at") else ""
    card_price = (
        '<div class="bcard">'
        f'<div class="bcard-h"><h3>防暗涨记录（近 30 天）</h3><span class="badge {stab["cls"]}">{E(stab["label"])}</span></div>'
        f'<div class="gauge"><b class="{band}">{score}</b><span>/ 100 防暗涨分</span>{_meter(score, band, "防暗涨分")}</div>'
        + CH.price_impulse_svg(stab.get("impulses") or [], now)
        + (f'<div class="bd">{"".join(chips)}</div>' if chips else "")
        + f'<p class="bnote">{E(stab["volatility_summary"])}</p>'
        '<dl class="bkv">'
        f'<dt>涨价记录</dt><dd>{E(up_txt)}</dd>'
        f'<dt>降价记录</dt><dd>{E(dn_txt)}</dd>'
        f'{last_up}'
        f'<dt>型号变动</dt><dd>上新 {stab["news"]} · 下架 {stab["removed"]} · 输出档调整 {stab["outs"]}</dd>'
        '</dl></div>'
    )

    # 2. Domain Longevity & Infra card
    days = it.get("domain_age_days")
    shared = it.get("shared_host")
    age_s, age_long, age_cls = SI.fmt_domain_age(days)
    if shared:
        age_s, age_long, age_cls = f"托管二级域名（*.{shared}）", "无法通过域名核实建站年限", "warn"
    lpct = SI.longevity_pct(days)
    reg_at = it.get("domain_registered_at") or "未知"
    exp_at = it.get("domain_expires_at") or "未知"
    registrar = (it.get("registrar") or "未公开")[:44]
    risk = SI.expiry_risk(it.get("domain_expires_at"), now)
    exp_badge = ""
    if risk and risk["level"] == "expired":
        exp_badge = ' <span class="badge danger">域名已过期</span>'
    elif risk and risk["level"] == "soon":
        exp_badge = f' <span class="badge warn">{risk["days_left"]} 天后到期</span>'
    ranks = []
    if it.get("tranco_rank"):
        ranks.append(f"Tranco #{int(it['tranco_rank']):,}")
    if it.get("umbrella_rank"):
        ranks.append(f"Umbrella #{int(it['umbrella_rank']):,}")
    if it.get("majestic_rank"):
        ranks.append(f"Majestic #{int(it['majestic_rank']):,}")
    rank_s = " · ".join(ranks) if ranks else "暂未进入全球公开流量榜"
    if shared:
        reg_line = f"无法通过域名核实建站年限（站点挂在 *.{shared} 平台的二级域名下）"
        exp_line = "不适用（平台主域名的注册信息不代表这家站）"
        rank_s = "不适用（平台主域名的流量不代表这家站）"
    else:
        reg_line = f"{reg_at} · {age_long}"
        exp_line = None
    att, suc = int(rs.get("attempts") or 0), int(rs.get("successes") or 0)
    ms_list = rs.get("ms") or []
    med_ms = f"{statistics.median(ms_list):.0f}ms" if ms_list else "—"
    uptime_s = f"{suc}/{att}（{suc / att:.0%}）· 中位响应 {med_ms}" if att else "样本累计中"
    lm_cls = "muted" if lpct is None else ("ok" if lpct >= 50 else ("info" if lpct >= 17 else "warn"))
    card_domain = (
        '<div class="bcard">'
        f'<div class="bcard-h"><h3>开站时长与知名度</h3><span class="badge {age_cls}">{E(age_s)}</span></div>'
        f'<div class="lmeter">{_meter(lpct, lm_cls, "开站时长（满格 3 年）")}'
        '<div class="ticks" aria-hidden="true"><span>0</span><span>1 年</span><span>2 年</span><span>3 年+</span></div></div>'
        '<dl class="bkv">'
        f'<dt>主域名注册</dt><dd>{E(reg_line)}</dd>'
        + (f'<dt>到期 / 注册商</dt><dd>{E(exp_line)}</dd>' if exp_line else
           f'<dt>到期 / 注册商</dt><dd>{E(str(exp_at))}{exp_badge} · {E(registrar)}</dd>') +
        f'<dt>全球流量排名</dt><dd>{E(rank_s)}</dd>'
        f'<dt>近 30 天巡检在线率</dt><dd>{E(uptime_s)}</dd>'
        '</dl></div>'
    )

    # 3. LLM Authenticity & Stage 2 Probe card
    canon_set = {q.canonical for q in qs}
    sample_model = next(
        (m for m in ("claude-sonnet-5-5", "claude-opus-5-5", "gpt-6-astra", "deepseek-v4.1-flash", "gpt-5.4") if m in canon_set),
        qs[0].canonical if qs else "claude-sonnet-5-5",
    )
    verify_href = f"/verify/?base_url={quote(f'https://{dom}', safe='')}&model={quote(sample_model, safe='')}"
    pr = sc.probe
    if pr:
        auth_badge = f'<span class="badge {"danger" if sc.cap_reason else "ok"}">已接入真实调用实测</span>'
        fid_dl = (
            f'<dt>已验证档位</dt><dd>{E(str(pr.get("verified_tier") or "L0"))} · 真伪 {_pct(pr.get("auth_pass_rate"))} · '
            f'智商 {_pct(pr.get("iq_pass_rate"))} · 篡改被接受 {int(pr.get("tamper_accepted_count") or 0)} 次</dd>'
            if any(pr.get(k) is not None for k in ("auth_pass_rate", "iq_pass_rate", "context_pass_rate")) else
            '<dt>真伪 / 智商</dt><dd>尚无 L1–L3 抽检样本（已验证档位 L0）</dd>'
        )
        auth_dl = fid_dl + (
            f'<dt>真实调用率</dt><dd>{pr.get("success_rate", 0):.1%}（{pr.get("total_samples", 0)} 次样本）</dd>'
            f'<dt>工具 / 缓存</dt><dd>工具 {pr["tool_call_pass_rate"]:.0%} · 缓存 {pr["cache_hit_rate"]:.0%}</dd>'
            if (pr.get("tool_call_pass_rate") is not None and pr.get("cache_hit_rate") is not None) else
            f'<dt>真实调用率</dt><dd>{pr.get("success_rate", 0):.1%}（{pr.get("total_samples", 0)} 次样本）</dd>'
        )
    else:
        auth_badge = '<span class="badge">仅公开报价（未人工实测）</span>'
        auth_dl = (
            '<dt>真伪验真状态</dt><dd>还没有经过 7 天真实调用实测，所以最高只给 B 级（74 分）</dd>'
            '<dt>自己测一测</dt><dd>用你自己的 Key 在验真页抽检：是不是真模型、有没有降智、长文会不会被截断</dd>'
        )
    block_auth = (
        '<div class="tc-block">'
        '<h4>模型真伪与降智抽检</h4>'
        f'<dl class="bkv">{auth_dl}</dl>'
        f'<div class="tc-cta"><a class="btn primary" href="{verify_href}">现场抽检这家站的真伪与智商 →</a></div>'
        '</div>'
    )
    srcs_n = len(it.get("sources") or [])
    if shared:
        dom_cls, dom_line = "warn", f"托管二级域名（*.{shared}），无法通过域名核实建站年限"
    elif days is not None:
        dom_cls, dom_line = ("ok" if lpct and lpct >= 50 else ""), f"主域名注册于 {reg_at}（{age_long}）"
        if risk and risk["level"] == "expired":
            dom_cls, dom_line = "danger", dom_line + " · 域名已过期"
        elif risk and risk["level"] == "soon":
            dom_cls, dom_line = "warn", dom_line + f" · {risk['days_left']} 天后到期"
    else:
        dom_cls, dom_line = "", "域名注册信息暂未查到"
    dom_line += f" · 外部社区收录 {srcs_n} 处" if srcs_n else " · 暂无外部社区收录"
    line_dom = f'<p class="tc-dom {dom_cls}"><span class="dot" aria-hidden="true"></span>{E(dom_line)}</p>'

    # 4. Community Reviews & Reputation card
    srcs = it.get("sources") or []
    rev_issue_url = (
        "https://github.com/Guoz7/relay_price/issues/new?template=station-review.yml&title="
        + quote(f"[实测评价] {dom}", safe="")
    )
    if revs:
        rsum = SI.review_summary(revs)
        if rsum["avg"] is not None:
            rev_badge = f'<span class="badge ok">★ {rsum["avg"]:.1f} / 5（{rsum["count"]} 条实测）</span>'
            stars = (
                f'<div class="stars-row"><span class="stars" style="--p:{rsum["pct"]}%" role="img" '
                f'aria-label="平均 {rsum["avg"]:.1f} 星（满分 5）">★★★★★</span>'
                f'<b>{rsum["avg"]:.1f}</b><small>{rsum["rated"]} 条评分</small></div>'
            )
        else:
            rev_badge = f'<span class="badge">{rsum["count"]} 条实测 · 未评分</span>'
            stars = ""
        sig_html = "".join(f'<span class="badge {cls}">{E(lbl)} ×{n}</span>' for _k, lbl, cls, n in rsum["signals"])
        items = []
        for r in revs[:3]:
            rv = SI.review_rating(r)
            rpct = int(round(rv / 5 * 100)) if rv is not None else 0
            tags = "".join(
                f'<span class="badge {SI.REVIEW_SIGNALS[k][1]}">{E(SI.REVIEW_SIGNALS[k][0])}</span>' for k in SI.review_signals(r)
            )
            items.append(
                '<div class="rev-item"><div class="rev-h">'
                + (f'<span class="stars stars-sm" style="--p:{rpct}%" role="img" aria-label="{rv:g} 星">★★★★★</span>' if rv is not None else "")
                + f'<code>{E(str(r.get("model") or ""))}</code>'
                f'<small>@{E(str(r.get("author") or "developer"))} · {E(str(r.get("date") or ""))}</small></div>'
                + (f'<div class="bd">{tags}</div>' if tags else "")
                + f'<p>{E(str(r.get("summary") or "")[:180])}</p></div>'
            )
        rev_body = stars + (f'<div class="bd">{sig_html}</div>' if sig_html else "") + '<div class="rev-list">' + "".join(items) + "</div>"
    else:
        rev_badge = f'<span class="badge">外部收录 {len(srcs)} 处 · 暂无站内实评</span>'
        src_preview = "、".join(srcs[:4]) + ("…" if len(srcs) > 4 else "") if srcs else "自主发现"
        rev_body = (
            '<dl class="bkv">'
            f'<dt>外部社区收录</dt><dd>{E(src_preview)}</dd>'
            '<dt>站内实测评价</dt><dd>为防刷评，仅收录带 GitHub 账号与客观证据的实测评价</dd>'
            '</dl>'
        )
    block_rev = (
        '<div class="tc-block">'
        f'<div class="tc-h"><h4>社区口碑与实测评价</h4>{rev_badge}</div>'
        f'{rev_body}'
        f'<div><a class="btn sm" href="{rev_issue_url}" target="_blank" rel="noopener">提交我的实测评价 / 避雷证据 →</a></div>'
        '</div>'
    )
    card_b = (
        '<section class="deep-card trust-card" aria-labelledby="tc-h">'
        f'<div class="deep-h"><h3 id="tc-h">真伪抽检与社区口碑</h3>{auth_badge}</div>'
        f'{block_auth}{line_dom}{block_rev}'
        '</section>'
    )
    return card_b, f'<div class="raw-grid">{card_price}{card_domain}</div>'


def price_strip_section(rows: list[dict[str, Any]]) -> str:
    svg = CH.station_price_strip_svg(rows)
    if not svg:
        return ""
    return (
        '<section class="chart-card"><h3>旗舰模型价格定位 <small class="muted">输入价 ¥/百万 token</small></h3>' + svg
        + '<p class="muted">灰带是全网大多数站点的价格范围，深色段是中间一半站点，竖线是中位价；蓝点是本站常规价，橙点是本站特惠/逆向价，紫线是官方价。</p></section>'
    )


def _tri(v: Any, yes: str, no: str) -> str:
    if v is None:
        return '<span class="muted" title="站点 /api/status 未公开该字段">未公开</span>'
    return f'<span class="badge {"warn" if v else "ok"}">{yes}</span>' if v else f'<span class="badge ok">{no}</span>'


def _fs_link(url: str | None, label: str | None = None) -> str:
    if not url:
        return '<span class="muted">未公开</span>'
    return f'<a class="ext" href="{E(url)}" rel="nofollow noopener" target="_blank">{E(label or url[:60])}</a>'


def _fs_rows(rows: list[tuple[str, str]]) -> str:
    return "".join(f"<div><dt>{E(k)}</dt><dd>{v}</dd></div>" for k, v in rows)


def fact_sheet(dom: str, st: dict[str, Any], qs: list[P.Quote]) -> str:
    """Fixed-slot 4-block station spec sheet (计费汇率 / 接入入口 / 账号风控 / 协议缓存); optional rows only when present."""
    tp = st.get("topup_price")
    fx = st.get("usd_exchange_rate")
    mode = st.get("topup_mode") or "unknown"
    fxb, basis = P.fx_basis(fx)
    mode_tip = {"discount": f"¥{tp:g} 买 $1 额度，低于汇率 ¥{fxb:g} 的一半" if tp is not None else "",
                "fx": f"≈ 按汇率 ¥{fxb:g} 充 $1" if tp is not None else "",
                "custom": f"¥{tp:g} / $1，介于折价与汇率之间" if tp is not None else ""}.get(mode, "")
    qpu = st.get("quota_per_unit")
    qdt = st.get("quota_display_type")
    sym = st.get("custom_currency_symbol")
    chg = (st.get("config_changes") or [None])[0]
    b1 = [
        ("充值比例", f"<b>¥{tp:g} / $1</b>" if tp is not None else f'<span class="muted" title="{E(st.get("topup_note") or "")}">未公开</span>'),
        ("充值模式", f'<span class="badge {"ok" if mode == "discount" else ""}">{E(P.TOPUP_MODE_LABELS.get(mode, mode))}</span>'
                     + (f" <small>{E(mode_tip)}</small>" if mode_tip else "")),
        ("站内汇率", (f"¥{fx:g} / $1" + (" <small>非人民币汇率口径，判定充值模式时按参考汇率 7.25</small>" if basis == "reference" else ""))
                     if fx is not None else '<span class="muted">未公开</span>'),
        ("额度单位", f"<code>{qpu:,}</code> 额度 = $1" if qpu else '<span class="muted">未公开</span>'),
        ("显示币种", (f"<code>{E(qdt)}</code>" + (f" <small>符号 {E(sym)}</small>" if (qdt == "CUSTOM" and sym) else "")) if qdt else '<span class="muted">未公开</span>'),
        ("最近汇率 / 分组倍率变动", (f'<span class="badge warn">{E(str(chg.get("run_at") or "")[:10])}</span> {E(str(chg.get("text") or ""))}'
                                 if chg else '<span class="muted">近 30 天未观测到变动</span>')),
    ]
    if st.get("min_topup") is not None:
        b1.append(("最低充值", f"{st['min_topup']:g}"))
    if st.get("new_user_usd") is not None:
        b1.append(("新用户赠送", f"${st['new_user_usd']:g} 额度"))
    if st.get("enable_online_topup") is not None:
        b1.append(("在线充值", "已开启" if st["enable_online_topup"] else "未开启"))
    ver = st.get("version")
    b2 = [
        ("API Base URL", f"<code>{E(st['server_address'])}</code>" if st.get("server_address")
         else f'<span class="muted">未公开</span> <small>可先试 <code>https://{E(dom)}</code></small>'),
        ("文档链接", _fs_link(st.get("docs_link"))),
    ]
    if st.get("top_up_link"):
        b2.append(("充值入口", _fs_link(st.get("top_up_link"))))
    b2.append(("系统版本", f"<code>{E(str(ver)[:40])}</code>" if ver else '<span class="muted">未公开</span>'))
    logins = [(lbl, st.get(k)) for lbl, k in (("GitHub", "github_oauth"), ("Linux.do", "linuxdo_oauth"), ("Telegram", "telegram_oauth"))]
    for lbl, k in (("微信", "wechat_login"), ("OIDC", "oidc_enabled")):
        if st.get(k) is not None:
            logins.append((lbl, st.get(k)))
    login_html = " ".join(f'<span class="badge {"ok" if v else "muted"}">{"✓" if v else "✗"} {E(lbl)}</span>' for lbl, v in logins)
    b3 = [
        ("邮箱验证", _tri(st.get("email_verification"), "注册需邮箱验证", "无需邮箱验证")),
        ("Turnstile 人机校验", _tri(st.get("turnstile_check"), "已开启", "未开启")),
        ("第三方登录", login_html),
    ]
    if st.get("aff_enabled") is not None:
        b3.append(("邀请返利", "已开启" if st["aff_enabled"] else "未开启"))
    eps = st.get("endpoints") or []
    ep_html = "".join(f'<span class="badge ep">{E(P.ENDPOINT_LABELS.get(e, e))}</span>' for e in eps) or '<span class="muted">未声明</span>'
    n_elig = st.get("cache_eligible_models") or 0

    def cov(v: Any) -> str:
        if v is None:
            return '<span class="muted">不适用</span>'
        return f'<span class="sbar {"ok" if v >= 60 else ("warn" if v >= 20 else "danger")}" style="--v:{int(v)}%">{int(v)}%</span> <small>{n_elig} 个可缓存型号</small>'

    groups = st.get("groups") or []
    b4 = [
        ("原生协议", ep_html),
        ("缓存读覆盖率", cov(st.get("cache_read_coverage"))),
        ("缓存写覆盖率", cov(st.get("cache_write_coverage"))),
        ("公开分组 / 有效型号", f"<b>{len(groups)}</b> 组 · <b>{st.get('parsed', len({q.model for q in qs}))}</b> 个型号"),
    ]
    blocks = (("① 计费与汇率口径", b1), ("② 接入与官方入口", b2), ("③ 账号与风控门槛", b3), ("④ 协议与缓存覆盖", b4))
    return (
        '<section class="factsheet" aria-labelledby="fs-h"><h3 id="fs-h">站点规格档案卡 <small>Fact Sheet · 只读 /api/status + /api/pricing 清洗结果，「未公开」不等于「免费」或「不支持」</small></h3>'
        '<div class="fs-grid">'
        + "".join(f'<div class="fs-card"><h3>{E(t)}</h3><dl class="fs-dl">{_fs_rows(rows)}</dl></div>' for t, rows in blocks)
        + '</div></section>'
    )


BAR_MODELS = tuple(dict.fromkeys(list(TS.TRACKED) + list(FLAGSHIP_MODELS)))
BAR_LABELS = {**TS.LABELS, "gpt-6.1-sol": "GPT-6.1 Sol", "gemini-4-carbon": "Gemini 4 Carbon"}


def consumer_context(stations, by_station, specs, cfg, changes, *, ledger: dict[str, Any] | None = None, intel=None,
                     run_stats=None, stability_map=None, monitored: bool = True) -> dict[str, Any]:
    """Everything the plain-language Layer 1 needs: price anchors, peers, flagship lanes, 30-day ledger and change events."""
    rate = float(cfg.get("usd_cny_rate", 7.25))
    cm = CM.compute_metrics(stations, by_station, specs or {}, rate)
    for dom, st in stations.items():
        st["cm"] = cm[dom]
    chg: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for c in sorted(changes or [], key=lambda x: x.get("run_at") or "", reverse=True):
        if c.get("type") in ("up", "down") and c.get("domain"):
            canon = P.canonical_model(str(c.get("model") or ""))
            if canon in TS.TRACKED:
                chg[(c["domain"], canon)].append(c)
    return {
        "cm": cm, "peers": CM.compute_peers(cm), "lanes": CM.model_lanes(by_station, BAR_MODELS),
        "official": {m: CM.official_lane((specs or {}).get(m), rate) for m in BAR_MODELS},
        "ledger": ledger or TS.empty_ledger(), "chg": chg, "intel": intel or {}, "run_stats": run_stats or {},
        "stab": stability_map or {}, "monitored": monitored,
    }


def _uptime_pct(st: dict[str, Any], dom: str, run_stats: dict[str, Any]) -> float | None:
    rs = merged_run_stats(st, dom, run_stats)
    return 100.0 * rs["successes"] / rs["attempts"] if rs["attempts"] else None


def ledger_series(led: dict[str, Any], dom: str, aliases: list[str] | tuple[str, ...] = ()) -> list[list[Any]]:
    """30-day ledger points of a station merged with its mirror domains (checks are logged per fetched host)."""
    rows: dict[str, list[Any]] = {}
    for d in [dom, *aliases]:
        for pt in TS.station_series(led, d):
            if len(pt) < 5:
                continue
            cur = rows.get(pt[0])
            if cur is None:
                rows[pt[0]] = [pt[0], pt[1], pt[2], pt[3], list(pt[4] or [None] * len(TS.TRACKED))]
                continue
            for k in (1, 2, 3):
                if cur[k] is None:
                    cur[k] = pt[k]
            cur[4] = [a if a is not None else b for a, b in zip(cur[4], pt[4] or [None] * len(cur[4]))]
    return [rows[k] for k in sorted(rows)]


def _trend_models(dom: str, cx: dict[str, Any], peers: list[str], aliases: list[str] | tuple[str, ...] = ()) -> list[dict[str, Any]]:
    led = cx["ledger"]
    series = ledger_series(led, dom, aliases)
    peer_series = {p: TS.station_series(led, p) for p in peers}
    market = TS.market_series(led)
    mine_now = (cx["cm"].get(dom) or {}).get("prices") or {}
    out = []
    for i, canon in enumerate(TS.TRACKED):
        pts = [(pt[0], pt[4][i]) for pt in series if len(pt) > 4 and pt[4] and pt[4][i] is not None]
        if canon not in mine_now and not pts:
            continue
        ref, ref_label = [], "同行均价"
        peer_at: dict[str, list[float]] = defaultdict(list)
        for ps in peer_series.values():
            for pt in ps:
                if len(pt) > 4 and pt[4] and pt[4][i] is not None:
                    peer_at[pt[0]].append(pt[4][i])
        if peer_at:
            ref = [(ts, statistics.fmean(v)) for ts, v in sorted(peer_at.items())]
        else:
            ref = [(row[0], row[1 + i]) for row in market if len(row) > 1 + i and row[1 + i] is not None]
            ref_label = "全网中位价"
        lanes = cx["lanes"].get(canon) or {}
        peer_now, peer_lbl = CM.peer_reference(lanes, peers, "in")
        evs = []
        for c in cx["chg"].get((dom, canon), [])[:4]:
            up = c["type"] == "up"
            evs.append(("warn" if up else "ok", f'{str(c.get("run_at") or "")[5:10]} {"涨价" if up else "降价"} {_chg_price(c.get("old"))}→{_chg_price(c.get("new"))}'))
        out.append({
            "model": canon, "label": TS.LABELS.get(canon, canon), "points": pts, "ref": ref,
            "ref_label": ref_label if len(pts) >= DB.MIN_CURVE_POINTS else peer_lbl,
            "mine": (lanes.get(dom) or {}).get("in", mine_now.get(canon)), "peer": peer_now,
            "official": (cx["official"].get(canon) or {}).get("in"), "events": evs,
        })
    return out


def _bar_rows(dom: str, cx: dict[str, Any], peers: list[str]) -> list[dict[str, Any]]:
    rows = []
    for canon in BAR_MODELS:
        lanes = cx["lanes"].get(canon) or {}
        mine = lanes.get(dom)
        if not mine:
            continue
        off = cx["official"].get(canon) or {}
        chat_ref, lbl = CM.peer_reference(lanes, peers, "chat")
        cache_ref, _ = CM.peer_reference(lanes, peers, "cache")
        rows.append({"label": BAR_LABELS.get(canon, canon), "peer_label": lbl,
                     "chat": (mine["chat"], chat_ref, off.get("chat")), "cache": (mine["cache"], cache_ref, off.get("cache"))})
    return rows[:6]


def _peer_row(d: str, stations, cx: dict[str, Any], mine_idx: float | None, me: bool = False) -> dict[str, Any]:
    st = stations[d]
    sc = st["score"]
    m = cx["cm"].get(d) or {}
    days = (cx["intel"].get(d) or {}).get("domain_age_days")
    stab = cx["stab"].get(d) or SI.default_stability()
    delta, delta_cls = CM.pct_delta(mine_idx, m.get("mkt_idx"))
    return {"dom": d, "name": str(st.get("display_name") or d)[:24], "me": me, "score": sc.total, "grade_html": grade_badge(sc.total),
            "disc": CM.fmt_discount_short(m.get("off_pct")), "delta": delta, "delta_cls": delta_cls,
            "age_txt": age_label(cx["intel"].get(d) or {})[0], "age_pct": SI.longevity_pct(days),
            "stab": int(stab.get("stability_score", 100)), "uptime": _uptime_pct(st, d, cx["run_stats"])}


def age_label(it: dict[str, Any]) -> tuple[str, str, str]:
    """(short, long, badge class) for the station's domain age; shared-host tenants never inherit the platform's age."""
    if it.get("shared_host"):
        return "托管域名", f"托管二级域名（*.{it['shared_host']}），无法通过域名核实建站年限", "warn"
    return SI.fmt_domain_age(it.get("domain_age_days"))


def ensure_ctags(stations: dict[str, dict[str, Any]], by_station, intel: dict[str, Any] | None) -> None:
    """Attach evidence-backed consumer tags (`st["ctags"]`) to every station that does not have them yet."""
    intel = intel or {}
    for dom, st in stations.items():
        if "ctags" not in st:
            st["ctags"] = CM.station_tags(st, by_station.get(dom, []), intel.get(dom) or {})


def consumer_layer1(dom: str, st: dict[str, Any], stations, cx: dict[str, Any]) -> tuple[str, str, str]:
    """(hero verdict, Section 01 dashboard + peer comparison, Section 02 flagship price bars) for one priced station."""
    sc: S.StationScore = st["score"]
    m = cx["cm"].get(dom) or {}
    pinfo = cx["peers"].get(dom) or {}
    peers = [p for p in pinfo.get("peers") or [] if p in stations]
    it = cx["intel"].get(dom) or {}
    stab = {**SI.default_stability(), **(cx["stab"].get(dom) or {})}
    rs = merged_run_stats(st, dom, cx["run_stats"])
    caveat_groups = sum(1 for g in (st.get("groups") or []) if g.get("selectable") and not g.get("welfare") and g.get("caveats"))
    greens, warns = CM.verdict_tags(
        stage=sc.stage, failed=bool(sc.cap_reason and sc.stage == 2), age_days=it.get("domain_age_days"),
        ups=int(stab.get("ups") or 0), sharp=int(stab.get("sharp_events") or 0), monitored=cx["monitored"],
        cache_cov=st.get("cache_read_coverage"), attempts=int(rs["attempts"]), successes=int(rs["successes"]),
        caveat_groups=caveat_groups, consecutive_failures=int(st.get("consecutive_failures") or 0),
        shared_host=it.get("shared_host"))
    verdict = DB.verdict_card(grade_html=grade_badge(sc.total), score=sc.total, headline=CM.price_headline(m), greens=greens,
                              warns=warns, chips=CM.client_chips(st.get("endpoints")), tags=st.get("ctags") or ())
    aliases = list(st.get("aliases") or [])
    dash = DB.dashboard(DB.heartbeat_card(ledger_series(cx["ledger"], dom, aliases)),
                        DB.price_trend_card(_trend_models(dom, cx, peers, aliases), TS.LABELS.values()))
    bars = DB.price_bars(_bar_rows(dom, cx, peers))
    mine_idx = m.get("mkt_idx")
    rows = [_peer_row(dom, stations, cx, mine_idx, me=True)] + [_peer_row(p, stations, cx, mine_idx) for p in peers]

    def alt(d: str | None, kind: str) -> dict[str, Any] | None:
        if not d or d not in stations:
            return None
        md = cx["cm"].get(d) or {}
        shared = len((m.get("models") or frozenset()) & (md.get("models") or frozenset()))
        if kind == "cheaper":
            delta, _ = CM.pct_delta(mine_idx, md.get("mkt_idx"))
            why = f"共同在卖 {shared} 款型号，整体{delta}"
        else:
            why = f"共同在卖 {shared} 款型号，靠谱分高 {stations[d]['score'].total - sc.total:.0f} 分（{stations[d]['score'].grade} 级）"
        return {"dom": d, "name": str(stations[d].get("display_name") or d)[:24], "why": why}

    href = "/compare/?s=" + ",".join(quote(x, safe="") for x in [dom] + peers[:2])
    pk = DB.peer_pk(rows, cheaper=alt(pinfo.get("cheaper"), "cheaper"), safer=alt(pinfo.get("safer"), "safer"), compare_href=href)
    return verdict, dash + pk, bars


def build_station_pages(
    site: Site, stations, by_station, intel, changes, cfg,
    *, run_stats: dict[str, Any] | None = None, stability_map: dict[str, Any] | None = None,
    reviews_map: dict[str, list[dict[str, Any]]] | None = None,
    market_dims: dict[str, float] | None = None, market_px: dict[str, dict[str, Any]] | None = None,
    cx: dict[str, Any] | None = None,
) -> None:
    now = site.now
    market_px = market_px or {}
    run_stats = run_stats or {}
    stability_map = stability_map or {}
    reviews_map = reviews_map or {}
    chg_by_dom: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in sorted(changes, key=lambda x: x.get("run_at") or "", reverse=True):
        if len(chg_by_dom[c["domain"]]) < 15:
            chg_by_dom[c["domain"]].append(c)
    ensure_ctags(stations, by_station, intel)

    def g_plain(g: dict[str, Any]) -> str:
        badge, text = CM.explain_group(str(g["name"]), str(g.get("desc") or ""), g.get("caveats") or ())
        cls = " ".join(c for c in ("badge", DB.GROUP_BADGE_CLS.get(badge, "")) if c)
        return f"<td class='desc'><span class=\"{cls}\">{E(badge)}</span> {E(text)}</td>"

    for dom, st in stations.items():
        qs = by_station.get(dom, [])
        name = st["display_name"]
        sc: S.StationScore = st["score"]
        groups = st.get("groups") or []
        g_rows = "".join(
            f"<tr><td><code>{E(str(g['name'])[:40])}</code></td><td class='n'>×{g['ratio']:g}</td>"
            f"<td>{'' if g['selectable'] else '<span class=muted>不可选</span>'}"
            + "".join(f' <span class="badge caveat">{E(c)}</span>' for c in (g.get("caveats") or []))
            + f"</td>{g_plain(g)}<td class='desc'>{E((g['desc'] or '')[:160])}</td></tr>"
            for g in groups[:80]
        )
        fam_counts: dict[str, int] = defaultdict(int)
        for q in qs:
            fam_counts[q.family] += 1
        fam_txt = "、".join(f"{P.FAMILY_LABELS.get(f, f)} {n}" for f, n in sorted(fam_counts.items(), key=lambda x: -x[1])[:6])
        ep_txt = "、".join(PLAIN_EP.get(e, e) for e in (st.get("endpoints") or []))
        tbl = quote_table(site, qs, stations, show_model=True, show_station=False, table_id="s")
        tp = st.get("topup_price")
        aliases = st.get("aliases") or []
        fails = st.get("consecutive_failures") or 0
        live = (
            f'<span class="live warn" role="status"><i class="dot" aria-hidden="true"></i>最近 {fails} 次刷新失败（{E(str(st.get("last_error") or ""))}），展示 {E(ago(st["fetched_at"], now))} 的数据</span>'
            if fails else '<span class="live ok"><i class="dot" aria-hidden="true"></i>最近一次公开接口刷新成功</span>'
        )
        it = intel.get(dom) or {}
        srcs = it.get("sources") or []
        st_chgs = chg_by_dom.get(dom) or []
        chg_html = ""
        if st_chgs:
            clis = "".join(
                f'<li><span class="badge {CHG_LABEL.get(c["type"], ("", ""))[1]}">{CHG_LABEL.get(c["type"], (c["type"], ""))[0]}</span> '
                f'<code>{E(c["model"])}</code> <small>{_chg_price(c.get("old"))} → {_chg_price(c.get("new"))} · {E(fmt_cst(parse_dt(c.get("run_at")), False))}</small></li>'
                for c in st_chgs[:10]
            )
            chg_html = f'<details class="groups"><summary>近 30 天调价/异动记录（{len(st_chgs)} 条）</summary><ul class="chg">{clis}</ul></details>'

        oauth_tags = []
        if st.get("github_oauth"):
            oauth_tags.append("支持 GitHub 登录")
        if st.get("linuxdo_oauth"):
            oauth_tags.append("支持 Linux.do 登录")
        if st.get("telegram_oauth"):
            oauth_tags.append("支持 Telegram 登录")

        trust_html, raw_cards = station_deep_dive(dom, st, qs, it, run_stats.get(dom) or {}, stability_map.get(dom),
                                                  reviews_map.get(dom) or [], now=now)
        verdict_html, trend_html, bars_html = consumer_layer1(dom, st, stations, cx) if cx else ("", "", "")
        stage_short = ("已通过真实调用实测" if sc.stage == 2 and not sc.cap_reason else
                       ("实测未达标" if sc.stage == 2 else "仅公开报价"))
        summary = (f"收录 {st.get('parsed', len(qs))} 个型号 · {len(groups)} 个分组 · 更新于 {ago(st['fetched_at'], now)}"
                   + (f" · {st.get('topup_note')}" if st.get("topup_note") else (f" · 充值 ¥{tp:g}" if tp is not None else "")))
        raw_kv = [("访问站点", site.ext_link(dom))]
        if aliases:
            raw_kv.append(("镜像域名", "、".join(E(a) for a in aliases)))
        if st.get("version"):
            raw_kv.append(("系统版本", f'<code>{E(str(st.get("version")))}</code>'))
        if oauth_tags:
            raw_kv.append(("登录方式", E("、".join(oauth_tags))))
        if ep_txt:
            raw_kv.append(("接口类型", E(ep_txt)))
        if fam_txt:
            raw_kv.append(("型号分布", E(fam_txt)))
        if srcs:
            raw_kv.append(("收录来源", E("、".join(srcs[:8])) + ("…" if len(srcs) > 8 else "")))
        raw_kv_html = '<dl class="raw-kv">' + "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in raw_kv) + "</dl>"
        probe_html = probe_panel(sc)
        sections = []
        if trend_html:
            sections.append(("sec-trend", "01", "动态与同行", "动态走势与同行对比",
                             "近 30 天站内定时巡检在线率、热门模型调价阶梯，以及同级别同行横向位置", trend_html))
        sections.append(("sec-pricing", "02", "比价与价目表", "模型比价与完整价目表",
                         "先看热门模型相对同行与官网的价格，再在完整价目表里按分组、接口和计费方式筛选",
                         bars_html + DB.group_legend(groups) + filter_bar("s", show_mode=True, show_ep=True, show_view=True) + tbl))
        sections.append(("sec-setup", "03", "接入与验真口碑", "接入教程与深度核验",
                         "决定试用后：复制配置接进你的软件，再看七维评分拆解、现场抽检真伪与社区实测口碑",
                         DB.setup_card(dom, st)
                         + f'<div class="deep-grid">{score_panel(st, cfg, market_dims)}{trust_html}</div>'))
        sections.append(("sec-raw", "04", "原始参数", "原始参数与完整记录",
                         "给进阶用户核对：站点基础信息、原始规格、全部分组倍率、调价记录与评分原始证据，默认收起",
                         raw_kv_html
                         + '<details class="adv-drawer"><summary>给进阶用户核对：站点原始规格参数（充值汇率、额度单位、API 地址、风控项）</summary>'
                         + fact_sheet(dom, st, qs) + '</details>'
                         + f'<details class="groups"><summary>全部分组倍率与自认标签（{len(groups)}）</summary>'
                         '<div class="tbl-wrap"><table class="plain"><thead><tr><th>分组</th><th class="n">倍率</th><th>状态 / 提醒</th>'
                         f'<th>通俗解释</th><th>站点原文说明</th></tr></thead><tbody>{g_rows}</tbody></table></div></details>'
                         + chg_html
                         + f'<details class="groups"><summary>近 30 天防暗涨与开站时长原始记录</summary>{raw_cards}</details>'
                         + (f'<details class="groups"><summary>真实调用实测原始证据</summary>{probe_html}</details>' if probe_html else "")))
        toc = DB.toc_bar([(sid, num, short) for sid, num, short, *_ in sections], "站点档案目录")
        sec_html = "".join(f'<section class="sec-block">{DB.section_head(num, title, sub, sid)}{inner}</section>'
                           for sid, num, _short, title, sub, inner in sections)
        body = f"""
<section class="hero sm st-hero">
  <p class="eyebrow">站点体检档案 · {E(stage_short)}</p>
  <h1>{E(name)} 靠谱吗？<span class="dom-tag">{E(dom)}</span></h1>
  <div class="st-meta">{live}{site.ext_link(dom, "访问官网 →")}{DB.fav_button(dom, name)}</div>
  <p class="st-sum">{E(summary)}</p>
  {verdict_html}
</section>
{toc}
{sec_html}"""
        site.page(
            f"/site/{dom}/",
            f"{name}（{dom}）靠谱吗？评分 {sc.grade} 级 {sc.total:.0f} 分、域名时长、价格稳定性与真伪评测",
            f"{name}（{dom}）站点档案（{sc.stage_label}）：综合 {sc.grade} 级 {sc.total:.0f} 分，{st.get('topup_note')}，收录 {st.get('parsed', len(qs))} 个型号与 {len(groups)} 个分组，含 30 日价格稳定性、域名注册时长与真伪鉴别入口。",
            body,
            crumbs=[("站点排行", "/stations/"), (name, f"/site/{dom}/")],
            lastmod=st["fetched_at"], priority=0.6,
        )


def build_closed_station_pages(site: Site, closed_stations: dict[str, dict[str, Any]]) -> None:
    """Stage 0 archive pages for 401/403 closed stations (never ranked or compared)."""
    for dom, st in closed_stations.items():
        code = st["http_status"]
        reason = "接口要求登录鉴权（HTTP 401）" if code == 401 else "接口开启了访问拦截或 WAF（HTTP 403）"
        body = f"""
<section class="hero sm">
  <p class="eyebrow">站点档案 · {E(S.STAGE_LABELS[0])}</p>
  <h1>{E(dom)} 靠谱吗？ <small>仅建档 · 未公开报价</small></h1>
  <p class="lead">{stage_badge(0)} <span class="badge warn">{E(reason)}</span></p>
</section>
<div class="callout">
  <p><b>为什么这里没有评分和比价表？</b> 本站只采集站点匿名公开的只读价格接口，绝不登录、不破解也不绕过验证码/WAF。最近一次只读探测（{E(fmt_cst(st.get("last_attempt_at")))} 北京时间）返回 <code>HTTP {code}</code>，因此按 <b>Stage 0</b> 仅保留基础档案，不参与信誉评分与模型比价。</p>
  <p>访问原站：{site.ext_link(dom)} · 如果你是该站站长，可按 <a href="/submit/">收录与 WAF 放行说明</a> 开放只读价格接口后自动升级至 Stage 1。</p>
</div>"""
        site.page(
            f"/site/{dom}/",
            f"{dom} 靠谱吗？站点档案（未公开报价 · Stage 0）",
            f"{dom} AI API 中转站基础档案：最近只读探测返回 HTTP {code}（未公开匿名报价），当前为 Stage 0 仅建档状态，不参与评分与比价。",
            body,
            crumbs=[("站点排行", "/stations/"), (dom, f"/site/{dom}/")],
            lastmod=st.get("last_attempt_at") or site.now,
            priority=0.4,
        )


# ---------------------------------------------------------------- indexes
RANK_DIMS = ("stability", "longevity", "transparency", "reputation", "latency", "purity_protocol")


FACETS = (
    ("按使用场景", (("ep-a", "写代码 / Claude Code"), ("ep-o", "日常聊天 / 翻译"), ("ep-g", "Gemini 模型"), ("cw", "长对话省钱缓存"))),
    ("按选站偏好", (("cheap", "省钱优选（前 30% 便宜）"), ("age1", "老牌稳用（1 年以上）"), ("stab", "近期没涨价"),
                ("gb", "高分优先（B 级+）"), ("easy", "好注册（支持快捷登录）"))),
    ("按省心福利", (("free", "有免费/可试测"), ("noemail", "免邮箱秒注册"), ("pure", "无逆向纯常规"))),
)
RANK_TAG_SHORT = {"free": "可白嫖试用", "noemail": "免邮箱注册", "pure": "纯常规线路"}


def station_facets(st: dict[str, Any], sc: S.StationScore, stab: dict[str, Any] | None, days: Any) -> str:
    """Space-separated facet tokens for the /stations/ scenario filter and bubble map (AND across selected chips)."""
    eps = set(st.get("endpoints") or [])
    stab = stab or SI.default_stability()
    chp = (st.get("cm") or {}).get("cheaper_pct")
    toks = [t for t, ok in (
        ("ep-a", "anthropic" in eps), ("ep-o", "openai" in eps or "openai-response" in eps), ("ep-g", "gemini" in eps),
        ("cw", (st.get("cache_read_coverage") or 0) > 0),
        ("cheap", isinstance(chp, (int, float)) and chp >= 70),
        ("age1", isinstance(days, (int, float)) and days >= 365),
        ("stab", int(stab.get("ups") or 0) == 0 and int(stab.get("stability_score", 100)) >= 80),
        ("gb", sc.grade in ("S", "A", "B")),
        ("easy", bool(st.get("github_oauth") or st.get("linuxdo_oauth") or st.get("telegram_oauth"))),
    ) if ok]
    toks += CM.tag_facets(st.get("ctags") or ())
    return " ".join(toks)


def facet_bar(total: int) -> str:
    groups = "".join(
        f'<div class="fc-g" role="group" aria-label="{E(g)}"><span class="fc-l">{E(g)}</span>'
        + "".join(f'<button type="button" class="chip" data-f="{t}" aria-pressed="false">{E(lbl)}</button>' for t, lbl in chips)
        + "</div>"
        for g, chips in FACETS
    )
    return (
        f'<div class="facets" id="facets" data-total="{total}">{groups}'
        f'<div class="fc-foot"><output id="facet-count" aria-live="polite">当前显示 {total} / {total} 家站点</output>'
        '<button type="button" class="btn sm" id="facet-reset">重置筛选</button>'
        '<a class="btn sm primary" href="/compare/">并排对比 →</a></div></div>'
    )


def compare_bar() -> str:
    return (
        '<div class="cmp-bar" id="cmp-bar" hidden role="region" aria-label="站点对比栏">'
        '<span>已选 <b id="cmp-n">0</b>/4 家站点</span><span class="cmp-names" id="cmp-names"></span>'
        '<button type="button" class="btn sm" id="cmp-clear">清空</button>'
        '<a class="btn sm primary" id="cmp-go" href="/compare/" aria-disabled="true">开始并排对比 →</a>'
        '<span class="cmp-msg" id="cmp-msg" role="status"></span></div>'
    )


def _dims_tip(sc: S.StationScore) -> str:
    parts = [f"{PLAIN_DIM_LONG.get(k, S.DIM_LABELS.get(k, k))} {sc.dims[k].score:.0f}" + ("" if sc.dims[k].known else "（证据不足）")
             for k in S.DIM_ORDER if k in sc.dims]
    return "分项：" + " · ".join(parts)


def station_rank_table(
    stations, by_station, now, limit: int | None = None, compact: bool = False,
    intel: dict[str, Any] | None = None, stability_map: dict[str, Any] | None = None,
    run_stats: dict[str, Any] | None = None,
) -> str:
    intel = intel or {}
    stability_map = stability_map or {}
    run_stats = run_stats or {}
    if not compact:
        ensure_ctags(stations, by_station, intel)
    items = sorted(
        stations.items(),
        key=lambda kv: (-kv[1]["score"].stage, -kv[1]["score"].total, -kv[1]["score"].raw_total, kv[0]),
    )
    if limit:
        items = items[:limit]
    rows = []
    for i, (dom, st) in enumerate(items, 1):
        sc: S.StationScore = st["score"]
        tp = st.get("topup_price")
        n_models = st.get("parsed", len(by_station.get(dom, [])))
        score_cell = (f'<td class="sc" data-v="{sc.total}" title="{E(_dims_tip(sc))}">{grade_badge(sc.total)} <b>{sc.total:.1f}</b>'
                      f'<small>{E(plain_stage(sc))}</small></td>')
        name_cell = f'<td class="st" data-v="{E(dom)}"><a href="/site/{E(dom)}/">{E(st["display_name"])}</a><small>{E(dom)}</small></td>'
        if compact:
            rows.append(
                f'<tr><td class="n" data-v="{i}">{i}</td>{name_cell}{score_cell}'
                f'<td class="n" data-v="{tp if tp is not None else 9e9}">{E(f"¥{tp:g}/$1" if tp is not None else "未知")}</td></tr>'
            )
            continue
        days = (intel.get(dom) or {}).get("domain_age_days")
        age_s, _, age_cls = age_label(intel.get(dom) or {})
        sb = stability_map.get(dom) or SI.default_stability()
        sb_v = int(sb.get("stability_score", 100))
        sb_band = sb.get("cls") or SI.stability_band(sb_v)
        sb_tip = E(f'{sb.get("label", "")}：{sb.get("volatility_summary", "")}')
        age_v = days if isinstance(days, (int, float)) and days >= 0 else "na"
        cm = st.get("cm") or {}
        off, chp = cm.get("off_pct"), cm.get("cheaper_pct")
        price_cell = (
            f'<td class="pr" data-v="{off:.2f}"><span class="badge {"ok" if off < 95 else ("" if off <= 105 else "danger")}">{E(CM.fmt_discount_short(off))}</span>'
            + (f'<small>{E(CM.fmt_cheaper(chp))}</small>' if chp is not None else "") + "</td>"
            if isinstance(off, (int, float)) else '<td class="pr" data-v="na"><span class="muted">暂无比价基线</span></td>'
        )
        up = _uptime_pct(st, dom, run_stats)
        ctags = st.get("ctags") or []
        rtags = "".join(f'<span class="rtag {t}" data-tip="{E(chr(65307).join(x["tip"] for x in ctags if x["key"] in CM.TAG_FACETS[t]))}" tabindex="0">'
                        f'{E(RANK_TAG_SHORT[t])}</span>' for t in CM.tag_facets(ctags))
        fav_attrs = (f' data-dom="{E(dom)}" data-name="{E(str(st["display_name"])[:24])}" data-g="{E(sc.grade)}"'
                     f' data-disc="{E(CM.fmt_discount_short(off))}" data-up="{"" if up is None else f"{up:.0f}"}"')
        name_cell = name_cell.replace('<td class="st" ', f'<td class="st"{fav_attrs} ', 1)
        if rtags:
            name_cell = name_cell[:-5] + f'<span class="rtags">{rtags}</span></td>'
        up_cell = (f'<td class="n" data-v="{up:.1f}"><span class="sbar {SI.stability_band(up)}" style="--v:{up:.0f}%">{up:.0f}%</span></td>'
                   if up is not None else '<td class="n" data-v="na"><span class="muted">累积中</span></td>')
        rows.append(
            f'<tr><td class="n" data-v="{i}" data-f="{station_facets(st, sc, sb, days)}">{i}</td>{name_cell}{score_cell}{price_cell}'
            f'<td class="n" data-v="{sb_v}" title="{sb_tip}"><span class="sbar {sb_band}" style="--v:{sb_v}%">{sb_v}</span></td>'
            f'<td class="n" data-v="{age_v}"><span class="badge {age_cls}">{E(age_s)}</span></td>'
            f'{up_cell}<td class="n" data-v="{n_models}">{n_models}</td>'
            f'<td class="cmp"><input type="checkbox" data-cmp="{E(dom)}" data-name="{E(str(st["display_name"])[:24])}"'
            f' aria-label="加入对比：{E(st["display_name"])}"></td></tr>'
        )
    head = ('<div class="tbl-wrap"><table class="qt sortable rank"><thead><tr>'
            '<th data-type="n"><button type="button">#</button></th><th data-type="s"><button type="button">站点</button></th>'
            '<th data-type="n" aria-sort="descending" title="0–100 综合靠谱分，悬停分数可看分项"><button type="button">靠谱度</button></th>')
    if compact:
        return head + '<th data-type="n"><button type="button">充值比例</button></th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
    return (
        head
        + '<th data-type="n" title="约为官网价的几折；越低越省钱"><button type="button">相对价格</button></th>'
        '<th data-type="n" data-first="desc" title="0–100，越高越稳定；近 30 天涨价次数与幅度扣分"><button type="button">防暗涨</button></th>'
        '<th data-type="n" data-first="desc"><button type="button">开站时长</button></th>'
        '<th data-type="n" data-first="desc" title="站内定时巡检公开接口的成功比例"><button type="button">近 30 天在线</button></th>'
        '<th data-type="n" data-first="desc"><button type="button">型号数</button></th>'
        '<th class="cmp" scope="col" title="勾选 2–4 家站点后并排对比">对比</th>'
        + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def model_list(items: list[tuple[str, dict[str, Any]]], stations) -> str:
    items = sorted(items, key=lambda x: (-x[1]["n"], x[0]))
    rows = []
    for canon, mp in items:
        r, c = mp["rec"], mp["cheap"]
        rec = (f'{grade_badge(r.score)} {E(stations[r.domain]["display_name"][:22])}' if r else '<span class="muted">—</span>')
        if c is None:
            cp = '<span class="muted">—</span>'
        elif c.kind == "token":
            cp = f"{money(c.input_cny)} / {money(c.output_cny)}"
        else:
            cp = f"{money(c.call_cny)}/次"
        rows.append(
            f'<tr><td class="mn" data-v="{E(canon)}"><a href="/model/{mp["slug"]}/"><code>{E(canon)}</code></a></td>'
            f'<td class="n" data-v="{mp["n"]}">{mp["n"]}</td>'
            f'<td class="st" data-v="{r.score if r else -1}">{rec}</td>'
            f'<td class="n" data-v="{(main_cny(c, {}) if c else None) or 9e9}">{cp}</td></tr>'
        )
    return (
        '<div class="tbl-wrap"><table class="qt sortable"><thead><tr>'
        '<th data-type="s"><button type="button">型号</button></th><th data-type="n"><button type="button">站点数</button></th>'
        '<th data-type="n"><button type="button">常规推荐</button></th>'
        '<th data-type="n"><button type="button">常规最低 输入/输出（每百万）</button></th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def build_indexes(
    site: Site, model_pages, stations, by_station, closed_stations, cfg, min_st,
    *, intel: dict[str, Any] | None = None, stability_map: dict[str, Any] | None = None, cx: dict[str, Any] | None = None,
):
    intel = intel or {}
    stability_map = stability_map or {}
    run_stats = (cx or {}).get("run_stats") or {}
    fam_models: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for canon, mp in model_pages.items():
        fam_models[mp["family"]].append((canon, mp))
    fam_order = sorted(fam_models, key=lambda f: (f == "other", -sum(m["n"] for _, m in fam_models[f])))
    for fam in fam_order:
        label = P.FAMILY_LABELS.get(fam, fam)
        items = fam_models[fam]
        site.page(
            f"/family/{fam}/", f"{label} 系列 API 中转推荐与价格排行",
            f"{label} 系列 {len(items)} 个型号的中转站常规推荐与最低价，按分层信誉与相对价格评分，每日更新。",
            f'<section class="hero sm"><h1>{E(label)} 系列 API 中转推荐与价格排行</h1><p class="lead">{len(items)} 个型号；「常规推荐」已隔离自称 Kiro/逆向/特惠分组，点型号查看全部分组。</p></section>{model_list(items, stations)}',
            crumbs=[("模型", "/models/"), (label, f"/family/{fam}/")], priority=0.7,
        )
    fam_nav = " ".join(f'<a class="chip" href="/family/{f}/">{E(P.FAMILY_LABELS.get(f, f))} <small>{len(fam_models[f])}</small></a>' for f in fam_order)
    site.page(
        "/models/", "全部模型 API 中转推荐与价格索引",
        f"收录 {len(model_pages)} 个模型在 {len(stations)} 家 API 中转站的分层评分与报价，按系列浏览，查看常规推荐与多分组价格。",
        f'<section class="hero sm"><h1>全部模型</h1><p class="lead">{len(model_pages)} 个型号（至少 {min_st} 家站点有报价）。</p><div class="chips">{fam_nav}</div></section>{model_list(list(model_pages.items()), stations)}',
        crumbs=[("模型", "/models/")], priority=0.7,
    )
    w = cfg.get("station_weights") or S.DEFAULT_STATION_WEIGHTS
    grades: dict[str, int] = defaultdict(int)
    n_stage2 = 0
    for st in stations.values():
        grades[st["score"].grade] += 1
        if st["score"].stage == 2:
            n_stage2 += 1
    gtxt = " · ".join(f"{g} 级 {grades.get(g, 0)} 家" for g in ("S", "A", "B", "C", "D"))
    bubble_rows = []
    ensure_ctags(stations, by_station, intel)
    for dom, st in stations.items():
        cm = st.get("cm") or {}
        days = (intel.get(dom) or {}).get("domain_age_days")
        bubble_rows.append({
            "dom": dom, "name": st["display_name"], "grade": st["score"].grade, "score": st["score"].total,
            "cheaper_pct": cm.get("cheaper_pct"), "off_pct": cm.get("off_pct"), "age_days": days,
            "uptime": _uptime_pct(st, dom, run_stats), "n_models": st.get("parsed") or cm.get("n_models") or 0,
            "facets": station_facets(st, st["score"], stability_map.get(dom), days),
        })
    bubble_html = DB.bubble_section(DB.bubble_payload(bubble_rows), len(stations))
    closed_html = ""
    if closed_stations:
        c_rows = "".join(
            f'<tr><td><a href="/site/{E(d)}/"><code>{E(d)}</code></a></td>'
            f'<td><span class="badge warn">HTTP {st["http_status"]}</span></td>'
            f'<td class="t">{E(fmt_cst(st.get("last_attempt_at")))}</td></tr>'
            for d, st in sorted(closed_stations.items())
        )
        closed_html = (
            f'<details class="groups"><summary>Stage 0 · 仅建档未公开报价的站点（{len(closed_stations)} 家 401/403 闭源或 WAF 拦截站）</summary>'
            f'<p class="muted">以下站点因登录鉴权（401）或访问拦截（403）无法读取公开价格，不参与评分与比价；站长可前往 <a href="/submit/">提交收录</a> 开放只读接口。</p>'
            f'<div class="tbl-wrap"><table class="plain"><thead><tr><th>站点域名</th><th>只读探测状态</th><th>最近尝试</th></tr></thead><tbody>{c_rows}</tbody></table></div></details>'
        )
    site.page(
        "/stations/", "AI API 中转站分层评分排行榜",
        f"{len(stations)} 家 AI API 中转站分层信誉评分排行（实测认证 {n_stage2} 家，公开校验 {len(stations) - n_stage2} 家，另含 {len(closed_stations)} 家仅建档闭源站）：{gtxt}。",
        DB.fav_bar()
        + f'<section class="hero sm"><p class="eyebrow">站点排行 · 共 {len(stations)} 家公开报价站</p><h1>AI API 中转站靠谱度排行榜</h1>'
        f'<p class="lead">先看图找到“既靠谱又便宜”的站，再用下面的场景按钮缩小范围。只抓取公开报价、还没人工实测的站最高只给 B 级（低于 74 分）；'
        f'通过真实调用实测才可能拿到 A/S 级。当前分布：{gtxt}。<a href="/about/">评分方法 →</a></p></section>'
        + '<section class="sec-block">'
        + DB.section_head("01", "省钱 vs 靠谱 分布图", "每个点是一家站，越靠右越省钱、越靠上越靠谱，右上角最值得先看", "sec-map")
        + bubble_html + '</section>'
        + '<section class="sec-block">'
        + DB.section_head("02", "全部中转站排行榜", "用场景按钮缩小范围，勾选 2–4 家即可并排对比", "sec-rank")
        + '<p class="sec-lead muted">「相对价格」是热门型号价格约为官网价的几折；「防暗涨」越高说明近 30 天越少偷偷涨价；「近 30 天在线」是站内定时巡检能打开接口的比例。数值列首次点击按从高到低排序，未知值始终排在末尾。</p>'
        + facet_bar(len(stations))
        + station_rank_table(stations, by_station, site.now, intel=intel, stability_map=stability_map, run_stats=run_stats)
        + compare_bar()
        + closed_html
        + '</section>',
        crumbs=[("站点排行", "/stations/")], priority=0.9,
    )
    return fam_order, fam_models


# ---------------------------------------------------------------- history / changes
def update_history(quotes: list[P.Quote], stations, now: datetime) -> list[dict[str, Any]]:
    """Store a compact snapshot per fetch run; diff consecutive snapshots into a change log."""
    HIST.mkdir(parents=True, exist_ok=True)
    last_run = load_json(DATA / "last-run.json", {}).get("run_at") or now.isoformat()
    stamp = last_run.replace(":", "").replace("-", "")[:15]
    cur = {
        f"{q.domain}|{q.model}|{q.lane}": [q.kind, q.input_cny, q.output_cny, q.call_cny, q.input_usd, q.output_usd, q.call_usd, q.group]
        for q in quotes
    }
    cur_file = HIST / f"{stamp}.json.gz"
    if not cur_file.exists():
        with gzip.open(cur_file, "wt", encoding="utf-8") as fh:
            json.dump({"run_at": last_run, "algo": P.ALGO_VERSION, "q": cur}, fh, ensure_ascii=False, separators=(",", ":"))
    files = sorted(HIST.glob("*.json.gz"))
    log_path = DATA / "changes.json"
    log: list[dict[str, Any]] = load_json(log_path, [])
    seen_runs = {c.get("run_at") for c in log}
    if len(files) >= 2:
        with gzip.open(files[-1], "rt", encoding="utf-8") as fh:
            c = json.load(fh)
        with gzip.open(files[-2], "rt", encoding="utf-8") as fh:
            p = json.load(fh)
        if c["run_at"] not in seen_runs and p.get("algo") == c.get("algo") == P.ALGO_VERSION:
            pq, cq = p["q"], c["q"]
            prev_doms = {k.split("|", 1)[0] for k in pq}
            cur_doms = {k.split("|", 1)[0] for k in cq}
            for k, v in cq.items():
                parts = k.split("|")
                dom, model = parts[0], parts[1]
                old = pq.get(k)
                if old is None:
                    if dom in prev_doms:
                        log.append({"run_at": c["run_at"], "type": "new", "domain": dom, "model": model, "new": v})
                    continue
                o_main = old[1] if old[0] == "token" else old[3]
                n_main = v[1] if v[0] == "token" else v[3]
                o_out, n_out = old[2], v[2]
                if o_main is None and n_main is None:
                    o_main, n_main = (old[4] if old[0] == "token" else old[6]), (v[4] if v[0] == "token" else v[6])
                if o_main is not None and n_main is not None and (abs(o_main - n_main) > 1e-9 or (o_out is not None and n_out is not None and abs(o_out - n_out) > 1e-9)):
                    log.append({"run_at": c["run_at"], "type": "down" if n_main < o_main else ("up" if n_main > o_main else "out"), "domain": dom, "model": model, "old": old, "new": v})
            for k, old in pq.items():
                parts = k.split("|")
                dom, model = parts[0], parts[1]
                if dom in cur_doms and k not in cq:
                    log.append({"run_at": c["run_at"], "type": "removed", "domain": dom, "model": model, "old": old})
    cutoff = (now - timedelta(days=30)).isoformat()
    log = [x for x in log if (x.get("run_at") or "") >= cutoff][-5000:]
    log_path.write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")
    for f in files[:-60]:
        f.unlink(missing_ok=True)
    return log


def station_config_snapshot(st: dict[str, Any]) -> dict[str, Any]:
    return {
        "price": st.get("topup_price"),
        "fx": st.get("usd_exchange_rate"),
        "groups": {str(g.get("name"))[:40]: g["ratio"] for g in (st.get("groups") or [])
                   if isinstance(g.get("ratio"), (int, float)) and not isinstance(g.get("ratio"), bool)},
    }


def _moved(a: Any, b: Any) -> bool:
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(float(a) - float(b)) > 1e-9 * max(1.0, abs(float(a)))


def diff_station_config(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Human-readable fixed-config moves between two runs (top-up price, station FX rate, group ratios)."""
    out: list[str] = []
    if _moved(old.get("price"), new.get("price")):
        out.append(f"充值比例 ¥{old['price']:g}→¥{new['price']:g}/$1")
    if _moved(old.get("fx"), new.get("fx")):
        out.append(f"站内汇率 {old['fx']:g}→{new['fx']:g}")
    og, ng = old.get("groups") or {}, new.get("groups") or {}
    moved = [(k, og[k], ng[k]) for k in sorted(set(og) & set(ng)) if _moved(og[k], ng[k])]
    if moved:
        out.append("分组倍率 " + "、".join(f"{k} ×{a:g}→×{b:g}" for k, a, b in moved[:2]) + (f" 等 {len(moved)} 组" if len(moved) > 2 else ""))
    return out


def update_config_changes(stations, now: datetime, state_path: Path | None = None, log_path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Diff station-level fixed config against the previous build; keep a 30-day `{domain: [{run_at, text}]}` map."""
    state_path = state_path or DATA / "station-config-state.json"
    log_path = log_path or DATA / "station-config-changes.json"
    prev: dict[str, Any] = load_json(state_path, {})
    log: dict[str, list[dict[str, Any]]] = load_json(log_path, {})
    run_at = now.isoformat(timespec="seconds")
    cur: dict[str, Any] = {}
    for dom, st in stations.items():
        snap = station_config_snapshot(st)
        cur[dom] = snap
        if dom in prev:
            txt = diff_station_config(prev[dom], snap)
            if txt:
                log.setdefault(dom, []).insert(0, {"run_at": run_at, "text": "；".join(txt)})
    cutoff = (now - timedelta(days=30)).isoformat()
    log = {d: [x for x in v if str(x.get("run_at") or "") >= cutoff][:10] for d, v in log.items()}
    log = {d: v for d, v in sorted(log.items()) if v}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({**prev, **cur}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log_path.write_text(json.dumps(log, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for dom, st in stations.items():
        st["config_changes"] = log.get(dom, [])
    return log


def _chg_price(v: list[Any] | None) -> str:
    if not v:
        return "—"
    kind, i_c, o_c, call_c, i_u, o_u, call_u, _g = v
    if kind == "token":
        return f"{money(i_c)} / {money(o_c)}" if i_c is not None else f"{money(i_u, '$')} / {money(o_u, '$')}"
    return f"{money(call_c) if call_c is not None else money(call_u, '$')}/次"


CHG_LABEL = {"down": ("降价", "ok"), "up": ("涨价", "warn"), "out": ("输出调价", ""), "new": ("上新", "info"), "removed": ("下架", "muted")}


def build_changes(site: Site, changes, stations, model_pages) -> None:
    canon_slug = {c: mp["slug"] for c, mp in model_pages.items()}
    recent = sorted(changes, key=lambda x: x.get("run_at") or "", reverse=True)[:600]
    rows = []
    for c in recent:
        t, cls = CHG_LABEL.get(c["type"], (c["type"], ""))
        slug = canon_slug.get(P.canonical_model(c["model"]))
        mlink = f'<a href="/model/{slug}/"><code>{E(c["model"])}</code></a>' if slug else f'<code>{E(c["model"])}</code>'
        st = stations.get(c["domain"])
        slink = f'<a href="/site/{E(c["domain"])}/">{E(st["display_name"])}</a> {grade_badge(st["score"].total)}' if st else E(c["domain"])
        rows.append(
            f'<tr><td class="t">{E(fmt_cst(parse_dt(c.get("run_at"))))}</td><td><span class="badge {cls}">{t}</span></td>'
            f'<td>{slink}</td><td>{mlink}</td><td class="n">{_chg_price(c.get("old"))}</td><td class="n">{_chg_price(c.get("new"))}</td></tr>'
        )
    body = (
        '<section class="hero sm"><h1>调价动态</h1><p class="lead">每次自动刷新后与上一次快照对比，记录降价、涨价、上新与下架（价格为 输入/输出 每百万 token）。也可通过 <a href="/changes.xml">RSS</a> 订阅。</p></section>'
        + ('<div class="tbl-wrap"><table class="plain"><thead><tr><th>时间</th><th>类型</th><th>站点</th><th>型号</th><th class="n">原价</th><th class="n">新价</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
           if rows else '<p class="muted">已建立当前算法版本的新快照，下一次刷新后这里会出现调价记录。</p>')
    )
    site.page("/changes/", "AI API 中转站调价动态", "实时追踪各家 AI API 中转站的降价、涨价、上新与下架记录。", body, crumbs=[("调价", "/changes/")], priority=0.7)
    items = []
    for c in recent[:100]:
        t, _ = CHG_LABEL.get(c["type"], (c["type"], ""))
        dt = parse_dt(c.get("run_at")) or site.now
        canon = P.canonical_model(c["model"])
        link = site.url(f"/model/{canon_slug[canon]}/") if canon in canon_slug else site.url("/changes/")
        title = f"[{t}] {c['domain']} · {c['model']}：{_chg_price(c.get('old'))} → {_chg_price(c.get('new'))}"
        guid = hashlib.sha1(f"{c.get('run_at')}|{c['domain']}|{c['model']}|{c['type']}".encode()).hexdigest()
        items.append(f"<item><title>{E(title)}</title><link>{E(link)}</link><guid isPermaLink=\"false\">{guid}</guid><pubDate>{dt.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>")
    write(DIST / "changes.xml",
          '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
          f"<title>{E(site.name)} · 调价动态</title><link>{E(site.url('/changes/'))}</link><description>AI API 中转站价格变动</description><language>zh-cn</language>"
          f"<lastBuildDate>{site.now.strftime('%a, %d %b %Y %H:%M:%S +0000')}</lastBuildDate>" + "".join(items) + "</channel></rss>\n")


# ---------------------------------------------------------------- about
def build_about(site: Site, cfg: dict[str, Any]) -> None:
    w = cfg.get("station_weights") or S.DEFAULT_STATION_WEIGHTS
    qw = cfg.get("quote_weights") or S.DEFAULT_QUOTE_WEIGHTS
    wi, wo = cfg.get("mix_input_weight", 3), cfg.get("mix_output_weight", 1)
    body = f"""
<section class="hero sm"><h1>评分与计价方法</h1><p class="lead">评分只使用可复核的公开信号与真实调用探针结果，每个数字旁均附客观依据。绝不接受付费改分。</p></section>
<section class="prose">
<h2>一、三档认证与非补偿封顶机制</h2>
<table class="plain">
<thead><tr><th>阶段</th><th>评级上限</th><th>准入与降级条件</th></tr></thead>
<tbody>
<tr><td><b>Stage 0 · 仅建档</b></td><td>不评分</td><td>接口返回 401/403（需登录或 WAF 拦截），仅保留基础档案页，不参与评分与比价。</td></tr>
<tr><td><b>Stage 1 · 公开校验</b></td><td><b>最高 B 级（严格 &lt; 74 分）</b></td><td>仅凭公开元数据（七维公开信号）无法验证真实调用质量，因此未接入真实探针的站点经单调平滑封顶映射到 [0, 74)，永远拿不到 A/S；报价综合分同样封顶 74 分。</td></tr>
<tr><td><b>Stage 2 · 实测认证</b></td><td><b>解锁 A / S 级</b></td><td>独立匿名小号连续 7 天真金白银探针（≥20 次样本，含连通/TTFT/TPOT、Tool-calling、Prompt Cache 命中与余额实扣对账）。实测分占 35%、公开分占 65%；若实测成功率 &lt; 90% 或账单实扣偏差超过 ±15%，触发非补偿否决，强制降级封顶 C 级（64 分）。另有真伪/智商否决，见下表。</td></tr>
</tbody></table>

<h3>真伪 + 智商验证分级（L0–L3）与双分否决</h3>
<p>延迟和可用率在多数中转站上已经够用；买家看不到、劣质中转最常作弊的是<b>模型真伪</b>（假模型、静默替换、伪造签名、剥离缓存）和<b>智商保真</b>（静默降级到弱档或量化模型、剥离推理档、静默截断上下文）。因此每次抽检都用一个新的随机种子（Mulberry32）现场生成题目，开源代码里没有可供正则识别的固定提示词；同一种子可完整复现，所有判分都是确定性的标准答案比对，不使用大模型当裁判。</p>
<table class="plain">
<thead><tr><th>档位</th><th>包含检测</th><th>说明</th></tr></thead>
<tbody>
<tr><td><b>L0 · 未验证</b></td><td>—</td><td>没有真伪抽检样本，或真伪通过率低于 85%，或出现过篡改签名被接受。</td></tr>
<tr><td><b>L1 · 快速真伪</b></td><td>协议指纹、Thinking 签名回传与篡改负检、动态工具调用、动态缓存命中</td><td>7 天真伪通过率 ≥ 85%，且篡改签名被接受 0 次。</td></tr>
<tr><td><b>L2 · 真伪 + 智商抽检</b></td><td>L1 + 6 步状态机追踪、4×3 逻辑推理（内置求解器保证唯一解）、4 条复合指令约束</td><td>在 L1 基础上，7 天智商抽检通过率 ≥ 65%。</td></tr>
<tr><td><b>L3 · 深度防降级</b></td><td>L2 + 长上下文 3 跳指针追踪（15% / 52% / 87% 深度）</td><td>在 L2 基础上，长文多跳通过率 ≥ 65%；同时比对上游 prompt_tokens 与估算值，标记静默截断或 token 虚增。</td></tr>
</tbody></table>
<ul>
<li><b>双分不互相抵消</b>：单次抽检分别给出<b>真伪特征分</b>与<b>智商保真分</b>（0–100）。篡改签名被接受、真伪分 &lt; 55 或智商分 &lt; 55 时，结论直接判为异常，不会被其他项目的高分平均掉。</li>
<li><b>Stage 2 真伪/智商否决</b>：7 天窗口内篡改签名被接受次数 &gt; 0、真伪通过率 &lt; 85% 或智商抽检通过率 &lt; 65%，即使首字 200ms、可用率 100%，也强制降级封顶 C 级（64 分）。存在真伪与智商样本时，实测分中真伪、智商的权重高于延迟。</li>
<li><b>双窗口</b>：同时统计近 24 小时与 7 天滚动窗口。近 24 小时通过率跌破否决线，或较 7 天均值骤降 25 个百分点以上，立即触发降级告警，防止站点在第 6 天悄悄切换到降级池。</li>
<li><b>预算熔断</b>：自动探针按单轮调用上限与每站每日调用上限执行，超出即跳过并记录原因，不会超支。</li>
</ul>

<h2>二、公开信号七维评分（0–100）</h2>
<p>站点信誉分衡量<b>可信度、可靠性、价格稳定、分组纯净与协议完备</b>，不衡量便宜程度；便宜程度只进入各模型页的报价综合分，并在站点页「旗舰模型价格定位」图中单独展示。</p>
<table class="plain">
<thead><tr><th>维度</th><th class="n">权重</th><th>计算依据</th></tr></thead>
<tbody>
<tr><td>稳定</td><td class="n">{w.get('stability', 0)}%</td><td>公开价格接口刷新成功率（贝叶斯平滑：先验 5 次、70% 成功），最近每连续失败一次扣 15 分，最多扣 45 分。</td></tr>
<tr><td>资历</td><td class="n">{w.get('longevity', 0)}%</td><td>主域名注册时长（ICANN RDAP / CNNIC WHOIS）：1 个月 15 分、半年 55 分、1 年 75 分、2 年 90 分、3 年以上 100 分；查不到按 35 分计。续费跑道：已过期 −35、30 天内到期 −20、60 天内 −12、180 天内 −4；已续费 1 年以上 +2、2 年以上 +4。</td></tr>
<tr><td>透明</td><td class="n">{w.get('transparency', 0)}%</td><td>公开充值比例 35 分；已定价型号占比 25 分；可选分组说明占比 20 分；异常低价占比越少越高（20 分）。</td></tr>
<tr><td>口碑</td><td class="n">{w.get('reputation', 0)}%</td><td>被多少个独立导航/社区/榜单收录（1 个 30 分、3 个 65 分、6 个以上 90 分），进入 Tranco / Umbrella / Majestic 流量榜加 10–25 分。</td></tr>
<tr><td>响应</td><td class="n">{w.get('latency', 0)}%</td><td>公开接口中位响应时间：0.6 秒内 100 分、1.5 秒 85 分、3 秒 60 分、6 秒 30 分。</td></tr>
<tr><td>价稳</td><td class="n">{w.get('price_stability', 0)}%</td><td>近 30 日公开报价稳定分：降价与 &lt;2% 舍入微调不扣分；按调价批次计，小幅上调每次 −6（最多 −30）、≥20% 急涨每次 −15（最多 −45）；最大涨幅 ≥50% 再 −12，涉及 ≥10 个型号 −8，≥3 次涨价 −6，输出档调整与批量下架少量扣分。</td></tr>
<tr><td>纯净</td><td class="n">{w.get('purity_protocol', 0)}%</td><td>分组纯净度与协议完备度：45% 纯净线路（可选付费分组中无 Kiro/Cursor/逆向/特惠自认标签的占比 50%、常规线报价占比 30%、无异常低价 20%）＋ 25% 价差克制（同一型号常规线与特惠/逆向线价差超过 4× 的占比越低越高）＋ 30% 旗舰协议与缓存完备（Claude 原生 Anthropic 协议、GPT-5/6 OpenAI Responses 协议、旗舰型号公开缓存价的覆盖率）。</td></tr>
</tbody></table>
<h3>短板拖累（弱链惩罚）</h3>
<p>加权算术均值允许“口碑高”抵消“刚暗涨 140%”，这正是旧版五维线性和的缺陷。新版对关键信任维度设置健康线 T 与强度 λ，低于健康线时按凸函数扣分：<code>拖累 = Σ λ·T·((T − x) / T)^1.5</code>，总拖累最多 20 分。当前参数：价稳 T=85、λ=0.6；纯净 T=60、λ=0.5；透明 T=60、λ=0.4；资历 T=60、λ=0.8。站点页雷达图中触发拖累的维度以橙色标出。</p>
<h3>Stage 1 平滑封顶</h3>
<p>公开信号分 r = 加权均值 − 短板拖累。未实测站点不再一刀切截断在 74（旧版有 112 家并列 74.0），而是经过严格单调、处处光滑的映射：</p>
<ol>
<li>软拐点压缩：<code>h(r) = r − (1 − m)·τ·[ln(1 + e^((r − K)/τ)) − ln(1 + e^(−K/τ))]</code>，K=45、m=0.65、τ=3，即低于 45 分基本不变，高于 45 分斜率平滑降为 0.65。</li>
<li>指数逼近上限：h ≤ 71 时 f = h；h &gt; 71 时 <code>f = 74 − 3·e^(−(h − 71)/3)</code>，在 71 处一阶导连续，永远严格低于 74。</li>
</ol>
<p>因此公开信号更好的站点永远得分更高，头部站点按真实差距拉开（2026-11-27 全量离线评估：764 家中 0 家并列 74.0，前 15% 分布在约 66.6–73.0）。Stage 2 实测站点不经过此映射：公开分 65% + 实测分 35%，实测否决仍强制封顶 C 级（64 分）。</p>
<h3>全网分位与不确定度</h3>
<ul>
<li><b>超过全网 N% 站点</b>：在所有参与评分的站点中，信誉分严格低于本站的占比。</li>
<li><b>± 不确定度</b>：按各维权重估算，有证据的维度按 ±6 分、缺数据按中性值填充的维度按 ±22 分合成标准差，再乘以平滑封顶在该点的斜率；<b>证据完整度</b> = 有真实证据的维度权重占比。</li>
<li>评分模型由 <code>tools/eval_scoring.py</code> 在全部真实站点数据上与“七维线性 + 封顶”“分位融合 + 风险惩罚”两个备选方案对比后选定，对比指标包括并列数、红旗站点排名位移以及权重 ±20% 扰动下的 Top-30 秩相关。</li>
</ul>
<p>等级划分：S ≥ 85，A ≥ 75，B ≥ 65，C ≥ 50，D &lt; 50。</p>

<h2>三、常规推荐防踩雷与多分组规则</h2>
<ul>
<li><b>绝不根据营销词自动盖章“官转/高稳”</b>：实证抽检表明“稳定/满血/不降智”等自夸文案误判率约 30%。我们只展示可核验的协议标签（<code>Anthropic</code> / <code>OpenAI Responses</code> / <code>Gemini</code> 等）、缓存价、分时档位，以及站点自己标注的 <b>Kiro / Cursor / 逆向反代 / 特惠限时</b> 提醒标签。</li>
<li><b>常规区与特惠逆向区隔离</b>：模型页默认将常规按量分组与特惠/逆向/福利分组分开，并支持展开同站全部分组对照价差。常规 Top 3 推荐严格否决自称逆向/特惠/测试分组、资历/透明不足站点及未实测却低于中位价 0.25× 的极端低价。</li>
<li><b>综合评分</b> = 价格分 × {qw['price']}% + 站点信誉分 × {qw['station']}%（未实测同样封顶 74 分）。站点信誉分本身不含便宜程度，避免“低价”被重复计分。价格分以常规中位价为 50 分，每便宜一半 +20 分、每贵一倍 −20 分。</li>
</ul>

<h2>四、人民币换算与双计费视角</h2>
<ol>
<li><b>对话 3:1 综合价</b> = (输入 × {wi} + 输出 × {wo}) ÷ {int(wi) + int(wo)}；<b>编程缓存 7:2:1 综合价</b> = 70% 缓存读 + 20% 未缓存输入 + 10% 输出（适合 Claude Code / Cursor / Agent 场景）。</li>
<li>最终价 = 基础价（或分档表达式最低档）× 分组倍率 × 公开充值比例。卡密折扣、充值赠送与套餐不计入。</li>
</ol>

<h2>五、收录、纠错与数据边界</h2>
<ul>
<li>每次刷新只读请求各站 <code>/api/status</code> 与 <code>/api/pricing</code>，绝不绕过验证码或防火墙；超过 {cfg.get('stale_days', 7)} 天未刷新标「待刷新」，超过 {cfg.get('hide_after_days', 30)} 天下线。</li>
<li>收录标准、WAF 放行方法与邮箱 / GitHub Issue 双通道纠错入口见 <a href="/submit/">提交收录 / 纠错</a>。</li>
</ul>

<h2>六、利益披露与独立性声明</h2>
<p>本站（<code>agentied.work</code>）由 Agentied 团队维护，团队同时运营自营中转站 <code>agentied.top</code>；站内部分第三方外链可能包含推广返佣参数（均标明 <code>rel="sponsored nofollow noopener"</code> 及「含返佣」提示）。</p>
<p><b>零优待原则</b>：自营站与所有合作或非合作第三方站点执行完全相同的公开抓取、分层封顶、否决与探针实测代码，不存在任何手工加分、白名单豁免或付费买分。任何价格、标签或实测异常均可按客观证据提交复核纠错。</p>

<h2>七、站点规格档案卡与三值缓存口径</h2>
<p>本站只做在线查看，<b>不提供批量数据下载或导出</b>。每家站点的固定规格在站点档案页底部的「给进阶用户核对」抽屉中展开查看；想在几家之间做决定，可用 <a href="/compare/">并排对比页</a>（仅展示对比所需的消费者口径指标，不含 API 地址、额度单位与分组倍率等原始配置）或 <a href="/stations/">排行榜</a> 的场景筛选。档案卡字段口径如下：</p>
<table class="plain">
<thead><tr><th>字段组</th><th>内容</th><th>来源与清洗</th></tr></thead>
<tbody>
<tr><td>计费与汇率口径</td><td>充值比例、充值模式（折价额度 / 按汇率直充 / 自定义比例）、站内汇率、额度单位 <code>quota_per_unit</code>、显示币种</td><td><code>/api/status</code> 公开设置；站内汇率在 4–12 之外（例如 1）时按参考汇率 7.25 判定充值模式</td></tr>
<tr><td>接入与官方入口</td><td>API Base URL、文档链接、充值入口、系统版本</td><td>剔除 localhost / 内网 IP / example.com / 非 http(s) 地址，以及 New API 模板默认的 <code>newapi.pro</code> 文档与上游仓库链接（站点自身位于该根域时除外）</td></tr>
<tr><td>账号与风控门槛</td><td>邮箱验证、Turnstile 人机校验、GitHub / Linux.do / Telegram 登录</td><td>只读公开开关，不采集任何用户或密钥信息</td></tr>
<tr><td>协议与缓存覆盖</td><td>原生协议、缓存读 / 缓存写公开覆盖率、公开分组数、有效型号数</td><td>覆盖率 = 公开缓存价的 token 型号 ÷ 可缓存 token 型号（不含「不适用」）</td></tr>
<tr><td>6 大旗舰价格</td><td>常规按量分组最低价的输入 / 输出 / 缓存读 / 缓存写与分组倍率</td><td>¥ / 百万 token；异常低价、免费与福利分组不参与</td></tr>
</tbody></table>
<ul>
<li><b>三值缓存口径</b>：缓存价格只显示三种状态——<b>免费</b>（站点明确配置 ×0）、<b>未公开</b>（站点没有填写，<b>不等于免费</b>）、<b>不适用</b>（生图、向量、按次计费或型号不支持缓存）。New API 的结构体默认零值（同一型号的写缓存同样为 0、或全站统一填 0 而没有任何正缓存倍率）一律归为「未公开」，不会误显示成免费。</li>
<li><b>缓存写</b>：同时读取 <code>create_cache_ratio</code> / <code>cache_creation_ratio</code> / <code>cache_creation_12h_ratio</code> 倍率字段与 <code>billing_expr</code> 表达式中的 <code>cc</code> / <code>cw</code> 项。</li>
<li><b>公式溯源</b>：站点页「原始倍率与公式视角」逐行给出可复核推导，例如 <code>模型×1.5 × 2 × 分组×0.4 × 充值¥1 = ¥1.2</code>；推导结果与表中输入价误差小于 1e-5。</li>
<li><b>最近变动</b>：每次构建比对上一轮的充值比例、站内汇率与分组倍率，近 30 天的变动显示在站点规格档案卡中。</li>
</ul>
</section>"""
    site.page("/about/", "评分与计价方法", "API 中转站 Stage 0/1/2 分层信誉评分、防踩雷推荐规则、7:2:1 缓存成本换算与利益披露说明。", body, crumbs=[("评分方法", "/about/")], priority=0.5)


# ---------------------------------------------------------------- home
def build_home(site: Site, cfg, model_pages, stations, closed_stations, fam_order, fam_models, changes) -> None:
    hot = sorted(model_pages.items(), key=lambda kv: -kv[1]["n"])[: int(cfg.get("hot_models_count", 24))]
    cards = []
    for canon, mp in hot:
        r, c = mp["rec"], mp["cheap"]
        rec = (f'<div class="rc">{grade_badge(r.score)} <span>推荐 {E(stations[r.domain]["display_name"][:16])}</span> '
               f'<small>{money(r.input_cny if r.kind == "token" else r.call_cny)}{"" if r.kind == "token" else "/次"}</small></div>') if r else ""
        if c is None:
            p = '<span class="muted">暂无常规人民币报价</span>'
        elif c.kind == "token":
            p = f'<small>常规最低</small> <b>{money(c.input_cny)}</b><small>输入</small> <b>{money(c.output_cny)}</b><small>输出</small>'
        else:
            p = f'<small>最低</small> <b>{money(c.call_cny)}</b><small>/次</small>'
        cards.append(f'<a class="card" href="/model/{mp["slug"]}/"><code>{E(canon)}</code>{rec}<div class="pr">{p}</div><div class="meta">{mp["n"]} 家站点</div></a>')
    fam_nav = " ".join(f'<a class="chip" href="/family/{f}/">{E(P.FAMILY_LABELS.get(f, f))} <small>{len(fam_models[f])}</small></a>' for f in fam_order)
    scene_cards = "".join(
        f'<a class="card scene-card" href="/scenes/{sc["slug"]}/"><b>{E(sc["name"])}</b><p class="muted">{E(sc.get("card") or sc["tagline"])}</p></a>'
        for sc in scenes.SCENES
    )
    recent = sorted([c for c in changes if c["type"] in ("down", "up", "new")], key=lambda x: x.get("run_at") or "", reverse=True)[:8]
    canon_slug = {c: mp["slug"] for c, mp in model_pages.items()}
    chg = "".join(
        f'<li><span class="badge {CHG_LABEL[c["type"]][1]}">{CHG_LABEL[c["type"]][0]}</span> '
        f'{E((stations.get(c["domain"]) or {}).get("display_name") or c["domain"])} · '
        + (f'<a href="/model/{canon_slug[P.canonical_model(c["model"])]}/"><code>{E(c["model"])}</code></a>' if P.canonical_model(c["model"]) in canon_slug else f'<code>{E(c["model"])}</code>')
        + f' <small>{_chg_price(c.get("old"))} → {_chg_price(c.get("new"))}</small></li>'
        for c in recent
    )
    latest = max(st["fetched_at"] for st in stations.values())
    n_stage2 = sum(1 for st in stations.values() if st["score"].stage == 2 and not st["score"].cap_reason)
    desc = cfg["description"]
    body = f"""
{DB.fav_bar()}
<section class="hero">
  <h1>{E(cfg.get('site_tagline', site.name))}</h1>
  <p class="lead">收录 <b>{len(stations)}</b> 家公开报价站（另含 {len(closed_stations)} 家闭源档案）与 <b>{len(model_pages)}</b> 个型号。展开多分组价差、支持协议与 7:2:1 缓存成本；未实测站点统一封顶 B 级（74 分）{f"，当前已有 {n_stage2} 家通过 Stage 2 实测认证" if n_stage2 else ""}。更新于 {E(fmt_cst(latest))}。</p>
  <div class="search big" role="search">
    <input id="q2" type="search" placeholder="输入模型名，回车直达推荐页，如 claude-sonnet-5-5 / deepseek-v4.1-flash" autocomplete="off" aria-label="搜索模型">
    <div id="sug2" class="sug" hidden></div>
  </div>
  <div class="tool-strip">
    <a class="tool-card info" href="/verify/"><b>LLM 模型真伪与智商抽检 →</b><span>动态种子现场出题：签名回传与篡改负检、工具与缓存真伪，加测状态机、逻辑推理、复合指令与长文多跳（支持本机脚本）</span></a>
    <a class="tool-card ok" href="/calc/"><b>月度成本算账与主备选站 →</b><span>对比官网直连 vs 常规中转 vs 80/20 混合路由，自动搭配 1 主 + 1 备独立站点</span></a>
    <a class="tool-card" href="/stations/"><b>站点高级档案与排行 →</b><span>查看 30 日价格稳定性、域名注册时长、全球流量榜排名与 GitHub 实测评价</span></a>
  </div>
  <div class="chips">{fam_nav}</div>
</section>
<section class="sec-block">
  {DB.section_head("01", "热门模型实时低价", "常规按量推荐 + 常规最低价（每百万 token · 人民币），已隔离逆向与特惠分组", "sec-hot", '<a class="more" href="/models/">全部模型 →</a>')}
  <div class="cards">{''.join(cards)}</div>
</section>
<section class="sec-block">
  {DB.section_head("02", "按你的场景选站", "写代码、日常聊天、Codex 还是生图？按你在用的软件直接看对应的常规推荐与真实缓存成本", "sec-scenes", '<a class="more" href="/scenes/">全部场景 →</a><a class="more" href="/calc/">月度成本算账 →</a>')}
  <div class="cards scene-cards">{scene_cards}</div>
</section>
<section class="sec-block">
  {DB.section_head("03", "近期调价动态", "每次刷新自动比对上一版公开价目，涨价、降价和上新一目了然", "sec-changes", '<a class="more" href="/changes/">全部调价 →</a>')}
  <div class="two">
  <div class="plain-card">
    <h3>最新调价</h3>
    {('<ul class="chg">' + chg + '</ul>') if chg else '<p class="muted">已建立当前版本快照，下一次刷新后显示调价记录。</p>'}
  </div>
  <div class="plain-card">
    <h3>怎么避开低价坑站</h3>
    <ul class="tips">
      <li><b>未实测不发 A/S 牌</b>：仅有公开数据的站点封顶 Stage 1（B 级 74 分），真金白银探针连续达标才解锁 A/S。</li>
      <li><b>常规区隔离逆向/特惠组</b>：自称 Kiro/Cursor/逆向/特惠及未核实极端低价不进常规 Top 3，展开行内分组可看全貌。</li>
      <li><b>按真实协议与缓存算账</b>：一键筛选 Anthropic / Responses 协议，切换 7:2:1 编程缓存成本。</li>
    </ul>
    <p><a href="/about/">完整评分方法与利益披露 →</a></p>
  </div>
  </div>
</section>
<section class="sec-block">
  {DB.section_head("04", "综合靠谱 Top 10", "按在线、资历、价格透明、防暗涨等七维公开信号综合打分，未实测站最高 B 级", "sec-top", '<a class="more" href="/stations/">完整排行 →</a>')}
  {station_rank_table(stations, {}, site.now, limit=10, compact=True)}
</section>"""
    site.page(
        "/", f"{site.name} - {cfg.get('site_tagline', '')}", desc, body,
        ld=[{"@context": "https://schema.org", "@type": "WebSite", "name": site.name, "url": site.url("/"), "description": desc,
             "potentialAction": {"@type": "SearchAction", "target": site.url("/models/?q={search_term_string}"), "query-input": "required name=search_term_string"}}],
        lastmod=latest, priority=1.0,
    )


# ---------------------------------------------------------------- seo files
def build_seo_files(site: Site, model_pages, stations, n_stage0: int) -> None:
    urls = "".join(
        f"<url><loc>{E(site.url(quote(p)))}</loc><lastmod>{lm}</lastmod><priority>{pr:.1f}</priority></url>"
        for p, lm, pr in sorted(site.urls, key=lambda x: -x[2])
    )
    write(DIST / "sitemap.xml", f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap.9">{urls}</urlset>\n'.replace("sitemap.9", "sitemap/0.9"))
    write(DIST / "robots.txt", f"User-agent: *\nAllow: /\nDisallow: /data/\nDisallow: /{SEARCH_INDEX}\n\nSitemap: {site.url('/sitemap.xml')}\n")
    lines = [
        f"# {site.name}", "", f"> {site.cfg['description']}", "",
        f"数据更新时间：{fmt_cst(site.now)}（北京时间）。收录 {len(stations)} 家公开报价站、{n_stage0} 家 Stage 0 仅建档闭源站、{len(model_pages)} 个型号。",
        "分层规则：未接入真金白银探针实测的站点一律封顶 Stage 1（最高 B 级 74 分）；实测达标解锁 Stage 2（A/S 级）。常规推荐区隔离自称 Kiro/Cursor/逆向/特惠分组。",
        "", "## 场景选站",
    ]
    for sc in scenes.SCENES:
        lines.append(f"- [{sc['name']}]({site.url('/scenes/' + sc['slug'] + '/')})：{sc['tagline']}")
    lines += [
        "", "## 主要页面",
        f"- [月度成本算账器与主备选站助手]({site.url('/calc/')})：官方直连 vs 常规中转 vs 80/20 混合路由，自动给出 1 主 + 1 备站点组合",
        f"- [LLM 模型真伪鉴别与协议自测工具]({site.url('/verify/')})：支持在线无状态边缘接口（POST /api/verify）与本机脚本，L1/L2/L3 三档动态种子抽检，分别给出真伪特征分与智商保真分",
        f"- [中转站靠谱度排行]({site.url('/stations/')})：「省钱 vs 靠谱」分布图 + 按使用场景 / 选站偏好筛选与勾选对比",
        f"- [中转站并排对比]({site.url('/compare/')})：2–4 家站点的靠谱分、约官网几折、注册门槛与 6 大热门模型价格逐行对齐，支持「只看差异项」与 ?s=域名1,域名2 分享链接（仅在线查看，不提供批量下载）",
        f"- [全部模型]({site.url('/models/')})",
        f"- [调价动态]({site.url('/changes/')})",
        f"- [评分与计价方法及利益披露]({site.url('/about/')})",
        f"- [提交收录 / 纠错]({site.url('/submit/')})：收录标准、WAF 放行说明与邮箱/GitHub Issue 双通道提交",
        f"- [English Overview]({site.url('/en/')})：Open-source AI API relay pricing & trust benchmark in USD",
        "", "## 站点信誉 Top 20",
    ]
    for i, (dom, st) in enumerate(sorted(stations.items(), key=lambda kv: (-kv[1]["score"].stage, -kv[1]["score"].total, -kv[1]["score"].raw_total))[:20], 1):
        sc = st["score"]
        lines.append(f"{i}. [{st['display_name']}]({site.url('/site/' + dom + '/')})（{dom}）：{sc.stage_label}，{sc.grade} 级 {sc.total:.0f} 分")
    lines += ["", "## 热门模型：常规按量推荐 / 常规最低价"]
    for canon, mp in sorted(model_pages.items(), key=lambda kv: -kv[1]["n"])[:60]:
        r, c = mp["rec"], mp["cheap"]
        rt = f"{who(r, stations)} {S.grade(r.score)} 级 {r.score:.0f} 分，{price_txt(r)}" if r else "—"
        ct = f"{who(c, stations)}，{price_txt(c)}" if c else "—"
        lines.append(f"- [{canon}]({site.url('/model/' + mp['slug'] + '/')})：{mp['n']} 家；推荐 {rt}；最低 {ct}")
    write(DIST / "llms.txt", "\n".join(lines) + "\n")


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", type=int, default=0, help="preview port after build")
    args = ap.parse_args()
    cfg = load_json(ROOT / "config.json", None)
    if not cfg:
        sys.exit("config.json missing")
    stats = build(cfg)
    print(json.dumps(stats, ensure_ascii=False))
    if args.serve:
        import functools
        import http.server

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DIST))
        print(f"preview: http://127.0.0.1:{args.serve}/")
        http.server.ThreadingHTTPServer(("127.0.0.1", args.serve), handler).serve_forever()


if __name__ == "__main__":
    main()
