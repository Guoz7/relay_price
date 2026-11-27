"""Collect slow-changing trust signals per station -> data/intel.json (cached, refreshed weekly).

Signals (all public, read-only):
- domain age: ICANN RDAP via rdap.org, or CNNIC WHOIS for .cn (dates/registrar only)
- traffic rank presence: Tranco API, local Cisco Umbrella / Majestic Million CSVs
- independent discovery sources: how many aggregators / communities / forums list the station
  (from priceai-research relay-hub DB, plus directory listings found by tools/mine_sources.py;
   links from other relay stations are kept as `linked_from` and do not count as independent listings)

Usage:
  python tools/intel.py            # update entries older than 7 days
  python tools/intel.py --force    # re-check everything
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
OUT = DATA / "intel.json"
HUB_TOOLS = ROOT.parent / "priceai-research" / "tools"
HUB_DB = ROOT.parent / "priceai-research" / "data" / "relay-hub.sqlite3"
sys.path.insert(0, str(HUB_TOOLS))

import relay_intel  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psl  # noqa: E402

MAX_AGE = timedelta(days=7)
MINE_SOURCES = DATA / "mine-sources.json"


def mined_sources(domains: list[str]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(directory labels, linking stations) per domain from data/mine-sources.json, matched by domain or root."""
    if not MINE_SOURCES.exists():
        return {}, {}
    raw = json.loads(MINE_SOURCES.read_text(encoding="utf-8"))
    by_root_dir: dict[str, set[str]] = {}
    by_root_link: dict[str, set[str]] = {}
    for dom, e in raw.items():
        r = psl.registrable_root(dom)
        by_root_dir.setdefault(r, set()).update(e.get("directory", []))
        by_root_link.setdefault(r, set()).update(e.get("station_link", []))
    dirs = {d: by_root_dir.get(psl.registrable_root(d), set()) for d in domains}
    links = {d: by_root_link.get(psl.registrable_root(d), set()) for d in domains}
    return dirs, links


def hub_sources(domains: list[str]) -> dict[str, list[str]]:
    """Distinct independent discovery sources per domain (also via same root domain)."""
    out: dict[str, set[str]] = {d: set() for d in domains}
    if not HUB_DB.exists():
        return {}
    con = sqlite3.connect(f"file:{HUB_DB.as_posix()}?mode=ro", uri=True)
    rows = con.execute(
        """SELECT s.domain, s.root_domain, COALESCE(NULLIF(src.display_name, ''), ss.source_id), src.category
           FROM station_sources ss JOIN stations s ON s.domain = ss.domain
           LEFT JOIN sources src ON src.source_id = ss.source_id"""
    ).fetchall()
    con.close()
    by_root: dict[str, set[str]] = {}
    by_dom: dict[str, set[str]] = {}
    for dom, _hub_root, sid, cat in rows:
        if (cat or "") == "direct_probe" or str(sid).startswith("manual"):
            continue
        by_dom.setdefault(dom, set()).add(sid)
        by_root.setdefault(psl.registrable_root(dom), set()).add(sid)
    for d in domains:
        out[d] |= by_dom.get(d, set()) | by_root.get(psl.registrable_root(d), set())
    return {d: sorted(v) for d, v in out.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-network", action="store_true", help="only refresh local signals (sources, local rank CSVs)")
    ap.add_argument("--deadline", type=float, default=900.0, help="overall seconds for network lookups")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    intel: dict[str, Any] = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    domains = sorted(f.name[: -len(".json.gz")] for f in RAW.glob("*.json.gz"))
    roots = {d: psl.registrable_root(d) for d in domains}
    shared = {d for d in domains if psl.shared_host_of(d)}

    srcs = hub_sources(domains)
    mdirs, mlinks = mined_sources(domains)
    umb, maj = relay_intel.scan_local_rank_csvs(set(domains) | set(roots.values()))
    for d in domains:
        e = intel[d] = psl.sanitize_intel_entry(d, intel.get(d) or {})
        e["root"] = roots[d]
        e["sources"] = sorted(set(srcs.get(d, [])) | mdirs.get(d, set()))
        if mlinks.get(d):
            e["linked_from"] = sorted(x.removeprefix("station:") for x in mlinks[d])
        if d in shared:
            continue
        r = roots[d]
        e["umbrella_rank"] = umb.get(d) or umb.get(r)
        e["majestic_rank"] = (maj.get(d) or maj.get(r) or (None,))[0]

    if not args.no_network:
        todo_roots = sorted(
            {
                roots[d] for d in domains
                if d not in shared and (args.force or not intel[d].get("net_checked_at")
                or now - datetime.fromisoformat(intel[d]["net_checked_at"]) > MAX_AGE)
            }
        )
        print(f"network intel for {len(todo_roots)} root domains (rdap + tranco, throttled)…", flush=True)
        results: dict[str, dict[str, Any]] = {}
        t0 = time.time()

        def work(root: str) -> tuple[str, dict[str, Any]]:
            age = relay_intel.fetch_domain_age(root, now)
            tr = relay_intel.fetch_tranco(root)
            return root, {**age, "tranco_rank": tr.get("tranco_rank")}

        ex = ThreadPoolExecutor(max_workers=6)
        futs = [ex.submit(work, r) for r in todo_roots]
        try:
            for i, f in enumerate(as_completed(futs, timeout=args.deadline), 1):
                try:
                    r, res = f.result()
                    results[r] = res
                except Exception as e:  # noqa: BLE001
                    print("  err", type(e).__name__)
                if i % 20 == 0 or i == len(todo_roots):
                    print(f"  {i}/{len(todo_roots)}  {time.time() - t0:.0f}s", flush=True)
        except FuturesTimeout:
            print(f"  deadline reached; {len(todo_roots) - len(results)} roots left for next run", flush=True)
        ex.shutdown(wait=False, cancel_futures=True)
        stamp = now.replace(microsecond=0).isoformat()
        for d in domains:
            res = results.get(roots[d])
            if res is None or d in shared:
                continue
            e = intel[d]
            e.update({k: res.get(k) for k in ("domain_registered_at", "domain_expires_at", "domain_age_days", "registrar", "tranco_rank")})
            e["net_checked_at"] = stamp

    intel = psl.sanitize_intel(intel)
    OUT.write_text(json.dumps(intel, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    have_age = sum(1 for d in domains if intel[d].get("domain_age_days") is not None)
    have_rank = sum(1 for d in domains if any(intel[d].get(k) for k in ("tranco_rank", "umbrella_rank", "majestic_rank")))
    print(f"intel: {len(domains)} domains, age known {have_age}, any traffic rank {have_rank}, avg sources {sum(len(intel[d]['sources']) for d in domains) / max(1, len(domains)):.2f}")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)  # do not wait for abandoned lookups
