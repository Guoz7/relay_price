"""Consumer Layer-1 upgrade: price anchors, B_hybrid peers, 30-day dashboard cards and a zero-download mini build."""
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build as B  # noqa: E402
import charts as CH  # noqa: E402
import compare_page as CP  # noqa: E402
import consumer as C  # noqa: E402
import dashboard as DB  # noqa: E402
import pricing as P  # noqa: E402
import timeseries as TS  # noqa: E402
import verify_dist as VD  # noqa: E402

NOW = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
BAD_NUM = re.compile(r"NaN|Infinity|\bnan\b|\binf\b")


def _node_check(test, js: str, name: str):
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(js)
        p = Path(tf.name)
    try:
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        test.assertEqual(r.returncode, 0, msg=f"{name}: {r.stderr}")
    finally:
        p.unlink(missing_ok=True)


def _svgs(html: str) -> list[str]:
    return re.findall(r"<svg\b.*?</svg>", html, re.S)


class PriceAnchorTests(unittest.TestCase):
    def test_fmt_official_discount_edges(self):
        cases = {5: "不到官网 1 折", 9.99: "不到官网 1 折", 10: "约官网 1.0 折", 18: "约官网 1.8 折", 94.9: "约官网 9.5 折",
                 95: "约等于官网价", 100: "约等于官网价", 105: "约等于官网价", 106: "高于官网（约 1.1 倍）",
                 250: "高于官网（约 2.5 倍）"}
        for v, want in cases.items():
            with self.subTest(v=v):
                self.assertEqual(C.fmt_official_discount(v), want)
        for bad in (None, 0, -3, float("nan"), float("inf"), True, "18"):
            with self.subTest(bad=bad):
                self.assertEqual(C.fmt_official_discount(bad), "暂无统一比价基线")

    def test_short_pill_and_headline(self):
        self.assertEqual(C.fmt_discount_short(18), "约 1.8 折")
        self.assertEqual(C.fmt_discount_short(3), "不到 1 折")
        self.assertEqual(C.fmt_discount_short(100), "≈官网价")
        self.assertEqual(C.fmt_discount_short(250), "官网 2.5 倍")
        self.assertEqual(C.fmt_discount_short(None), "—")
        self.assertEqual(C.price_headline({"off_pct": 18, "cheaper_pct": 62.4}), "约官网 1.8 折 · 比全网 62% 同行便宜")
        self.assertEqual(C.price_headline({"off_pct": None, "cheaper_pct": 40}), "比全网 40% 同行便宜")
        self.assertEqual(C.price_headline(None), "暂无统一比价基线")
        self.assertEqual(C.pct_delta(10, 8), ("比本站便宜 20%", "ok"))
        self.assertEqual(C.pct_delta(10, 13), ("比本站贵 30%", "warn"))
        self.assertEqual(C.pct_delta(10, 10.2), ("和本站差不多", ""))
        self.assertEqual(C.pct_delta(None, 3)[1], "muted")

    def test_market_index_trimmed_and_cheaper_percentile(self):
        self.assertAlmostEqual(C.market_index({"a": 2, "b": 8}, {"a": 4, "b": 4}), 100.0)
        self.assertIsNone(C.market_index({"x": 1}, {"a": 1}))
        cp = C.cheaper_percentiles({"lo": 10.0, "mid": 50.0, "hi": 300.0, "none": None})
        self.assertEqual((cp["lo"], cp["mid"], cp["hi"], cp["none"]), (100.0, 50.0, 0.0, None))


def _m(models, idx, score, primary="claude", aliases=(), top=None):
    ms = frozenset(models)
    return {"models": ms, "top": frozenset(top) if top is not None else ms, "n_models": len(ms), "primary": primary,
            "mkt_idx": idx, "score": score, "aliases": frozenset(aliases)}


