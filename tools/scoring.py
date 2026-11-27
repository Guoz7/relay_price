"""Scoring model. Pure functions, no I/O.

Staged certification + non-compensatory capping (V1.0 / V2.0):
- Stage 0: Archived / unpriced (e.g. 401/403 login-gated or WAF-blocked). Not scored, never recommended.
- Stage 1: Public-metadata verified only (not yet probed with real paid API keys).
           Station score and Quote score are HARD-CAPPED at 74.0 (Grade B) — never awarded S or A.
- Stage 2: Probed with real paid API keys over a rolling window (success rate, TTFT/TPOT,
           tool-calling, prompt-cache hit rate, billing reconciliation). Passing Stage 2 unlocks A/S;
           failing critical health/billing gates triggers a non-compensatory cap at Grade C (64.0).

V3.0 composite (chosen by tools/eval_scoring.py on the real 764-station corpus, "Model B"):
- 7 dimensions: fetch stability, longevity (incl. expiry runway), transparency, reputation, latency,
  price_stability (30-day hike analytics) and purity_protocol (caveat-free lanes, lane spread restraint,
  native protocol + cache disclosure).
- Weighted arithmetic mean minus a convex weak-link drag on critical trust dimensions:
  drag_k = lambda_k * T_k * ((T_k - x_k) / T_k) ** gamma when x_k < T_k (total drag capped).
- Stage 1 smooth monotonic soft-cap (see soft_cap): a softplus knee (slope 1 -> m) followed by a C1 exponential
  approach to C = 74. Strictly increasing and strictly below 74, so top stations no longer collide at 74.0.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Any

DEFAULT_STATION_WEIGHTS = {
    "stability": 18, "longevity": 16, "transparency": 14, "reputation": 12, "latency": 8,
    "price_stability": 17, "purity_protocol": 15,
}
LEGACY_STATION_WEIGHTS = {"stability": 30, "longevity": 25, "transparency": 20, "reputation": 15, "latency": 10}
DIM_ORDER = ("stability", "longevity", "transparency", "reputation", "latency", "price_stability", "purity_protocol")
DEFAULT_QUOTE_WEIGHTS = {"price": 40, "station": 60}

STAGE1_CAP = 74.0  # Unprobed stations & quotes can reach at most Grade B (74)
STAGE2_FAIL_CAP = 64.0  # Probed stations that fail reliability or billing drift cap at Grade C (64)
PROBE_BLEND_WEIGHT = 0.35  # Stage 2: 35% real probe score + 65% public metadata score
SOFT_CAP = {"knee": 45.0, "slope": 0.65, "tau": 3.0, "top": 71.0}  # Stage 1 soft-cap (see soft_cap)
WEAK_LINK_GAMMA = 1.5
WEAK_LINK_MAX = 20.0
# key -> (healthy threshold T, lambda)
DEFAULT_WEAK_LINK: dict[str, tuple[float, float]] = {
    "price_stability": (85.0, 0.6),
    "purity_protocol": (60.0, 0.5),
    "transparency": (60.0, 0.4),
    "longevity": (60.0, 0.8),
}

STAGE_LABELS = {
    0: "Stage 0 · 仅建档（未公开报价）",
    1: "Stage 1 · 公开校验（未实测）",
    2: "Stage 2 · 实测认证",
}

DIM_LABELS = {
    "stability": "稳定",
    "longevity": "资历",
    "transparency": "透明",
    "reputation": "口碑",
    "latency": "响应",
    "price_stability": "价稳",
    "purity_protocol": "纯净",
    "probe": "实测",
}
DIM_LONG_LABELS = {
    "stability": "接口稳定",
    "longevity": "域名资历",
    "transparency": "定价透明",
    "reputation": "社区口碑",
    "latency": "响应速度",
    "price_stability": "价格稳定",
    "purity_protocol": "分组纯净与协议",
}


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def interp(x: float, pts: list[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation over sorted (x, y) points; flat outside the range."""
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def grade(score: float | None) -> str:
    if score is None:
        return "—"
    if score >= 85:
        return "S"
    if score >= 75:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "D"


@dataclass
class Dim:
    key: str
    score: float
    evidence: str
    known: bool = True  # False -> neutral default because data is missing


