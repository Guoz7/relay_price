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

V1.1 canonical fixed-field schema (no change to input/output math, so ALGO_VERSION stays put):
- Raw ratio provenance on every Quote (model / completion / cache / cache-write ratio, billing mode & expr).
- Strict three-valued cache semantics (`cache_status`): "priced" | "free" (explicit ×0) | "unknown"
  (undisclosed or Go-struct-zero default) | "na" (per-call, media-only, or the station says no cache).
- Cache write unified from `create_cache_ratio` / `cache_creation_ratio` / `cache_creation_12h_ratio`
  and from `cc` / `cw` terms inside `billing_expr`.
- Placeholder / template / loopback URLs in /api/status are dropped by `sanitize_station_url`.

Everything here is pure (no I/O) so it can be unit-tested.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field, asdict
from typing import Any
from urllib.parse import urlsplit

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
    (re.compile(r"(?:逆向|反代|反重力|antigravity|reverse)", re.I), "自称逆向/反代"),
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

MEDIA_ENDPOINTS = {"image", "video", "embeddings"}
CACHE_WRITE_KEYS = (
    "create_cache_ratio", "cache_creation_ratio", "cache_write_ratio",
    "cache_creation_ratio_5m", "cache_creation_5m_ratio", "create_cache_ratio_5m", "cache_creation_12h_ratio",
)
CACHE_STATUS_LABELS = {"priced": "已公开", "free": "免费 · ×0", "unknown": "未公开", "na": "不适用"}
TAG_MAX, TAG_LEN = 6, 16
EXPR_MAX = 600

