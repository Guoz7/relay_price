"""Scoring model. Pure functions, no I/O.

Staged certification + non-compensatory capping (V1.0 / V2.0):
- Stage 0: Archived / unpriced (e.g. 401/403 login-gated or WAF-blocked). Not scored, never recommended.
- Stage 1: Public-metadata verified only (not yet probed with real paid API keys).
           Station score and Quote score are HARD-CAPPED at 74.0 (Grade B) — never awarded S or A.
- Stage 2: Probed with real paid API keys over a rolling window (success rate, TTFT/TPOT,
           tool-calling, prompt-cache hit rate, billing reconciliation). Passing Stage 2 unlocks A/S;
           failing critical health/billing gates triggers a non-compensatory cap at Grade C (64.0).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

DEFAULT_STATION_WEIGHTS = {"stability": 30, "longevity": 25, "transparency": 20, "reputation": 15, "latency": 10}
DEFAULT_QUOTE_WEIGHTS = {"price": 40, "station": 60}

STAGE1_CAP = 74.0  # Unprobed stations & quotes can reach at most Grade B (74)
STAGE2_FAIL_CAP = 64.0  # Probed stations that fail reliability or billing drift cap at Grade C (64)
PROBE_BLEND_WEIGHT = 0.35  # Stage 2: 35% real probe score + 65% public metadata score

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
    "probe": "实测",
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


def score_longevity(age_days: int | None, registered_at: str | None = None) -> Dim:
    if age_days is None:
        return Dim("longevity", 35.0, "域名注册时间未查到", known=False)
    s = interp(age_days, [(0, 5), (30, 15), (90, 35), (180, 55), (365, 75), (730, 90), (1095, 100)])
    if age_days >= 365:
        age_txt = f"{age_days / 365:.1f} 年"
    elif age_days >= 30:
        age_txt = f"{age_days // 30} 个月"
    else:
        age_txt = f"{age_days} 天"
    return Dim("longevity", s, f"域名已注册 {age_txt}" + (f"（{registered_at}）" if registered_at else ""))


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


def score_probe(probe: dict[str, Any] | None) -> tuple[Dim, int, str]:
    """Evaluate real-call probe summary.

    Returns (Dim, effective_stage, cap_reason):
    - No probe / insufficient samples -> stage 1 (capped at B / 74)
    - Probed and passed gates -> stage 2 (unlocks A / S)
    - Probed and failed reliability (<90%) or billing drift (>15%) -> stage 2 with veto cap (C / 64)
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

    s_sr = interp(sr * 100, [(50, 0), (80, 35), (90, 65), (95, 82), (99, 96), (100, 100)])
    s_drift = 80.0 if drift is None else interp(abs(float(drift)), [(0, 100), (5, 92), (10, 78), (15, 60), (30, 15), (50, 0)])
    s_tool = 75.0 if tool_r is None else clamp(float(tool_r) * 100)
    s_cache = 70.0 if cache_r is None else clamp(float(cache_r) * 100)
    s_ttft = 75.0 if ttft is None else interp(float(ttft), [(600, 100), (1500, 88), (3000, 68), (6000, 35), (10000, 10)])
    p_score = clamp(0.45 * s_sr + 0.25 * s_drift + 0.15 * s_tool + 0.10 * s_cache + 0.05 * s_ttft)

    parts = [f"近 {probe.get('window_days', 7)} 天 {samples} 次实测", f"成功率 {sr:.1%}"]
    if ttft is not None:
        parts.append(f"首字 {ttft:.0f}ms")
    if tool_r is not None:
        parts.append(f"工具调用 {float(tool_r):.0%}")
    if cache_r is not None:
        parts.append(f"缓存命中 {float(cache_r):.0%}")
    if drift is not None:
        parts.append(f"实扣偏差 {float(drift):+.1f}%")

    veto_reasons = []
    if sr < 0.90:
        veto_reasons.append(f"实测成功率仅 {sr:.1%}（低于 90%）")
    if drift is not None and abs(float(drift)) > 15.0:
        veto_reasons.append(f"账单实扣偏差 {float(drift):+.1f}%（超过 ±15%）")
    if veto_reasons:
        return Dim("probe", p_score, "，".join(parts), known=True), 2, "；".join(veto_reasons) + "，触发降级封顶 C 级"
    if not probe.get("passed_stage2", samples >= 20):
        return Dim("probe", p_score, "，".join(parts) + "（实测样本未满 20 次，暂按 Stage 1 封顶）", known=True), 1, "实测样本不足，暂封顶 B 级"
    return Dim("probe", p_score, "，".join(parts), known=True), 2, ""


def station_score(
    dims: list[Dim],
    weights: dict[str, float] | None = None,
    attempts: int = 0,
    probe: dict[str, Any] | None = None,
) -> StationScore:
    w = weights or DEFAULT_STATION_WEIGHTS
    tot_w = sum(w.get(d.key, 0) for d in dims)
    meta_total = sum(d.score * w.get(d.key, 0) for d in dims) / tot_w if tot_w else 0.0
    probe_dim, stage, cap_reason = score_probe(probe)

    all_dims = {d.key: d for d in dims}
    all_dims["probe"] = probe_dim

    if stage == 2:
        blended = (1.0 - PROBE_BLEND_WEIGHT) * meta_total + PROBE_BLEND_WEIGHT * probe_dim.score
        raw_total = round(blended, 1)
        if cap_reason:  # failed Stage 2 veto gate
            total = min(STAGE2_FAIL_CAP, raw_total)
        else:
            total = raw_total
    else:
        raw_total = round(meta_total, 1)
        total = min(STAGE1_CAP, raw_total)
        if raw_total <= STAGE1_CAP and not probe_dim.known:
            cap_reason = ""  # score naturally <= 74; still stage 1 unprobed

    unknown = sum(1 for d in dims if not d.known)
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
    )


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
