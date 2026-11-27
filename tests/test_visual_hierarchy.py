"""Typography tokens, self-hosted font and editorial de-cluttering of /site/<domain>/ (Section 01–04 structure)."""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build as B  # noqa: E402
import compare_page as CP  # noqa: E402
import pricing as P  # noqa: E402
import psl  # noqa: E402
import timeseries as TS  # noqa: E402
import verify_dist as VD  # noqa: E402

CSS = ROOT / "assets" / "style.css"
FONT = ROOT / "assets" / "fonts" / "inter-latin-var.woff2"
NOW = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
CDN = ("fonts.googleapis.com", "fonts.gstatic.com")

# Bordered surfaces a reader perceives as a separate "box" (counted once per element).
BOX = {"verdict", "dash-card", "chart-card", "peer-pk", "price-bars", "setup-card", "deep-card", "bcard", "gl-strip",
       "kpis", "kpi", "score-card", "bento-4", "card", "plain-card", "toc-bar", "detail-block"}
# Pill-shaped labels: status/grade badges, tags, chips, plus tab buttons and jump-bar links (counted once per element).
PILL = {"badge", "ptag", "rtag", "chip", "pill", "pulse", "app-chip", "lt", "gr", "tag"}


def _tokens(css: str) -> dict[str, float]:
    root = re.search(r":root\s*\{(.*?)\}", css, re.S).group(1)
    return {k: float(v) for k, v in re.findall(r"--(fs-[\w-]+|space-sec):\s*([\d.]+)px", root)}


def _drop_hidden(html: str) -> str:
    """Remove elements carrying the boolean `hidden` attribute (inactive tab panels, empty fav bar)."""
    open_re = re.compile(r"<(\w+)\b[^>]*\shidden(?=[\s>/])[^>]*>")
    while True:
        m = open_re.search(html)
        if not m:
            return html
        tag, depth, pos = m.group(1), 1, m.end()
        pat = re.compile(r"<(/?)%s\b[^>]*>" % tag)
        while depth:
            n = pat.search(html, pos)
            if not n:
                pos = len(html)
                break
            depth += -1 if n.group(1) else 1
            pos = n.end()
        html = html[:m.start()] + html[pos:]


def _strip(html: str) -> str:
    """Visible markup only: scripts, collapsed <details> drawers and `hidden` elements removed."""
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S)
    html = re.sub(r"<details\b.*?</details>", "", html, flags=re.S)
    return _drop_hidden(html)


def _above_table(html: str) -> str:
    """Visible markup between <body> and the quote table toolbar (filters + table excluded), collapsed drawers removed."""
    body = html[html.find("<body"):]
    cut = body.find('<div class="filters"')
    if cut < 0:
        cut = body.find('class="qt sortable"')
    return _strip(body[:cut] if cut >= 0 else body)


def _count(html: str, classes: set[str]) -> int:
    return sum(1 for m in re.finditer(r'class="([^"]+)"', html) if classes & set(m.group(1).split()))


def _pills(html: str) -> int:
    n = 0
    for m in re.finditer(r"<(?:a|b|span|button|i|em|small)\b([^>]*)>", html):
        attrs = m.group(1)
        cls = set((re.search(r'class="([^"]*)"', attrs) or [None, ""])[1].split())
        if cls & PILL or 'role="tab"' in attrs or "data-toc" in attrs:
            n += 1
    return n


def _blocks(html: str, cls: str) -> list[str]:
    """Inner HTML of every element carrying class `cls` (same-tag depth matching)."""
    out = []
    for m in re.finditer(r'<(\w+)\b[^>]*\bclass="(?:[^"]*\s)?%s(?:\s[^"]*)?"[^>]*>' % re.escape(cls), html):
        tag, depth, pos = m.group(1), 1, m.end()
        pat = re.compile(r"<(/?)%s\b[^>]*>" % tag)
        while depth:
            n = pat.search(html, pos)
            if not n:
                pos = len(html)
                break
            depth += -1 if n.group(1) else 1
            pos = n.end()
        out.append(html[m.end():pos])
    return out


