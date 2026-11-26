"""Probe mined candidates (data/mine-candidates.json) for public pricing and adopt the ones that parse.

Read-only GETs via refresh.fetch_one (same adapters as the daily refresh). For each candidate:
  1. https://<dom>/api/pricing
  2. if the host answered at all: /api/public/pricing, api./www. sub-hosts, http://, and the public
     server_address from /api/status (never localhost/private addresses)
  3. if the host did not answer: only https://api.<root>/api/pricing
Adopted stations must yield at least MIN_QUOTES priced quotes after normalization.
Writes data/raw/<dom>.json.gz, seeds.txt, data/endpoints.json, data/mine-sources.json, data/mine-last.json.
"""
from __future__ import annotations

import argparse
import gzip
import ipaddress
import json
import time
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any

import mine_extract as X
import pricing as P
import refresh as R

DATA = R.DATA
CANDS = DATA / "mine-candidates.json"
SOURCES = DATA / "mine-sources.json"
LAST = DATA / "mine-last.json"
PROGRESS = DATA / "mine-probe-progress.jsonl"  # one line per finished candidate; lets an interrupted run resume
MIN_QUOTES = 3


def public_origin(url: Any) -> str | None:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    sp = urllib.parse.urlsplit(url.strip())
    h = (sp.hostname or "").lower()
    if not h or h == "localhost" or h.endswith((".local", ".lan", ".internal")):
        return None
    try:
        ip = ipaddress.ip_address(h)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return None
    except ValueError:
        pass
    return f"{sp.scheme}://{sp.netloc}"


def alt_urls(dom: str, first: dict[str, Any]) -> list[str]:
    root = X.root_of(dom)
    alive = first.get("status_http") is not None or first.get("pricing_http") is not None
    if not alive:
        return [f"https://api.{root}/api/pricing"] if not dom.startswith("api.") else []
    out = []
    hint = public_origin((first.get("status") or {}).get("server_address"))
    if hint and urllib.parse.urlsplit(hint).hostname not in (dom, "www." + dom):
        out.append(hint + "/api/pricing")
    out.append(f"https://{dom}/api/public/pricing")
    for h in (f"api.{root}", f"www.{root}", root):
        if h != dom:
            out.append(f"https://{h}/api/pricing")
    out.append(f"http://{dom}/api/pricing")
    return list(dict.fromkeys(out))


def priced_quotes(dom: str, res: dict[str, Any]) -> int:
    try:
        qs, _ = P.normalize_station(dom, res["pricing"], res.get("status"))
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for q in qs if (q.input_usd or 0) > 0 or (q.call_usd or 0) > 0 or (q.input_cny or 0) > 0 or (q.call_cny or 0) > 0)


def reason_of(res: dict[str, Any]) -> str:
    code = res.get("pricing_http")
    err = res.get("pricing_error") or res.get("status_error") or ""
    if code in (401, 403):
        return "接口拦截（401/403，可能为 WAF 或关闭匿名价格）"
    if code == 404:
        return "无 /api/pricing（404，非 New API 系或已改路径）"
    if err == "未支持的价格格式":
        return "返回了 JSON 但格式未支持或计价单位不明"
    if err == "非 JSON 响应":
        return "返回 200 但不是价格 JSON（多为网页）"
    if err == "价格表为空":
        return "价格接口可读但没有任何型号"
    if code == 200:
        return "返回 200 但不是价格 JSON（多为网页或空数据）"
    if code is not None:
        return f"HTTP {code}"
    if "Timeout" in err or "超时" in err:
        return "超时"
    return "无法连接（DNS/TLS/拒绝连接）"


def probe(dom: str, ua: str, timeout: float) -> dict[str, Any]:
    first = R.fetch_one(dom, ua, timeout)
    tried = [first.get("pricing_url")]
    best = first
    if first.get("pricing") is None:
        for u in alt_urls(dom, first):
            r = R.fetch_one(dom, ua, timeout, u)
            tried.append(u)
            if r.get("pricing") is not None:
                best = r
                break
    n = priced_quotes(dom, best) if best.get("pricing") is not None else 0
    return {"domain": dom, "res": best, "first": first, "tried": tried, "quotes": n}


