"""Normalize New API style `/api/pricing` + `/api/status` into comparable quotes.

Pricing semantics (New API / One API family):
- quota unit is "USD quota"; `price` in /api/status = CNY paid per 1 USD quota (top-up price).
- quota_type 0 (token billing): input USD/1M = model_ratio * 2; output = input * completion_ratio;
  cache read = input * cache_ratio.
- quota_type 1 (per call): model_price USD per call.
- billing_expr (tiered expression) overrides the ratio fields: `tier("name", p * IN + c * OUT + cr * CACHE)`
  coefficients are USD quota per 1M tokens; `fixed(N)` means N USD per call.
- Final = base * group_ratio[group] * price (CNY).

V1.0 multi-group & self-admitted caveat extraction:
- Never auto-certifies marketing claims ("满血/稳定/官转") as official/reliable.
- Extracts only verifiable structural attributes (endpoints, cache pricing, time-of-day tiers)
  and explicit self-admitted caveat cues (Kiro/Cursor/reverse/promo/test).
- Emits one representative Quote per lane present ("standard" | "promo" | "welfare"),
  while attaching every selectable group in `all_groups` so buyers can inspect full group spreads.

Everything here is pure (no I/O) so it can be unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

ALGO_VERSION = 7  # bump when normalization changes, so the change log never reports algorithm changes as price moves
PLACEHOLDER_RATIO = 37.5  # New API default ratio for unconfigured models -> no real price
PRICE_MIN_PLAUSIBLE = 0.05  # CNY per USD quota; below this is almost certainly not a top-up price
PRICE_MAX_PLAUSIBLE = 20.0

_TIER_START_RE = re.compile(r'tier\(\s*"([^"]*)"\s*,')
_TERM_RE = re.compile(r'\b([a-z][a-z0-9_]*)\s*\*\s*([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)')
_FIXED_RE = re.compile(r'fixed\(\s*([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\s*\)')
_TIME_RE = re.compile(r'\b(?:hour|weekday|month|day|minute)\(')
NOUSE_RE = re.compile(r"(?:禁止使用|禁用|勿用|不要用|请勿使用|停用|已下线|废弃|内部使用|仅限内部)")
WELFARE_RE = re.compile(r"(?:公益|福利|免费|白嫖|体验|试用|测试|(?<![a-z])(?:free|test)(?![a-z]))", re.I)

# Strictly self-admitted caveat tags (never auto-certifies "official" / "high stability").
CAVEAT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:kiro|krio)", re.I), "自称Kiro"),
    (re.compile(r"cursor", re.I), "自称Cursor"),
    (re.compile(r"(?:逆向|反代|反重力|antigravity)", re.I), "自称逆向/反代"),
    (re.compile(r"(?:特惠|特价|特供|限时|促销|超低价|低价|跑量|混池|活动|秒杀|(?<![a-z])promo(?![a-z]))", re.I), "特惠/限时分组"),
]

ENDPOINT_LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI Chat",
    "openai-response": "OpenAI Responses",
    "gemini": "Gemini",
    "image": "生图",
    "video": "视频",
    "embeddings": "Embedding",
}


def extract_caveats(group: str | None, desc: str | None) -> list[str]:
    """Return objective self-admitted caveat tags present in group name or station description."""
    txt = f"{group or ''} {desc or ''}".strip()
    if not txt:
        return []
    out: list[str] = []
    for rx, label in CAVEAT_RULES:
        if rx.search(txt) and label not in out:
            out.append(label)
    return out


def endpoint_tags(endpoints: list[str] | None) -> list[str]:
    """Map raw New API `supported_endpoint_types` strings to canonical filter keys."""
    if not endpoints:
        return []
    tags: list[str] = []

    def add(k: str) -> None:
        if k not in tags:
            tags.append(k)

    for raw in endpoints:
        s = str(raw or "").strip().lower()
        if not s:
            continue
        if "anthropic" in s:
            add("anthropic")
        elif "response" in s:
            add("openai-response")
        elif "gemini" in s:
            add("gemini")
        elif "image" in s or "midjourney" in s or "dall" in s:
            add("image")
        elif "video" in s:
            add("video")
        elif "embed" in s:
            add("embeddings")
        elif "openai" in s:
            add("openai")
    return tags


@dataclass
class ExprQuote:
    kind: str  # "token" | "call" | "unknown"
    input_usd: float | None = None
    output_usd: float | None = None
    cache_usd: float | None = None
    call_usd: float | None = None
    tier_name: str | None = None
    tiers: int = 0
    time_based: bool = False
    max_input_usd: float | None = None  # most expensive token tier (e.g. peak hours)
    max_output_usd: float | None = None
    conditional: bool = False


def _tier_bodies(expr: str) -> list[tuple[str, str]]:
    """Return [(tier_name, body)] for every tier("name", body) call, with paren matching."""
    out: list[tuple[str, str]] = []
    for m in _TIER_START_RE.finditer(expr):
        i = m.end()
        depth = 1
        j = i
        while j < len(expr) and depth:
            ch = expr[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            j += 1
        out.append((m.group(1), expr[i : j - 1]))
    return out


def parse_billing_expr(expr: str | None) -> ExprQuote:
    if not expr or not isinstance(expr, str):
        return ExprQuote(kind="unknown")
    e = re.sub(r"^v\d+:", "", expr.strip())
    bodies = _tier_bodies(e)
    time_based = bool(_TIME_RE.search(e))
    if not bodies:
        return ExprQuote(kind="unknown", time_based=time_based)
    token_cands: list[ExprQuote] = []
    call_cands: list[ExprQuote] = []
    for name, body in bodies:
        fm = _FIXED_RE.search(body)
        terms = {k: float(v) for k, v in _TERM_RE.findall(body)}
        if "p" in terms or "c" in terms:
            token_cands.append(
                ExprQuote(
                    kind="token",
                    input_usd=terms.get("p"),
                    output_usd=terms.get("c"),
                    cache_usd=terms.get("cr"),
                    tier_name=name,
                )
            )
        elif fm:
            call_cands.append(ExprQuote(kind="call", call_usd=float(fm.group(1)), tier_name=name))
    conditional = len(bodies) > 1 or "?" in e
    if token_cands:
        # cheapest tier = short-context / standard / off-priority branch (conservative headline)
        best = min(token_cands, key=lambda q: ((q.input_usd if q.input_usd is not None else 9e9), (q.output_usd or 0)))
    elif call_cands:
        nonzero = [q for q in call_cands if (q.call_usd or 0) > 0] or call_cands
        best = min(nonzero, key=lambda q: q.call_usd or 0)
    else:
        return ExprQuote(kind="unknown", tiers=len(bodies), time_based=time_based, conditional=conditional)
    best.tiers = len(bodies)
    best.time_based = time_based
    if token_cands and len(token_cands) > 1:
        worst = max(token_cands, key=lambda q: ((q.input_usd or 0), (q.output_usd or 0)))
        best.max_input_usd, best.max_output_usd = worst.input_usd, worst.output_usd
    best.conditional = conditional
    return best


@dataclass
class Quote:
    domain: str
    model: str  # raw model name on that station
    canonical: str
    family: str
    kind: str  # token | call
    group: str | None
    group_ratio: float | None
    group_desc: str | None
    input_usd: float | None = None
    output_usd: float | None = None
    cache_usd: float | None = None
    call_usd: float | None = None
    input_cny: float | None = None
    output_cny: float | None = None
    cache_cny: float | None = None
    call_cny: float | None = None
    free: bool = False
    welfare: bool = False  # 公益/福利/免费/体验/测试分组：不进常规推荐
    anomaly: bool = False  # set by build: far below the model median
    low_outlier: bool = False  # set by build: <0.25x median unprobed, vetoed from Top 3
    median_ratio: float | None = None  # set by build: price / model median
    price_score: float | None = None  # set by build (scoring.price_score)
    score: float | None = None  # set by build (scoring.quote_score, stage-capped)
    raw_score: float | None = None  # set by build (uncapped composite tie-breaker)
    lane: str = "standard"  # "standard" | "promo" | "welfare"
    caveats: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)
    all_groups: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PREFIX_TAG_RE = re.compile(r"^\s*(?:\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）)\s*")
_SUFFIX_TAG_RE = re.compile(r"\s*(?:\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）)\s*$")


def canonical_model(name: str) -> str:
    n = name.strip()
    for _ in range(3):
        n2 = _PREFIX_TAG_RE.sub("", n)
        n2 = _SUFFIX_TAG_RE.sub("", n2)
        if n2 == n:
            break
        n = n2
    n = n.lower()
    if "/" in n:
        n = n.rsplit("/", 1)[-1]
    n = re.sub(r"\s+", "-", n)
    return n or name.strip().lower()


def slugify(canon: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", canon.lower()).strip("-.")
    return s or "model"


def classify_family(name: str) -> str:
    n = name.lower()
    table = [
        ("deepseek", ("deepseek",)),
        ("claude", ("claude", "fable")),
        ("gpt", ("gpt", "o1", "o3", "o4", "codex", "chatgpt", "dall-e", "sora")),
        ("gemini", ("gemini", "imagen", "veo", "nano-banana")),
        ("grok", ("grok",)),
        ("qwen", ("qwen", "qwq")),
        ("glm", ("glm", "zhipu", "cogview")),
        ("kimi", ("kimi", "moonshot")),
        ("doubao", ("doubao", "seedance", "seedream")),
        ("minimax", ("minimax", "abab", "hailuo")),
    ]
    for fam, keys in table:
        if any(k in n for k in keys):
            return fam
    return "other"


FAMILY_LABELS = {
    "deepseek": "DeepSeek",
    "claude": "Claude",
    "gpt": "GPT / OpenAI",
    "gemini": "Gemini",
    "grok": "Grok",
    "qwen": "通义千问 Qwen",
    "glm": "智谱 GLM",
    "kimi": "Kimi",
    "doubao": "豆包 / Seed",
    "minimax": "MiniMax",
    "other": "其他模型",
}


def topup_price(status: dict[str, Any] | None) -> tuple[float | None, str]:
    """Return (CNY per USD quota, note)."""
    if not isinstance(status, dict):
        return None, "未获取站点状态"
    p = status.get("price")
    if isinstance(p, str):
        try:
            p = float(p.strip())
        except ValueError:
            p = None
    if not isinstance(p, (int, float)):
        return None, "站点未公开充值比例"
    p = float(p)
    if p < PRICE_MIN_PLAUSIBLE or p > PRICE_MAX_PLAUSIBLE:
        return None, f"充值比例异常（{p:g}），不换算"
    return p, f"¥{p:g} = $1 额度"


def _is_welfare(group: str | None, _desc: str | None = None) -> bool:
    """Match welfare/free/test keywords ONLY against the group name (never free-form marketing copy in desc)."""
    return bool(WELFARE_RE.search(group or ""))


def _precompute_group_meta(group_ratio: dict[str, Any], usable_group: dict[str, Any]) -> dict[str, tuple[str | None, bool, bool, list[str]]]:
    """Precompute (desc, nouse, welfare, caveats) once per unique group per station."""
    out: dict[str, tuple[str | None, bool, bool, list[str]]] = {}
    keys = set(group_ratio.keys()) | set(usable_group.keys())
    for g in keys:
        d = str(usable_group.get(g)) if usable_group.get(g) else None
        nouse = bool(NOUSE_RE.search(f"{g} {d or ''}"))
        welf = _is_welfare(g)
        cav = extract_caveats(g, d)
        out[g] = (d, nouse, welf, cav)
    return out


def _collect_ratio_groups(
    enable_groups: list[str],
    group_ratio: dict[str, Any],
    usable_group: dict[str, Any],
    gmeta: dict[str, tuple[str | None, bool, bool, list[str]]],
    base_in: float | None,
    base_out: float | None,
    base_cache: float | None,
    base_call: float | None,
    kind: str,
    model_welfare: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Collect every selectable group with effective USD prices, lane, and caveats.

    Returns (candidates, ratio_known).
    """
    cands: list[tuple[str, float]] = []
    for g in enable_groups or []:
        r = group_ratio.get(g)
        if not isinstance(r, (int, float)):
            continue
        if usable_group and g not in usable_group:
            continue  # not user-selectable
        gm = gmeta.get(g)
        if gm and gm[1]:
            continue  # station itself says do not use
        cands.append((g, float(r)))
    if not cands and enable_groups:
        cands = [(g, float(group_ratio[g])) for g in enable_groups if isinstance(group_ratio.get(g), (int, float))]

    def get_gm(g: str | None) -> tuple[str | None, bool, bool, list[str]]:
        if not g:
            return None, False, False, []
        gm = gmeta.get(g)
        if gm is None:
            d = str(usable_group.get(g)) if usable_group.get(g) else None
            gm = (d, bool(NOUSE_RE.search(f"{g} {d or ''}")), _is_welfare(g), extract_caveats(g, d))
            gmeta[g] = gm
        return gm

    if not cands:
        g0 = enable_groups[0] if enable_groups else None
        d0, _, w0, cav = get_gm(g0)
        free = all((v or 0) == 0 for v in (base_in, base_out, base_cache, base_call))
        welf = model_welfare or w0
        lane = "welfare" if (free or welf) else ("promo" if cav else "standard")
        return [{
            "kind": kind, "group": g0, "ratio": None, "eff_ratio": 1.0, "desc": d0,
            "in": base_in, "out": base_out, "cache": base_cache, "call": base_call,
            "free": free, "welfare": welf, "caveats": cav, "lane": lane,
        }], False

    out: list[dict[str, Any]] = []
    for g, r in cands:
        d, _, wg, cav = get_gm(g)
        in_u = None if base_in is None else base_in * r
        out_u = None if base_out is None else base_out * r
        cache_u = None if base_cache is None else base_cache * r
        call_u = None if base_call is None else base_call * r
        free = (r == 0) or all((v or 0) == 0 for v in (in_u, out_u, cache_u, call_u))
        welf = model_welfare or wg or r == 0
        lane = "welfare" if (free or welf) else ("promo" if cav else "standard")
        out.append({
            "kind": kind, "group": g, "ratio": r, "eff_ratio": r, "desc": d,
            "in": in_u, "out": out_u, "cache": cache_u, "call": call_u,
            "free": free, "welfare": welf, "caveats": cav, "lane": lane,
        })
    return out, True