def _node_check(test, js: str, name: str):
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(js)
        p = Path(tf.name)
    try:
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        test.assertEqual(r.returncode, 0, msg=f"{name}: {r.stderr}")
    finally:
        p.unlink(missing_ok=True)


class TypeScaleTests(unittest.TestCase):
    def setUp(self):
        self.css = CSS.read_text(encoding="utf-8")
        self.t = _tokens(self.css)

    def test_modular_ratios(self):
        t = self.t
        for k in ("fs-h1", "fs-h1-sm", "fs-h2", "fs-h3", "fs-body", "space-sec"):
            self.assertIn(k, t)
        self.assertGreaterEqual(t["fs-h1"] / t["fs-h2"], 1.40)
        self.assertGreaterEqual(t["fs-h1-sm"] / t["fs-h2"], 1.40)
        self.assertGreaterEqual(t["fs-h2"] / t["fs-body"], 1.50)
        self.assertGreaterEqual(t["fs-h2"] / t["fs-h3"], 1.35)
        self.assertGreaterEqual(t["space-sec"], 44)
        for v in re.findall(r"--space-sec:\s*([\d.]+)px", self.css):
            self.assertGreaterEqual(float(v), 44, "every --space-sec override (incl. mobile) stays >= 44px")
        self.assertRegex(self.css, r"--measure:\s*66ch")
        self.assertRegex(self.css, r"\.sec-sub\{[^}]*max-width:\s*var\(--measure\)")

    def test_self_hosted_font(self):
        for host in CDN:
            self.assertNotIn(host, self.css)
            self.assertNotIn(host, (ROOT / "tools" / "build.py").read_text(encoding="utf-8"))
        ff = re.search(r"@font-face\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(ff)
        self.assertIn('"InterVar"', ff.group(1))
        self.assertIn('url("/assets/fonts/inter-latin-var.woff2")', ff.group(1))
        self.assertRegex(ff.group(1), r"font-display:\s*swap")
        self.assertRegex(ff.group(1), r"font-weight:\s*100 900")
        self.assertRegex(self.css, r'--font:\s*"InterVar",\s*"PingFang SC"')
        self.assertEqual(FONT.read_bytes()[:4], b"wOF2")
        self.assertTrue((ROOT / "assets" / "fonts" / "LICENSE-Inter-OFL.txt").is_file())

    def test_assets_copied_recursively(self):
        tmp = Path(tempfile.mkdtemp(prefix="rc-assets-"))
        try:
            files = B.copy_assets(tmp)
            self.assertIn(FONT, files)
            out = tmp / "assets" / "fonts" / "inter-latin-var.woff2"
            self.assertEqual(out.read_bytes(), FONT.read_bytes())
            self.assertTrue((tmp / "assets" / "style.css").is_file())
            self.assertTrue(all(f.is_file() for f in B.asset_tree()))
            self.assertEqual(VD.check_fonts(tmp), [])
            (tmp / "x.html").write_text('<link href="https://fonts.googleapis.com/css2?family=Inter">', encoding="utf-8")
            self.assertTrue(any("fonts.googleapis.com" in e for e in VD.check_fonts(tmp)))
            out.write_bytes(b"NOPE")
            self.assertTrue(any("bad magic" in e for e in VD.check_fonts(tmp)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


BASE = {"claude-sonnet-5-5": (1.5, 5, 0.1), "gpt-6-astra": (1.25, 8, 0.1), "deepseek-v4.1-flash": (0.15, 4, 0.1),
        "gemini-3.8-flash": (0.15, 4, 0.25)}
PLAIN = {"price": 1, "usd_exchange_rate": 7.3, "quota_per_unit": 500000, "email_verification": True}
FEAT = {"price": 1, "usd_exchange_rate": 7.3, "quota_per_unit": 500000, "email_verification": False, "linuxdo_oauth": True,
        "linuxdo_min_tl": 2, "checkin_enabled": True, "web_playground": True, "quota_for_new_user": 500000, "min_topup": 5,
        "chats": ["Cherry Studio", "CC Switch"]}
MINI = {
    "jbbt.pages.dev": (PLAIN, {"default": 1, "claude-kiro": 0.4}, list(BASE)),
    "api.apiyi.com": (FEAT, {"default": 0.6, "codex-pro": 0.3, "公益免费": 0}, list(BASE)),
    "sparse.example.org": (PLAIN, {"default": 0.9}, list(BASE)[:1]),
    "peer-a.example.net": (PLAIN, {"default": 0.8}, list(BASE)),
    "peer-b.example.io": (PLAIN, {"default": 0.5}, list(BASE)),
}
INTEL = {
    "jbbt.pages.dev": {"root": "pages.dev", "domain_age_days": 2276, "tranco_rank": 162, "majestic_rank": 315,
                       "net_checked_at": "2026-11-26T17:13:09+00:00", "sources": []},
    "api.apiyi.com": {"root": "apiyi.com", "domain_age_days": 900, "tranco_rank": 50000,
                      "net_checked_at": "2026-11-26T12:16:00+00:00", "sources": ["a", "b"]},
}


def _pricing(groups, models):
    return {"data": [{"model_name": m, "quota_type": 0, "model_ratio": BASE[m][0], "completion_ratio": BASE[m][1],
                      "cache_ratio": BASE[m][2], "enable_groups": list(groups)} for m in models],
            "group_ratio": groups, "usable_group": {g: g for g in groups}}


def _ledger_points(n: int) -> list[list]:
    out = []
    for k in range(n):
        ts = (NOW - timedelta(hours=6 * (n - 1 - k))).strftime("%Y-%m-%dT%H:00Z")
        ok = 0 if k == 4 else 1
        px = [10.0 + (k // 4), 8.0 - (k // 6), 2.0, None, None]
        out.append([ts, ok, None if ok == 0 else 280 + 13 * k, 40.0, px])
    return out


class EditorialStationPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = B.load_json(ROOT / "config.json", {})
        stations, by_station = {}, defaultdict(list)
        for i, (d, (status, groups, models)) in enumerate(MINI.items()):
            qs, meta = P.normalize_station(d, _pricing(groups, models), status)
            st = dict(meta)
            st.update({"display_name": d.split(".")[0].title(), "fetched_at": NOW, "parsed": len(models), "http_status": 200,
                       "endpoints": ["anthropic", "openai", "openai-response"] if i % 2 == 0 else ["openai"]})
            stations[d] = st
            by_station[d].extend(qs)
        intel = psl.sanitize_intel({d: dict(INTEL.get(d) or {"domain_age_days": 300 + 50 * i}) for i, d in enumerate(MINI)})
        by_model = defaultdict(list)
        for qs in by_station.values():
            for q in qs:
                by_model[q.canonical].append(q)
        B.mark_anomalies(by_model, cfg)
        md = B.compute_station_scores(stations, by_station, intel, {}, cfg, {}, stability_map={}, now=NOW, monitored=True)
        B.compute_quote_scores(by_model, stations, cfg)
        led = TS.empty_ledger()
        for d in ("jbbt.pages.dev", "api.apiyi.com", "peer-a.example.net", "peer-b.example.io"):
            led["s"][d] = _ledger_points(24)
        specs = B.MS.load_model_specs(B.DATA)
        cx = B.consumer_context(stations, by_station, specs, cfg, [], ledger=led, intel=intel, run_stats={},
                                stability_map={}, monitored=True)
        cls.tmp = Path(tempfile.mkdtemp(prefix="rc-vh-"))
        old = B.DIST
        B.DIST = cls.tmp
        try:
            B.copy_assets(cls.tmp)
            site = B.Site(cfg, NOW, "t1")
            B.build_station_pages(site, stations, by_station, intel, [], cfg, run_stats={}, stability_map={}, reviews_map={},
                                  market_dims=md, market_px=B.flagship_market(by_model, specs, cfg), cx=cx)
            B.build_indexes(site, {}, stations, by_station, {}, cfg, 3, intel=intel, stability_map={}, cx=cx)
            CP.build_compare(site, CP.stations_payload(stations, by_station, intel, {}, NOW, B.FLAGSHIP_MODELS))
            B.build_home(site, cfg, {}, stations, {}, [], {}, [])
            site.flush()
            B.build_seo_files(site, {}, stations, 0)
        finally:
            B.DIST = old
        cls.stations = stations

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def read(self, rel):
        return (self.tmp / rel).read_text(encoding="utf-8")

    def site(self, dom):
        return self.read(f"site/{dom}/index.html")

    def test_font_copied_into_dist(self):
        f = self.tmp / "assets" / "fonts" / "inter-latin-var.woff2"
        self.assertEqual(f.read_bytes()[:4], b"wOF2")
        self.assertEqual(VD.check_fonts(self.tmp), [])
        html = self.site("jbbt.pages.dev")
        self.assertIn('<link rel="preload" href="/assets/fonts/inter-latin-var.woff2" as="font" type="font/woff2" crossorigin>', html)

    def test_jbbt_above_table_budget(self):
        html = self.site("jbbt.pages.dev")
        self.assertIn('class="qt sortable"', html)
        above = _above_table(html)
        boxes, pills = _count(above, BOX), _pills(above)
        self.assertLessEqual(boxes, 8, f"boxed cards above the quote table: {boxes}")
        self.assertLessEqual(pills, 22, f"pills/badges above the quote table: {pills}")
        self.assertIn("线路类型一眼看懂", above)
        self.assertIn('class="gl-strip"', above)

    def test_budget_holds_for_mature_and_sparse_stations(self):
        for dom in ("api.apiyi.com", "sparse.example.org"):
            above = _above_table(self.site(dom))
            self.assertLessEqual(_count(above, BOX), 8, dom)
            self.assertLessEqual(_pills(above), 22, dom)
        apiyi = self.site("api.apiyi.com")
        vd = _strip(_blocks(apiyi, "verdict")[0])
        self.assertLessEqual(_count(vd, {"ptag"}), 4, "verdict shows at most 4 practical tags; the rest fold away")

    def test_section_architecture(self):
        for dom in MINI:
            html = self.site(dom)
            for cls in ("setup-card", "peer-pk", "chart-card", "dash-card", "deep-card", "verdict"):
                for blk in _blocks(html, cls):
                    self.assertNotIn("<h2", blk, f"{dom}: <h2> inside .{cls}")
            heads = _blocks(html, "sec-head")
            self.assertEqual(html.count("<h2"), sum(h.count("<h2") for h in heads), f"{dom}: every <h2> lives in .sec-head")
            for sid in ("sec-pricing", "sec-setup", "sec-raw"):
                self.assertIn(f'id="{sid}"', html, dom)
            self.assertLess(html.index('id="sec-pricing"'), html.index('id="sec-setup"'), dom)
            self.assertLess(html.index('id="sec-setup"'), html.index('id="sec-raw"'), dom)
            self.assertEqual(html.count("<h1"), 1, dom)
        jb = self.site("jbbt.pages.dev")
        toc = _blocks(jb, "toc-bar")
        self.assertEqual(len(toc), 1)
        for sid in ("sec-trend", "sec-pricing", "sec-setup", "sec-raw"):
            self.assertIn(f'href="#{sid}"', toc[0])
            self.assertIn(f'id="{sid}"', jb)
        self.assertLess(jb.index('id="sec-trend"'), jb.index('id="sec-pricing"'))
        for t in ("动态走势与同行对比", "模型比价与完整价目表", "接入教程与深度核验"):
            self.assertRegex(jb, r'<header class="sec-head"[^>]*>.*?<h2>%s</h2>' % t)
        for t in ("站点在线心跳与接口响应", "热门模型历史价格走势", "同级别同行直接对比", "热门模型一眼比价", "30 秒接进我的软件",
                  "七维评分拆解", "真伪抽检与社区口碑"):
            self.assertRegex(jb, r"<h3\b[^>]*>%s" % re.escape(t))

    def test_legacy_duplicates_removed_and_merged(self):
        for dom in MINI:
            html = self.site(dom)
            for bad in ('class="kpis"', "score-card", "bento-4", "detail-block", "评分明细与站点详情"):
                self.assertNotIn(bad, html, f"{dom}: legacy {bad}")
            self.assertEqual(html.count('class="vd-score"'), 1, f"{dom}: exactly one big score (the verdict)")
        jb = self.site("jbbt.pages.dev")
        setup = jb[jb.index('id="sec-setup"'):jb.index('id="sec-raw"')]
        self.assertIn('class="deep-grid"', setup)
        self.assertIn('class="setup-card', setup)
        self.assertIn("<svg", _blocks(setup, "score-break")[0], "radar stays in the score breakdown card")
        trust = _blocks(setup, "trust-card")[0]
        self.assertIn("现场抽检这家站的真伪与智商 →", trust)
        self.assertIn('href="/verify/', trust)
        self.assertIn("提交我的实测评价", trust)
        self.assertIn("托管二级域名（*.pages.dev）", trust)
        for bad in ("Tranco #162", "Majestic #315", "老站"):
            self.assertNotIn(bad, jb)
        hero = jb[:jb.index('id="sec-pricing"')]
        self.assertIn('class="dom-tag"', hero)
        self.assertIn("访问官网 →", hero)
        self.assertIn("☆ 收藏常用站", hero)
        self.assertIn("站点体检档案", hero)

    def test_index_pages_share_hierarchy(self):
        st = self.read("stations/index.html")
        for sid in ("sec-map", "sec-rank"):
            self.assertRegex(st, r'<header class="sec-head" id="%s"' % sid)
        for blk in _blocks(st, "bubble-card"):
            self.assertNotIn("<h2", blk)
        home = self.read("index.html")
        for page, name in ((st, "stations"), (home, "home")):
            heads = _blocks(page, "sec-head")
            self.assertGreaterEqual(len(heads), 2, name)
            self.assertEqual(page.count("<h2"), sum(h.count("<h2") for h in heads), f"{name}: every <h2> lives in .sec-head")
        self.assertNotIn("OpenAI Responses", VD.first_screen_text(home))

    def test_guards_stay_green(self):
        self.assertEqual(VD.check_zero_download(self.tmp), [])
        self.assertEqual(VD.check_plain_language(self.tmp), [])
        self.assertFalse((self.tmp / "data").exists() and any((self.tmp / "data").rglob("*")))
        for f in self.tmp.rglob("*.html"):
            txt = f.read_text(encoding="utf-8")
            for host in CDN:
                self.assertNotIn(host, txt, f.name)
            self.assertEqual(VD.jargon_hits(txt), [], f.relative_to(self.tmp).as_posix())
        self.assertIn("Disallow: /data/", self.read("robots.txt"))

    def test_node_check_js(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        for rel in ("assets/app.js", "functions/api/verify.js"):
            r = subprocess.run(["node", "--check", str(ROOT / rel)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, msg=f"{rel}: {r.stderr}")
        seen: set[str] = set()
        for rel in ("index.html", "stations/index.html", "compare/index.html", *[f"site/{d}/index.html" for d in MINI]):
            for attrs, js in re.findall(r"<script\b([^>]*)>(.*?)</script>", self.read(rel), re.S):
                if "src=" in attrs or "json" in attrs or not js.strip() or js in seen:
                    continue
                seen.add(js)
                _node_check(self, js, rel)
        self.assertTrue(seen, "at least one inline script was checked")


if __name__ == "__main__":
    unittest.main()