class PeerMatchingTests(unittest.TestCase):
    CORE = ["claude-opus-5-5", "claude-sonnet-5-5", "gpt-6-astra", "gemini-3.8-flash", "deepseek-v4.1-flash"]

    def test_similarity_formula(self):
        a, b = _m(self.CORE, 50.0, 70.0), _m(self.CORE[:4], 100.0, 60.0)
        sim, shared = C.similarity(a, b)
        j = 4 / 5
        want = 0.40 * j + 0.20 * j + 0.25 * math.exp(-abs(math.log(0.5)) / 0.6) + 0.15 * (1 - 10 / 35)
        self.assertEqual(shared, 4)
        self.assertAlmostEqual(sim, want, places=9)
        self.assertAlmostEqual(C.similarity(a, dict(b, primary="openai"))[0], want * 0.75, places=9)
        lonely = _m(["claude-opus-5-5", "x-1", "x-2", "x-3", "x-4"], 50.0, 70.0)
        sim_lonely, shared_lonely = C.similarity(a, lonely)
        self.assertEqual(shared_lonely, 1)
        j1 = 1 / 9
        self.assertAlmostEqual(sim_lonely, (0.40 * j1 + 0.20 * j1 + 0.25 + 0.15) * 0.20, places=9)

    def test_brand_mirror_and_fingerprint_dedup(self):
        self.assertEqual(C.brand_stem("api.bianxie.ai"), C.brand_stem("bianxieai.com"))
        metrics = {
            "bianxieai.com": _m(self.CORE, 50.0, 70.0),
            "api.bianxie.ai": _m(self.CORE, 50.5, 70.0),         # same brand stem -> clone of the station itself
            "mirror-x.com": _m(self.CORE, 61.0, 65.0, aliases=["mirror-y.com"]),
            "mirror-y.com": _m(self.CORE, 61.0, 65.0),           # alias link -> one of the pair only
            "twin-a.com": _m(self.CORE, 70.0, 64.0),
            "twin-b.net": _m(self.CORE, 70.04, 64.0),            # identical (n_models, round(idx, 1)) fingerprint
            "solo-1.com": _m(self.CORE, 80.0, 62.0),
            "solo-2.com": _m(self.CORE[:4], 90.0, 60.0),
            "solo-3.com": _m(self.CORE[:3], 120.0, 58.0),
        }
        self.assertTrue(C.is_clone("twin-a.com", "twin-b.net", metrics))
        self.assertTrue(C.is_clone("mirror-y.com", "mirror-x.com", metrics))
        self.assertFalse(C.is_clone("solo-1.com", "solo-2.com", metrics))
        peers = C.compute_peers(metrics, k=5)["bianxieai.com"]["peers"]
        self.assertNotIn("api.bianxie.ai", peers)
        self.assertEqual(len(peers), 5)
        self.assertEqual(len({"mirror-x.com", "mirror-y.com"} & set(peers)), 1)
        self.assertEqual(len({"twin-a.com", "twin-b.net"} & set(peers)), 1)
        for i, p in enumerate(peers):
            for q in peers[i + 1:]:
                self.assertFalse(C.is_clone(p, q, metrics), (p, q))

    def test_cheaper_and_safer_alternatives(self):
        metrics = {
            "me.com": _m(self.CORE, 100.0, 60.0),
            "cheap.com": _m(self.CORE[:3], 80.0, 57.0),          # >=5% cheaper, score within 5 pts
            "toocheap.com": _m(self.CORE, 50.0, 40.0),           # much cheaper but far less reliable -> not offered
            "safe.com": _m(self.CORE[:4], 130.0, 66.0),          # +6 pts, price <= 1.5x
            "pricy.com": _m(self.CORE, 400.0, 72.0),             # safer but 4x price -> not offered
            "far.com": _m(["glm-5", "qwen-4"], 60.0, 72.0, primary="other"),  # shares < 2 models -> never an alternative
        }
        res = C.compute_peers(metrics, k=5)["me.com"]
        self.assertEqual(res["cheaper"], "cheap.com")
        self.assertEqual(res["safer"], "safe.com")
        self.assertEqual(res["peers"][-1], "far.com")
        none = C.compute_peers({"a.com": _m(self.CORE, 50.0, 70.0), "b.com": _m(self.CORE, 52.0, 69.0)})["a.com"]
        self.assertIsNone(none["cheaper"])
        self.assertIsNone(none["safer"])

    def test_niche_station_falls_back_to_nearest_peers(self):
        metrics = {
            "niche.com": _m(["qwen-4"], 100.0, 60.0, primary="qwen"),
            "big1.com": _m(self.CORE, 90.0, 61.0),
            "big2.com": _m(self.CORE[:3], 300.0, 50.0),
            "qwen-shop.com": _m(["qwen-4", "glm-5"], 110.0, 59.0, primary="qwen"),
        }
        res = C.compute_peers(metrics, k=5)["niche.com"]
        self.assertEqual(res["peers"][0], "qwen-shop.com")
        self.assertEqual(set(res["peers"]), {"qwen-shop.com", "big1.com", "big2.com"})
        self.assertIsNone(res["cheaper"])

    def test_client_chips_are_everyday_apps(self):
        labels = [lbl for lbl, _ in C.client_chips(["openai", "anthropic", "gemini", "image", "weird"])]
        self.assertEqual(labels, ["Claude Code / Cursor", "ChatBox / Cherry Studio", "Gemini 客户端", "AI 生图"])


