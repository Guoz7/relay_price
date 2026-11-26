"""Measure which alternative public endpoints work for stations whose /api/pricing failed.

Read-only GETs only. No login, no cookies, no challenge solving, no cached/archived copies.
Output: data/alt-probe.json
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import refresh as R  # noqa: E402

HUB = ROOT.parent / "priceai-research" / "data" / "relay-hub.sqlite3"
UA = "Mozilla/5.0 (compatible; RelayPriceBot/0.1)"
CTX = ssl.create_default_context()

PATHS = [
    "/api/pricing",
    "/api/available_model",      # one-hub / done-hub
    "/api/prices",
    "/api/model/pricing",
    "/api/v1/pricing",
    "/api/public/pricing",
    "/api/models",
    "/v1/models",
]


def get(url: str, timeout: float = 7.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.geturl(), r.read(3_000_000), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, url, b"", ""
    except Exception as e:  # noqa: BLE001
        return None, url, b"", type(e).__name__


def shape(body: bytes) -> str | None:
    try:
        j = json.loads(body.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None
    if isinstance(j, dict):
        d = j.get("data")
        if isinstance(d, list) and d and isinstance(d[0], dict):
            return "list:" + ",".join(sorted(d[0].keys())[:8])
        if isinstance(d, dict):
            return "dict:" + ",".join(sorted(d.keys())[:8])
        return "obj:" + ",".join(sorted(j.keys())[:8])
    if isinstance(j, list):
        return f"array:{len(j)}"
    return None


def hosts_for(d: str) -> list[str]:
    parts = d.split(".")
    out = [d]
    root = ".".join(parts[-2:]) if len(parts) >= 2 else d
    if d.startswith("www."):
        out.append(d[4:])
    if d == root:
        out += [f"api.{root}", f"www.{root}"]
    elif d != root and not d.startswith("api."):
        out.append(f"api.{root}")
    return list(dict.fromkeys(out))


def probe(d: str) -> dict:
    found = []
    # 1) same host, alternative paths (https)
    for p in PATHS:
        code, final, body, _ = get(f"https://{d}{p}")
        sh = shape(body) if code == 200 and body else None
        if sh:
            found.append({"url": f"https://{d}{p}", "final": final, "shape": sh})
            if p == "/api/pricing":
                break
    # 2) /api/status server_address hint
    code, _, body, _ = get(f"https://{d}/api/status")
    hint = None
    if code == 200 and body:
        try:
            j = json.loads(body)
            j = j.get("data", j)
            hint = j.get("server_address") if isinstance(j, dict) else None
        except Exception:  # noqa: BLE001
            pass
    # 3) other hosts + http for /api/pricing
    if not any(f["url"].endswith("/api/pricing") for f in found):
        cands = [f"https://{h}/api/pricing" for h in hosts_for(d)[1:]] + [f"http://{d}/api/pricing"]
        if hint and isinstance(hint, str) and hint.startswith("http"):
            cands.insert(0, hint.rstrip("/") + "/api/pricing")
        for u in cands:
            code, final, body, _ = get(u)
            sh = shape(body) if code == 200 and body else None
            if sh:
                found.append({"url": u, "final": final, "shape": sh})
                break
    return {"domain": d, "found": found, "server_address": hint}


def main() -> None:
    have = {f.name[:-8] for f in (ROOT / "data" / "raw").glob("*.json.gz")}
    con = sqlite3.connect(f"file:{HUB.as_posix()}?mode=ro", uri=True)
    hub = {R.norm_domain(r[0]) for r in con.execute("SELECT domain FROM stations WHERE verification_tier != 'filtered_non_relay'")}
    con.close()
    todo = sorted((hub | set(R.read_seeds())) - have - {""})
    print("alt-probe", len(todo), "domains", flush=True)
    res = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=40) as ex:
        futs = [ex.submit(probe, d) for d in todo]
        for i, f in enumerate(as_completed(futs), 1):
            res.append(f.result())
            if i % 100 == 0:
                print(f"  {i}/{len(todo)} {time.time() - t0:.0f}s hits={sum(1 for r in res if r['found'])}", flush=True)
    (ROOT / "data" / "alt-probe.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    hits = [r for r in res if r["found"]]
    print("domains with any JSON hit:", len(hits))
    c = Counter()
    for r in hits:
        for f in r["found"]:
            path = "/" + f["url"].split("/", 3)[3]
            c[(path, f["shape"][:70])] += 1
    for k, v in c.most_common(40):
        print(v, k)
    alt_host = [r for r in hits if any(f["url"].endswith("/api/pricing") and not f["url"].startswith(f"https://{r['domain']}/") for f in r["found"])]
    print("pricing found on other host/scheme:", len(alt_host))


if __name__ == "__main__":
    main()