@dataclass
class StationScore:
    total: float
    dims: dict[str, Dim] = field(default_factory=dict)
    confidence: str = "中"  # 低 / 中 / 高
    stage: int = 1
    raw_total: float = 0.0
    cap_reason: str = ""
    probe: dict[str, Any] | None = None
    linear_total: float = 0.0  # weighted arithmetic mean before weak-link drag
    penalty: float = 0.0  # weak-link drag in raw points
    penalized: list[str] = field(default_factory=list)  # dims that triggered weak-link drag
    completeness: float = 1.0  # share of weight backed by real (known) evidence
    uncertainty: float = 0.0  # approximate ± points on the displayed total
    percentile: float | None = None  # share of scored stations with a lower total (set by build)

    def __post_init__(self) -> None:
        if not self.raw_total:
            self.raw_total = self.total

    @property
    def grade(self) -> str:
        return "—" if self.stage == 0 else grade(self.total)

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, STAGE_LABELS[1])


# ---------------------------------------------------------------- dimensions
def score_stability(attempts: int, successes: int, consecutive_failures: int) -> Dim:
    prior_n, prior_p = 5.0, 0.7  # conservative: a new station must earn its stability score
    rate = (successes + prior_n * prior_p) / (attempts + prior_n)
    s = rate * 100 - min(45, 15 * consecutive_failures)
    ev = f"公开接口刷新成功 {successes}/{attempts} 次"
    if consecutive_failures:
        ev += f"，最近连续失败 {consecutive_failures} 次"
    if attempts < 5:
        ev += "（样本不足，分数会随刷新次数增加而收敛）"
    return Dim("stability", clamp(s), ev, known=attempts >= 5)


def score_longevity(age_days: int | None, registered_at: str | None = None,
                    expiry_days_left: int | None = None) -> Dim:
    """Domain age curve plus renewal runway: expired / expiring domains are penalised, multi-year renewals get a small bonus."""
    if age_days is None:
        s, ev, known = 35.0, "域名注册时间未查到", False
    else:
        s = interp(age_days, [(0, 5), (30, 15), (90, 35), (180, 55), (365, 75), (730, 90), (1095, 100)])
        if age_days >= 365:
            age_txt = f"{age_days / 365:.1f} 年"
        elif age_days >= 30:
            age_txt = f"{age_days // 30} 个月"
        else:
            age_txt = f"{age_days} 天"
        ev, known = f"域名已注册 {age_txt}" + (f"（{registered_at}）" if registered_at else ""), True
    if isinstance(expiry_days_left, int) and not isinstance(expiry_days_left, bool):
        left = expiry_days_left
        if left < 0:
            s, ev = s - 35, ev + "；域名已过期"
        elif left <= 30:
            s, ev = s - 20, ev + f"；{left} 天后到期"
        elif left <= 60:
            s, ev = s - 12, ev + f"；{left} 天后到期"
        elif left <= 180:
            s, ev = s - 4, ev + f"；{left} 天后到期"
        elif left >= 730:
            s, ev = s + 4, ev + "；已续费 2 年以上"
        elif left >= 365:
            s, ev = s + 2, ev + "；已续费 1 年以上"
    return Dim("longevity", clamp(s), ev, known=known)


def score_transparency(topup_known: bool, priced: int, placeholders: int, groups_selectable: int,
                       groups_with_desc: int, quotes: int, anomalies: int) -> Dim:
    s = 0.0
    parts = []
    if topup_known:
        s += 35
        parts.append("公开充值比例")
    else:
        parts.append("未公开充值比例")
    total = priced + placeholders
    priced_share = priced / total if total else 0
    s += 25 * priced_share
    parts.append(f"{priced_share:.0%} 型号已定价")
    desc_share = groups_with_desc / groups_selectable if groups_selectable else 0
    s += 20 * desc_share
    parts.append(f"{desc_share:.0%} 分组有说明")
    anom_share = anomalies / quotes if quotes else 0
    s += 20 * (1 - min(1.0, anom_share * 5))  # 20% anomalous quotes -> 0
    if anomalies:
        parts.append(f"{anomalies} 个异常低价")
    return Dim("transparency", clamp(s), "，".join(parts))