# ---------------------------------------------------------------- dirty URL sanitizer
_BLOCK_HOSTS = {"localhost", "0.0.0.0", "127.0.0.1", "::1", "example.com", "example.org", "example.net", "your-domain.com", "yourdomain.com"}
_TEMPLATE_HOST_ROOTS = ("newapi.pro",)  # New API template docs: newapi.pro, docs.newapi.pro
_TEMPLATE_REPO_PREFIXES = ("github.com/quantumnous", "github.com/songquanpeng")
_BARE_HOST_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?(?:[/?#]|$)", re.I)


def _root_domain(host: str) -> str:
    parts = [p for p in (host or "").lower().strip(".").split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else ".".join(parts)


def sanitize_station_url(url: Any, station_domain: str | None = None) -> str | None:
    """Return a clean absolute http(s) URL, or None for placeholders, loopback/private hosts and upstream templates.

    Relative paths ("/docs") are resolved against the station domain. Upstream template defaults
    (newapi.pro docs, QuantumNous / songquanpeng repos) are dropped unless the station itself lives on that root.
    """
    if not isinstance(url, str):
        return None
    u = url.strip()
    if not u or any(ch in u for ch in ' \t\n"<>{}'):
        return None
    own = (station_domain or "").strip().lower().strip(".")
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("/"):
        if not own:
            return None
        u = f"https://{own}{u}"
    elif "://" not in u:
        if not _BARE_HOST_RE.match(u):
            return None
        u = "https://" + u
    try:
        parts = urlsplit(u)
        host = (parts.hostname or "").lower().strip(".")
        _ = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    bare = host[4:] if host.startswith("www.") else host
    if bare in _BLOCK_HOSTS or bare.endswith((".localhost", ".local", ".example.com", ".internal")) or "." not in bare:
        return None
    try:
        ip = ipaddress.ip_address(bare)
        if ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_reserved:
            return None
    except ValueError:
        pass
    own_root = _root_domain(own)
    for root in _TEMPLATE_HOST_ROOTS:
        if (bare == root or bare.endswith("." + root)) and own_root != root:
            return None
    path = (bare + (parts.path or "")).lower()
    if any(path.startswith(pre) for pre in _TEMPLATE_REPO_PREFIXES) and own_root != "github.com":
        return None
    return u[:240]


def clean_tags(raw: Any) -> list[str]:
    """Station-defined model tags (comma string or list) -> short de-duplicated list."""
    if isinstance(raw, str):
        items = re.split(r"[,，;；|]", raw)
    elif isinstance(raw, list):
        items = [x for x in raw if isinstance(x, str)]
    else:
        return []
    out: list[str] = []
    for t in items:
        t = re.sub(r"\s+", " ", t).strip()
        if t and len(t) <= TAG_LEN and t not in out:
            out.append(t)
        if len(out) >= TAG_MAX:
            break
    return out


def compact_expr(expr: Any) -> str | None:
    if not isinstance(expr, str) or not expr.strip():
        return None
    return re.sub(r"\s+", " ", expr.strip())[:EXPR_MAX]


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def cache_write_ratio_of(m: dict[str, Any]) -> float | None:
    """First positive cache-write ratio among the known New API / fork field names."""
    for k in CACHE_WRITE_KEYS:
        v = _num(m.get(k))
        if v is not None and v > 0:
            return v
    return None


def cache_zero_kind(m: dict[str, Any], keyed_share: float, station_has_positive: bool) -> str:
    """Classify an explicit `cache_ratio == 0` (Experiment A).

    - "na":     the station says this model has no cache read (`supports_cache_read: false`) or it is per-call.
    - "struct": Go-struct-zero default (companion cache-write field also exactly 0, the field is emitted on
                ~every model by a non-omitempty fork, or the station never discloses any positive cache ratio).
    - "free":   a deliberate ×0 on an omitempty-style station that does price cache reads elsewhere.
    """
    if m.get("supports_cache_read") is False:
        return "na"
    qt = m.get("quota_type")
    if qt in (1, "1"):
        return "na"
    for k in CACHE_WRITE_KEYS:
        if k in m and _num(m.get(k)) == 0:
            return "struct"
    if keyed_share >= 0.95 or not station_has_positive:
        return "struct"
    return "free"


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
    cache_write_usd: float | None = None  # `cc` / `cw` (cache creation) coefficient of the chosen tier
    has_cache_term: bool = False  # tier declares a `cr` term (so 0 means an explicit free cache read)
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
            cw = next((terms[k] for k in ("cc", "cw", "cc5m", "cc1h") if k in terms), None)
            token_cands.append(
                ExprQuote(
                    kind="token",
                    input_usd=terms.get("p"),
                    output_usd=terms.get("c"),
                    cache_usd=terms.get("cr"),
                    cache_write_usd=cw,
                    has_cache_term="cr" in terms,
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
    # ---- V1.1 fixed billing fields (raw provenance; never alters input/output math)
    model_ratio: float | None = None  # raw model_ratio (or group_price base price); None for expr / per-call
    completion_ratio: float | None = None  # raw completion_ratio; None = not set (New API bills output ×1)
    cache_ratio: float | None = None  # sanitized: None when Go-struct-zero default / undisclosed
    cache_write_ratio: float | None = None  # unified create_cache_ratio / cache_creation_ratio / *_12h
    cache_write_usd: float | None = None
    cache_write_cny: float | None = None
    model_price: float | None = None  # raw per-call USD price
    billing_mode: str | None = None
    billing_expr: str | None = None  # whitespace-collapsed raw expression (capped)
    tags: list[str] = field(default_factory=list)
    cache_status: str = "unknown"  # "priced" | "free" | "unknown" | "na"

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
    base_cw: float | None = None,
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
            "in": base_in, "out": base_out, "cache": base_cache, "call": base_call, "cw": base_cw,
            "free": free, "welfare": welf, "caveats": cav, "lane": lane,
        }], False

    out: list[dict[str, Any]] = []
    for g, r in cands:
        d, _, wg, cav = get_gm(g)
        in_u = None if base_in is None else base_in * r
        out_u = None if base_out is None else base_out * r
        cache_u = None if base_cache is None else base_cache * r
        call_u = None if base_call is None else base_call * r
        cw_u = None if base_cw is None else base_cw * r
        free = (r == 0) or all((v or 0) == 0 for v in (in_u, out_u, cache_u, call_u))
        welf = model_welfare or wg or r == 0
        lane = "welfare" if (free or welf) else ("promo" if cav else "standard")
        out.append({
            "kind": kind, "group": g, "ratio": r, "eff_ratio": r, "desc": d,
            "in": in_u, "out": out_u, "cache": cache_u, "call": call_u, "cw": cw_u,
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
            hit_ok = isinstance(hit, (int, float)) and 0 < hit < 1
            cache_u = (base_in * float(hit) * r) if hit_ok else None
            cwr = cache_write_ratio_of(e)
            cw_u = base_in * cwr * r if cwr else None
            call_u, kind = None, "token"
            prov = {"mr": price, "crr": cr_e if isinstance(e.get("completion_ratio"), (int, float)) or isinstance(completion_ratio, (int, float)) else None,
                    "cache_r": float(hit) if hit_ok else None, "cwr": cwr, "mp": None}
        else:
            in_u = out_u = cache_u = cw_u = None
            call_u, kind = price * r, "call"
            prov = {"mr": None, "crr": None, "cache_r": None, "cwr": None, "mp": price}
        free = (r == 0) or all((v or 0) == 0 for v in (in_u, out_u, cache_u, call_u))
        welf = model_welfare or wg or r == 0
        lane = "welfare" if (free or welf) else ("promo" if cav else "standard")
        cands.append({
            "kind": kind, "group": g, "ratio": r, "eff_ratio": r, "desc": desc,
            "in": in_u, "out": out_u, "cache": cache_u, "call": call_u, "cw": cw_u,
            "free": free, "welfare": welf, "caveats": cav, "lane": lane, **prov,
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
    meta.update(station_billing_meta(domain, status, price))

    def cny(v: float | None) -> float | None:
        return None if (v is None or price is None) else v * price

    quotes: list[Quote] = []
    parsed_models = 0
    station_endpoints: set[str] = set()
    dict_models = [m for m in data if isinstance(m, dict)]
    keyed_share = (sum(1 for m in dict_models if "cache_ratio" in m) / len(dict_models)) if dict_models else 0.0
    has_pos_cache = any((_num(m.get("cache_ratio")) or 0) > 0 for m in dict_models)
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
        in_u = out_u = cache_u = call_u = cw_u = None
        used_expr = False
        cache_na = m.get("supports_cache_read") is False
        prov: dict[str, Any] = {"mr": None, "crr": None, "cache_r": None, "cwr": None, "mp": None}
        if expr:
            eq = parse_billing_expr(expr)
            if eq.kind == "token":
                kind, in_u, out_u, cache_u = "token", eq.input_usd, eq.output_usd, eq.cache_usd
                if cache_u is not None and cache_u <= 0:
                    cache_u = 0.0 if eq.has_cache_term else None
                cw_u = eq.cache_write_usd if (eq.cache_write_usd or 0) > 0 else None
                used_expr = True
            elif eq.kind == "call":
                kind, call_u = "call", eq.call_usd
                used_expr = True
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
                        prov["mp"] = float(mp)
                elif isinstance(mr, (int, float)):
                    if float(mr) == PLACEHOLDER_RATIO:
                        meta["dropped_placeholder"] += 1
                        continue
                    kind = "token"
                    in_u = float(mr) * 2.0
                    out_u = in_u * (float(cr) if isinstance(cr, (int, float)) else 1.0)
                    prov["mr"] = float(mr)
                    prov["crr"] = float(cr) if isinstance(cr, (int, float)) else None
                    c_num = _num(cache_r)
                    if c_num is not None and c_num > 0:
                        cache_u, prov["cache_r"] = in_u * c_num, c_num
                    elif c_num == 0:
                        zk = cache_zero_kind(m, keyed_share, has_pos_cache)
                        if zk == "free":
                            cache_u, prov["cache_r"] = 0.0, 0.0
                        elif zk == "na":
                            cache_na = True
                    cwr = cache_write_ratio_of(m)
                    if cwr:
                        cw_u, prov["cwr"] = in_u * cwr, cwr
            if kind is None:
                meta["dropped_unknown"] += 1
                continue
            eg = m.get("enable_groups") if isinstance(m.get("enable_groups"), list) else []
            eg = [str(g) for g in eg]
            cands, ratio_known = _collect_ratio_groups(
                eg, group_ratio, usable_group, gmeta, in_u, out_u, cache_u, call_u, kind, model_welfare, base_cw=cw_u
            )

        parsed_models += 1
        raw_eps = [str(x) for x in (m.get("supported_endpoint_types") or []) if isinstance(x, str)]
        ep_tags = endpoint_tags(raw_eps)
        station_endpoints.update(ep_tags)
        if ep_tags and set(ep_tags) <= MEDIA_ENDPOINTS:
            cache_na = True
        bmode = m.get("billing_mode") if isinstance(m.get("billing_mode"), str) and m.get("billing_mode").strip() else None
        tags = clean_tags(m.get("tags"))
        expr_c = compact_expr(expr) if used_expr else None

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
            pv = pick if used_gp else prov
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
                model_ratio=_r(pv.get("mr"), 10),
                completion_ratio=_r(pv.get("crr"), 10),
                cache_ratio=_r(pv.get("cache_r"), 10),
                cache_write_ratio=_r(pv.get("cwr"), 10),
                cache_write_usd=_r(pick.get("cw")),
                cache_write_cny=_r(cny(pick.get("cw"))),
                model_price=_r(pv.get("mp"), 10),
                billing_mode=bmode[:24] if bmode else ("tiered_expr" if used_expr else None),
                billing_expr=expr_c,
                tags=tags,
                cache_status=cache_status_of(pick["kind"], pick["cache"], cache_na),
            )
            quotes.append(q)
    meta["parsed"] = parsed_models
    meta["endpoints"] = sorted(station_endpoints)
    meta.update(cache_coverage(quotes))
    return quotes, meta


# ---------------------------------------------------------------- station billing semantics
REFERENCE_USD_CNY = 7.25  # fallback when the station's usd_exchange_rate is not a plausible CNY rate (e.g. 1)
FX_PLAUSIBLE = (4.0, 12.0)


def fx_basis(fx: float | None) -> tuple[float, str]:
    if fx is not None and FX_PLAUSIBLE[0] <= fx <= FX_PLAUSIBLE[1]:
        return fx, "station"
    return REFERENCE_USD_CNY, "reference"


def topup_mode(price: float | None, fx: float | None) -> str:
    """Classify the top-up model: discount (¥ per $1 below half the FX rate), fx (within ±15%), custom or unknown.

    `fx` is the station's usd_exchange_rate when it is a plausible CNY rate, else the reference rate,
    so a ¥1 = $1 station that sets usd_exchange_rate=1 is still classified as a discount-quota station.
    """
    if price is None:
        return "unknown"
    fx = fx_basis(fx)[0]
    if price < fx * 0.5:
        return "discount"
    if abs(price - fx) <= fx * 0.15:
        return "fx"
    return "custom"


TOPUP_MODE_LABELS = {"discount": "折价额度", "fx": "按汇率直充", "custom": "自定义比例", "unknown": "未公开"}


def _opt_bool(st: dict[str, Any], key: str) -> bool | None:
    v = st.get(key)
    return bool(v) if isinstance(v, (bool, int)) and key in st else None


def station_billing_meta(domain: str, status: dict[str, Any] | None, price: float | None) -> dict[str, Any]:
    """Sanitized fixed station fields from /api/status (no secrets; dirty URLs dropped)."""
    st = status if isinstance(status, dict) else {}
    fx = _num(st.get("usd_exchange_rate"))
    fx = fx if fx is not None and 0.5 <= fx <= 50 else None
    qpu = _num(st.get("quota_per_unit"))
    qpu = (int(qpu) if float(qpu).is_integer() else qpu) if qpu is not None and qpu > 0 else None
    qdt = st.get("quota_display_type")
    qdt = qdt.strip().upper()[:16] if isinstance(qdt, str) and qdt.strip() else None
    sym = st.get("custom_currency_symbol")
    sym = sym.strip()[:4] if isinstance(sym, str) and sym.strip() else None
    ccr = _num(st.get("custom_currency_exchange_rate"))
    out: dict[str, Any] = {
        "usd_exchange_rate": fx,
        "quota_per_unit": qpu,
        "quota_display_type": qdt,
        "custom_currency_symbol": sym,
        "custom_currency_exchange_rate": ccr if ccr is not None and ccr > 0 else None,
        "topup_mode": topup_mode(price, fx),
        "fx_basis": fx_basis(fx)[1],
        "email_verification": _opt_bool(st, "email_verification"),
        "turnstile_check": _opt_bool(st, "turnstile_check"),
        "docs_link": sanitize_station_url(st.get("docs_link"), domain),
        "top_up_link": sanitize_station_url(st.get("top_up_link"), domain),
        "server_address": sanitize_station_url(st.get("server_address"), domain),
    }
    for k in ("enable_online_topup", "aff_enabled", "wechat_login", "oidc_enabled"):
        b = _opt_bool(st, k)
        if b is not None:
            out[k] = b
    mt = _num(st.get("min_topup"))
    if mt is not None and mt > 0:
        out["min_topup"] = mt
    qn = _num(st.get("quota_for_new_user"))
    if qn is not None and qn > 0 and qpu:
        out["new_user_usd"] = round(qn / float(qpu), 4)
    return out


def cache_status_of(kind: str, cache_val: float | None, na: bool) -> str:
    if kind != "token":
        return "na"
    if cache_val is not None:
        return "priced" if cache_val > 0 else "free"
    return "na" if na else "unknown"


def cache_coverage(quotes: list[Quote]) -> dict[str, int | None]:
    """Share (%) of a station's token models (excluding 不适用) that disclose cache read / cache write prices."""
    by_model: dict[str, Quote] = {}
    for q in quotes:
        if q.kind == "token" and q.model not in by_model:
            by_model[q.model] = q
    elig = [q for q in by_model.values() if q.cache_status != "na"]
    if not elig:
        return {"cache_read_coverage": None, "cache_write_coverage": None, "cache_eligible_models": 0}
    rd = sum(1 for q in elig if q.cache_status in ("priced", "free"))
    wr = sum(1 for q in elig if q.cache_write_usd is not None)
    return {"cache_read_coverage": round(100 * rd / len(elig)), "cache_write_coverage": round(100 * wr / len(elig)),
            "cache_eligible_models": len(elig)}


# ---------------------------------------------------------------- formula provenance
def _g(x: float) -> str:
    return f"{x:.10g}"


def format_formula(q: Quote, topup_price: float | None) -> str:
    """Verifiable derivation of the headline price, e.g. `模型×1.5 × 2 × 分组×0.4 × 充值¥1 = ¥1.2`.

    The first segment always multiplies out (within 1e-5) to `input_cny` (token) or `call_cny` (per call).
    """
    gr = q.group_ratio if q.group_ratio is not None else 1.0
    g_txt = f"分组×{_g(gr)}" + ("" if q.group_ratio is not None else "（未公开·按1）")

    def tail(v: float, per: str = "") -> str:
        if topup_price is None:
            return f" = ${_g(round(v, 6))}{per}（充值比例未公开）"
        return f" × 充值¥{_g(topup_price)} = ¥{_g(round(v * topup_price, 6))}{per}"

    extras: list[str] = []
    if q.kind == "token" and q.model_ratio is not None:
        head = f"模型×{_g(q.model_ratio)} × 2 × {g_txt}" + tail(q.model_ratio * 2 * gr)
        extras.append(f"输出 = 输入 × 补全×{_g(q.completion_ratio)}" if q.completion_ratio is not None else "输出 = 输入 × 补全×1（默认）")
        if q.cache_ratio is not None:
            extras.append(f"缓存读 = 输入 × 缓存×{_g(q.cache_ratio)}")
        if q.cache_write_ratio is not None:
            extras.append(f"缓存写 = 输入 × 写缓存×{_g(q.cache_write_ratio)}")
    elif q.billing_expr:
        eq = parse_billing_expr(q.billing_expr)
        tier = f"「{eq.tier_name}」" if eq.tier_name else ""
        if q.kind == "token" and eq.input_usd is not None:
            head = f"表达式{tier} p×{_g(eq.input_usd)} × {g_txt}" + tail(eq.input_usd * gr)
            for k, v in (("c", eq.output_usd), ("cr", eq.cache_usd), ("cc", eq.cache_write_usd)):
                if v is not None:
                    extras.append(f"{k}×{_g(v)}")
        elif q.kind == "call" and eq.call_usd is not None:
            head = f"表达式{tier} 按次${_g(eq.call_usd)} × {g_txt}" + tail(eq.call_usd * gr, "/次")
        else:
            return ""
    elif q.kind == "call" and q.model_price is not None:
        head = f"按次${_g(q.model_price)} × {g_txt}" + tail(q.model_price * gr, "/次")
    else:
        return ""
    return head + ("；" + "；".join(extras) if extras else "")