def save_snapshot(dom: str, res: dict[str, Any], run_at: str) -> None:
    snap = {"domain": dom, "fetched_at": res["fetched_at"], "run_at": run_at, "status": R._slim_status(res.get("status")),
            "pricing": {k: res["pricing"].get(k) for k in ("data", "group_ratio", "usable_group") if k in res["pricing"]},
            "system": res.get("system"), "pricing_url": res.get("pricing_url")}
    R.RAW.mkdir(parents=True, exist_ok=True)
    with gzip.open(R.RAW / f"{dom}.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, separators=(",", ":"))


def load_progress(mined_at: str) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Results already recorded for this candidate set (same mined_at)."""
    adopted: dict[str, dict[str, Any]] = {}
    rejected: dict[str, dict[str, Any]] = {}
    if PROGRESS.exists():
        for line in PROGRESS.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("mined_at") != mined_at:
                continue
            if r.get("adopted"):
                adopted[r["domain"]] = r["adopted"]
                rejected.pop(r["domain"], None)
            else:
                rejected[r["domain"]] = {"domain": r["domain"], "reason": r["reason"]}
    return adopted, list(rejected.values())


def recover_orphans(cands: list[str], seeds: set[str]) -> dict[str, dict[str, Any]]:
    """Snapshots written by an interrupted probe (in data/raw but not yet in seeds.txt)."""
    out = {}
    for d in cands:
        f = R.RAW / f"{d}.json.gz"
        if d in seeds or not f.exists():
            continue
        try:
            snap = json.load(gzip.open(f, "rt", encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        n = priced_quotes(d, {"pricing": snap.get("pricing") or {}, "status": snap.get("status")})
        if n >= MIN_QUOTES:
            out[d] = {"pricing_url": snap.get("pricing_url") or f"https://{d}/api/pricing", "system": snap.get("system"), "quotes": n}
    return out


def record(fh, mined_at: str, domain: str, adopted: dict[str, Any] | None = None, reason: str | None = None) -> None:
    fh.write(json.dumps({"mined_at": mined_at, "domain": domain, "adopted": adopted, "reason": reason}, ensure_ascii=False) + "\n")
    fh.flush()


def cmd_probe(args: argparse.Namespace) -> None:
    ua = R.load_config().get("user_agent") or "Mozilla/5.0 (compatible; RelayPriceBot/0.1)"
    mined = json.loads(CANDS.read_text(encoding="utf-8"))
    seeds = set(R.read_seeds())
    all_cands = [c["domain"] for c in mined["items"]]
    adopted, rejected = load_progress(mined["mined_at"])
    orphans = recover_orphans(all_cands, seeds)
    adopted.update(orphans)
    done = set(adopted) | {r["domain"] for r in rejected}
    listed = seeds | {f.name[:-8] for f in R.RAW.glob("*.json.gz")}
    by_dom = {c["domain"]: c for c in mined["items"] if c["domain"] not in seeds or c["domain"] in adopted}
    items = [c for c in mined["items"] if c["domain"] not in listed and c["domain"] not in done]
    if args.limit:
        items = items[: args.limit]
    print(f"probe: {len(items)} to probe; resumed {len(adopted)} adopted ({len(orphans)} recovered from data/raw), "
          f"{len(rejected)} rejected (deadline {args.deadline:.0f}s)", flush=True)
    run_at = R.now_iso()
    t0 = time.time()
    with PROGRESS.open("a", encoding="utf-8") as fh, R.nowait_pool(args.workers) as ex:
        for d, a in orphans.items():
            record(fh, mined["mined_at"], d, adopted=a)
        futs = {ex.submit(probe, c["domain"], ua, args.timeout): c["domain"] for c in items}
        for i, fut in enumerate(R.safe_completed(futs, args.deadline), 1):
            d = futs[fut]
            try:
                p = fut.result()
            except Exception as e:  # noqa: BLE001
                reason = "整体时限内未完成" if isinstance(e, TimeoutError) else type(e).__name__
                rejected.append({"domain": d, "reason": reason})
                record(fh, mined["mined_at"], d, reason=reason)
                continue
            res = p["res"]
            if res.get("pricing") is not None and p["quotes"] >= MIN_QUOTES:
                save_snapshot(d, res, run_at)
                adopted[d] = {"pricing_url": res.get("pricing_url"), "system": res.get("system"), "quotes": p["quotes"]}
                record(fh, mined["mined_at"], d, adopted=adopted[d])
            elif res.get("pricing") is not None:
                rejected.append({"domain": d, "reason": f"价格接口可读但有效报价少于 {MIN_QUOTES} 条"})
                record(fh, mined["mined_at"], d, reason=rejected[-1]["reason"])
            else:
                rejected.append({"domain": d, "reason": reason_of(p["first"])})
                record(fh, mined["mined_at"], d, reason=rejected[-1]["reason"])
            if i % 50 == 0 or i == len(futs):
                print(f"  {i}/{len(futs)} adopted={len(adopted)} {time.time() - t0:.0f}s", flush=True)
    write_results(mined, by_dom, adopted, rejected, run_at, time.time() - t0)


def write_results(mined, by_dom, adopted, rejected, run_at: str, elapsed: float) -> None:
    endpoints = R.load_endpoints()
    for d, a in adopted.items():
        if a["pricing_url"] != f"https://{d}/api/pricing":
            endpoints[d] = a["pricing_url"]
    R.ENDPOINTS.write_text(json.dumps(endpoints, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    new_seeds = sorted(set(adopted) - set(R.read_seeds()))  # idempotent: a resumed run never duplicates seeds
    if new_seeds:
        with R.SEEDS.open("a", encoding="utf-8") as fh:
            fh.write(f"# --- mine_sources {run_at} ---\n" + "\n".join(new_seeds) + "\n")
    # discovery sources per mined domain (all candidates, so intel.py can count independent listings)
    srcs = json.loads(SOURCES.read_text(encoding="utf-8")) if SOURCES.exists() else {}
    hits = [(c["domain"], c["sources"]) for c in mined["items"]] + list((mined.get("listed_hits") or {}).items())
    for dom, sources in hits:
        e = srcs.setdefault(dom, {})
        for kind, labs in sources.items():
            e[kind] = sorted(set(e.get(kind, [])) | set(labs))
        e["last_mined_at"] = mined["mined_at"]
    SOURCES.write_text(json.dumps(srcs, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    def origin(d: str) -> str:
        s = by_dom.get(d, {}).get("sources", {})
        return "both" if len(s) > 1 else next(iter(s), "?")

    src_count: Counter[str] = Counter()
    for c in by_dom.values():
        for labs in c["sources"].values():
            src_count.update(labs)
    report = {
        "run_at": run_at, "elapsed_s": round(elapsed), "candidates": len(by_dom), "adopted": len(adopted),
        "adopted_by_origin": dict(Counter(origin(d) for d in adopted)),
        "candidates_by_origin": dict(Counter(origin(d) for d in by_dom)),
        "adopted_in_hub_before": sum(1 for d in adopted if by_dom.get(d, {}).get("in_hub")),
        "systems": dict(Counter(a["system"] for a in adopted.values())),
        "alt_endpoints": sum(1 for d, a in adopted.items() if a["pricing_url"] != f"https://{d}/api/pricing"),
        "reject_reasons": dict(Counter(r["reason"] for r in rejected).most_common()),
        "top_sources_by_candidates": src_count.most_common(25),
        "adopted_sources": {d: by_dom.get(d, {}).get("sources", {}) for d in sorted(adopted)},
        "adopted_detail": adopted,
        "rejected": sorted(rejected, key=lambda r: r["domain"]),
    }
    LAST.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"probe done: adopted {len(adopted)} of {len(by_dom)}; systems {report['systems']}; "
          f"reasons {list(report['reject_reasons'].items())[:6]}")