def score_reputation(n_sources: int, tranco: int | None, umbrella: int | None, majestic: int | None) -> Dim:
    s = interp(n_sources, [(0, 10), (1, 30), (2, 50), (3, 65), (4, 78), (6, 90)])
    ranks = []
    if tranco:
        ranks.append(f"Tranco #{tranco:,}")
    if umbrella:
        ranks.append(f"Umbrella #{umbrella:,}")
    if majestic:
        ranks.append(f"Majestic #{majestic:,}")
    if ranks:
        best = min(r for r in (tranco, umbrella, majestic) if r)
        s += interp(math.log10(best), [(4, 25), (5, 18), (6, 10)])  # top 10k +25 … top 1M +10
    ev = f"被 {n_sources} 个独立导航 / 社区 / 榜单收录"
    if ranks:
        ev += "；流量榜：" + "、".join(ranks)
    return Dim("reputation", clamp(s), ev)


def score_latency(median_ms: float | None) -> Dim:
    if median_ms is None:
        return Dim("latency", 50.0, "无响应时间样本", known=False)
    s = interp(median_ms, [(600, 100), (1500, 85), (3000, 60), (6000, 30), (10000, 10)])
    return Dim("latency", s, f"公开接口中位响应 {median_ms / 1000:.1f} 秒")


def score_price_stability(stab: dict[str, Any] | None, monitored: bool = True) -> Dim:
    """30-day price stability from station_insights.compute_price_stability (benign drops / <2% rounding are free)."""
    if not monitored:
        return Dim("price_stability", 80.0, "尚无连续两次报价快照，无法比对调价", known=False)
    if not stab:
        return Dim("price_stability", 100.0, "近 30 天未监测到公开报价变动")
    v = stab.get("stability_score", 100)
    v = float(v) if isinstance(v, (int, float)) else 100.0
    return Dim("price_stability", clamp(v), str(stab.get("volatility_summary") or "近 30 天未监测到公开报价变动"))


def score_purity_protocol(
    *, groups_paid: int, groups_caveat: int, quotes_paid: int, quotes_promo: int, quotes_anomaly: int,
    dual_lane_models: int, extreme_spread_models: int, max_spread: float | None,
    claude_quotes: int, claude_native: int, gpt_quotes: int, gpt_responses: int,
    cache_eligible: int, cache_disclosed: int,
) -> Dim:
    """分组纯净度与协议完备度 (0–100).

    45% clean lanes (caveat-free groups, standard-lane quotes, no anomalies) + 25% lane spread restraint
    (share of models whose standard vs reverse/promo price gap exceeds 4×) + 30% flagship protocol & cache
    coverage (native Anthropic for Claude, OpenAI Responses for GPT-5/6, explicit cache price).
    """
    if quotes_paid <= 0:
        return Dim("purity_protocol", 50.0, "暂无可比付费报价", known=False)
    cav_share = groups_caveat / groups_paid if groups_paid else 0.0
    promo_share = quotes_promo / quotes_paid
    anom_share = quotes_anomaly / quotes_paid
    clean = 100 * (0.5 * (1 - cav_share) + 0.3 * (1 - promo_share) + 0.2 * (1 - min(1.0, anom_share * 5)))
    ext_share = extreme_spread_models / dual_lane_models if dual_lane_models else 0.0
    spread = interp(ext_share, [(0, 100), (0.2, 70), (0.5, 35), (1.0, 10)])
    proto_parts = []
    if claude_quotes:
        proto_parts.append(claude_native / claude_quotes)
    if gpt_quotes:
        proto_parts.append(gpt_responses / gpt_quotes)
    if cache_eligible:
        proto_parts.append(cache_disclosed / cache_eligible)
    proto = 100 * sum(proto_parts) / len(proto_parts) if proto_parts else 50.0
    s = 0.45 * clean + 0.25 * spread + 0.30 * proto
    parts = [f"{1 - cav_share:.0%} 分组无自认逆向/特惠标签"]
    if quotes_promo:
        parts.append(f"{promo_share:.0%} 报价走特惠/逆向线")
    if dual_lane_models:
        sp = f"，最大价差 {max_spread:.1f}×" if max_spread else ""
        parts.append(f"{extreme_spread_models}/{dual_lane_models} 个双线型号价差超 4×{sp}")
    if claude_quotes:
        parts.append(f"Claude 原生协议 {claude_native}/{claude_quotes}")
    if gpt_quotes:
        parts.append(f"GPT Responses {gpt_responses}/{gpt_quotes}")
    if cache_eligible:
        parts.append(f"缓存价公开 {cache_disclosed}/{cache_eligible}")
    return Dim("purity_protocol", clamp(s), "，".join(parts))


