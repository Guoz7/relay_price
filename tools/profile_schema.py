"""Full-fleet raw schema audit for data/raw/*.json.gz (stdlib only, read-only).

  python tools/profile_schema.py                 # human-readable report + hard invariants
  python tools/profile_schema.py --json out.json # also dump the numbers
  python tools/profile_schema.py --no-payload    # skip Experiment C (real scoring pass)

Reports empirical coverage, zero rates, enum values, dirty placeholder URLs and the `billing_expr`
variable vocabulary, then settles the three schema experiments:
  A. `cache_ratio == 0` disambiguation (Go-struct-zero default vs explicit free vs not applicable)
  B. coverage-gated Fact Sheet / compare fields (always-visible vs show-when-valid)
  C. compact `stations.json` payload size (must stay <= 280 KB)
Exit code 1 when a hard invariant fails.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import pricing as P  # noqa: E402

ROOT = TOOLS.parent
RAW = ROOT / "data" / "raw"
VAR_RE = re.compile(r"\b([a-z][a-z0-9_]*)\s*\*\s*([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)")
URL_FIELDS = ("docs_link", "server_address", "top_up_link")
TEMPLATE_ROOTS = ("newapi.pro",)
TEMPLATE_REPOS = ("github.com/quantumnous", "github.com/songquanpeng")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
MIN_CACHE_WRITE = 20_000
ALWAYS_COVERAGE = 50.0  # Experiment B threshold (% of stations with a valid value)
CRITICAL_FIELDS = {"topup_price", "topup_mode", "email_verification", "turnstile_check", "cache_read_coverage", "cache_write_coverage"}


def load_all() -> list[dict[str, Any]]:
    def one(f: Path) -> dict[str, Any] | None:
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [s for s in pool.map(one, sorted(RAW.glob("*.json.gz"))) if s and s.get("domain")]


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:5.1f}%" if b else "  n/a"


def _host(u: str) -> str:
    try:
        return (urlsplit(u if "://" in u else "https://" + u.lstrip("/")).hostname or "").lower()
    except ValueError:
        return ""


def dirty_reason(raw: Any, dom: str) -> str | None:
    """Independent (non-sanitizer) classification of a raw status URL, for auditing the sanitizer."""
    if not isinstance(raw, str) or not raw.strip():
        return "blank"
    u = raw.strip()
    low = u.lower()
    if re.match(r"^[a-z][a-z0-9+.-]*:", low) and not low.startswith(("http://", "https://")):
        return "non-http scheme"
    h = _host(u) if not u.startswith("/") else dom
    if h in LOOPBACK_HOSTS or h.endswith(".localhost") or re.match(r"^(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.", h):
        return "loopback/private"
    if h == "example.com" or h.endswith(".example.com"):
        return "example.com"
    own_root = ".".join(dom.split(".")[-2:])
    hb = h[4:] if h.startswith("www.") else h
    path = hb + (urlsplit(u if "://" in u else "https://" + u.lstrip("/")).path or "").lower()
    if any(hb == r or hb.endswith("." + r) for r in TEMPLATE_ROOTS) and own_root != "newapi.pro":
        return "upstream template"
    if any(path.startswith(r) for r in TEMPLATE_REPOS) and own_root != "github.com":
        return "upstream template"
    return None


def leaked(clean: str | None, dom: str) -> bool:
    if not clean:
        return False
    return dirty_reason(clean, dom) not in (None, "blank")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", default="", help="write the report numbers to this path")
    ap.add_argument("--no-payload", action="store_true", help="skip Experiment C (needs the scoring pass)")
    args = ap.parse_args()
    snaps = load_all()
    report: dict[str, Any] = {}
    fails: list[str] = []

    # ------------------------------------------------------------ 1. fleet
    n_models = 0
    status_present = Counter()
    status_nonempty = Counter()
    status_falsy = Counter()
    status_enum: dict[str, Counter] = defaultdict(Counter)
    field_cov = Counter()
    field_zero = Counter()
    enums: dict[str, Counter] = defaultdict(Counter)
    vocab = Counter()
    vocab_zero = Counter()
    n_status = 0
    for s in snaps:
        st = s.get("status") if isinstance(s.get("status"), dict) else None
        if st:
            n_status += 1
            for k, v in st.items():
                status_present[k] += 1
                if v not in (None, "", [], {}):
                    status_nonempty[k] += 1
                if v in (False, 0) and not isinstance(v, str):
                    status_falsy[k] += 1
                if isinstance(v, (str, bool, int, float)) and not isinstance(v, float) and len(str(v)) <= 24:
                    status_enum[k][str(v)] += 1
        for m in (s.get("pricing") or {}).get("data") or []:
            if not isinstance(m, dict):
                continue
            n_models += 1
            for k, v in m.items():
                field_cov[k] += 1
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0:
                    field_zero[k] += 1
            enums["quota_type"][str(m.get("quota_type"))] += 1
            enums["billing_mode"][str(m.get("billing_mode"))] += 1
            for e in m.get("supported_endpoint_types") or []:
                enums["supported_endpoint_types"][str(e)] += 1
            ex = m.get("billing_expr")
            if isinstance(ex, str):
                for var, val in VAR_RE.findall(ex):
                    vocab[var] += 1
                    if float(val) == 0:
                        vocab_zero[var] += 1
    print(f"== 1. Fleet: {len(snaps)} raw snapshots, {n_status} with /api/status, {n_models:,} model entries")
    report["fleet"] = {"snapshots": len(snaps), "with_status": n_status, "model_entries": n_models}

    print("\n== 2. /api/status field coverage (present · non-empty · false/0 · top values)")
    for k, c in status_present.most_common():
        top = ", ".join(f"{v}×{n}" for v, n in status_enum[k].most_common(4)) if k not in URL_FIELDS + ("system_name", "version") else ""
        print(f"  {k:30s} {pct(c, n_status)} {pct(status_nonempty[k], n_status)} {pct(status_falsy[k], n_status)}  {top[:90]}")
    report["status_coverage"] = {k: {"present": c, "nonempty": status_nonempty[k], "falsy": status_falsy[k]} for k, c in status_present.items()}

    print("\n== 3. /api/pricing model field coverage (top 40 · zero rate among present)")
    for k, c in field_cov.most_common(40):
        z = f"zero {pct(field_zero[k], c)}" if field_zero[k] else ""
        print(f"  {k:28s} {c:7,d} {pct(c, n_models)}  {z}")
    report["model_field_coverage"] = {k: {"n": c, "zero": field_zero[k]} for k, c in field_cov.most_common(80)}

    print("\n== 4. Enums")
    for k, c in enums.items():
        print(f"  {k}: " + ", ".join(f"{v}×{n:,}" for v, n in c.most_common(10)))
    print("  quota_display_type: " + ", ".join(f"{v}×{n}" for v, n in status_enum["quota_display_type"].most_common(8)))
    report["enums"] = {k: dict(c.most_common(20)) for k, c in enums.items()}

    print("\n== 6. billing_expr variable vocabulary (uses · explicit ×0)")
    print("  " + ", ".join(f"{v}×{n:,}" + (f"(0:{vocab_zero[v]})" if vocab_zero[v] else "") for v, n in vocab.most_common(24)))
    report["expr_vocab"] = {v: {"n": n, "zero": vocab_zero[v]} for v, n in vocab.most_common(40)}

    # ------------------------------------------------------------ 5. dirty URLs
    print("\n== 5. Dirty placeholder URLs in /api/status (raw → after sanitize_station_url)")
    url_rep: dict[str, Any] = {}
    for fld in URL_FIELDS:
        reasons = Counter()
        kept = leak = present = rel = 0
        for s in snaps:
            st = s.get("status") if isinstance(s.get("status"), dict) else {}
            if fld not in st:
                continue
            raw = st.get(fld)
            r = dirty_reason(raw, s["domain"])
            if r != "blank":
                present += 1
            if r:
                reasons[r] += 1
            if isinstance(raw, str) and raw.strip().startswith("/"):
                rel += 1
            clean = P.sanitize_station_url(raw, s["domain"])
            if clean:
                kept += 1
                if leaked(clean, s["domain"]):
                    leak += 1
        dirty = sum(n for r, n in reasons.items() if r != "blank")
        print(f"  {fld:15s} non-blank {present:4d} · dirty {dirty:4d} {dict(reasons)} · relative→absolute {rel} · kept {kept} · leaked {leak}")
        url_rep[fld] = {"nonblank": present, "dirty": dirty, "reasons": dict(reasons), "relative": rel, "kept": kept, "leaked": leak}
        if leak:
            fails.append(f"{fld}: {leak} dirty URLs leaked through sanitize_station_url")
    report["urls"] = url_rep

    # ------------------------------------------------------------ normalize everything once
    quotes_all: list[P.Quote] = []
    metas: list[dict[str, Any]] = []
    raw_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    station_ctx: dict[str, tuple[float, bool]] = {}
    for s in snaps:
        dom = s["domain"]
        data = [m for m in (s.get("pricing") or {}).get("data") or [] if isinstance(m, dict)]
        if data:
            station_ctx[dom] = (sum(1 for m in data if "cache_ratio" in m) / len(data),
                                any(isinstance(m.get("cache_ratio"), (int, float)) and m["cache_ratio"] > 0 for m in data))
        for m in data:
            raw_by_key[(dom, str(m.get("model_name") or "").strip())] = m
        qs, meta = P.normalize_station(dom, s.get("pricing") or {}, s.get("status"))
        if qs:
            quotes_all.extend(qs)
            metas.append(meta)

    # ------------------------------------------------------------ Experiment A
    print("\n== 7. Experiment A · cache_ratio == 0 disambiguation")
    zero_kinds = Counter()
    zero_sig = Counter()
    for (dom, _name), m in raw_by_key.items():
        if m.get("cache_ratio") != 0 or isinstance(m.get("cache_ratio"), bool):
            continue
        ks, pos = station_ctx.get(dom, (0.0, False))
        if m.get("billing_expr"):
            zero_kinds["shadowed by billing_expr (ignored)"] += 1
            continue
        k = P.cache_zero_kind(m, ks, pos)
        zero_kinds[k] += 1
        if k == "struct":
            if any(kk in m and m.get(kk) == 0 for kk in P.CACHE_WRITE_KEYS):
                zero_sig["companion cache-write also 0"] += 1
            elif ks >= 0.95:
                zero_sig["field emitted on ≥95% models (non-omitempty fork)"] += 1
            else:
                zero_sig["station never discloses a positive cache ratio"] += 1
    print(f"  raw cache_ratio==0 rows: {sum(zero_kinds.values())} → {dict(zero_kinds)}")
    print(f"  struct-zero signatures: {dict(zero_sig)}")
    status_c = Counter(q.cache_status for q in quotes_all if q.kind == "token")
    print(f"  normalized token quotes cache_status: {dict(status_c)}")
    false_free = 0
    expr_free = 0
    for q in quotes_all:
        if q.cache_status != "free" or q.group_ratio == 0:
            continue
        if q.billing_expr:
            expr_free += 1
            continue
        m = raw_by_key.get((q.domain, q.model)) or {}
        ks, pos = station_ctx.get(q.domain, (0.0, False))
        struct_sig = (any(kk in m and m.get(kk) == 0 for kk in P.CACHE_WRITE_KEYS) or ks >= 0.95 or not pos
                      or m.get("supports_cache_read") is False)
        if struct_sig:
            false_free += 1
    print(f"  explicit free quotes: ratio-field ×0 {status_c['free'] - expr_free} · billing_expr cr×0 {expr_free} · struct-zero false-free {false_free}")
    report["exp_a"] = {"raw_zero": dict(zero_kinds), "struct_signatures": dict(zero_sig), "cache_status": dict(status_c),
                       "expr_free": expr_free, "false_free": false_free}
    if false_free:
        fails.append(f"{false_free} Go-struct-zero cache defaults rendered as free")

    # ------------------------------------------------------------ cache write capture
    cw_q = [q for q in quotes_all if q.cache_write_usd is not None]
    cw_src = Counter("billing_expr cc/cw" if q.billing_expr else "ratio field" for q in cw_q)
    cw_models = len({(q.domain, q.model) for q in cw_q})
    print(f"\n== 8. Cache-write capture: {len(cw_q):,} quotes ({cw_models:,} station-models) {dict(cw_src)}")
    report["cache_write"] = {"quotes": len(cw_q), "station_models": cw_models, "by_source": dict(cw_src)}
    if len(cw_q) < MIN_CACHE_WRITE:
        fails.append(f"only {len(cw_q)} cache-write records captured (< {MIN_CACHE_WRITE})")
    fx_ok = sum(1 for q in quotes_all if q.kind == "token" and q.model_ratio is not None and q.input_cny is not None)
    bad_fx = 0
    for q in quotes_all:
        if q.kind == "token" and q.model_ratio is not None and q.input_cny is not None:
            meta_price = q.input_cny / q.input_usd if q.input_usd else None
            if meta_price and abs(q.model_ratio * 2 * (q.group_ratio if q.group_ratio is not None else 1.0) * meta_price - q.input_cny) > 1e-5:
                bad_fx += 1
    print(f"  formula round-trip (模型×2×分组×充值 == input_cny ±1e-5): {fx_ok - bad_fx:,}/{fx_ok:,} ok")
    if bad_fx:
        fails.append(f"{bad_fx} ratio quotes fail the formula round-trip")

    # ------------------------------------------------------------ Experiment B
    print(f"\n== 9. Experiment B · coverage-gated Fact Sheet fields ({len(metas)} priced stations; always-visible if ≥{ALWAYS_COVERAGE:g}% or billing/access-critical)")
    fields = ["topup_price", "topup_mode", "usd_exchange_rate", "quota_per_unit", "quota_display_type", "version", "server_address",
              "docs_link", "top_up_link", "email_verification", "turnstile_check", "github_oauth", "linuxdo_oauth", "telegram_oauth",
              "custom_currency_symbol", "cache_read_coverage", "cache_write_coverage", "min_topup", "new_user_usd", "enable_online_topup",
              "aff_enabled", "wechat_login", "oidc_enabled"]
    decisions: dict[str, Any] = {}
    for f in fields:
        if f in ("github_oauth", "linuxdo_oauth", "telegram_oauth"):
            n = sum(1 for m in metas if m.get(f))
            label = "true"
        elif f == "topup_mode":
            n = sum(1 for m in metas if m.get(f) not in (None, "unknown"))
            label = "known"
        elif f in ("cache_read_coverage", "cache_write_coverage"):
            n = sum(1 for m in metas if (m.get(f) or 0) > 0)
            label = ">0%"
        elif f == "custom_currency_symbol":
            n = sum(1 for m in metas if m.get("quota_display_type") == "CUSTOM" and m.get(f))
            label = "CUSTOM"
        else:
            n = sum(1 for m in metas if m.get(f) is not None)
            label = "valid"
        cov = 100 * n / len(metas) if metas else 0
        if f in ("github_oauth", "linuxdo_oauth", "telegram_oauth"):
            mode = "always (login row)"
        else:
            mode = "always" if (cov >= ALWAYS_COVERAGE or f in CRITICAL_FIELDS) else "conditional"
        decisions[f] = {"coverage": round(cov, 1), "n": n, "display": mode}
        print(f"  {f:28s} {label:6s} {n:4d} {cov:5.1f}%  → {mode}")
    report["exp_b"] = decisions
    mode_c = Counter(m.get("topup_mode") for m in metas)
    basis = Counter(m.get("fx_basis") for m in metas)
    print(f"  topup_mode distribution: {dict(mode_c)} · FX basis {dict(basis)} (station rate used only when 4–12 ¥/$)")
    report["topup_mode"] = {"modes": dict(mode_c), "fx_basis": dict(basis)}

    # ------------------------------------------------------------ Experiment C
    if not args.no_payload:
        import build as B  # noqa: E402  (read-only helpers only; never calls update_history or writes dist/)
        import compare_page as CP  # noqa: E402
        import station_insights as SI  # noqa: E402
        import probes  # noqa: E402

        cfg = B.load_json(ROOT / "config.json", {})
        now = datetime.now(timezone.utc)
        quotes, stations = B.load_snapshots(cfg, now)
        intel = B.load_json(ROOT / "data" / "intel.json", {})
        quotes = B.merge_mirrors(quotes, stations, intel)
        by_model: dict[str, list[P.Quote]] = defaultdict(list)
        by_station: dict[str, list[P.Quote]] = defaultdict(list)
        for q in quotes:
            by_model[q.canonical].append(q)
            by_station[q.domain].append(q)
        B.mark_anomalies(by_model, cfg)
        changes = B.load_json(ROOT / "data" / "changes.json", [])
        stab = SI.compute_price_stability(changes)
        cfg_chg = B.load_json(ROOT / "data" / "station-config-changes.json", {})
        for d, lst in cfg_chg.items():
            if d in stations:
                stations[d]["config_changes"] = lst
        B.compute_station_scores(stations, by_station, intel, B.load_run_stats(now), cfg, probes.load_probe_summary(ROOT / "data"),
                                 stability_map=stab, now=now, monitored=len(list((ROOT / "data" / "history").glob("*.json.gz"))) >= 2)
        B.compute_quote_scores(by_model, stations, cfg)
        payload = CP.stations_payload(stations, by_station, intel, stab, now, B.FLAGSHIP_MODELS, cfg.get("base_url", ""))
        raw = CP.dumps(payload).encode("utf-8")
        csv_b = CP.stations_csv(payload).encode("utf-8")
        n_fs = sum(len(r[CP.COL_NAMES.index("fs")]) for r in payload["rows"])
        fs_cw = sum(1 for r in payload["rows"] for c in r[CP.COL_NAMES.index("fs")] if isinstance(c[4], (int, float)))
        print(f"\n== 10. Experiment C · stations.json: {len(payload['rows'])} stations, {len(payload['cols'])} cols, {n_fs} flagship cells "
              f"({fs_cw} with cache-write) → {len(raw):,} bytes ({len(raw) / 1024:.1f} KB, budget {CP.MAX_BYTES // 1024} KB) · CSV {len(csv_b) / 1024:.1f} KB")
        report["exp_c"] = {"stations": len(payload["rows"]), "bytes": len(raw), "csv_bytes": len(csv_b), "flagship_cells": n_fs, "flagship_cache_write": fs_cw}
        if len(raw) > CP.MAX_BYTES:
            fails.append(f"stations.json {len(raw)} bytes > {CP.MAX_BYTES}")
        leak = sum(1 for r in payload["rows"] for i in (CP.COL_NAMES.index("api"), CP.COL_NAMES.index("docs"), CP.COL_NAMES.index("topup"))
                   if r[i] and leaked(CP.full_url(r[i]), r[0]))
        if leak:
            fails.append(f"stations.json leaks {leak} dirty URLs")

    print("\n== Invariants: " + ("ALL PASS" if not fails else "FAIL"))
    for f in fails:
        print("  FAIL", f)
    report["fails"] = fails
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
