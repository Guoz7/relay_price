"""Automated search-engine URL push utility (IndexNow for Bing/cn.bing.com/Yandex + Baidu active push).

Usage:
  python tools/push_urls.py --indexnow                       # push top high-value URLs to IndexNow
  python tools/push_urls.py --baidu --baidu-token TOKEN      # push top URLs to Baidu Search Resource Platform
  python tools/push_urls.py --all --dry-run                  # inspect selected URLs without network calls
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
CONFIG = ROOT / "config.json"


def select_priority_urls(sitemap_path: Path, limit: int = 200) -> list[str]:
    """Read sitemap.xml ordered by highest priority first (already sorted in build_seo_files)."""
    if not sitemap_path.exists():
        return []
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    root = ET.parse(sitemap_path).getroot()
    scored: list[tuple[float, str]] = []
    for u in root.findall(f"{ns}url"):
        loc = (u.findtext(f"{ns}loc") or "").strip()
        if not loc:
            continue
        try:
            pr = float(u.findtext(f"{ns}priority") or "0.5")
        except ValueError:
            pr = 0.5
        scored.append((pr, loc))
    scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    return [loc for _, loc in scored[:limit]]


def push_indexnow(base_url: str, key: str, urls: list[str], *, dry_run: bool = False) -> dict[str, Any]:
    host = urlsplit(base_url).netloc
    payload = {
        "host": host,
        "key": key,
        "keyLocation": f"{base_url.rstrip('/')}/{key}.txt",
        "urlList": urls,
    }
    if dry_run:
        return {"engine": "indexnow", "dry_run": True, "count": len(urls), "sample": urls[:5]}
    req = urllib.request.Request(
        "https://api.indexnow.org/indexnow",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "RelayPricePush/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return {"engine": "indexnow", "ok": resp.status in (200, 202), "status": resp.status, "count": len(urls)}
    except urllib.error.HTTPError as e:
        return {"engine": "indexnow", "ok": False, "status": e.code, "body": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:  # noqa: BLE001
        return {"engine": "indexnow", "ok": False, "error": f"{type(e).__name__}: {e}"}


def push_baidu(base_url: str, token: str, urls: list[str], *, dry_run: bool = False) -> dict[str, Any]:
    site = base_url.rstrip("/")
    if dry_run:
        return {"engine": "baidu", "dry_run": True, "count": len(urls), "sample": urls[:5]}
    endpoint = f"http://data.zz.baidu.com/urls?site={site}&token={token}"
    req = urllib.request.Request(
        endpoint,
        data=("\n".join(urls) + "\n").encode("utf-8"),
        headers={"Content-Type": "text/plain", "User-Agent": "RelayPricePush/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = {"raw": body[:200]}
            return {"engine": "baidu", "ok": resp.status == 200, "status": resp.status, "resp": parsed}
    except urllib.error.HTTPError as e:
        return {"engine": "baidu", "ok": False, "status": e.code, "body": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:  # noqa: BLE001
        return {"engine": "baidu", "ok": False, "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    ap = argparse.ArgumentParser(description="Push high-priority URLs to IndexNow (Bing/cn.bing.com) & Baidu")
    ap.add_argument("--indexnow", action="store_true")
    ap.add_argument("--baidu", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--baidu-token", default=os.environ.get("BAIDU_PUSH_TOKEN", ""))
    ap.add_argument("--limit", type=int, default=100, help="Max URLs per push (Baidu free daily quota is often 10-100)")
    ap.add_argument("--indexnow-limit", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    base_url = cfg["base_url"].rstrip("/")
    results = []
    if args.indexnow or args.all:
        key = str(cfg.get("indexnow_key") or "").strip()
        urls = select_priority_urls(DIST / "sitemap.xml", limit=args.indexnow_limit)
        if key and urls:
            results.append(push_indexnow(base_url, key, urls, dry_run=args.dry_run))
        else:
            results.append({"engine": "indexnow", "skipped": True, "reason": "missing indexnow_key or sitemap.xml"})
    if args.baidu or args.all:
        tok = (args.baidu_token or "").strip()
        urls = select_priority_urls(DIST / "sitemap.xml", limit=args.limit)
        if tok and urls:
            results.append(push_baidu(base_url, tok, urls, dry_run=args.dry_run))
        else:
            results.append({"engine": "baidu", "skipped": True, "reason": "BAIDU_PUSH_TOKEN not configured or sitemap.xml missing"})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