def _softplus(x: float) -> float:
    return x + math.log1p(math.exp(-x)) if x > 0 else math.log1p(math.exp(x))


def _knee(r: float, k1: float, m: float, tau: float) -> float:
    """Smooth piecewise-linear compression: slope 1 below k1, slope m above, blended over ~tau points."""
    return r - (1 - m) * tau * (_softplus((r - k1) / tau) - _softplus(-k1 / tau))


def soft_cap(raw: float, cap: float = STAGE1_CAP, soft: dict[str, float] | None = None) -> float:
    """Smooth, strictly increasing Stage 1 map of [0, 100] onto [0, cap).

    h(r) = r − (1 − m)·τ·[softplus((r − K)/τ) − softplus(−K/τ)]   (soft knee: slope 1 → m around K)
    f(h) = h for h ≤ T, else C − (C − T)·exp(−(h − T)/(C − T))        (C1 exponential approach to C = 74)
    """
    p = {**SOFT_CAP, **(soft or {})}
    h = _knee(clamp(raw), p["knee"], p["slope"], p["tau"])
    top = min(p["top"], cap - 0.5)
    if h <= top:
        return h
    span = cap - top
    return cap - span * math.exp(-(h - top) / span)


def soft_cap_slope(raw: float, cap: float = STAGE1_CAP, soft: dict[str, float] | None = None) -> float:
    p = {**SOFT_CAP, **(soft or {})}
    r = clamp(raw)
    z = (r - p["knee"]) / p["tau"]
    dh = 1 - (1 - p["slope"]) / (1 + math.exp(-z)) if z > -50 else 1.0
    h = _knee(r, p["knee"], p["slope"], p["tau"])
    top = min(p["top"], cap - 0.5)
    return dh if h <= top else dh * math.exp(-(h - top) / (cap - top))


def weak_link_penalty(
    scores: dict[str, float],
    weak_link: dict[str, tuple[float, float]] | None = None,
    gamma: float = WEAK_LINK_GAMMA,
    max_penalty: float = WEAK_LINK_MAX,
) -> tuple[float, list[str]]:
    """Convex drag for critical trust dims below their healthy threshold; a strong reputation cannot buy back a hike."""
    wl = DEFAULT_WEAK_LINK if weak_link is None else weak_link
    pen, hit = 0.0, []
    for k, (thr, lam) in wl.items():
        x = scores.get(k)
        if x is None or thr <= 0 or x >= thr:
            continue
        pen += lam * thr * ((thr - x) / thr) ** gamma
        hit.append(k)
    return min(max_penalty, pen), hit


