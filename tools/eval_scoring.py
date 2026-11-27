"""Offline scoring experiment on the real local corpus (read-only, stdlib only).

  python tools/eval_scoring.py            # compare Baseline vs Model A / B / C
  python tools/eval_scoring.py --json out.json

Loads data/raw/*.json.gz, data/changes.json, data/fetch-runs/*.json, data/intel.json and data/model-specs.json
through the same helpers as tools/build.py (load_snapshots / merge_mirrors / station_dims), never writes data/.
Metrics: exact-74.0 pins, Top-50 unique scores, red-flag rank lift, Top-10 red flags, and Top-30 Spearman
robustness under ±20% single-weight perturbation and drop-1-reputation-source perturbation.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build as B  # noqa: E402
import scoring as S  # noqa: E402
import station_insights as SI  # noqa: E402

DIMS = S.DIM_ORDER
LEGACY_DIMS = ("stability", "longevity", "transparency", "reputation", "latency")


# ---------------------------------------------------------------- data
def load_corpus(now: datetime | None = None) -> dict[str, Any]:
    cfg = B.load_json(B.ROOT / "config.json", {})
    now = now or datetime.now(timezone.utc)
    quotes, stations = B.load_snapshots(cfg, now)
    intel = B.load_json(B.DATA / "intel.json", {})
    quotes = B.merge_mirrors(quotes, stations, intel)
    by_model: dict[str, list[Any]] = defaultdict(list)
    by_station: dict[str, list[Any]] = defaultdict(list)
    for q in quotes:
        by_model[q.canonical].append(q)
        by_station[q.domain].append(q)
    B.mark_anomalies(by_model, cfg)
    run_stats = B.load_run_stats(now)
    changes = B.load_json(B.DATA / "changes.json", [])
    stab = SI.compute_price_stability(changes)
    monitored = len(list(B.HIST.glob("*.json.gz"))) >= 2
    specs = B.MS.load_model_specs(B.DATA)
    sig: dict[str, dict[str, Any]] = {}
    for dom, st in stations.items():
        it = intel.get(dom) or {}
        rs = B.merged_run_stats(st, dom, run_stats)
        qs = by_station.get(dom, [])
        dims = B.station_dims(dom, st, qs, it, rs, stab.get(dom), cfg, now, monitored=monitored)
        dims_drop = B.station_dims(dom, st, qs, it, rs, stab.get(dom), cfg, now, monitored=monitored, drop_sources=1)
        pin = B.purity_inputs(st, qs, cfg)
        sb = stab.get(dom) or SI.default_stability()
        sig[dom] = {
            "dims": {d.key: d for d in dims},
            "rep_drop": dims_drop[3],
            "legacy_longevity": S.score_longevity(it.get("domain_age_days"), it.get("domain_registered_at")),
            "attempts": rs["attempts"],
            "stab": sb,
            "caveat_share": (pin["groups_caveat"] / pin["groups_paid"]) if pin["groups_paid"] else 0.0,
            "age": it.get("domain_age_days"),
        }
    return {"cfg": cfg, "now": now, "sig": sig, "n_quotes": len(quotes), "n_changes": len(changes),
            "n_specs": len(specs), "monitored": monitored}


# ---------------------------------------------------------------- models
Scores = dict[str, tuple[float, float]]  # dom -> (displayed total, raw tie-breaker)


def _lin(x: dict[str, float], w: dict[str, float]) -> float:
    tw = sum(w[k] for k in x if k in w)
    return sum(x[k] * w[k] for k in x if k in w) / tw if tw else 0.0


def _vals(sig: dict[str, Any], keys: tuple[str, ...], rep_drop: bool = False, legacy: bool = False) -> dict[str, float]:
    out = {k: sig["dims"][k].score for k in keys}
    if legacy:
        out["longevity"] = sig["legacy_longevity"].score
    if rep_drop:
        out["reputation"] = sig["rep_drop"].score
    return out


def baseline(corpus, w=None, rep_drop=False) -> Scores:
    w = w or S.LEGACY_STATION_WEIGHTS
    out = {}
    for dom, sg in corpus["sig"].items():
        raw = round(_lin(_vals(sg, LEGACY_DIMS, rep_drop, legacy=True), w), 1)
        out[dom] = (min(S.STAGE1_CAP, raw), raw)
    return out


def model_a(corpus, w=None, rep_drop=False, soft=None) -> Scores:
    w = w or S.DEFAULT_STATION_WEIGHTS
    out = {}
    for dom, sg in corpus["sig"].items():
        raw = _lin(_vals(sg, DIMS, rep_drop), w)
        out[dom] = (round(S.soft_cap(raw, S.STAGE1_CAP, soft), 1), round(raw, 1))
    return out


def model_b(corpus, w=None, rep_drop=False, soft=None, wl=None) -> Scores:
    w = w or S.DEFAULT_STATION_WEIGHTS
    out = {}
    for dom, sg in corpus["sig"].items():
        x = _vals(sg, DIMS, rep_drop)
        pen, _ = S.weak_link_penalty(x, wl)
        raw = S.clamp(_lin(x, w) - pen)
        out[dom] = (round(S.soft_cap(raw, S.STAGE1_CAP, soft), 1), round(raw, 1))
    return out


def model_c(corpus, w=None, rep_drop=False, soft=None, wl=None) -> Scores:
    """Empirical percentile-rank fusion (mid-rank ties) + the same weak-link risk penalty + soft-cap."""
    w = w or S.DEFAULT_STATION_WEIGHTS
    xs = {dom: _vals(sg, DIMS, rep_drop) for dom, sg in corpus["sig"].items()}
    pct: dict[str, dict[str, float]] = defaultdict(dict)
    n = len(xs)
    for k in DIMS:
        col = sorted(v[k] for v in xs.values())
        import bisect
        for dom, v in xs.items():
            lo, hi = bisect.bisect_left(col, v[k]), bisect.bisect_right(col, v[k])
            pct[dom][k] = 100.0 * ((lo + hi) / 2) / n
    out = {}
    for dom, x in xs.items():
        pen, _ = S.weak_link_penalty(x, wl)
        raw = S.clamp(_lin(pct[dom], w) - pen)
        out[dom] = (round(S.soft_cap(raw, S.STAGE1_CAP, soft), 1), round(raw, 1))
    return out


# ---------------------------------------------------------------- metrics
def ranking(sc: Scores) -> list[str]:
    return sorted(sc, key=lambda d: (-sc[d][0], -sc[d][1], d))


def spearman(a: list[float], b: list[float]) -> float:
    def rk(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for t in range(i, j + 1):
                r[order[t]] = (i + j) / 2
            i = j + 1
        return r
    ra, rb = rk(a), rk(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else 1.0


def top_k_spearman(base: Scores, pert: Scores, k: int = 30) -> float:
    rb, rp = ranking(base), ranking(pert)
    pos_b = {d: i for i, d in enumerate(rb)}
    pos_p = {d: i for i, d in enumerate(rp)}
    top = rb[:k]
    return spearman([pos_b[d] for d in top], [pos_p[d] for d in top])


def red_flags(corpus) -> dict[str, set[str]]:
    sig = corpus["sig"]
    return {
        "danger_hike": {d for d, s in sig.items() if s["stab"].get("cls") == "danger" or s["stab"].get("stability_score", 100) < 60},
        "caveat_gt40": {d for d, s in sig.items() if s["caveat_share"] > 0.40},
        "new_lt180d": {d for d, s in sig.items() if isinstance(s["age"], (int, float)) and 0 <= s["age"] < 180},
    }


def evaluate(corpus, name: str, fn: Callable[..., Scores], base_rank: list[str]) -> dict[str, Any]:
    sc = fn(corpus)
    rk = ranking(sc)
    pos = {d: i + 1 for i, d in enumerate(rk)}
    bpos = {d: i + 1 for i, d in enumerate(base_rank)}
    totals = [sc[d][0] for d in rk]
    n = len(rk)
    top15 = totals[: max(1, round(n * 0.15))]
    flags = red_flags(corpus)
    lift = {}
    for key, doms in flags.items():
        shifts = [pos[d] - bpos[d] for d in doms if d in pos]
        lift[key] = {"n": len(shifts), "median_shift": statistics.median(shifts) if shifts else 0,
                     "median_rank": statistics.median(pos[d] for d in doms) if doms else None,
                     "top10": sum(1 for d in rk[:10] if d in doms)}
    rob = []
    w0 = S.LEGACY_STATION_WEIGHTS if name == "Baseline" else S.DEFAULT_STATION_WEIGHTS
    for k in w0:
        for f in (0.8, 1.2):
            w = dict(w0)
            w[k] = w0[k] * f
            rob.append(("w:" + k + f"×{f}", top_k_spearman(sc, fn(corpus, w=w))))
    rob.append(("drop-1-source", top_k_spearman(sc, fn(corpus, rep_drop=True))))
    return {
        "name": name,
        "pinned_74": sum(1 for t in totals if t == S.STAGE1_CAP),
        "top50_unique": len({round(t, 1) for t in totals[:50]}) / min(50, n),
        "max": max(totals), "median": statistics.median(totals),
        "top15_range": (min(top15), max(top15)),
        "lift": lift,
        "rob_min": min(v for _, v in rob), "rob_mean": statistics.fmean(v for _, v in rob),
        "rob_worst": min(rob, key=lambda kv: kv[1])[0],
        "top10": rk[:10],
        "apiyi_rank": pos.get("api.apiyi.com"), "apiyi_score": sc.get("api.apiyi.com", (None,))[0],
    }


def production_parity(corpus) -> float:
    """Max |scoring.station_score − Model B| over the corpus (proves build uses the evaluated model)."""
    cfg = corpus["cfg"]
    params = B.score_params(cfg)
    w = cfg.get("station_weights") or S.DEFAULT_STATION_WEIGHTS
    mb = model_b(corpus, w=w, soft=params["soft"], wl=params["weak_link"])
    diff = 0.0
    for dom, sg in corpus["sig"].items():
        sc = S.station_score([sg["dims"][k] for k in DIMS], w, attempts=sg["attempts"], **params)
        diff = max(diff, abs(sc.total - mb[dom][0]))
    return diff


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="optional path for the full metrics JSON")
    args = ap.parse_args()
    corpus = load_corpus()
    print(f"corpus: {len(corpus['sig'])} priced stations, {corpus['n_quotes']} quotes, {corpus['n_changes']} change rows, "
          f"{corpus['n_specs']} model specs, price monitoring {'on' if corpus['monitored'] else 'off'}")
    base = baseline(corpus)
    base_rank = ranking(base)
    flags = red_flags(corpus)
    print("red-flag sets: " + ", ".join(f"{k}={len(v)}" for k, v in flags.items()))
    models = [("Baseline", baseline), ("Model A", model_a), ("Model B", model_b), ("Model C", model_c)]
    res = [evaluate(corpus, n, f, base_rank) for n, f in models]
    hdr = ["metric"] + [r["name"] for r in res]
    rows = [
        ["pinned at 74.0 (goal ≤3)"] + [str(r["pinned_74"]) for r in res],
        ["Top-50 unique (goal ≥85%)"] + [f"{r['top50_unique']:.0%}" for r in res],
        ["max / median"] + [f"{r['max']:.1f} / {r['median']:.1f}" for r in res],
        ["top-15% range"] + [f"{r['top15_range'][0]:.1f}–{r['top15_range'][1]:.1f}" for r in res],
    ]
    for key in flags:
        rows.append([f"{key} median shift (+ = down)"] + [f"{r['lift'][key]['median_shift']:+g}" for r in res])
        rows.append([f"{key} in Top 10"] + [str(r["lift"][key]["top10"]) for r in res])
    rows += [
        ["api.apiyi.com rank / score"] + [f"#{r['apiyi_rank']} / {r['apiyi_score']}" for r in res],
        ["Top-30 Spearman min (goal ≥0.92)"] + [f"{r['rob_min']:.3f}" for r in res],
        ["Top-30 Spearman mean"] + [f"{r['rob_mean']:.3f}" for r in res],
        ["worst perturbation"] + [r["rob_worst"] for r in res],
    ]
    widths = [max(len(str(x[i])) for x in [hdr] + rows) for i in range(len(hdr))]
    print("| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(hdr)) + " |")
    print("|" + "|".join("-" * (wd + 2) for wd in widths) + "|")
    for r in rows:
        print("| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)) + " |")
    print("\nsoft-cap sweep (Model B): knee/slope/top -> pinned / top50 unique / top-15% / median / share ≥65 (B)")
    for kn, m, tp in ((60, 0.35, 71), (50, 0.5, 71), (45, 0.65, 71), (45, 0.65, 70), (40, 0.7, 71), (35, 0.74, 72)):
        sp = {"knee": kn, "slope": m, "top": tp}
        f = (lambda c, w=None, rep_drop=False, sp=sp: model_b(c, w, rep_drop, soft=sp))
        r = evaluate(corpus, "B*", f, base_rank)
        b_share = sum(1 for v in f(corpus).values() if v[0] >= 65) / len(corpus["sig"])
        print(f"  K={kn} m={m} T={tp}: {r['pinned_74']} / {r['top50_unique']:.0%} / {r['top15_range'][0]:.1f}–{r['top15_range'][1]:.1f}"
              f" / {r['median']:.1f} / {b_share:.0%} / rob {r['rob_min']:.3f}")
    b_base = sum(1 for v in base.values() if v[0] >= 65) / len(base)
    print(f"  baseline share ≥65 (B): {b_base:.0%}")
    print("\nModel B Top 10: " + ", ".join(res[2]["top10"]))
    print(f"production parity |scoring.station_score − Model B| max = {production_parity(corpus):.3f}")
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=list), encoding="utf-8")


if __name__ == "__main__":
    main()