def _collect_group_price(
    gp: dict[str, Any], qt: Any, completion_ratio: Any, usable_group: dict[str, Any],
    gmeta: dict[str, tuple[str | None, bool, bool, list[str]]], model_welfare: bool
) -> list[dict[str, Any]]:
    """Fork format: model["group_price"] = {group: {type, price, group_ratio, cache_hits_ratio, ...}}."""
    want = 1 if qt == 1 else 0
    cr = float(completion_ratio) if isinstance(completion_ratio, (int, float)) else 1.0
    cands: list[dict[str, Any]] = []
    for g, e in gp.items():
        if not isinstance(e, dict) or not isinstance(e.get("price"), (int, float)):
            continue
        t = e.get("type")
        t = int(t) if isinstance(t, (int, float)) or (isinstance(t, str) and t.isdigit()) else want
        if t != want:
            continue
        if usable_group and g not in usable_group:
            continue
        gm = gmeta.get(g)
        if gm is None:
            desc = str(usable_group.get(g)) if usable_group.get(g) else None
            gm = (desc, bool(NOUSE_RE.search(f"{g} {desc or ''}")), _is_welfare(g), extract_caveats(g, desc))
            gmeta[g] = gm
        desc, nouse, wg, cav = gm
        if nouse:
            continue
        r = e.get("group_ratio")
        r = float(r) if isinstance(r, (int, float)) else 1.0
        ur = e.get("user_ratio")
        r *= float(ur) if isinstance(ur, (int, float)) and ur > 0 else 1.0
        price = float(e["price"])
        if t == 0:
            if price == PLACEHOLDER_RATIO:
                continue
            base_in = price * 2.0
            hit = e.get("cache_hits_ratio")
            cr_e = float(e["completion_ratio"]) if isinstance(e.get("completion_ratio"), (int, float)) else cr
            in_u, out_u = base_in * r, base_in * cr_e * r
            cache_u = (base_in * float(hit) * r) if isinstance(hit, (int, float)) and 0 < hit < 1 else None
            call_u, kind = None, "token"
        else:
            in_u = out_u = cache_u = None
            call_u, kind = price * r, "call"
        free = (r == 0) or all((v or 0) == 0 for v in (in_u, out_u, cache_u, call_u))
        welf = model_welfare or wg or r == 0
        lane = "welfare" if (free or welf) else ("promo" if cav else "standard")
        cands.append({
            "kind": kind, "group": g, "ratio": r, "eff_ratio": r, "desc": desc,
            "in": in_u, "out": out_u, "cache": cache_u, "call": call_u,
            "free": free, "welfare": welf, "caveats": cav, "lane": lane,
        })
    return cands