def score_probe(probe: dict[str, Any] | None) -> tuple[Dim, int, str]:
    """Evaluate real-call probe summary.

    Returns (Dim, effective_stage, cap_reason):
    - No probe / insufficient samples -> stage 1 (capped at B / 74)
    - Probed and passed gates -> stage 2 (unlocks A / S)
    - Probed and failed reliability (<90%) or billing drift (>15%) -> stage 2 with veto cap (C / 64)
    - Non-compensatory authenticity / IQ veto -> stage 2 with veto cap (C / 64), regardless of TTFT or uptime:
      tampered signature accepted, auth_pass_rate < 0.85, iq_pass_rate < 0.65, or a 24h dual-window downgrade alert
    - When authenticity / IQ rates are present they outweigh latency in the probe score
    """
    if not probe or not isinstance(probe, dict) or (probe.get("total_samples") or 0) < 5:
        return (
            Dim("probe", 0.0, "尚未接入真金白银探针实测，评级最高封顶 B 级（74 分）", known=False),
            1,
            "未实测封顶 B 级（74 分）",
        )
    samples = int(probe.get("total_samples") or 0)
    sr = float(probe.get("success_rate") or 0.0)
    ttft = probe.get("avg_ttft_ms")
    tool_r = probe.get("tool_call_pass_rate")
    cache_r = probe.get("cache_hit_rate")
    drift = probe.get("billing_drift_pct")
    auth_r = probe.get("auth_pass_rate")
    iq_r = probe.get("iq_pass_rate")
    ctx_r = probe.get("context_pass_rate")
    tamper_n = int(probe.get("tamper_accepted_count") or 0)
    v_tier = probe.get("verified_tier")

    s_sr = interp(sr * 100, [(50, 0), (80, 35), (90, 65), (95, 82), (99, 96), (100, 100)])
    s_drift = 80.0 if drift is None else interp(abs(float(drift)), [(0, 100), (5, 92), (10, 78), (15, 60), (30, 15), (50, 0)])
    s_tool = 75.0 if tool_r is None else clamp(float(tool_r) * 100)
    s_cache = 70.0 if cache_r is None else clamp(float(cache_r) * 100)
    s_ttft = 75.0 if ttft is None else interp(float(ttft), [(600, 100), (1500, 88), (3000, 68), (6000, 35), (10000, 10)])
    if auth_r is None and iq_r is None:
        p_score = clamp(0.45 * s_sr + 0.25 * s_drift + 0.15 * s_tool + 0.10 * s_cache + 0.05 * s_ttft)
    else:
        s_auth = 70.0 if auth_r is None else clamp(float(auth_r) * 100)
        s_iq = 70.0 if iq_r is None else clamp(float(iq_r) * 100)
        if ctx_r is not None:
            s_iq = 0.75 * s_iq + 0.25 * clamp(float(ctx_r) * 100)
        if tamper_n > 0:
            s_auth = 0.0
        p_score = clamp(
            0.25 * s_auth + 0.25 * s_iq + 0.20 * s_sr + 0.15 * s_drift
            + 0.07 * s_tool + 0.05 * s_cache + 0.03 * s_ttft
        )

    parts = [f"近 {probe.get('window_days', 7)} 天 {samples} 次实测", f"成功率 {sr:.1%}"]
    if ttft is not None:
        parts.append(f"首字 {ttft:.0f}ms")
    if tool_r is not None:
        parts.append(f"工具调用 {float(tool_r):.0%}")
    if cache_r is not None:
        parts.append(f"缓存命中 {float(cache_r):.0%}")
    if drift is not None:
        parts.append(f"实扣偏差 {float(drift):+.1f}%")
    if auth_r is not None:
        parts.append(f"真伪通过 {float(auth_r):.0%}")
    if iq_r is not None:
        parts.append(f"智商抽检通过 {float(iq_r):.0%}")
    if ctx_r is not None:
        parts.append(f"长文多跳 {float(ctx_r):.0%}")
    if v_tier:
        parts.append(f"已验证 {v_tier}")

    veto_reasons = []
    if sr < 0.90:
        veto_reasons.append(f"实测成功率仅 {sr:.1%}（低于 90%）")
    if drift is not None and abs(float(drift)) > 15.0:
        veto_reasons.append(f"账单实扣偏差 {float(drift):+.1f}%（超过 ±15%）")
    fid_reasons = []
    if tamper_n > 0:
        fid_reasons.append(f"篡改签名被接受 {tamper_n} 次")
    if auth_r is not None and float(auth_r) < 0.85:
        fid_reasons.append(f"真伪通过率仅 {float(auth_r):.0%}（低于 85%）")
    if iq_r is not None and float(iq_r) < 0.65:
        fid_reasons.append(f"智商抽检通过率仅 {float(iq_r):.0%}（低于 65%）")
    if probe.get("downgrade_alert"):
        fid_reasons.extend(str(x) for x in (probe.get("downgrade_reasons") or ["近 24h 真伪/智商骤降"]))
    if fid_reasons:
        reason = "；".join(fid_reasons + veto_reasons) + "，实测触发真伪/智商否决，强制降级封顶 C 级"
        return Dim("probe", p_score, "，".join(parts), known=True), 2, reason
    if veto_reasons:
        return Dim("probe", p_score, "，".join(parts), known=True), 2, "；".join(veto_reasons) + "，触发降级封顶 C 级"
    if not probe.get("passed_stage2", samples >= 20):
        return Dim("probe", p_score, "，".join(parts) + "（实测样本未满 20 次，暂按未实测规则最高 B 级）", known=True), 1, "实测样本不足，暂封顶 B 级"
    return Dim("probe", p_score, "，".join(parts), known=True), 2, ""