def _hb_points(n: int, fail_at: int = 3) -> list[list]:
    out = []
    for k in range(n):
        ts = (NOW - timedelta(hours=6 * (n - 1 - k))).strftime("%Y-%m-%dT%H:00Z")
        ok = 0 if k == fail_at else 1
        out.append([ts, ok, None if ok == 0 else 300 + 17 * k, 50.0, [10.0, None, None, None, None]])
    return out


class DashboardCardTests(unittest.TestCase):
    def test_heartbeat_empty_cold_and_mature(self):
        self.assertIn("还没有巡检记录", DB.heartbeat_card([]))
        self.assertIn("还没有巡检记录", DB.heartbeat_card([["2026-11-27T06:00Z", None, None, 50.0, [None] * 5]]))
        cold = DB.heartbeat_card(_hb_points(3, fail_at=1))
        self.assertIn("已连续记录 3 次巡检", cold)
        self.assertIn("67%", cold)
        self.assertEqual(cold.count('class="pulse '), 3)
        self.assertEqual(_svgs(cold), [])
        mature = DB.heartbeat_card(_hb_points(24))
        svgs = _svgs(mature)
        self.assertEqual(len(svgs), 2)
        for s in svgs:
            ET.fromstring(s)
        self.assertIn("平时中位", mature)
        self.assertIn("23/24 次巡检", mature)
        self.assertIn("data-tip=", mature)
        self.assertIsNone(BAD_NUM.search(mature))
        self.assertIn("不等同于模型回复速度", mature)

    def test_heartbeat_svg_degenerate_inputs_stay_finite(self):
        same = [[f"2026-11-27T0{h}:00Z", 1, 400, None, []] for h in range(6)]
        all_down = [[f"2026-11-27T0{h}:00Z", 0, None, None, []] for h in range(6)]
        for pts in (same, all_down, [["bad-ts", 1, 400, None, []]] * 6):
            html = DB.heartbeat_card(pts)
            self.assertIsNone(BAD_NUM.search(html), html[:200])
            for s in _svgs(html):
                ET.fromstring(s)

    def _trend(self, n: int, mine: float = 10.0) -> dict:
        pts = [((NOW - timedelta(hours=6 * (n - 1 - k))).strftime("%Y-%m-%dT%H:00Z"), mine - (k // 3)) for k in range(n)]
        ref = [(t, 12.0) for t, _ in pts]
        return {"model": "claude-opus-5-5", "label": "Claude Opus 5.5", "points": pts, "ref": ref, "ref_label": "同行均价",
                "mine": pts[-1][1] if pts else mine, "peer": 12.0, "official": 36.0, "events": []}

    def test_price_trend_cold_start_vs_mature(self):
        cold = DB.price_trend_card([self._trend(2)])
        self.assertIn("已记录 2 次快照，满 5 次自动展开 30 天阶梯曲线", cold)
        self.assertIn("近 30 天暂无调价波动", cold)
        for lbl in ("本站", "同行均价", "官方原价"):
            self.assertIn(lbl, cold)
        self.assertEqual(_svgs(cold), [])
        mature = DB.price_trend_card([self._trend(8), dict(self._trend(1), model="gpt-6-astra", label="GPT-6 Astra")])
        svgs = _svgs(mature)
        self.assertEqual(len(svgs), 1)
        ET.fromstring(svgs[0])
        self.assertIn("近 8 次快照", mature)
        self.assertIn('role="tab"', mature)
        self.assertIn("GPT-6 Astra", mature)
        self.assertIsNone(BAD_NUM.search(mature))
        self.assertIn("本站暂未上架热门追踪模型", DB.price_trend_card([], TS.LABELS.values()))
        ev = DB.price_trend_card([dict(self._trend(2), events=[("warn", "11-26 涨价 +20%")])])
        self.assertIn("11-26 涨价 +20%", ev)
        self.assertNotIn("近 30 天暂无调价波动", ev)

    def test_step_svg_flat_and_zero_series(self):
        flat = [(f"2026-11-2{d}T00:00Z", 5.0) for d in range(1, 8)]
        svg = CH.price_step_svg(flat, [(t, 5.0) for t, _ in flat])
        ET.fromstring(svg)
        self.assertIsNone(BAD_NUM.search(svg))
        self.assertEqual(CH.price_step_svg([(t, 0.0) for t, _ in flat]), "")  # free / zero prices are never plotted
        self.assertEqual(CH.price_step_svg(flat[:1]), "")
        free = DB.price_trend_card([{"model": "m", "label": "M", "points": [(t, 0.0) for t, _ in flat], "mine": 0.0}])
        self.assertNotIn("chart-box", free)
        self.assertIn("暂不画走势曲线", free)
        self.assertNotIn("满 5 次", free)
        mixed = [(t, 0.0 if i == 0 else 4.0 + i) for i, (t, _) in enumerate(flat)]
        rising = DB.price_trend_card([{"model": "m", "label": "M", "points": mixed, "mine": 10.0}])
        self.assertIn("chart-box", rising)
        self.assertIn("上涨 100%", rising)
        self.assertEqual(B.BAR_LABELS["gpt-6.1-sol"], "GPT-6.1 Sol")
        self.assertTrue(all(m in B.BAR_LABELS for m in B.BAR_MODELS))

    def test_price_bars_cap_extreme_outliers(self):
        html = DB.price_bars([{"label": "Claude Opus 5.5", "chat": (365.0, 20.0, 36.5), "cache": (None, None, None)},
                              {"label": "GPT-6 Astra", "chat": (3.0, 6.0, 30.0), "cache": (0.3, 0.6, 3.0)}])
        self.assertIn("高于官网 10.0 倍", html)
        self.assertIn("高于官网（约 10.0 倍）", html)
        self.assertIn("约官网 1.0 折", html)
        widths = [int(w) for w in re.findall(r'style="width:(\d+)%"', html)]
        self.assertTrue(widths and max(widths) <= 100 and min(widths) >= 2)
        self.assertIn("日常聊天价", html)
        self.assertIn("长对话缓存价", html)
        self.assertIn("本站热门模型暂未公开这项价格", DB.price_bars([{"label": "X", "chat": (1.0, 2.0, 3.0)}]))
        self.assertEqual(DB.price_bars([]), "")
        for bad in ("P10", "IQR", "对数轴"):
            self.assertNotIn(bad, html)

    def test_peer_pk_and_bubble_payload(self):
        rows = [{"dom": "me.com", "name": "Me", "grade_html": "B", "score": 70.0, "disc": "约 1.8 折", "delta": "", "delta_cls": "",
                 "age_txt": "2.0 年", "age_pct": 66, "stab": 100, "uptime": 99.0, "me": True},
                {"dom": "p.com", "name": "Peer", "grade_html": "B", "score": 66.0, "disc": "约 2.0 折", "delta": "比本站贵 11%",
                 "delta_cls": "warn", "age_txt": "1.0 年", "age_pct": 33, "stab": 80, "uptime": None}]
        html = DB.peer_pk(rows, cheaper={"dom": "c.com", "name": "C", "why": "便宜 12%"}, safer=None,
                          compare_href="/compare/?s=me.com,p.com")
        for txt in ("同级别同行直接对比", "同类更省钱备选", "/site/c.com/", "去对比页深度 PK", "/compare/?s=me.com,p.com"):
            self.assertIn(txt, html)
        self.assertNotIn("同类更稳备选", html)
        self.assertEqual(DB.peer_pk(rows[:1]), "")
        pay = DB.bubble_payload([{"dom": "a.com", "name": "A", "grade": "B", "score": 70.04, "cheaper_pct": 55.55, "off_pct": float("nan"),
                                  "age_days": 400, "uptime": 99.4, "n_models": 30, "facets": "cheap"},
                                 {"dom": "b.com", "cheaper_pct": None}])
        self.assertEqual(pay["cols"], DB.BUBBLE_COLS)
        self.assertEqual(pay["rows"], [["a.com", "A", "B", 70.0, 55.5, None, 400, 99, 30, "cheap"]])
        sec = DB.bubble_section(pay, 2)
        blob = re.search(r'<script type="application/json" id="bubble-data">(.*?)</script>', sec, re.S).group(1)
        self.assertEqual(json.loads(blob), json.loads(json.dumps(pay)))
        self.assertIn('id="bubble-chart"', sec)
        self.assertIn("另有 1 家", sec)


# ---------------------------------------------------------------- mini end-to-end build into a temp dist
BASE = {"claude-sonnet-5-5": (1.5, 5, 0.1), "claude-opus-5-5": (2.5, 5, 0.1), "gpt-6-astra": (1.25, 8, 0.1),
        "deepseek-v4.1-flash": (0.15, 4, 0.1), "gemini-3.8-flash": (0.15, 4, 0.25), "gpt-6.1-sol": (1, 8, 0.1)}
ALL = list(BASE)
STATUS = {"price": 1, "usd_exchange_rate": 7.3, "quota_per_unit": 500000, "quota_display_type": "USD", "email_verification": True,
          "turnstile_check": True, "github_oauth": True, "server_address": "https://api.example-relay.test"}
FIXTURE = {"alpha.ai": (0.2, ALL), "beta.cc": (0.3, ALL), "gammaapi.com": (0.5, ALL[:4]), "delta.top": (1.2, ALL[:3]),
           "eps.xyz": (3.5, ALL), "zeta.net": (0.25, ALL[2:]), "eta.dev": (0.8, ALL[:5]), "theta.io": (60.0, ALL[:2])}


def _pricing(mult, models):
    return {"data": [{"model_name": m, "quota_type": 0, "model_ratio": BASE[m][0], "completion_ratio": BASE[m][1],
                      "cache_ratio": BASE[m][2], "create_cache_ratio": 1.25, "enable_groups": ["default"]} for m in models],
            "group_ratio": {"default": mult}}


class MiniBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = B.load_json(ROOT / "config.json", {})
        stations, by_station = {}, defaultdict(list)
        for i, (d, (mult, ms)) in enumerate(FIXTURE.items()):
            qs, meta = P.normalize_station(d, _pricing(mult, ms), STATUS)
            st = dict(meta)
            st.update({"display_name": d.split(".")[0].title(), "fetched_at": NOW, "parsed": len(ms), "http_status": 200,
                       "endpoints": ["anthropic", "openai", "openai-response"] if i % 2 == 0 else ["openai", "gemini", "image"]})
            stations[d] = st
            by_station[d].extend(qs)
        intel = {d: {"domain_age_days": 100 + 200 * i} for i, d in enumerate(FIXTURE)}
        by_model = defaultdict(list)
        for qs in by_station.values():
            for q in qs:
                by_model[q.canonical].append(q)
        B.mark_anomalies(by_model, cfg)
        md = B.compute_station_scores(stations, by_station, intel, {}, cfg, {}, stability_map={}, now=NOW, monitored=True)
        B.compute_quote_scores(by_model, stations, cfg)
        led = TS.empty_ledger()
        led["s"]["alpha.ai"] = _hb_points(12)
        led["s"]["beta.cc"] = [["2026-11-27T06:00Z", 1, 500, 60.0, [12.0, None, None, None, None]]]
        specs = B.MS.load_model_specs(B.DATA)
        cx = B.consumer_context(stations, by_station, specs, cfg, [], ledger=led, intel=intel, run_stats={}, stability_map={},
                                monitored=True)
        cls.tmp = Path(tempfile.mkdtemp(prefix="rc-mini-"))
        cls.old_dist = B.DIST
        B.DIST = cls.tmp
        try:
            site = B.Site(cfg, NOW, "t1")
            B.build_station_pages(site, stations, by_station, intel, [], cfg, run_stats={}, stability_map={}, reviews_map={},
                                  market_dims=md, market_px=B.flagship_market(by_model, specs, cfg), cx=cx)
            B.build_indexes(site, {}, stations, by_station, {}, cfg, 3, intel=intel, stability_map={}, cx=cx)
            CP.build_compare(site, CP.stations_payload(stations, by_station, intel, {}, NOW, B.FLAGSHIP_MODELS))
            site.flush()
            B.build_seo_files(site, {}, stations, 0)
        finally:
            B.DIST = cls.old_dist
        cls.cx = cx

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def read(self, rel: str) -> str:
        return (self.tmp / rel).read_text(encoding="utf-8")

    def test_zero_download_and_plain_language_guards(self):
        self.assertFalse((self.tmp / "data").exists())
        self.assertEqual(VD.check_zero_download(self.tmp), [])
        self.assertEqual(VD.check_plain_language(self.tmp), [])
        robots = self.read("robots.txt")
        self.assertIn("Disallow: /data/", robots)
        self.assertNotIn("Allow: /data/", robots)
        llms = self.read("llms.txt")
        self.assertNotIn("/data/", llms)
        self.assertNotIn(".csv", llms)

    def test_jargon_lives_only_inside_collapsed_drawers(self):
        page = self.read("site/alpha.ai/index.html")
        self.assertIn('<details class="adv-drawer">', page)
        self.assertIn("给进阶用户核对", page)
        drawer = re.search(r'<details class="adv-drawer">.*?</details>', page, re.S).group(0)
        self.assertIn("Turnstile", drawer)
        self.assertEqual(VD.jargon_hits(page), [])

    def test_stations_page_bubble_and_scenario_filters(self):
        html = self.read("stations/index.html")
        for txt in ('id="bubble-chart"', 'id="bubble-data"', "省钱 vs 靠谱", "写代码 / Claude Code", "当前显示", "并排对比 →",
                    "相对价格", "开站时长", "近 30 天在线"):
            self.assertIn(txt, html)
        for bad in ("下载 JSON", "导出 CSV", "/data/stations", 'class="ch hist'):
            self.assertNotIn(bad, html)
        blob = re.search(r'<script type="application/json" id="bubble-data">(.*?)</script>', html, re.S).group(1)
        pay = json.loads(blob)
        self.assertEqual(pay["cols"], DB.BUBBLE_COLS)
        self.assertEqual({r[0] for r in pay["rows"]}, set(FIXTURE))
        self.assertTrue(all(0 <= r[4] <= 100 for r in pay["rows"]))
        self.assertIsNone(BAD_NUM.search(blob))

    def test_station_layer1_cards(self):
        a = self.read("site/alpha.ai/index.html")
        self.assertIn("不到官网 1 折 · 比全网 100% 同行便宜", a)
        self.assertRegex(self.read("site/eps.xyz/index.html"), r"约官网 4\.1 折 · 比全网 14% 同行便宜")
        for txt in ("Claude Code / Cursor", "ChatBox / Cherry Studio", "站内每 6 小时自动巡检公开接口", "平时中位",
                    "热门模型历史价格走势", "同级别同行直接对比", "去对比页深度 PK", "热门模型一眼比价", "仅公开报价"):
            self.assertIn(txt, a)
        self.assertIn("/compare/?s=alpha.ai,", a)
        for s in _svgs(a):
            ET.fromstring(s)
        b = self.read("site/beta.cc/index.html")
        self.assertIn("已连续记录 1 次巡检", b)
        self.assertIn("满 5 次自动展开 30 天阶梯曲线", b)
        self.assertIn("高于官网", self.read("site/theta.io/index.html"))
        for f in self.tmp.rglob("*.html"):
            first = VD.first_screen_text(f.read_text(encoding="utf-8"))
            self.assertIsNone(BAD_NUM.search(first), f.name)
            self.assertNotIn("https://api.example-relay.test", first)

    def test_peer_alternatives_computed_for_fixture(self):
        peers = self.cx["peers"]
        self.assertEqual(set(peers), set(FIXTURE))
        for dom, p in peers.items():
            self.assertNotIn(dom, p["peers"])
            self.assertLessEqual(len(p["peers"]), 5)
        self.assertTrue(any(p["cheaper"] for p in peers.values()))
        self.assertTrue(any(p["safer"] for p in peers.values()))

    def test_compare_page_is_display_only(self):
        html = self.read("compare/index.html")
        blob = re.search(r'<script type="application/json" id="cmp-data">(.*?)</script>', html, re.S).group(1)
        for bad in ("example-relay", "quota_per_unit", "usd_exchange_rate", "billing_expr", "group_ratio", "server_address"):
            self.assertNotIn(bad, blob)
        self.assertEqual(len(json.loads(blob)["rows"]), len(FIXTURE))

    def test_node_check_all_scripts(self):
        for rel in ("assets/app.js", "functions/api/verify.js"):
            r = subprocess.run(["node", "--check", str(ROOT / rel)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, msg=f"{rel}: {r.stderr}")
        n = 0
        for rel in ("compare/index.html", "stations/index.html", "site/alpha.ai/index.html", "site/beta.cc/index.html"):
            for attrs, js in re.findall(r"<script\b([^>]*)>(.*?)</script>", self.read(rel), re.S):
                if "application/json" in attrs or "application/ld+json" in attrs or "src=" in attrs or not js.strip():
                    continue
                _node_check(self, js, rel)
                n += 1
        self.assertGreaterEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
