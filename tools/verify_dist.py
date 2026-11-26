"""Static checks on the generated dist/ and an optional HTTP smoke test of a local preview.

  python tools/verify_dist.py
  python tools/verify_dist.py --http http://127.0.0.1:8790
Exit code 1 when any check fails.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
TAGS = ("table", "thead", "tbody", "tr", "form", "pre", "details")
SMOKE = {  # path -> text that must appear
    "/": "API",
    "/scenes/": "Claude Code",
    "/scenes/claude-code/": "ANTHROPIC_BASE_URL",
    "/scenes/deepseek/": "deepseek",
    "/scenes/codex-gpt/": "OpenAI",
    "/scenes/gemini-image/": "Gemini",
    "/submit/": 'id="sub-form"',
    "/stations/": "<table",
    "/model/gpt-6-astra/": "gpt-6-astra",
    "/about/": "利益披露",
}


def _check_one(f: Path) -> tuple[list[str], int, str | None]:
    errs: list[str] = []
    html = f.read_text(encoding="utf-8")
    rel = f.relative_to(DIST).as_posix()
    ni: str | None = None
    if 'content="noindex' in html:
        ni = "/" + rel[:-10] if rel.endswith("index.html") else "/" + rel
    n_ld = 0
    for block in LD_RE.findall(html):
        n_ld += 1
        try:
            obj = json.loads(block)
            if not isinstance(obj, dict) or "@type" not in obj or "@context" not in obj:
                errs.append(f"{rel}: JSON-LD without @context/@type")
        except ValueError as e:
            errs.append(f"{rel}: JSON-LD invalid ({e})")
    for t in TAGS:
        o = len(re.findall(rf"<{t}[\s>]", html))
        c = html.count(f"</{t}>")
        if o != c:
            errs.append(f"{rel}: <{t}> opened {o} closed {c}")
    if 'href="/submit/"' not in html:
        errs.append(f"{rel}: no link to /submit/")
    return errs, n_ld, ni


def check_files() -> list[str]:
    errs: list[str] = []
    pages = list(DIST.rglob("*.html"))
    n_ld = 0
    noindex_paths: set[str] = set()
    with ThreadPoolExecutor(max_workers=12) as pool:
        for e_list, cnt, ni in pool.map(_check_one, pages):
            errs.extend(e_list)
            n_ld += cnt
            if ni:
                noindex_paths.add(ni)
    try:
        locs = [e.text or "" for e in ET.parse(DIST / "sitemap.xml").getroot().iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
        for req in ("/submit/", "/scenes/", "/scenes/claude-code/"):
            if not any(x.endswith(req) for x in locs):
                errs.append(f"sitemap.xml: {req} missing")
        if any("example.com" in x for x in locs):
            errs.append("sitemap.xml: placeholder example.com still present")
        for x in locs:
            p = unquote(urlsplit(x).path)
            if p in noindex_paths:
                errs.append(f"sitemap.xml: noindex page leaked ({p})")
    except (ET.ParseError, OSError) as e:
        errs.append(f"sitemap.xml: {e}")
        locs = []
    if "/submit/" not in (DIST / "llms.txt").read_text(encoding="utf-8"):
        errs.append("llms.txt: /submit/ missing")
    if not (DIST / "GITHUB_README.md").exists():
        errs.append("GITHUB_README.md missing")
    print(f"files: {len(pages)} html pages ({len(noindex_paths)} noindex), {n_ld} JSON-LD blocks, {len(locs)} sitemap URLs, {len(errs)} problems")
    return errs


def check_http(base: str) -> list[str]:
    errs = []
    for path, needle in SMOKE.items():
        try:
            with urllib.request.urlopen(base.rstrip("/") + path, timeout=10) as r:
                body = r.read().decode("utf-8", "replace")
                ok = r.status == 200 and needle in body
                print(f"  {r.status} {path} {len(body)} bytes {'ok' if ok else 'MISSING ' + needle}")
                if not ok:
                    errs.append(f"http {path}: status {r.status} or content missing")
        except Exception as e:  # noqa: BLE001
            errs.append(f"http {path}: {type(e).__name__} {e}")
            print(f"  ERR {path} {e}")
    return errs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", default="", help="base URL of a running preview")
    args = ap.parse_args()
    errs = check_files()
    if args.http:
        errs += check_http(args.http)
    for e in errs[:40]:
        print("FAIL", e)
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