def station_score(
    dims: list[Dim],
    weights: dict[str, float] | None = None,
    attempts: int = 0,
    probe: dict[str, Any] | None = None,
    *,
    soft: dict[str, float] | None = None,
    weak_link: dict[str, tuple[float, float]] | None = None,
) -> StationScore:
    w = weights or DEFAULT_STATION_WEIGHTS
    tot_w = sum(w.get(d.key, 0) for d in dims)
    linear = sum(d.score * w.get(d.key, 0) for d in dims) / tot_w if tot_w else 0.0
    penalty, penalized = weak_link_penalty({d.key: d.score for d in dims if w.get(d.key, 0)}, weak_link)
    meta_total = clamp(linear - penalty)
    probe_dim, stage, cap_reason = score_probe(probe)

    all_dims = {d.key: d for d in dims}
    all_dims["probe"] = probe_dim

    if stage == 2:
        blended = (1.0 - PROBE_BLEND_WEIGHT) * meta_total + PROBE_BLEND_WEIGHT * probe_dim.score
        raw_total = round(blended, 1)
        total = min(STAGE2_FAIL_CAP, raw_total) if cap_reason else raw_total
        slope = 1.0 - PROBE_BLEND_WEIGHT
    else:
        raw_total = round(meta_total, 1)
        total = min(STAGE1_CAP, soft_cap(meta_total, STAGE1_CAP, soft))
        slope = soft_cap_slope(meta_total, STAGE1_CAP, soft)
        if meta_total - total < 0.05 and not probe_dim.known:
            cap_reason = ""  # soft-cap is (near) identity here; still stage 1 unprobed

    unknown = sum(1 for d in dims if not d.known)
    known_w = sum(w.get(d.key, 0) for d in dims if d.known)
    completeness = known_w / tot_w if tot_w else 0.0
    sd = math.sqrt(sum((w.get(d.key, 0) / tot_w * (6.0 if d.known else 22.0)) ** 2 for d in dims)) if tot_w else 10.0
    uncertainty = round(max(0.5, sd * slope), 1)
    if stage == 2 and unknown == 0:
        conf = "高"
    elif attempts >= 10 and unknown == 0:
        conf = "高"
    elif attempts < 3 or unknown >= 2:
        conf = "低"
    else:
        conf = "中"
    return StationScore(
        total=round(total, 1),
        dims=all_dims,
        confidence=conf,
        stage=stage,
        raw_total=raw_total,
        cap_reason=cap_reason,
        probe=probe if probe_dim.known else None,
        linear_total=round(linear, 1),
        penalty=round(penalty, 1),
        penalized=penalized,
        completeness=round(completeness, 3),
        uncertainty=uncertainty,
    )


def assign_percentiles(scores: list[StationScore]) -> None:
    """Market percentile = share of other scored stations with a strictly lower displayed total."""
    vals = sorted(sc.total for sc in scores)
    n = len(vals)
    for sc in scores:
        sc.percentile = round(bisect.bisect_left(vals, sc.total) / (n - 1) * 100, 1) if n > 1 else None


def market_medians(scores: list[StationScore], keys: tuple[str, ...] = DIM_ORDER) -> dict[str, float]:
    out: dict[str, float] = {}
    for k in keys:
        vals = sorted(sc.dims[k].score for sc in scores if k in sc.dims)
        if vals:
            m = len(vals) // 2
            out[k] = vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2
    return out


# ---------------------------------------------------------------- quote level
def price_score(value: float | None, median: float | None, anomaly: bool = False) -> float | None:
    """50 at the model median, +20 per halving, -20 per doubling. Anomalies get no price bonus."""
    if value is None or median is None or median <= 0:
        return None
    if value <= 0:
        return 100.0
    s = clamp(50 + 20 * math.log2(median / value))
    if anomaly:
        s = min(s, 50.0)
    return round(s, 1)


def quote_score(
    p_score: float | None,
    s_score: float,
    weights: dict[str, float] | None = None,
    *,
    cap: float | None = None,
) -> float:
    w = weights or DEFAULT_QUOTE_WEIGHTS
    wp, ws = float(w.get("price", 40)), float(w.get("station", 60))
    p = 30.0 if p_score is None else p_score  # price not comparable in CNY -> treated as below average
    val = (p * wp + s_score * ws) / (wp + ws)
    if cap is not None:
        val = min(val, cap)
    return round(val, 1)


def summarize_weights(w: dict[str, Any]) -> str:
    return " · ".join(f"{DIM_LABELS.get(k, k)} {v}%" for k, v in w.items() if k != "probe")