def _fmt(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:.6f}".rstrip("0").rstrip(".") if x < 1 else f"{x:.2f}".rstrip("0").rstrip(".")


def _r(x: float | None, nd: int = 6) -> float | None:
    return None if x is None else round(x, nd)


def normalize_station(domain: str, pricing: dict[str, Any], status: dict[str, Any] | None) -> tuple[list[Quote], dict[str, Any]]:
    """Return (quotes, station_meta). Unpriced/placeholder models are dropped.

    Emits one representative Quote per lane present ("standard", "promo", "welfare") for each model,
    ordered standard -> promo -> welfare, with every selectable group attached in `all_groups`.
    """
    data = pricing.get("data") if isinstance(pricing, dict) else None
    meta: dict[str, Any] = {"domain": domain, "parsed": 0, "dropped_placeholder": 0, "dropped_unknown": 0}
    if not isinstance(data, list):
        meta["error"] = "pricing 非 New API 列表格式"
        return [], meta
    group_ratio = pricing.get("group_ratio") if isinstance(pricing.get("group_ratio"), dict) else {}
    usable_group = pricing.get("usable_group") if isinstance(pricing.get("usable_group"), dict) else {}
    gmeta = _precompute_group_meta(group_ratio, usable_group)
    price, price_note = topup_price(status)
    meta.update(
        {
            "topup_price": price,
            "topup_note": price_note,
            "groups": [
                {
                    "name": g,
                    "ratio": r,
                    "desc": gmeta[g][0],
                    "selectable": (not usable_group) or g in usable_group,
                    "caveats": gmeta[g][3],
                    "welfare": gmeta[g][2] or r == 0,
                }
                for g, r in sorted(group_ratio.items(), key=lambda kv: (kv[1] if isinstance(kv[1], (int, float)) else 9e9))
                if isinstance(r, (int, float))
            ],
            "system_name": (status or {}).get("system_name") if isinstance(status, dict) else None,
            "version": (status or {}).get("version") if isinstance(status, dict) else None,
            "linuxdo_oauth": bool((status or {}).get("linuxdo_oauth")) if isinstance(status, dict) else False,
            "github_oauth": bool((status or {}).get("github_oauth")) if isinstance(status, dict) else False,
            "telegram_oauth": bool((status or {}).get("telegram_oauth")) if isinstance(status, dict) else False,
            "total_models": len(data),
        }
    )

    def cny(v: float | None) -> float | None:
        return None if (v is None or price is None) else v * price

    quotes: list[Quote] = []
    parsed_models = 0
    station_endpoints: set[str] = set()
    for m in data:
        if not isinstance(m, dict):
            continue
        name = str(m.get("model_name") or "").strip()
        if not name:
            continue
        qt = m.get("quota_type")
        if isinstance(qt, str) and qt.strip().lstrip("-").isdigit():
            qt = int(qt)
        mr = m.get("model_ratio")
        cr = m.get("completion_ratio")
        cache_r = m.get("cache_ratio")
        mp = m.get("model_price")
        expr = m.get("billing_expr")
        base_flags: list[str] = []
        kind = None
        peak: tuple[float | None, float | None] | None = None
        in_u = out_u = cache_u = call_u = None
        if expr:
            eq = parse_billing_expr(expr)
            if eq.kind == "token":
                kind, in_u, out_u, cache_u = "token", eq.input_usd, eq.output_usd, eq.cache_usd
            elif eq.kind == "call":
                kind, call_u = "call", eq.call_usd
            if eq.time_based:
                base_flags.append("分时计价")
                if eq.kind == "token" and eq.max_input_usd is not None and eq.max_input_usd != eq.input_usd:
                    peak = (eq.max_input_usd, eq.max_output_usd)
            if eq.conditional:
                base_flags.append("多档计价·取最低档")
        gp = m.get("group_price") if isinstance(m.get("group_price"), dict) else None
        model_welfare = bool(WELFARE_RE.search(name))
        cands: list[dict[str, Any]] = []
        ratio_known = True
        used_gp = False
        if kind is None and gp and not isinstance(mr, (int, float)):
            cands = _collect_group_price(gp, qt, m.get("completion_ratio"), usable_group, gmeta, model_welfare)
            if not cands:
                meta["dropped_unknown"] += 1
                continue
            kind = cands[0]["kind"]
            used_gp = True
        if not used_gp:
            if kind is None:
                if qt == 1:
                    if isinstance(mp, (int, float)):
                        kind, call_u = "call", float(mp)
                elif isinstance(mr, (int, float)):
                    if float(mr) == PLACEHOLDER_RATIO:
                        meta["dropped_placeholder"] += 1
                        continue
                    kind = "token"
                    in_u = float(mr) * 2.0
                    out_u = in_u * (float(cr) if isinstance(cr, (int, float)) else 1.0)
                    if isinstance(cache_r, (int, float)):
                        cache_u = in_u * float(cache_r)
            if kind is None:
                meta["dropped_unknown"] += 1
                continue
            eg = m.get("enable_groups") if isinstance(m.get("enable_groups"), list) else []
            eg = [str(g) for g in eg]
            cands, ratio_known = _collect_ratio_groups(
                eg, group_ratio, usable_group, gmeta, in_u, out_u, cache_u, call_u, kind, model_welfare
            )

        parsed_models += 1
        raw_eps = [str(x) for x in (m.get("supported_endpoint_types") or []) if isinstance(x, str)]
        ep_tags = endpoint_tags(raw_eps)
        station_endpoints.update(ep_tags)

        def cand_sort_key(c: dict[str, Any]) -> tuple[float, str]:
            val = c["in"] if c["kind"] == "token" else c["call"]
            return (val if val is not None else 9e9, str(c["group"] or ""))

        cands.sort(key=cand_sort_key)
        all_groups = [
            {
                "group": c["group"],
                "ratio": c["ratio"],
                "desc": (c["desc"][:140] if c["desc"] else None),
                "lane": c["lane"],
                "caveats": list(c["caveats"]),
                "in_cny": _r(cny(c["in"])),
                "out_cny": _r(cny(c["out"])),
                "cache_cny": _r(cny(c["cache"])),
                "call_cny": _r(cny(c["call"])),
                "in_usd": _r(c["in"]),
                "out_usd": _r(c["out"]),
                "call_usd": _r(c["call"]),
            }
            for c in cands
        ]
        by_lane: dict[str, list[dict[str, Any]]] = {"standard": [], "promo": [], "welfare": []}
        for c in cands:
            by_lane[c["lane"]].append(c)
        has_alt_welfare = bool(by_lane["welfare"])
        canon = canonical_model(name)
        fam = classify_family(canon)

        for lane in ("standard", "promo", "welfare"):
            lane_cands = by_lane[lane]
            if not lane_cands:
                continue
            # Do not emit a redundant welfare row when the model already has standard/promo paid quotes
            # (keeps welfare accessible in `all_groups` without doubling noise on station pages;
            # emit welfare row when the station ONLY offers welfare/free for this model).
            if lane == "welfare" and (by_lane["standard"] or by_lane["promo"]):
                continue
            pick = lane_cands[0]
            flags = list(base_flags)
            if not ratio_known:
                flags.append("分组倍率未公开·按1倍")
            if pick["welfare"] and not pick["free"]:
                flags.append("公益/福利/体验/测试分组")
            if has_alt_welfare and lane != "welfare":
                flags.append("另有免费或福利分组")
            if peak is not None and not used_gp:
                gr_eff = pick["eff_ratio"]
                pi = None if peak[0] is None else peak[0] * gr_eff
                po = None if peak[1] is None else peak[1] * gr_eff
                if price is not None:
                    flags.append(f"高峰档 ¥{_fmt(cny(pi))} / ¥{_fmt(cny(po))}")
                else:
                    flags.append(f"高峰档 ${_fmt(pi)} / ${_fmt(po)}")
            q = Quote(
                domain=domain,
                model=name,
                canonical=canon,
                family=fam,
                kind=pick["kind"],
                group=pick["group"],
                group_ratio=pick["ratio"],
                group_desc=pick["desc"],
                input_usd=_r(pick["in"]),
                output_usd=_r(pick["out"]),
                cache_usd=_r(pick["cache"]),
                call_usd=_r(pick["call"]),
                input_cny=_r(cny(pick["in"])),
                output_cny=_r(cny(pick["out"])),
                cache_cny=_r(cny(pick["cache"])),
                call_cny=_r(cny(pick["call"])),
                free=pick["free"],
                welfare=pick["welfare"],
                lane=lane,
                caveats=list(pick["caveats"]),
                flags=flags,
                endpoints=raw_eps,
                all_groups=all_groups,
            )
            quotes.append(q)
    meta["parsed"] = parsed_models
    meta["endpoints"] = sorted(station_endpoints)
    return quotes, meta
