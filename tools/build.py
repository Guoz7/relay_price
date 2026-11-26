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
import en_page  # noqa: E402
import export_readme  # noqa: E402
import model_specs as MS  # noqa: E402
import pricing as P  # noqa: E402
import probes  # noqa: E402
import scenes  # noqa: E402
import scoring as S  # noqa: E402
import station_insights as SI  # noqa: E402
import submit_page  # noqa: E402
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
        return '<span class="badge ok">Stage 2 · 实测认证</span>'
    if stage == 2 and failed_veto:
        return '<span class="badge danger">Stage 2 · 实测降级</span>'
    if stage == 0:
        return '<span class="badge warn">Stage 0 · 仅建档</span>'
    return '<span class="badge">Stage 1 · 未实测</span>'


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
    """Same root domain + same station name = one operator; keep the domain with most quotes."""
    order = seed_order()
    counts: dict[str, int] = defaultdict(int)
    for q in quotes:
        counts[q.domain] += 1
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for dom, st in stations.items():
        root = (intel.get(dom) or {}).get("root") or dom
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
def compute_station_scores(stations, by_station, intel, run_stats, cfg, probe_map: dict[str, Any] | None = None) -> None:
    weights = cfg.get("station_weights") or S.DEFAULT_STATION_WEIGHTS
    probe_map = probe_map or {}
    for dom, st in stations.items():
        rs = run_stats.get(dom) or {"attempts": 0, "successes": 0, "ms": []}
        for a in st.get("aliases") or []:
            ra = run_stats.get(a)
            if ra:
                rs = {"attempts": rs["attempts"] + ra["attempts"], "successes": rs["successes"] + ra["successes"], "ms": rs["ms"] + ra["ms"]}
        it = intel.get(dom) or {}
        qs = by_station.get(dom, [])
        groups = [g for g in (st.get("groups") or []) if g.get("selectable")]
        dims = [
            S.score_stability(rs["attempts"], rs["successes"], st.get("consecutive_failures") or 0),
            S.score_longevity(it.get("domain_age_days"), it.get("domain_registered_at")),
            S.score_transparency(
                topup_known=st.get("topup_price") is not None,
                priced=st.get("parsed") or 0,
                placeholders=st.get("dropped_placeholder") or 0,
                groups_selectable=len(groups),
                groups_with_desc=sum(1 for g in groups if g.get("desc")),
                quotes=len(qs),
                anomalies=sum(1 for q in qs if q.anomaly),
            ),
            S.score_reputation(len(it.get("sources") or []), it.get("tranco_rank"), it.get("umbrella_rank"), it.get("majestic_rank")),
            S.score_latency(statistics.median(rs["ms"]) if rs["ms"] else None),
        ]
        st["score"] = S.station_score(dims, weights, attempts=rs["attempts"], probe=probe_map.get(dom))


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
        if crumbs:
            items = [("首页", "/")] + crumbs
            crumbs_html = '<nav class="crumbs" aria-label="面包屑">' + " / ".join(
                f'<a href="{E(u)}">{E(t)}</a>' if i < len(items) - 1 else f"<span>{E(t)}</span>"
                for i, (t, u) in enumerate(items)
            ) + "</nav>"
            ld.append({
                "@context": "https://schema.org", "@type": "BreadcrumbList",
                "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": t, "item": self.url(u)} for i, (t, u) in enumerate(items)],
            })
        full_title = f"{title} | {self.name}" if path != "/" else title
        robots = '<meta name="robots" content="noindex,follow">' if noindex else ""
        is_en = lang.lower().startswith("en")
        nav_html = (
            '<a href="/en/">Overview</a><a href="/calc/">TCO Calc</a><a href="/verify/">Verify Model</a>'
            '<a href="/scenes/claude-code/">Claude Code</a><a href="/stations/">Relays</a>'
            '<a href="/about/">Method</a><a href="/submit/">Submit</a><a href="/">中文</a>'
            if is_en else
            '<a href="/scenes/">场景</a><a href="/calc/">算账</a><a href="/verify/">验真</a>'
            '<a href="/stations/">排行</a><a href="/models/">模型</a><a href="/changes/">调价</a>'
            '<a href="/about/">方法</a><a href="/submit/">收录</a><a href="/en/" title="English Overview">EN</a>'
        )
        foot_html = (
            f'<p>Unprobed relays are hard-capped at Stage 1 (Grade B / 74). Only relays passing 7-day real-call probes unlock Stage 2 (A/S). Prices come from read-only public APIs and official models.dev / LiteLLM baselines; verify with small top-ups first.</p>'
            f'<p>Built {fmt_cst(self.now)} (UTC+8) · <a href="/calc/">TCO Calculator</a> · <a href="/verify/">Model Verifier</a> · <a href="/about/">Methodology &amp; Disclosure</a> · <a href="/submit/">Submit / Correct</a> · <a href="/">中文主站</a> · <a href="/sitemap.xml">Sitemap</a></p>'
            if is_en else
            f'<p>未接入真金白银探针实测的站点统一封顶 Stage 1（最高 B 级 74 分），仅实测达标站点解锁 A/S 认证。价格按站点公开只读接口、分组倍率与公开充值比例换算，未计入卡密折扣与活动；请以站点实时页面为准，大额充值前先小额测试。</p>'
            f'<p>本页生成于 {fmt_cst(self.now)}（北京时间）· <a href="/scenes/">场景选站</a> · <a href="/calc/">月度算账器</a> · <a href="/verify/">模型验真</a> · <a href="/about/">评分方法与利益披露</a> · <a href="/submit/">提交收录 / 纠错</a> · <a href="/en/">English</a> · <a href="/changes.xml">RSS</a> · <a href="/sitemap.xml">Sitemap</a></p>'
        )
        brand_lbl = "API Price Radar" if is_en else self.name
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
def filter_bar(tid: str, *, show_mode: bool = True, show_ep: bool = True) -> str:
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
        '<option value="openai-response">OpenAI Responses</option>'
        '<option value="openai">OpenAI Chat</option>'
        '<option value="gemini">Gemini</option>'
        '<option value="image">生图</option>'
        '<option value="video">视频</option>'
        '</select></label>'
    ) if show_ep else ""
    return (
        f'<div class="filters" data-for="{tid}">{mode_html}{ep_html}'
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
            f'<th data-type="{t}" class="{cls}" scope="col"{" aria-sort=descending" if h == "综合评分" else ""}><button type="button">{E(h)}</button></th>'
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
        ep_pills = "".join(f'<span class="badge ep">{E(P.ENDPOINT_LABELS.get(x, x))}</span>' for x in eps[:3])
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
            f'{E(ago(f, now) + (" · 待刷新" if stale else ""))}</td>'
        )

    def badges(q: P.Quote) -> str:
        b = ['<span class="badge free">免费</span>'] if q.free else []
        if q.median_ratio is not None and not q.free and not q.welfare:
            if q.median_ratio < 0.25:
                b.append(f'<span class="badge warn">中位价 {q.median_ratio:.2f}×</span>')
        b += [f'<span class="badge">{E(x)}</span>' for x in q.flags]
        return ("<div class='bd'>" + "".join(b) + "</div>") if b else ""

    def price_cell(cny: float | None, usd: float | None, strong: bool = False) -> str:
        if cny is None and usd is None:
            return '<td class="n" data-v="9e9">—</td>'
        sortv = cny if cny is not None else 9e8 + usd
        main = money(cny) if cny is not None else f'<span class="usd" title="站点未公开充值比例，显示美元额度">{money(usd, "$")}</span>'
        sub = f"<small>{money(usd, '$')}</small>" if (cny is not None and usd is not None and abs(cny - usd) > 1e-12) else ""
        return f'<td class="n{" hl" if strong else ""}" data-v="{sortv}">{main}{sub}</td>'

    def mix_cell(q: P.Quote) -> str:
        mv = mix(q, cfg, "cny")
        cv = cache_mix_cny(q, cfg)
        l_mix = money(mv) if mv is not None else '<span class="muted">—</span>'
        vm = f"{mv:.6g}" if mv is not None else "9e9"
        vc = f"{cv:.6g}" if cv is not None else ""
        return f'<td class="n mix-col" data-v="{vm}" data-m="{vm}" data-c="{vc}">{l_mix}</td>'

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
                f'{price_cell(q.input_cny, q.input_usd, strong=True)}{price_cell(q.output_cny, q.output_usd)}'
                f'{price_cell(q.cache_cny, q.cache_usd)}{mix_cell(q)}'
                f'{tail(q, st)}</tr>{sub_row(q, cls, eps_csv)}'
            )
        parts.append(
            f'<h2 id="{table_id}-token">{E(title_prefix)}按 Token 计费 <small>每百万 token 人民币（小字为站内美元额度）· 可切 7:2:1 缓存视角</small></h2>'
            f'<div class="tbl-wrap"><table class="qt sortable" id="{table_id}-t"><thead><tr>'
            + head([("输入", "n", ""), ("输出", "n", ""), ("缓存读", "n", ""), ("3:1 综合价", "n", "mix-th")])
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
                f'{price_cell(q.call_cny, q.call_usd, strong=True)}{tail(q, st)}</tr>{sub_row(q, cls, eps_csv)}'
            )
        parts.append(
            f'<h2 id="{table_id}-call">{E(title_prefix)}按次计费 <small>每次调用人民币</small></h2>'
            f'<div class="tbl-wrap"><table class="qt sortable" id="{table_id}-c"><thead><tr>'
            + head([("每次", "n", "")])
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
        f'<h3>Stage 2 真实调用探针证据 <small>近 {pr.get("window_days", 7)} 天 {pr.get("total_samples", 0)} 次实测 · 最后探测 {E(fmt_cst(parse_dt(pr.get("last_probed_at"))))}</small></h3>'
        f'<div class="kpis">'
        f'<div><b>{pr.get("success_rate", 0):.1%}</b><span>真实成功率</span></div>'
        f'<div><b>{ttft}</b><span>中位首字 TTFT（TPOT {tpot}）</span></div>'
        f'<div><b>{tool_r} / {cache_r}</b><span>工具调用 / 缓存命中</span></div>'
        f'<div><b>{drift_txt}</b><span>余额实扣 vs 公开标价偏差</span></div>'
        f'</div>'
        + (f'<div class="probe-bars" aria-label="近 7 日每日成功率">{"".join(bars)}</div>' if bars else "")
        + "</section>"
    )


