"""Read-only refresh of public New API endpoints for every seeded station.

Only two public GET requests per station: `/api/status` and `/api/pricing`.
No login, no keys, no challenge solving. Stations that block or fail keep their
previous snapshot, which the site then labels as stale.

Usage:
  python tools/refresh.py seed-from-hub          # add domains from priceai-research relay-hub DB
  python tools/refresh.py fetch                  # refresh all seeds (concurrent)
  python tools/refresh.py fetch --only a.com b.com
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import ssl
import sys
import time
import urllib.error
from urllib.parse import urlsplit
import urllib.request
import contextlib
import os
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapters  # noqa: E402

DATA = ROOT / "data"
RAW = DATA / "raw"
SEEDS = ROOT / "seeds.txt"
STATE = DATA / "stations-state.json"
RUNS = DATA / "fetch-runs"
ENDPOINTS = DATA / "endpoints.json"  # domain -> pricing URL when it is not https://<domain>/api/pricing
HUB_DB = ROOT.parent / "priceai-research" / "data" / "relay-hub.sqlite3"
CONFIG = ROOT / "config.json"

MAX_BYTES = 12_000_000


@contextlib.contextmanager
def nowait_pool(workers: int):
    """Thread pool that never blocks on exit: hung sockets (e.g. stuck DNS) must not hang the whole refresh."""
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        yield ex
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def safe_completed(futs: dict, overall: float):
    """as_completed() with an overall deadline; unfinished jobs are reported as failed instead of waited on."""
    try:
        for f in as_completed(futs, timeout=overall):
            yield f
    except FuturesTimeout:
        for f in list(futs):
            if not f.done():
                ff: Future = Future()
                ff.set_exception(TimeoutError("整体时限内未完成"))
                futs[ff] = futs[f]
                yield ff


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def norm_domain(s: str) -> str:
    s = s.strip().lower()
    if not s or s.startswith("#"):
        return ""
    for p in ("https://", "http://"):
        if s.startswith(p):
            s = s[len(p):]
    s = s.split("/")[0].split(":")[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def read_seeds() -> list[str]:
    if not SEEDS.exists():
        return []
    out, seen = [], set()
    for line in SEEDS.read_text(encoding="utf-8").splitlines():
        d = norm_domain(line.split("#", 1)[0])
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def cmd_seed_from_hub(args: argparse.Namespace) -> None:
    if not HUB_DB.exists():
        sys.exit(f"relay-hub DB not found: {HUB_DB}")
    con = sqlite3.connect(f"file:{HUB_DB.as_posix()}?mode=ro", uri=True)
    tiers = tuple(args.tiers)
    q = f"SELECT domain FROM stations WHERE verification_tier IN ({','.join('?' * len(tiers))}) ORDER BY domain"
    hub = [norm_domain(r[0]) for r in con.execute(q, tiers)]
    con.close()
    existing = read_seeds()
    extra = [d for d in hub if d and d not in set(existing)]
    lines = SEEDS.read_text(encoding="utf-8").splitlines() if SEEDS.exists() else [
        "# 每行一个站点域名；# 后为注释。refresh.py fetch 会只读拉取 /api/status 与 /api/pricing。",
    ]
    if extra:
        lines.append(f"# --- seed-from-hub {now_iso()} tiers={','.join(tiers)} ---")
        lines.extend(extra)
    SEEDS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"seeds: {len(existing)} existing + {len(extra)} new from hub = {len(existing) + len(extra)}")


def _get(url: str, ua: str, timeout: float) -> tuple[int | None, bytes, str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
    ctx = ssl.create_default_context()
    try:
        deadline = time.time() + timeout * 4  # hard cap for slow-drip responses
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            chunks, size = [], 0
            while True:
                if time.time() > deadline:
                    return r.status, b"", "下载超时"
                b = r.read(65536)
                if not b:
                    break
                chunks.append(b)
                size += len(b)
                if size > MAX_BYTES:
                    return r.status, b"", "响应过大"
            return r.status, b"".join(chunks), None
    except urllib.error.HTTPError as e:
        return e.code, b"", f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, b"", type(e).__name__


def _json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None


def load_endpoints() -> dict[str, str]:
    return json.loads(ENDPOINTS.read_text(encoding="utf-8")) if ENDPOINTS.exists() else {}


def fetch_one(domain: str, ua: str, timeout: float, pricing_url: str | None = None) -> dict[str, Any]:
    pricing_url = pricing_url or f"https://{domain}/api/pricing"
    sp = urlsplit(pricing_url)
    origin = f"{sp.scheme}://{sp.netloc}"
    t0 = time.time()
    st_code, st_body, st_err = _get(f"{origin}/api/status", ua, timeout)
    status = None
    sj = _json(st_body) if st_body else None
    if isinstance(sj, dict):
        status = sj.get("data") if isinstance(sj.get("data"), dict) else sj
    pr_code, pr_body, pr_err = _get(pricing_url, ua, timeout)
    pricing = None
    system = None
    pj = _json(pr_body) if pr_body else None
    ad = adapters.adapt(pj) if pj is not None else None
    if ad is not None:
        pricing, st_override, system = ad
        if st_override:
            status = {**(status or {}), **st_override}
    elif pr_code == 200 and not pr_err:
        if pj is None:
            pr_err = "非 JSON 响应"  # usually an HTML page (SPA fallback) served on /api/pricing
        elif isinstance(pj, dict) and pj.get("data") in ([], {}):
            pr_err = "价格表为空"
        else:
            pr_err = "未支持的价格格式"
    return {
        "domain": domain,
        "fetched_at": now_iso(),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "status_http": st_code,
        "pricing_http": pr_code,
        "status_error": st_err,
        "pricing_error": pr_err,
        "status": status,
        "pricing": pricing,
        "system": system,
        "pricing_url": pricing_url,
    }


def _slim_status(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(status, dict):
        return None
    keep = (
        "system_name", "price", "usd_exchange_rate", "quota_per_unit", "quota_display_type",
        "display_in_currency", "custom_currency_symbol", "custom_currency_exchange_rate", "version",
        "linuxdo_oauth", "github_oauth", "telegram_oauth", "email_verification", "turnstile_check",
        "top_up_link", "server_address", "docs_link",
        # public site settings (read-only, never user/admin secrets)
        "enable_online_topup", "min_topup", "quota_for_new_user", "aff_enabled", "wechat_login", "oidc_enabled",
        "uptime_kuma", "uptime_kuma_enabled",
    )
    out = {k: status.get(k) for k in keep if k in status and not isinstance(status.get(k), (dict, list))}
    chats = status.get("chats")
    if isinstance(chats, list):  # keep client names only; drop link templates that may embed {key}/{address}
        names = [str(k)[:40] for c in chats if isinstance(c, dict) for k in c.keys()]
        if names:
            out["chats"] = names[:12]
    return out


def cmd_fetch(args: argparse.Namespace) -> None:
    cfg = load_config()
    ua = cfg.get("user_agent") or "Mozilla/5.0 (compatible; RelayPriceBot/0.1)"
    domains = [norm_domain(d) for d in args.only] if args.only else read_seeds()
    domains = [d for d in domains if d]
    if not domains:
        sys.exit("no seeds; run seed-from-hub or edit seeds.txt")
    RAW.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    run_at = now_iso()
    ok = fail = 0
    log: list[dict[str, Any]] = []
    t0 = time.time()
    endpoints = load_endpoints()
    with nowait_pool(args.workers) as ex:
        futs = {ex.submit(fetch_one, d, ua, args.timeout, endpoints.get(d)): d for d in domains}
        for i, fut in enumerate(safe_completed(futs, args.deadline), 1):
            d = futs[fut]
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                res = {"domain": d, "fetched_at": now_iso(), "pricing": None, "pricing_error": type(e).__name__}
            st = state.setdefault(d, {})
            st["last_attempt_at"] = res["fetched_at"]
            st["last_status_http"] = res.get("status_http")
            st["last_pricing_http"] = res.get("pricing_http")
            if res.get("pricing") is not None:
                ok += 1
                st["last_success_at"] = res["fetched_at"]
                st["consecutive_failures"] = 0
                st["last_error"] = None
                snap = {
                    "domain": d,
                    "fetched_at": res["fetched_at"],
                    "run_at": run_at,
                    "status": _slim_status(res.get("status")),
                    "pricing": {k: res["pricing"].get(k) for k in ("data", "group_ratio", "usable_group") if k in res["pricing"]},
                    "system": res.get("system"),
                    "pricing_url": res.get("pricing_url"),
                }
                with gzip.open(RAW / f"{d}.json.gz", "wt", encoding="utf-8") as fh:
                    json.dump(snap, fh, ensure_ascii=False, separators=(",", ":"))
            else:
                fail += 1
                st["consecutive_failures"] = int(st.get("consecutive_failures") or 0) + 1
                st["last_error"] = res.get("pricing_error") or res.get("status_error") or "unknown"
            log.append({k: res.get(k) for k in ("domain", "status_http", "pricing_http", "pricing_error", "elapsed_ms")} | {"ok": res.get("pricing") is not None})
            if i % 25 == 0 or i == len(domains):
                print(f"  {i}/{len(domains)}  ok={ok} fail={fail}  {time.time() - t0:.0f}s", flush=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    run_file = RUNS / f"{run_at.replace(':', '').replace('-', '')[:15]}Z.json"
    run_file.write_text(json.dumps({"run_at": run_at, "ok": ok, "fail": fail, "domains": len(domains), "log": log}, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "last-run.json").write_text(json.dumps({"run_at": run_at, "ok": ok, "fail": fail, "domains": len(domains)}, ensure_ascii=False), encoding="utf-8")
    print(f"done: ok={ok} fail={fail} of {len(domains)} in {time.time() - t0:.0f}s -> {run_file.name}")


def cmd_discover(args: argparse.Namespace) -> None:
    """One-off read-only probe of hub domains that are not seeds yet; successes are appended to seeds.txt."""
    cfg = load_config()
    ua = cfg.get("user_agent") or "Mozilla/5.0 (compatible; RelayPriceBot/0.1)"
    con = sqlite3.connect(f"file:{HUB_DB.as_posix()}?mode=ro", uri=True)
    tiers = tuple(args.tiers)
    hub = [norm_domain(r[0]) for r in con.execute(
        f"SELECT domain FROM stations WHERE verification_tier IN ({','.join('?' * len(tiers))})", tiers)]
    con.close()
    seeds = set(read_seeds())
    todo = sorted({d for d in hub if d and d not in seeds})
    print(f"discover: probing {len(todo)} hub domains not yet in seeds (tiers={','.join(tiers)})", flush=True)
    RAW.mkdir(parents=True, exist_ok=True)
    found, reasons = [], {}
    run_at = now_iso()
    t0 = time.time()
    with nowait_pool(args.workers) as ex:
        futs = {ex.submit(fetch_one, d, ua, args.timeout): d for d in todo}
        for i, fut in enumerate(safe_completed(futs, args.deadline), 1):
            d = futs[fut]
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                res = {"domain": d, "pricing": None, "pricing_error": type(e).__name__}
            if res.get("pricing") is not None:
                found.append(d)
                snap = {
                    "domain": d, "fetched_at": res["fetched_at"], "run_at": run_at,
                    "status": _slim_status(res.get("status")),
                    "pricing": {k: res["pricing"].get(k) for k in ("data", "group_ratio", "usable_group") if k in res["pricing"]},
                    "system": res.get("system"),
                    "pricing_url": res.get("pricing_url"),
                }
                with gzip.open(RAW / f"{d}.json.gz", "wt", encoding="utf-8") as fh:
                    json.dump(snap, fh, ensure_ascii=False, separators=(",", ":"))
            else:
                k = res.get("pricing_error") or f"HTTP {res.get('pricing_http')}"
                reasons[k] = reasons.get(k, 0) + 1
            if i % 100 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}  found={len(found)}  {time.time() - t0:.0f}s", flush=True)
    if found:
        with SEEDS.open("a", encoding="utf-8") as fh:
            fh.write(f"# --- discover {run_at} ---\n" + "\n".join(sorted(found)) + "\n")
    (DATA / "discover-last.json").write_text(json.dumps({"run_at": run_at, "probed": len(todo), "found": sorted(found), "fail_reasons": reasons}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"discover done: {len(found)} new stations with public pricing; fail reasons: {sorted(reasons.items(), key=lambda x: -x[1])[:8]}")


def cmd_adopt_alt(args: argparse.Namespace) -> None:
    """Try the alternative pricing URLs found by tools/alt_probe.py; keep the ones an adapter can parse."""
    cfg = load_config()
    ua = cfg.get("user_agent") or "Mozilla/5.0 (compatible; RelayPriceBot/0.1)"
    res = json.loads((DATA / "alt-probe.json").read_text(encoding="utf-8"))
    cands: list[tuple[str, str]] = []
    for r in res:
        urls = [f["url"] for f in r.get("found") or [] if any(f["url"].endswith(p) for p in ("/api/pricing", "/api/public/pricing"))]
        for u in urls:
            cands.append((r["domain"], u))
    print(f"adopt-alt: testing {len(cands)} candidate URLs", flush=True)
    endpoints = load_endpoints()
    seeds = set(read_seeds())
    adopted: dict[str, str] = {}
    systems: dict[str, int] = {}
    with nowait_pool(args.workers) as ex:
        futs = {ex.submit(fetch_one, d, ua, args.timeout, u): (d, u) for d, u in cands}
        for fut in safe_completed(futs, args.deadline):
            d, u = futs[fut]
            try:
                out = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if out.get("pricing") is None or d in adopted:
                continue
            adopted[d] = u
            systems[out.get("system") or "?"] = systems.get(out.get("system") or "?", 0) + 1
            snap = {"domain": d, "fetched_at": out["fetched_at"], "run_at": now_iso(), "status": _slim_status(out.get("status")),
                    "pricing": out["pricing"], "system": out.get("system"), "pricing_url": u}
            with gzip.open(RAW / f"{d}.json.gz", "wt", encoding="utf-8") as fh:
                json.dump(snap, fh, ensure_ascii=False, separators=(",", ":"))
    for d, u in adopted.items():
        if u != f"https://{d}/api/pricing":
            endpoints[d] = u
    ENDPOINTS.write_text(json.dumps(endpoints, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    new = sorted(d for d in adopted if d not in seeds)
    if new:
        with SEEDS.open("a", encoding="utf-8") as fh:
            fh.write(f"# --- adopt-alt {now_iso()} ---\n" + "\n".join(new) + "\n")
    print(f"adopt-alt: adopted {len(adopted)} ({len(new)} new seeds), endpoint overrides {len(endpoints)}; systems {systems}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed-from-hub")
    s.add_argument("--tiers", nargs="+", default=["tier_a_pricing", "tier_b1_gateway", "legacy_catalog"])
    s.set_defaults(func=cmd_seed_from_hub)
    f = sub.add_parser("fetch")
    f.add_argument("--only", nargs="*")
    f.add_argument("--workers", type=int, default=24)
    f.add_argument("--timeout", type=float, default=10.0)
    f.set_defaults(func=cmd_fetch)
    al = sub.add_parser("adopt-alt")
    al.add_argument("--workers", type=int, default=24)
    al.add_argument("--timeout", type=float, default=15.0)
    al.set_defaults(func=cmd_adopt_alt)
    dsc = sub.add_parser("discover")
    dsc.add_argument("--tiers", nargs="+", default=["directory_unprobed", "tier_b2_web", "web_live", "models_json", "pricing_json", "unreachable"])
    dsc.add_argument("--workers", type=int, default=32)
    dsc.add_argument("--timeout", type=float, default=8.0)
    dsc.set_defaults(func=cmd_discover)
    for sp in (f, dsc, al):
        sp.add_argument("--deadline", type=float, default=900.0, help="overall seconds before abandoning unfinished sites")
    args = ap.parse_args()
    args.func(args)
    sys.stdout.flush()
    os._exit(0)  # do not wait for abandoned worker threads


if __name__ == "__main__":
    main()
