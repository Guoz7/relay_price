"""Read-only source mining: find relay domains we do not list yet, then probe them for public pricing.

Two public, read-only expansion paths (plain GET only: no login, no keys, no challenge solving, no archives):
  A. known stations: links in their public /api/status, /api/notice, /api/about, /api/home_page_content
  B. directories: public navigation sites, GitHub Awesome lists and price aggregators
     (sources table of priceai-research relay-hub DB, read-only, plus a curated list of newer GitHub lists)

Usage:
  python tools/mine_sources.py mine  [--skip-a] [--skip-b] [--github-search] [--deadline 600]
  python tools/mine_sources.py probe [--deadline 600]      # see tools/mine_probe.py
  python tools/mine_sources.py all   [--github-search]
Outputs: data/mine-candidates.json (mine), data/mine-sources.json + data/mine-last.json (probe)
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mine_extract as X  # noqa: E402
import refresh as R  # noqa: E402

DATA = R.DATA
CANDS = DATA / "mine-candidates.json"
CACHE_A = DATA / "mine-cache-a.json"  # last station-link mining result, reused with --reuse-a
STATION_PATHS = ("/api/status", "/api/notice", "/api/about", "/api/home_page_content")
GITHUB_LISTS = [  # newer public GitHub navigation / awesome lists (checked 2026-11-27), in addition to the hub DB
    "boyan0928/ai-welfare-hub", "wynx1123/ai-welfare-hub", "panxunying/ai-coding-welfare", "Q-shuang-dot/ai-coding-welfare",
    "1sh1ro/ai-api-zhongzhuan", "jliushi/ai-relay", "tokenforfree/free-llm", "liumowan/Free-API", "findbestapi/ai-member-prices",
    "apizhongzhuanzhan/apizhongzhuanzhan.github.io", "Leo-sail/bullrouteAPI", "hanzohanzhe/Grandet", "Joss-fml/awesome-ai-api-relay",
    "deverzh/ai-api-proxy-list", "LiaBrio/ai-api-hub", "xxx-x-x/OmwAI_Lab", "ZerouterAI/ai-zhongzhuan",
    "yangqi1309134997-coder/free-model-links", "cjmarklll/token-harbor", "gpt5free/free2ai-relay-065", "kirito8/free-newapi",
]
GITHUB_QUERIES = ["中转站 api", "公益站 new-api", "api中转 导航", "ai api relay list", "awesome api proxy"]
SEARCH_KEEP = re.compile(r"中转|公益|导航|比价|relay|proxy list|api hub|zhongzhuan", re.I)
SEARCH_DROP = re.compile(r"email|smtp|scrap|vpn|梯子|机场|节点|self-host|自托管|插件|plugin|签到|check-?in|dashboard|管理|webui|cli", re.I)
TEXT_EXT = (".md", ".json", ".yml", ".yaml", ".csv", ".txt", ".html", ".js", ".ts", ".tsx", ".astro", ".toml")


def get(url: str, ua: str, timeout: float = 8.0, cap: int = 3_000_000, accept: str = "*/*") -> tuple[int | None, str]:
    """Plain GET with a hard download deadline; never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": accept})
    try:
        deadline = time.time() + timeout * 3
        with urllib.request.urlopen(req, timeout=timeout) as r:
            chunks, size = [], 0
            while time.time() < deadline:
                b = r.read(65536)
                if not b:
                    break
                chunks.append(b)
                size += len(b)
                if size > cap:
                    break
            return r.status, b"".join(chunks).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return None, ""


def known_domains() -> tuple[set[str], set[str], set[str]]:
    """(listed: seeds + raw snapshots, hub: every hub station, non_relay: hub filtered_non_relay)."""
    listed = set(R.read_seeds()) | {f.name[:-8] for f in R.RAW.glob("*.json.gz")}
    hub, bad = set(), set()
    if R.HUB_DB.exists():
        con = sqlite3.connect(f"file:{R.HUB_DB.as_posix()}?mode=ro", uri=True)
        for d, tier in con.execute("SELECT domain, verification_tier FROM stations"):
            d = R.norm_domain(d or "")
            (bad if tier == "filtered_non_relay" else hub).add(d)
        con.close()
    return listed, hub - {""}, bad - {""}


def add(found: dict[str, set[str]], hosts: set[str], label: str) -> None:
    for h in hosts:
        found.setdefault(h, set()).add(label)