def score_panel(st: dict[str, Any], cfg: dict[str, Any]) -> str:
    sc: S.StationScore = st["score"]
    w = cfg.get("station_weights") or S.DEFAULT_STATION_WEIGHTS
    lis = []
    for key in ("stability", "longevity", "transparency", "reputation", "latency"):
        d = sc.dims[key]
        lis.append(
            f'<li><span class="dl">{S.DIM_LABELS[key]}<small>{w.get(key, 0)}%</small></span>'
            f'<span class="bar"><i style="width:{d.score:.0f}%"></i></span><b>{d.score:.0f}</b>'
            f'<small class="ev">{E(d.evidence)}{"（缺数据，按中性值计）" if (not d.known and "样本不足" not in d.evidence and "未" not in d.evidence and "无" not in d.evidence) else ""}</small></li>'
        )
    pd = sc.dims["probe"]
    p_cls = "" if pd.known else "pending"
    p_wt = f"{int(S.PROBE_BLEND_WEIGHT * 100)}%" if sc.stage == 2 else "未计"
    lis.append(
        f'<li class="{p_cls}"><span class="dl">实测<small>{p_wt}</small></span>'
        f'<span class="bar"><i style="width:{pd.score if pd.known else 0:.0f}%"></i></span>'
        f'<b>{f"{pd.score:.0f}" if pd.known else "—"}</b>'
        f'<small class="ev">{E(pd.evidence)}</small></li>'
    )
    cap_line = ""
    if sc.stage == 1 and sc.raw_total > S.STAGE1_CAP:
        cap_line = f'<small class="muted">公开信号分 {sc.raw_total:.0f} → 未实测封顶 B 级（74）</small>'
    elif sc.cap_reason:
        cap_line = f'<small class="badge danger">{E(sc.cap_reason)}</small>'
    return (
        f'<section class="score-card"><div class="big">{grade_badge(sc.total)}<span class="num">{sc.total:.0f}</span>'
        f'{stage_badge(sc.stage, bool(sc.cap_reason and sc.stage == 2))}'
        f'<small>站点信誉分 · 置信度{sc.confidence}</small>{cap_line}</div><ul class="dims">{"".join(lis)}</ul></section>'
        + probe_panel(sc)
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
    for f in ASSETS.iterdir():
        shutil.copy2(f, DIST / "assets" / f.name)
    asset_ver = hashlib.sha1(b"".join(f.read_bytes() for f in sorted(ASSETS.iterdir()))).hexdigest()[:8]
    site = Site(cfg, now, asset_ver)
    min_st = int(cfg.get("min_stations_for_model_page", 3))
    index_min_st = int(cfg.get("index_min_stations", 10))

    intel = load_json(DATA / "intel.json", {})
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
    compute_station_scores(stations, by_station, intel, run_stats, cfg, probe_map)
    compute_quote_scores(by_model, stations, cfg)

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

    changes = update_history(quotes, stations, now)
    stability_map = SI.compute_price_stability(changes)
    reviews_map = SI.load_reviews(DATA)
    _log(f"scored {len(model_pages)} models, starting page render")
    build_model_pages(site, model_pages, stations, cfg, specs)
    build_station_pages(site, stations, by_station, intel, changes, cfg, run_stats=run_stats, stability_map=stability_map, reviews_map=reviews_map)
    build_closed_station_pages(site, closed_stations)
    fam_order, fam_models = build_indexes(site, model_pages, stations, by_station, closed_stations, cfg, min_st, intel=intel, stability_map=stability_map)
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
    write(DIST / "data" / "search.json", json.dumps(idx, ensure_ascii=False, separators=(",", ":")))
    write(DIST / "data" / "summary.json", json.dumps({
        "generated_at": now.isoformat(),
        "stations": len(stations),
        "stage0_stations": len(closed_stations),
        "stage2_stations": sum(1 for st in stations.values() if st["score"].stage == 2),
        "models": len(model_pages),
        "indexed_models": sum(1 for mp in model_pages.values() if mp["indexed"]),
        "quotes": len(quotes),
    }, ensure_ascii=False))
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
    write(DIST / "_headers", "/assets/*\n  Cache-Control: public, max-age=31536000, immutable\n/data/*\n  Cache-Control: public, max-age=600\n/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n")
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
  {('<h2 class="h-rec">常规按量推荐（未实测最高封顶 B 级）</h2><div class="recs">' + top_cards + '</div>') if top_cards else ''}
  {kpis}
</section>
{filter_bar("m", show_mode=bool(any(q.kind == "token" for q in rows)), show_ep=True)}
{std_tbl or '<p class="muted">该型号暂无常规按量报价，请展开下方特惠/福利分组查看。</p>'}
{alt_section}
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
def _station_bento(
    dom: str,
    st: dict[str, Any],
    qs: list[P.Quote],
    it: dict[str, Any],
    rs: dict[str, Any],
    stab: dict[str, Any] | None,
    revs: list[dict[str, Any]],
) -> str:
    sc: S.StationScore = st["score"]
    # 1. Price Stability card
    stab = stab or {"total": 0, "price_moves": 0, "ups": 0, "downs": 0, "outs": 0, "news": 0, "removed": 0, "max_up_pct": 0.0, "max_down_pct": 0.0, "label": "价格平稳", "cls": "ok"}
    up_txt = f"{stab['ups']} 次（最大 +{stab['max_up_pct']:g}%）" if stab["ups"] else "0 次"
    dn_txt = f"{stab['downs']} 次（最大 -{stab['max_down_pct']:g}%）" if stab["downs"] else "0 次"
    card_price = (
        '<div class="bcard">'
        f'<div class="bcard-h"><h3>价格稳定性（近 30 日）</h3><span class="badge {stab["cls"]}">{E(stab["label"])}</span></div>'
        '<dl class="bkv">'
        f'<dt>涨价异动</dt><dd>{E(up_txt)}</dd>'
        f'<dt>降价调整</dt><dd>{E(dn_txt)}</dd>'
        f'<dt>型号变动</dt><dd>上新 {stab["news"]} · 下架 {stab["removed"]} · 输出档调整 {stab["outs"]}</dd>'
        f'<dt>公开调价记录</dt><dd>{"见下方 30 日调价明细" if stab["total"] else "近 30 天内未监测到公开报价变动"}</dd>'
        '</dl></div>'
    )

    # 2. Domain Longevity & Infra card
    age_s, age_long, age_cls = SI.fmt_domain_age(it.get("domain_age_days"))
    reg_at = it.get("domain_registered_at") or "未知"
    exp_at = it.get("domain_expires_at") or "未知"
    registrar = (it.get("registrar") or "未公开")[:44]
    ranks = []
    if it.get("tranco_rank"):
        ranks.append(f"Tranco #{int(it['tranco_rank']):,}")
    if it.get("umbrella_rank"):
        ranks.append(f"Umbrella #{int(it['umbrella_rank']):,}")
    if it.get("majestic_rank"):
        ranks.append(f"Majestic #{int(it['majestic_rank']):,}")
    rank_s = " · ".join(ranks) if ranks else "暂未进入全球公开流量榜"
    att, suc = int(rs.get("attempts") or 0), int(rs.get("successes") or 0)
    ms_list = rs.get("ms") or []
    med_ms = f"{statistics.median(ms_list):.0f}ms" if ms_list else "—"
    uptime_s = f"{suc}/{att}（{suc / att:.0%}）· 中位响应 {med_ms}" if att else "样本累计中"
    card_domain = (
        '<div class="bcard">'
        f'<div class="bcard-h"><h3>域名时长与基础设施</h3><span class="badge {age_cls}">{E(age_s)}</span></div>'
        '<dl class="bkv">'
        f'<dt>主域名注册</dt><dd>{E(str(reg_at))} · {E(age_long)}</dd>'
        f'<dt>到期 / 注册商</dt><dd>{E(str(exp_at))} · {E(registrar)}</dd>'
        f'<dt>全球流量排名</dt><dd>{E(rank_s)}</dd>'
        f'<dt>30日接口可用</dt><dd>{E(uptime_s)}</dd>'
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
        auth_badge = f'<span class="badge {"danger" if sc.cap_reason else "ok"}">Stage 2 探针已接入</span>'
        auth_dl = (
            f'<dt>真实调用率</dt><dd>{pr.get("success_rate", 0):.1%}（{pr.get("total_samples", 0)} 次样本）</dd>'
            f'<dt>工具 / 缓存</dt><dd>工具 {pr["tool_call_pass_rate"]:.0%} · 缓存 {pr["cache_hit_rate"]:.0%}</dd>'
            if (pr.get("tool_call_pass_rate") is not None and pr.get("cache_hit_rate") is not None) else
            f'<dt>真实调用率</dt><dd>{pr.get("success_rate", 0):.1%}（{pr.get("total_samples", 0)} 次样本）</dd>'
        )
    else:
        auth_badge = '<span class="badge">Stage 1 未官测</span>'
        auth_dl = (
            '<dt>真伪验真状态</dt><dd>尚未纳入 7 日官测探针池（评级封顶 B 级 74 分）</dd>'
            '<dt>自带 Key 自测</dt><dd>支持一键检验 Thinking 加密签名回传、Tool-Calling 与两轮缓存命中</dd>'
        )
    card_auth = (
        '<div class="bcard">'
        f'<div class="bcard-h"><h3>LLM 模型真伪鉴别</h3>{auth_badge}</div>'
        f'<dl class="bkv">{auth_dl}</dl>'
        f'<div><a class="btn sm primary" href="{verify_href}">现场鉴别这家站模型真伪 →</a></div>'
        '</div>'
    )

    # 4. Community Reviews & Reputation card
    srcs = it.get("sources") or []
    rev_issue_url = (
        "https://github.com/Guoz7/relay_price/issues/new?template=station-review.yml&title="
        + quote(f"[实测评价] {dom}", safe="")
    )
    if revs:
        avg_r = sum(float(r.get("rating") or 0) for r in revs) / len(revs)
        rev_badge = f'<span class="badge ok">★ {avg_r:.1f} / 5（{len(revs)} 条实测）</span>'
        rev_body = '<div class="rev-list">' + "".join(
            f'<div class="rev-item"><b>{"★" * int(r.get("rating") or 0)}</b> <code>{E(str(r.get("model") or ""))}</code> '
            f'<small>@{E(str(r.get("author") or "developer"))} · {E(str(r.get("date") or ""))}</small>'
            f'<p>{E(str(r.get("summary") or "")[:180])}</p></div>'
            for r in revs[:3]
        ) + "</div>"
    else:
        rev_badge = f'<span class="badge">外部收录 {len(srcs)} 处 · 暂无站内实评</span>'
        src_preview = "、".join(srcs[:4]) + ("…" if len(srcs) > 4 else "") if srcs else "自主发现"
        rev_body = (
            '<dl class="bkv">'
            f'<dt>外部社区收录</dt><dd>{E(src_preview)}</dd>'
            '<dt>站内实测评价</dt><dd>为防刷评，仅收录带 GitHub 账号与客观证据的实测评价</dd>'
            '</dl>'
        )
    card_rev = (
        '<div class="bcard">'
        f'<div class="bcard-h"><h3>社区口碑与实测评价</h3>{rev_badge}</div>'
        f'{rev_body}'
        f'<div><a class="btn sm" href="{rev_issue_url}" target="_blank" rel="noopener">提交我的实测评价 / 避雷证据 →</a></div>'
        '</div>'
    )
    return f'<section class="bento-4" aria-label="站点高级详情">{card_price}{card_domain}{card_auth}{card_rev}</section>'


def build_station_pages(
    site: Site, stations, by_station, intel, changes, cfg,
    *, run_stats: dict[str, Any] | None = None, stability_map: dict[str, Any] | None = None,
    reviews_map: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    now = site.now
    run_stats = run_stats or {}
    stability_map = stability_map or {}
    reviews_map = reviews_map or {}
    chg_by_dom: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in sorted(changes, key=lambda x: x.get("run_at") or "", reverse=True):
        if len(chg_by_dom[c["domain"]]) < 15:
            chg_by_dom[c["domain"]].append(c)

    for dom, st in stations.items():
        qs = by_station.get(dom, [])
        name = st["display_name"]
        sc: S.StationScore = st["score"]
        groups = st.get("groups") or []
        g_rows = "".join(
            f"<tr><td><code>{E(str(g['name'])[:40])}</code></td><td class='n'>×{g['ratio']:g}</td>"
            f"<td>{'' if g['selectable'] else '<span class=muted>不可选</span>'}"
            + "".join(f' <span class="badge caveat">{E(c)}</span>' for c in (g.get("caveats") or []))
            + f"</td><td class='desc'>{E((g['desc'] or '')[:160])}</td></tr>"
            for g in groups[:80]
        )
        fam_counts: dict[str, int] = defaultdict(int)
        for q in qs:
            fam_counts[q.family] += 1
        fam_txt = "、".join(f"{P.FAMILY_LABELS.get(f, f)} {n}" for f, n in sorted(fam_counts.items(), key=lambda x: -x[1])[:6])
        ep_pills = "".join(f'<span class="badge ep">{E(P.ENDPOINT_LABELS.get(e, e))}</span>' for e in (st.get("endpoints") or []))
        tbl = quote_table(site, qs, stations, show_model=True, show_station=False, table_id="s")
        tp = st.get("topup_price")
        aliases = st.get("aliases") or []
        fails = st.get("consecutive_failures") or 0
        health = (
            f'<span class="badge warn">最近 {fails} 次刷新失败（{E(str(st.get("last_error") or ""))}），展示 {E(ago(st["fetched_at"], now))} 的数据</span>'
            if fails else '<span class="badge ok">最近一次公开接口刷新成功</span>'
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

        bento_html = _station_bento(dom, st, qs, it, run_stats.get(dom) or {}, stability_map.get(dom), reviews_map.get(dom) or [])
        body = f"""
<section class="hero sm">
  <p class="eyebrow">站点口碑档案 · {E(sc.stage_label)}</p>
  <h1>{E(name)} 靠谱吗？ <small>{E(dom)}</small></h1>
  <p class="lead">{health}</p>
  {score_panel(st, cfg)}
  {bento_html}
  <div class="kpis">
    <div><b>{E(f"¥{tp:g}" if tp is not None else "未知")}</b><span>{E(st.get("topup_note") or "")}</span></div>
    <div><b>{st.get("parsed", len(qs))}</b><span>有效定价型号</span></div>
    <div><b>{len(groups)}</b><span>公开分组</span></div>
    <div><b>{E(ago(st["fetched_at"], now))}</b><span>数据更新</span></div>
  </div>
  <p>访问站点：{site.ext_link(dom)}{(' · 镜像域名：' + '、'.join(E(a) for a in aliases)) if aliases else ''}{(' · 系统版本：<code>' + E(str(st.get("version"))) + '</code>') if st.get("version") else ''}{(' · ' + ' · '.join(oauth_tags)) if oauth_tags else ''}</p>
  {('<div class="bd">' + ep_pills + '</div>') if ep_pills else ''}
  <p class="muted">型号分布：{E(fam_txt)}{('；收录来源：' + E('、'.join(srcs[:8])) + ('…' if len(srcs) > 8 else '')) if srcs else ''}</p>
</section>
<details class="groups"><summary>全部分组倍率与自认标签（{len(groups)}）</summary>
<div class="tbl-wrap"><table class="plain"><thead><tr><th>分组</th><th class="n">倍率</th><th>状态 / 提醒</th><th>站点原文说明</th></tr></thead><tbody>{g_rows}</tbody></table></div></details>
{chg_html}
{filter_bar("s", show_mode=True, show_ep=True)}
{tbl}"""
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
def station_rank_table(
    stations, by_station, now, limit: int | None = None, compact: bool = False,
    intel: dict[str, Any] | None = None, stability_map: dict[str, Any] | None = None,
) -> str:
    intel = intel or {}
    stability_map = stability_map or {}
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
        dims = "" if compact else "".join(
            f'<td class="n" data-v="{sc.dims[k].score:.1f}"><span class="{"" if sc.dims[k].known else "muted"}">{sc.dims[k].score:.0f}</span></td>'
            for k in ("stability", "longevity", "transparency", "reputation", "latency")
        )
        extra_cols = ""
        if not compact:
            days = (intel.get(dom) or {}).get("domain_age_days")
            age_s, _, age_cls = SI.fmt_domain_age(days)
            sb = stability_map.get(dom)
            if sb and (sb["ups"] or sb["downs"]):
                sb_txt = f'涨{sb["ups"]}·降{sb["downs"]}'
                sb_cls = sb["cls"]
                sb_v = sb["ups"] * 100 + sb["price_moves"]
            else:
                sb_txt, sb_cls, sb_v = "平稳", "ok", 0
            extra_cols = (
                f'<td class="n" data-v="{days if isinstance(days, (int, float)) else -1}"><span class="badge {age_cls}">{E(age_s)}</span></td>'
                f'<td class="n" data-v="{sb_v}"><span class="badge {sb_cls}">{E(sb_txt)}</span></td>'
            )
        st_lbl = "Stage 2 实测" if (sc.stage == 2 and not sc.cap_reason) else ("Stage 2 降级" if sc.stage == 2 else "Stage 1 未实测")
        rows.append(
            f'<tr><td class="n" data-v="{i}">{i}</td>'
            f'<td class="st" data-v="{E(dom)}"><a href="/site/{E(dom)}/">{E(st["display_name"])}</a><small>{E(dom)}</small></td>'
            f'<td class="sc" data-v="{sc.total}">{grade_badge(sc.total)} <b>{sc.total:.0f}</b><small>{st_lbl} · 置信度{sc.confidence}</small></td>'
            f'{dims}{extra_cols}'
            f'<td class="n" data-v="{tp if tp is not None else 9e9}">{E(f"¥{tp:g}/$1" if tp is not None else "未知")}</td>'
            + ("" if compact else f'<td class="n" data-v="{st.get("parsed", len(by_station.get(dom, [])))}">{st.get("parsed", len(by_station.get(dom, [])))}</td>'
               f'<td class="t" data-v="{int(st["fetched_at"].timestamp())}">{E(ago(st["fetched_at"], now))}</td>')
            + "</tr>"
        )
    dim_heads = "" if compact else "".join(f'<th data-type="n"><button type="button">{S.DIM_LABELS[k]}</button></th>' for k in ("stability", "longevity", "transparency", "reputation", "latency"))
    extra_heads = "" if compact else '<th data-type="n"><button type="button">域名时长</button></th><th data-type="n"><button type="button">30日价格</button></th>'
    return (
        '<div class="tbl-wrap"><table class="qt sortable"><thead><tr>'
        '<th data-type="n"><button type="button">#</button></th><th data-type="s"><button type="button">站点</button></th>'
        '<th data-type="n" aria-sort="descending"><button type="button">信誉分</button></th>'
        f'{dim_heads}{extra_heads}'
        '<th data-type="n"><button type="button">充值比例</button></th>'
        + ("" if compact else '<th data-type="n"><button type="button">型号</button></th><th data-type="n"><button type="button">更新</button></th>')
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
    *, intel: dict[str, Any] | None = None, stability_map: dict[str, Any] | None = None,
):
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
        f'<section class="hero sm"><h1>AI API 中转站分层评分排行榜</h1>'
        f'<p class="lead">未接入真实调用探针的站点统一 <b>Stage 1 封顶 B 级（74 分）</b>，仅实测达标解锁 A/S 认证。公开分维度：{E(S.summarize_weights(w))}。当前分布：{gtxt}。<a href="/about/">评分方法 →</a></p></section>'
        + station_rank_table(stations, by_station, site.now, intel=intel, stability_map=stability_map)
        + closed_html,
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
<tr><td><b>Stage 1 · 公开校验</b></td><td><b>最高 B 级（74 分）</b></td><td>仅凭公开元数据（稳定、资历、透明、口碑、响应）无法验证真实调用质量，因此无论公开信号分多高，未接入真实探针的站点与报价一律封顶 74 分（B 级）。</td></tr>
<tr><td><b>Stage 2 · 实测认证</b></td><td><b>解锁 A / S 级</b></td><td>独立匿名小号连续 7 天真金白银探针（≥20 次样本，含连通/TTFT/TPOT、Tool-calling、Prompt Cache 命中与余额实扣对账）。实测分占 35%、公开分占 65%；若实测成功率 &lt; 90% 或账单实扣偏差超过 ±15%，触发非补偿否决，强制降级封顶 C 级（64 分）。</td></tr>
</tbody></table>

<h2>二、公开信号五维评分（0–100）</h2>
<table class="plain">
<thead><tr><th>维度</th><th class="n">权重</th><th>计算依据</th></tr></thead>
<tbody>
<tr><td>稳定</td><td class="n">{w['stability']}%</td><td>公开价格接口刷新成功率（贝叶斯平滑：先验 5 次、70% 成功），最近每连续失败一次扣 15 分，最多扣 45 分。</td></tr>
<tr><td>资历</td><td class="n">{w['longevity']}%</td><td>主域名注册时长（ICANN RDAP / CNNIC WHOIS）：1 个月 15 分、半年 55 分、1 年 75 分、2 年 90 分、3 年以上 100 分；查不到按 35 分计。</td></tr>
<tr><td>透明</td><td class="n">{w['transparency']}%</td><td>公开充值比例 35 分；已定价型号占比 25 分；可选分组说明占比 20 分；异常低价占比越少越高（20 分）。</td></tr>
<tr><td>口碑</td><td class="n">{w['reputation']}%</td><td>被多少个独立导航/社区/榜单收录（1 个 30 分、3 个 65 分、6 个以上 90 分），进入 Tranco / Umbrella / Majestic 流量榜加 10–25 分。</td></tr>
<tr><td>响应</td><td class="n">{w['latency']}%</td><td>公开接口中位响应时间：0.6 秒内 100 分、1.5 秒 85 分、3 秒 60 分、6 秒 30 分。</td></tr>
</tbody></table>
<p>等级划分：S ≥ 85，A ≥ 75，B ≥ 65，C ≥ 50，D &lt; 50。</p>

<h2>三、常规推荐防踩雷与多分组规则</h2>
<ul>
<li><b>绝不根据营销词自动盖章“官转/高稳”</b>：实证抽检表明“稳定/满血/不降智”等自夸文案误判率约 30%。我们只展示可核验的协议标签（<code>Anthropic</code> / <code>OpenAI Responses</code> / <code>Gemini</code> 等）、缓存价、分时档位，以及站点自己标注的 <b>Kiro / Cursor / 逆向反代 / 特惠限时</b> 提醒标签。</li>
<li><b>常规区与特惠逆向区隔离</b>：模型页默认将常规按量分组与特惠/逆向/福利分组分开，并支持展开同站全部分组对照价差。常规 Top 3 推荐严格否决自称逆向/特惠/测试分组、资历/透明不足站点及未实测却低于中位价 0.25× 的极端低价。</li>
<li><b>综合评分</b> = 价格分 × {qw['price']}% + 站点信誉分 × {qw['station']}%（未实测同样封顶 74 分）。价格分以常规中位价为 50 分，每便宜一半 +20 分、每贵一倍 −20 分。</li>
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
        f'<a class="card scene-card" href="/scenes/{sc["slug"]}/"><b>{E(sc["name"])}</b><p class="muted">{E(sc["tagline"])}</p></a>'
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
<section class="hero">
  <h1>{E(cfg.get('site_tagline', site.name))}</h1>
  <p class="lead">收录 <b>{len(stations)}</b> 家公开报价站（另含 {len(closed_stations)} 家闭源档案）与 <b>{len(model_pages)}</b> 个型号。展开多分组价差、支持协议与 7:2:1 缓存成本；未实测站点统一封顶 B 级（74 分）{f"，当前已有 {n_stage2} 家通过 Stage 2 实测认证" if n_stage2 else ""}。更新于 {E(fmt_cst(latest))}。</p>
  <div class="search big" role="search">
    <input id="q2" type="search" placeholder="输入模型名，回车直达推荐页，如 claude-sonnet-5-5 / deepseek-v4.1-flash" autocomplete="off" aria-label="搜索模型">
    <div id="sug2" class="sug" hidden></div>
  </div>
  <div class="tool-strip">
    <a class="tool-card info" href="/verify/"><b>LLM 模型真伪鉴别 →</b><span>现场检验 Thinking 加密签名回传、Tool-Calling 与两轮 Prompt Cache 真命中（支持本机脚本）</span></a>
    <a class="tool-card ok" href="/calc/"><b>月度成本算账与主备选站 →</b><span>对比官网直连 vs 常规中转 vs 80/20 混合路由，自动搭配 1 主 + 1 备独立站点</span></a>
    <a class="tool-card" href="/stations/"><b>站点高级档案与排行 →</b><span>查看 30 日价格稳定性、域名注册时长、全球流量榜排名与 GitHub 实测评价</span></a>
  </div>
  <div class="chips">{fam_nav}</div>
</section>
<section>
  <h2>按使用场景直接选站 <a class="more" href="/scenes/">全部场景 →</a> <a class="more" href="/calc/">月度成本算账与主备选站 →</a></h2>
  <div class="cards scene-cards">{scene_cards}</div>
</section>
<section>
  <h2>中转站分层信誉排行 Top 10 <a class="more" href="/stations/">完整排行 →</a></h2>
  {station_rank_table(stations, {}, site.now, limit=10, compact=True)}
</section>
<section>
  <h2>热门模型 <small>常规按量推荐 + 常规最低价（每百万 token · 人民币）</small></h2>
  <div class="cards">{''.join(cards)}</div>
</section>
<section class="two">
  <div>
    <h2>最新调价 <a class="more" href="/changes/">全部 →</a></h2>
    {('<ul class="chg">' + chg + '</ul>') if chg else '<p class="muted">已建立当前版本快照，下一次刷新后显示调价记录。</p>'}
  </div>
  <div>
    <h2>怎么避开低价坑站</h2>
    <ul class="tips">
      <li><b>未实测不发 A/S 牌</b>：仅有公开数据的站点封顶 Stage 1（B 级 74 分），真金白银探针连续达标才解锁 A/S。</li>
      <li><b>常规区隔离逆向/特惠组</b>：自称 Kiro/Cursor/逆向/特惠及未核实极端低价不进常规 Top 3，展开行内分组可看全貌。</li>
      <li><b>按真实协议与缓存算账</b>：一键筛选 Anthropic / Responses 协议，切换 7:2:1 编程缓存成本。</li>
    </ul>
    <p><a href="/about/">完整评分方法与利益披露 →</a></p>
  </div>
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
    write(DIST / "robots.txt", f"User-agent: *\nAllow: /\nDisallow: /data/\n\nSitemap: {site.url('/sitemap.xml')}\n")
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
        f"- [LLM 模型真伪鉴别与协议自测工具]({site.url('/verify/')})：支持在线无状态边缘接口（POST /api/verify）与本机脚本，校验 Thinking 签名、Tool-Calling 与两轮缓存命中",
        f"- [中转站分层评分排行]({site.url('/stations/')})",
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