# ---------------------------------------------------------------- A: known stations
def station_text(origin: str, ua: str, timeout: float) -> str:
    parts = []
    for p in STATION_PATHS:
        code, body = get(origin + p, ua, timeout, accept="application/json")
        if code != 200 or not body:
            continue
        try:
            j = json.loads(body)
        except ValueError:
            continue
        d = j.get("data", j) if isinstance(j, dict) else j
        if isinstance(d, dict):
            d = {k: v for k, v in d.items() if k not in ("chats", "HeaderNavModules", "SidebarModulesAdmin")}  # chat-client links
        parts.append(json.dumps(d, ensure_ascii=False) if not isinstance(d, str) else d)
    return "\n".join(parts)


def mine_stations(ua: str, args: argparse.Namespace, block: set[str]) -> tuple[dict[str, set[str]], dict[str, Any]]:
    found: dict[str, set[str]] = {}
    stats = {"stations": 0, "with_text": 0}
    snaps = sorted(R.RAW.glob("*.json.gz"))
    jobs = {}
    for f in snaps:
        d = f.name[:-8]
        try:
            snap = json.load(gzip.open(f, "rt", encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        sp = urllib.parse.urlsplit(snap.get("pricing_url") or f"https://{d}/api/pricing")
        origin = f"{sp.scheme}://{sp.netloc}" if sp.netloc and not X.is_ip(sp.hostname or "") and sp.hostname != "localhost" else f"https://{d}"
        st = snap.get("status") or {}
        offline = " ".join(str(st.get(k) or "") for k in ("server_address", "docs_link", "top_up_link"))
        selfs = {X.root_of(d), X.root_of(X.norm(sp.hostname or d))}
        add(found, X.candidates(offline, self_roots=selfs, bare=False, extra_block=block), f"station:{d}")
        jobs[d] = (origin, selfs)
    stats["stations"] = len(jobs)
    t0 = time.time()
    with R.nowait_pool(args.workers) as ex:
        futs = {ex.submit(station_text, o, ua, args.timeout): d for d, (o, _) in jobs.items()}
        for i, fut in enumerate(R.safe_completed(futs, args.deadline), 1):
            d = futs[fut]
            try:
                text = fut.result()
            except Exception:  # noqa: BLE001
                text = ""
            if text:
                stats["with_text"] += 1
                add(found, X.candidates(text, self_roots=jobs[d][1], extra_block=block), f"station:{d}")
            if i % 100 == 0 or i == len(futs):
                print(f"  A {i}/{len(futs)} hosts={len(found)} {time.time() - t0:.0f}s", flush=True)
    return found, stats


# ---------------------------------------------------------------- B: directories
def hub_source_urls() -> list[tuple[str, str]]:
    """(label, url) for public sources recorded in the hub DB. Search-engine/forum query sources are skipped."""
    out = []
    if not R.HUB_DB.exists():
        return out
    con = sqlite3.connect(f"file:{R.HUB_DB.as_posix()}?mode=ro", uri=True)
    for sid, cat, name, url in con.execute("SELECT source_id, category, display_name, source_url FROM sources"):
        label = name or sid
        if cat in ("tavily_search", "forum_blog", "direct_probe") or str(sid).startswith(("tavily:", "gemini_anti:", "pojia_anti:")):
            continue
        if url and not re.match(r"https?://(www\.)?(zhihu|bilibili)\.com/?$", url) and url.rstrip("/") != "https://github.com":
            out.append((label, url))
        elif not url and re.match(r"^[a-z0-9.-]+\.[a-z]{2,}(/\S*)?$", sid or ""):
            host, _, paths = sid.partition("/")
            for p in (paths.split("+") if paths else [""]):
                out.append((label, f"https://{host}/{p}"))
    con.close()
    return out


def github_search(ua: str) -> list[str]:
    repos = []
    for q in GITHUB_QUERIES:
        code, body = get("https://api.github.com/search/repositories?per_page=15&sort=updated&q=" + urllib.parse.quote(q), ua,
                         12, accept="application/vnd.github+json")
        if code == 200:
            for it in json.loads(body).get("items", []):
                text = f"{it.get('full_name')} {it.get('description') or ''}"
                if it.get("stargazers_count", 0) >= 1 and not it.get("fork") and SEARCH_KEEP.search(text) and not SEARCH_DROP.search(text):
                    repos.append(it["full_name"])
        time.sleep(2.5)  # unauthenticated search limit: 10 requests/minute
    return repos


def repo_files(repo: str, ua: str, timeout: float) -> tuple[str, list[tuple[str, int]]]:
    """(ref, [(path, size)]) of a public repo. jsDelivr's public listing first (no GitHub API quota), then the
    GitHub tree API (60 unauthenticated requests/hour)."""
    for ref in ("main", "master"):
        code, body = get(f"https://data.jsdelivr.com/v1/packages/gh/{repo}@{ref}?structure=flat", ua, timeout, accept="application/json")
        if code == 200:
            try:
                return ref, [(f["name"].lstrip("/"), int(f.get("size") or 0)) for f in json.loads(body).get("files", [])]
            except (ValueError, KeyError, TypeError):
                pass
    code, body = get(f"https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1", ua, timeout, accept="application/vnd.github+json")
    if code == 200:
        try:
            return "HEAD", [(t["path"], int(t.get("size") or 0)) for t in json.loads(body).get("tree", []) if t.get("type") == "blob"]
        except (ValueError, KeyError, TypeError):
            pass
    return "HEAD", []


def repo_texts(repo: str, ua: str, timeout: float, max_files: int = 14) -> list[str]:
    """README plus small data files (json/md/yaml/…) of a public repo, fetched from raw.githubusercontent.com."""
    ref, files = repo_files(repo, ua, timeout)
    files = [(p, s) for p, s in files if s < 1_500_000 and p.lower().endswith(TEXT_EXT)
             and not re.search(r"(^|/)(node_modules|dist|\.github|package(-lock)?\.json|tsconfig)", p)]

    def rank(f: tuple[str, int]) -> tuple:
        p = f[0].lower()
        return (not p.startswith("readme"), not re.search(r"site|station|relay|list|data|nav|api|provider", p), p.count("/"), p)

    paths = [p for p, _ in sorted(files, key=rank)[:max_files]] or ["README.md"]
    out = []
    for p in paths:
        c, b = get(f"https://raw.githubusercontent.com/{repo}/{ref}/{urllib.parse.quote(p)}", ua, timeout)
        if c == 200 and b:
            out.append(b)
    return out


def page_texts(url: str, ua: str, timeout: float) -> list[str]:
    """A public page plus up to 5 same-origin JSON files it references (SPA navigation sites)."""
    code, body = get(url, ua, timeout)
    if code != 200 or not body:
        return []
    out = [body]
    base = urllib.parse.urlsplit(url)
    refs = re.findall(r"""["'(]([^"'()\s]+\.json)(?:\?[^"'\s]*)?["')]""", body)[:5]
    for ref in refs:
        u = urllib.parse.urljoin(url, ref)
        if urllib.parse.urlsplit(u).netloc == base.netloc:
            c, b = get(u, ua, timeout)
            if c == 200 and b:
                out.append(b)
    return out


def mine_directories(ua: str, args: argparse.Namespace, block: set[str]) -> tuple[dict[str, set[str]], dict[str, Any]]:
    jobs: dict[str, tuple[str, str]] = {}  # label -> (kind, target)
    for label, url in hub_source_urls():
        m = re.match(r"https://github\.com/([\w.-]+/[\w.-]+)/?$", url)
        jobs[label] = ("repo", m.group(1)) if m else ("page", url)
    repos = list(GITHUB_LISTS) + (github_search(ua) if args.github_search else [])
    have = {t for k, t in jobs.values() if k == "repo"}
    for r in repos:
        if r not in have:
            have.add(r)
            jobs[r] = ("repo", r)
    print(f"  B {len(jobs)} directory sources ({sum(1 for k, _ in jobs.values() if k == 'repo')} GitHub repos)", flush=True)
    found: dict[str, set[str]] = {}
    per_source: dict[str, dict[str, Any]] = {}
    with R.nowait_pool(6) as ex:  # gentle on GitHub
        futs = {ex.submit(repo_texts if k == "repo" else page_texts, t, ua, args.timeout): lab for lab, (k, t) in jobs.items()}
        for fut in R.safe_completed(futs, args.deadline):
            lab = futs[fut]
            try:
                texts = fut.result()
            except Exception:  # noqa: BLE001
                texts = []
            kind, target = jobs[lab]
            selfs = {X.root_of(X.norm(urllib.parse.urlsplit(target).hostname or ""))} if kind == "page" else set()
            hosts: set[str] = set()
            for t in texts:
                hosts |= X.candidates(t, self_roots=selfs, extra_block=block)
            add(found, hosts, lab)
            per_source[lab] = {"kind": kind, "target": target, "fetched": len(texts), "hosts": len(hosts)}
    return found, {"sources": per_source}


def cmd_mine(args: argparse.Namespace) -> None:
    ua = R.load_config().get("user_agent") or "Mozilla/5.0 (compatible; RelayPriceBot/0.1)"
    listed, hub, bad = known_domains()
    block = bad | set(R.load_config().get("exclude_domains") or [])
    t0 = time.time()
    if args.reuse_a and CACHE_A.exists():
        cache = json.loads(CACHE_A.read_text(encoding="utf-8"))
        fa, sa = {h: set(v) for h, v in cache["found"].items()}, {**cache["stats"], "reused_from": cache["at"]}
    elif args.skip_a:
        fa, sa = {}, {}
    else:
        fa, sa = mine_stations(ua, args, block)
        CACHE_A.write_text(json.dumps({"at": R.now_iso(), "stats": sa, "found": {h: sorted(v) for h, v in fa.items()}},
                                      ensure_ascii=False), encoding="utf-8")
    fb, sb = ({}, {}) if args.skip_b else mine_directories(ua, args, block)
    allh: dict[str, dict[str, set[str]]] = {}
    for kind, f in (("station_link", fa), ("directory", fb)):
        for h, labs in f.items():
            allh.setdefault(h, {}).setdefault(kind, set()).update(labs)
    groups = X.collapse(allh, listed)
    listed_by_root = {X.root_of(d): d for d in sorted(listed, key=len, reverse=True)}
    listed_hits: dict[str, dict[str, set[str]]] = {}
    for h, kinds in allh.items():
        d = h if h in listed else listed_by_root.get(X.root_of(h))
        if d:
            for kind, labs in kinds.items():
                listed_hits.setdefault(d, {}).setdefault(kind, set()).update(labs)
    cands = []
    for pref, hosts in sorted(groups.items()):
        src: dict[str, set[str]] = {}
        for h in hosts:
            for kind, labs in allh[h].items():
                src.setdefault(kind, set()).update(labs)
        cands.append({"domain": pref, "hosts": hosts, "in_hub": any(h in hub for h in hosts),
                      "sources": {k: sorted(v) for k, v in src.items()}})
    out = {"mined_at": R.now_iso(), "elapsed_s": round(time.time() - t0), "listed": len(listed), "hub": len(hub),
           "raw_hosts": len(allh), "candidates": len(cands), "station_mining": sa, "directory_mining": sb, "items": cands,
           "listed_hits": {d: {k: sorted(v) for k, v in ks.items()} for d, ks in sorted(listed_hits.items())}}
    CANDS.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"mine: {len(allh)} hosts -> {len(cands)} new candidate roots "
          f"({sum(1 for c in cands if 'directory' in c['sources'])} from directories, "
          f"{sum(1 for c in cands if 'station_link' in c['sources'])} from station links) in {time.time() - t0:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("mine", "probe", "all"):
        s = sub.add_parser(name)
        s.add_argument("--workers", type=int, default=32)
        s.add_argument("--timeout", type=float, default=8.0)
        s.add_argument("--deadline", type=float, default=600.0, help="overall seconds per phase")
        s.add_argument("--skip-a", action="store_true", help="skip mining known stations")
        s.add_argument("--reuse-a", action="store_true", help="reuse the cached station-link result (data/mine-cache-a.json)")
        s.add_argument("--skip-b", action="store_true", help="skip mining directories")
        s.add_argument("--github-search", action="store_true", help="also use GitHub repository search (10 req/min limit)")
        s.add_argument("--limit", type=int, default=0, help="probe at most N candidates (0 = all)")
    args = ap.parse_args()
    if args.cmd in ("mine", "all"):
        cmd_mine(args)
    if args.cmd in ("probe", "all"):
        import mine_probe
        mine_probe.cmd_probe(args)
    sys.stdout.flush()
    os._exit(0)  # never wait for abandoned sockets


if __name__ == "__main__":
    main()
