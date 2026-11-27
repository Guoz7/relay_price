import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build as B  # noqa: E402
import compare_page as CP  # noqa: E402
import pricing as P  # noqa: E402
import scoring as S  # noqa: E402
import verify_dist as VD  # noqa: E402

NOW = datetime(2026, 11, 27, tzinfo=timezone.utc)
PRICING = {
    "data": [
        {"model_name": "claude-sonnet-5-5", "quota_type": 0, "model_ratio": 1.5, "completion_ratio": 5, "cache_ratio": 0.1,
         "create_cache_ratio": 1.25, "enable_groups": ["default"]},
        {"model_name": "gpt-6-astra", "quota_type": 0, "model_ratio": 1.25, "completion_ratio": 8, "cache_ratio": 0,
         "create_cache_ratio": 0, "enable_groups": ["default"]},
        {"model_name": "deepseek-v4.1-flash", "quota_type": 0, "model_ratio": 0.5, "completion_ratio": 4, "cache_ratio": 0,
         "enable_groups": ["default"]},
        {"model_name": "img", "quota_type": 1, "model_price": 0.02, "cache_ratio": 0, "enable_groups": ["default"]},
        {"model_name": "gemini-4-carbon", "billing_expr": 'tier("base", p * 2 + c * 12 + cr * 0.2 + cc * 2.5)', "enable_groups": ["default"]},
        {"model_name": "glm-5", "quota_type": 0, "model_ratio": 1, "enable_groups": ["default"]},
    ],
    "group_ratio": {"default": 0.4},
}
STATUS = {"price": 1, "usd_exchange_rate": 7.3, "docs_link": "https://docs.newapi.pro/", "server_address": "http://localhost:3000",
          "top_up_link": "/topup", "quota_per_unit": 500000, "quota_display_type": "USD", "email_verification": True,
          "turnstile_check": False, "github_oauth": True}


def _score(total_bias: float = 0.0) -> S.StationScore:
    dims = [S.Dim(k, 80.0 + total_bias, f"ev-{k}") for k in S.DIM_ORDER]
    sc = S.station_score(dims, S.DEFAULT_STATION_WEIGHTS, attempts=10)
    S.assign_percentiles([sc])
    return sc


def _station(dom: str = "x.com", bias: float = 0.0):
    qs, meta = P.normalize_station(dom, PRICING, STATUS)
    st = dict(meta)
    st.update({"display_name": dom.split(".")[0].title(), "score": _score(bias), "topup_price": 1.0, "parsed": len({q.model for q in qs}),
               "fetched_at": NOW, "groups": [{"name": "default", "ratio": 0.4, "selectable": True, "desc": "", "caveats": []}],
               "endpoints": ["anthropic", "openai", "openai-response"], "github_oauth": True, "version": "v0.9.1",
               "turnstile_check": False, "email_verification": True})
    return qs, st


class DummySite:
    def __init__(self):
        self.cfg = {"site_name": "API 比价雷达", "base_url": "https://agentied.work"}
        self.now = NOW
        self.pages = {}

    def page(self, path, title, desc, body, **kwargs):
        self.pages[path] = {"title": title, "desc": desc, "body": body, "kwargs": kwargs}


def _balanced(test: unittest.TestCase, html: str, tags=("table", "thead", "tbody", "tr", "form", "pre", "details", "dl", "section")):
    for t in tags:
        test.assertEqual(len(re.findall(rf"<{t}[\s>]", html)), html.count(f"</{t}>"), t)


def _node_check(test: unittest.TestCase, js: str):
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(js)
        p = Path(tf.name)
    try:
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        test.assertEqual(r.returncode, 0, msg=r.stderr)
    finally:
        p.unlink(missing_ok=True)


class SanitizeAndCacheTests(unittest.TestCase):
    def test_dirty_url_filtering(self):
        bad = ["http://localhost:3000", "http://127.0.0.1/", "https://0.0.0.0:8080", "http://192.168.1.2/", "https://example.com/",
               "ftp://x.com/a", "javascript:alert(1)", "https://docs.newapi.pro/", "https://newapi.pro/zh/docs",
               "https://github.com/QuantumNous/new-api", "https://github.com/songquanpeng/one-api", "", None]
        for u in bad:
            self.assertIsNone(P.sanitize_station_url(u, "x.com"), u)
        self.assertEqual(P.sanitize_station_url("https://api.x.com/v1", "x.com"), "https://api.x.com/v1")
        self.assertEqual(P.sanitize_station_url("/topup", "x.com"), "https://x.com/topup")
        self.assertEqual(P.sanitize_station_url("https://docs.newapi.pro/x", "newapi.pro"), "https://docs.newapi.pro/x")
        self.assertEqual(P.sanitize_station_url("https://newapi.prorisehub.com/", "prorisehub.com"), "https://newapi.prorisehub.com/")
        _, meta = P.normalize_station("x.com", PRICING, STATUS)
        self.assertIsNone(meta["docs_link"])
        self.assertIsNone(meta["server_address"])
        self.assertEqual(meta["top_up_link"], "https://x.com/topup")

    def test_cache_write_from_ratio_and_expr(self):
        self.assertEqual(P.parse_billing_expr('tier("base", p * 2 + c * 8 + cw * 2.5)').cache_write_usd, 2.5)
        self.assertEqual(P.parse_billing_expr('tier("base", p * 2 + c * 8 + cc * 3.75)').cache_write_usd, 3.75)
        self.assertIsNone(P.parse_billing_expr('tier("base", p * 2 + c * 8)').cache_write_usd)
        qs, _ = P.normalize_station("x.com", PRICING, STATUS)
        by = {q.model: q for q in qs}
        self.assertEqual(by["claude-sonnet-5-5"].cache_write_ratio, 1.25)
        self.assertAlmostEqual(by["claude-sonnet-5-5"].cache_write_cny, 1.5)
        self.assertAlmostEqual(by["gemini-4-carbon"].cache_write_cny, 1.0)
        self.assertEqual(by["gemini-4-carbon"].billing_mode, "tiered_expr")
        self.assertIn("cc * 2.5", by["gemini-4-carbon"].billing_expr)
        for k in ("cache_creation_ratio", "cache_creation_12h_ratio"):
            self.assertEqual(P.cache_write_ratio_of({k: 2}), 2.0)

    def test_cache_ratio_zero_disambiguation(self):
        qs, meta = P.normalize_station("x.com", PRICING, STATUS)
        by = {q.model: q for q in qs}
        self.assertEqual(by["claude-sonnet-5-5"].cache_status, "priced")
        self.assertEqual(by["gpt-6-astra"].cache_status, "unknown")  # companion cache-write 0 → Go struct zero
        self.assertIsNone(by["gpt-6-astra"].cache_cny)
        self.assertIsNone(by["gpt-6-astra"].cache_ratio)
        self.assertEqual(by["deepseek-v4.1-flash"].cache_status, "free")  # explicit ×0 beside positive cache ratios
        self.assertEqual(by["deepseek-v4.1-flash"].cache_cny, 0.0)
        self.assertEqual(by["img"].cache_status, "na")
        self.assertEqual(by["glm-5"].cache_status, "unknown")
        uniform = {"data": [{"model_name": f"m{i}", "quota_type": 0, "model_ratio": 1, "cache_ratio": 0, "enable_groups": ["default"]}
                            for i in range(5)], "group_ratio": {"default": 1}}
        qs2, _ = P.normalize_station("y.com", uniform, {"price": 1})
        self.assertTrue(all(q.cache_status == "unknown" and q.cache_cny is None for q in qs2))
        self.assertEqual(P.cache_zero_kind({"cache_ratio": 0, "quota_type": 1}, 0.1, True), "na")
        self.assertEqual(P.cache_zero_kind({"cache_ratio": 0, "create_cache_ratio": 0}, 0.1, True), "struct")
        self.assertEqual(P.cache_zero_kind({"cache_ratio": 0}, 0.1, True), "free")
        self.assertEqual(P.cache_zero_kind({"cache_ratio": 0}, 0.1, False), "struct")
        self.assertEqual((meta["cache_read_coverage"], meta["cache_write_coverage"], meta["cache_eligible_models"]), (60, 40, 5))

    def test_topup_mode_and_meta(self):
        self.assertEqual(P.topup_mode(1.0, 7.3), "discount")
        self.assertEqual(P.topup_mode(7.0, 7.3), "fx")
        self.assertEqual(P.topup_mode(5.0, 7.3), "custom")
        self.assertEqual(P.topup_mode(None, 7.3), "unknown")
        self.assertEqual(P.topup_mode(7.2, 1), "fx")  # implausible station FX → reference 7.25
        _, meta = P.normalize_station("x.com", PRICING, STATUS)
        self.assertEqual((meta["topup_mode"], meta["usd_exchange_rate"], meta["quota_per_unit"], meta["quota_display_type"]),
                         ("discount", 7.3, 500000, "USD"))
        self.assertEqual((meta["email_verification"], meta["turnstile_check"]), (True, False))

    def test_formula_round_trip(self):
        qs, _ = P.normalize_station("x.com", PRICING, STATUS)
        n = 0
        for q in qs:
            f = P.format_formula(q, 1.0)
            m = re.match(r"模型×([0-9.e+-]+) × 2 × 分组×([0-9.e+-]+) × 充值¥([0-9.e+-]+) = ¥([0-9.e+-]+)", f)
            if q.kind == "token" and q.model_ratio is not None:
                self.assertTrue(m, f)
                mr, gr, tp, out = map(float, m.groups())
                self.assertLess(abs(mr * 2 * gr * tp - q.input_cny), 1e-5)
                self.assertLess(abs(out - q.input_cny), 1e-5)
                n += 1
        self.assertEqual(n, 4)
        expr = next(q for q in qs if q.model == "gemini-4-carbon")
        self.assertIn("表达式「base」 p×2", P.format_formula(expr, 1.0))
        call = next(q for q in qs if q.model == "img")
        self.assertIn("按次$0.02 × 分组×0.4 × 充值¥1 = ¥0.008/次", P.format_formula(call, 1.0))
        self.assertIn("充值比例未公开", P.format_formula(qs[0], None))


class RenderTests(unittest.TestCase):
    def test_three_valued_cells_and_ratio_view(self):
        qs, st = _station()
        site = DummySite()
        html = B.quote_table(site, qs, {"x.com": st}, show_model=True, show_station=False, table_id="s")
        for txt in ("未公开", "免费", "缓存写", "ratio-col", "cny-x", "公式溯源", "模型×1.5 × 2 × 分组×0.4", "tiered_expr"):
            self.assertIn(txt, html)
        self.assertNotIn('data-v="9e9">—</td><td class="n cny-x" data-v="9e9">—', html)
        _balanced(self, html)
        model_view = B.quote_table(site, qs, {"x.com": st}, show_model=False, show_station=True, table_id="m")
        self.assertNotIn("ratio-col", model_view)
        self.assertNotIn("缓存写", model_view)
        q0 = qs[0]
        self.assertEqual(B.three_valued(q0, None, None), "unknown")
        self.assertEqual(B.three_valued(q0, 0.0, 0.0), "free")
        self.assertIn("不适用", B.three_cell("na"))
        self.assertIn('data-v="0"', B.three_cell("free"))
        self.assertIn('data-v="9e9"', B.three_cell("unknown", " cny-x"))
        self.assertEqual(B.three_valued(P.Quote(domain="d", model="m", canonical="m", family="x", kind="call", group=None,
                                                group_ratio=None, group_desc=None), None, None), "na")
        bar = B.filter_bar("s", show_view=True)
        self.assertIn('data-view="ratio"', bar)
        self.assertIn("人民币价格视角", bar)
        self.assertNotIn("data-view", B.filter_bar("m"))

    def test_fact_sheet_blocks_and_conditional_rows(self):
        qs, st = _station()
        st["config_changes"] = [{"run_at": "2026-11-20T00:00:00+00:00", "text": "充值比例 ¥0.8→¥1/$1"}]
        html = B.fact_sheet("x.com", st, qs)
        for txt in ("① 计费与汇率口径", "② 接入与官方入口", "③ 账号与风控门槛", "④ 协议与缓存覆盖", "折价额度", "500,000", "充值入口",
                    "https://x.com/topup", "2026-11-20", "充值比例 ¥0.8→¥1/$1", "60%", "40%", "Anthropic", "注册需邮箱验证", "✓ GitHub"):
            self.assertIn(txt, html)
        self.assertNotIn("newapi.pro", html)
        self.assertNotIn("localhost", html)
        self.assertEqual(html.count("<div"), html.count("</div>"))
        _balanced(self, html)
        st2 = dict(st, top_up_link=None, config_changes=[], email_verification=None)
        html2 = B.fact_sheet("x.com", st2, qs)
        self.assertNotIn("充值入口", html2)
        self.assertIn("近 30 天未观测到变动", html2)
        self.assertIn("未公开", html2)

    def test_rank_table_facets_and_compare_column(self):
        qs, st = _station("a.test")
        _, st_b = _station("b.test", bias=-30)
        st_b.update(endpoints=["openai"], github_oauth=False, turnstile_check=True, topup_mode="fx",
                    cache_read_coverage=0, cache_write_coverage=0)
        stations = {"a.test": st, "b.test": st_b}
        html = B.station_rank_table(stations, {}, NOW, intel={"a.test": {"domain_age_days": 800}})
        self.assertEqual(html.count("<tr>"), html.count("</tr>"))
        f_a = re.search(r'data-f="([^"]*)">1<', html).group(1).split()
        self.assertTrue({"ep-a", "ep-o", "cw", "stab", "age1", "easy"} <= set(f_a))
        f_b = re.search(r'data-f="([^"]*)">2<', html).group(1).split()
        self.assertEqual(set(f_b) & {"ep-a", "cw", "age1", "easy", "gb"}, set())
        self.assertIn("ep-o", f_b)
        self.assertIn('data-cmp="a.test"', html)
        self.assertIn('<th class="cmp" scope="col"', html)
        compact = B.station_rank_table(stations, {}, NOW, compact=True)
        self.assertNotIn("data-f=", compact)
        self.assertNotIn("data-cmp", compact)
        bar = B.facet_bar(2)
        for txt in ("按使用场景", "按选站偏好", "写代码 / Claude Code", "日常聊天 / 翻译", "Gemini 模型", "长对话省钱缓存",
                    "省钱优选（前 30% 便宜）", "老牌稳用（1 年以上）", "近期没涨价", "高分优先（B 级+）", "好注册（支持快捷登录）",
                    "当前显示 2 / 2 家站点", "重置筛选", 'href="/compare/"'):
            self.assertIn(txt, bar)
        for bad in ("下载 JSON", "导出 CSV", "/data/", "Turnstile", "OpenAI Responses"):
            self.assertNotIn(bad, bar)
        self.assertEqual({t for _, chips in B.FACETS for t, _ in chips},
                         {"ep-a", "ep-o", "ep-g", "cw", "cheap", "age1", "stab", "gb", "easy", "free", "noemail", "pure"})
        self.assertEqual(bar.count("<div"), bar.count("</div>"))
        self.assertIn("已选", B.compare_bar())
        self.assertIn("开始并排对比", B.compare_bar())

    def test_station_config_change_tracking(self):
        old = {"price": 0.8, "fx": 7.3, "groups": {"default": 1.0, "vip": 0.5, "a": 1, "b": 1}}
        new = {"price": 1.0, "fx": 7.3, "groups": {"default": 1.2, "vip": 0.5, "a": 2, "b": 3}}
        txt = B.diff_station_config(old, new)
        self.assertEqual(txt[0], "充值比例 ¥0.8→¥1/$1")
        self.assertIn("分组倍率 a ×1→×2、b ×1→×3 等 3 组", txt[1])
        self.assertEqual(B.diff_station_config(old, old), [])
        self.assertEqual(B.diff_station_config({"price": None}, {"price": 1}), [])
        with tempfile.TemporaryDirectory() as td:
            sp, lp = Path(td) / "state.json", Path(td) / "log.json"
            st = {"topup_price": 0.8, "usd_exchange_rate": 7.3, "groups": [{"name": "default", "ratio": 1.0}]}
            B.update_config_changes({"x.com": st}, NOW, sp, lp)
            self.assertEqual(st["config_changes"], [])
            st2 = {"topup_price": 1.0, "usd_exchange_rate": 7.0, "groups": [{"name": "default", "ratio": 1.0}]}
            log = B.update_config_changes({"x.com": st2}, NOW, sp, lp)
            self.assertEqual(log["x.com"][0]["text"], "充值比例 ¥0.8→¥1/$1；站内汇率 7.3→7")
            self.assertEqual(st2["config_changes"][0]["run_at"][:10], "2026-11-27")
            self.assertEqual(json.loads(lp.read_text(encoding="utf-8")), log)

    def test_about_documents_no_download_and_three_values(self):
        site = DummySite()
        B.build_about(site, {})
        body = site.pages["/about/"]["body"]
        for txt in ("不提供批量数据下载或导出", "三值缓存口径", "未公开", "不适用", "cache_creation_ratio", "/compare/"):
            self.assertIn(txt, body)
        for bad in ("/data/stations.json", "/data/stations.csv", "下载 JSON", "导出 CSV"):
            self.assertNotIn(bad, body)
        for tag in ("table", "tr", "ul", "li", "p", "section"):
            self.assertEqual(body.count(f"<{tag}>") + body.count(f"<{tag} "), body.count(f"</{tag}>"), tag)


class InlineCompareDataTests(unittest.TestCase):
    def _payload(self, n: int = 764):
        stations, by_station = {}, {}
        for i in range(n):
            dom = f"relay{i:03d}.ai.cn"
            qs, st = _station(dom, bias=(i % 37) - 18)
            st["server_address"] = f"https://api.{dom}"
            st["docs_link"] = f"https://docs.{dom}/guide"
            st["cm"] = {"off_pct": 10.0 + i % 120, "cheaper_pct": (i * 7) % 101}
            st["config_changes"] = [{"run_at": "2026-11-20T00:00:00+00:00", "text": "分组倍率 default ×1→×1.2"}] if i % 5 == 0 else []
            stations[dom], by_station[dom] = st, qs
        return CP.stations_payload(stations, by_station, {}, {}, NOW, B.FLAGSHIP_MODELS)

    def test_display_only_schema_and_size_budget(self):
        payload = self._payload()
        raw = CP.dumps(payload)
        self.assertLessEqual(CP.gz_size(payload), CP.MAX_GZ_BYTES)
        obj = json.loads(raw)
        self.assertEqual(obj["v"], CP.SCHEMA_VERSION)
        self.assertEqual(obj["cols"], CP.COL_NAMES)
        self.assertEqual(len(obj["rows"]), 764)
        self.assertTrue(all(len(r) == len(obj["cols"]) for r in obj["rows"]))
        for key in CP.FORBIDDEN_KEYS:
            self.assertNotIn(key, obj["cols"])
        for leak in ("https://api.relay", "docs.relay", "/topup", "quota_per_unit", "billing_expr", "v0.9.1", "group_ratio"):
            self.assertNotIn(leak, raw)
        ix = {c: i for i, c in enumerate(obj["cols"])}
        r0 = obj["rows"][0]
        self.assertEqual(r0[ix["mode"]], "discount")
        self.assertEqual(r0[ix["eps"]], "aor")
        self.assertEqual(r0[ix["login"]], [1, 0, 0])
        self.assertIsInstance(r0[ix["off"]], (int, float))
        self.assertTrue(0 <= r0[ix["chp"]] <= 100)
        fs = {c[0]: c for c in r0[ix["fs"]]}
        self.assertEqual(sorted(fs), [0, 2, 4, 5])
        self.assertTrue(all(len(c) == 4 for c in fs.values()))
        self.assertEqual(fs[0][1:4], [1.2, 6, 0.12])
        self.assertIsNone(fs[2][3])  # struct-zero → 未公开 (null), never 0
        self.assertEqual(fs[4][3], 0)  # explicit ×0 → 免费
        scores = [r[ix["score"]] for r in obj["rows"]]
        self.assertEqual(scores, sorted(scores, reverse=True))  # rank is implied by row order
        self.assertFalse(hasattr(CP, "stations_csv"))

    def test_dumps_cannot_break_out_of_script(self):
        payload = {"rows": [["x.test", "</script><script>alert(1)</script><!--"]]}
        txt = CP.dumps(payload)
        self.assertNotIn("</script>", txt)
        self.assertNotIn("<!--", txt)
        self.assertEqual(json.loads(txt), payload)

    def test_compare_page_inline_data_and_js_parses(self):
        site = DummySite()
        payload = self._payload(5)
        CP.build_compare(site, payload)
        body = site.pages["/compare/"]["body"]
        for txt in ('id="compare-app"', 'id="cmp-data"', "只看差异项", "复制对比链接", 'href="/submit/"', "预载靠谱度排名前 3",
                    "claude-sonnet-5-5"):
            self.assertIn(txt, body)
        for bad in ("data-src", "/data/stations.json", "下载 JSON", "导出 CSV", "fetch(", "https://api.relay"):
            self.assertNotIn(bad, body)
        self.assertEqual(VD.jargon_hits(body), [])
        _balanced(self, body)
        blob = re.search(r'<script type="application/json" id="cmp-data">(.*?)</script>', body, re.S).group(1)
        self.assertEqual(json.loads(blob)["cols"], CP.COL_NAMES)
        js = re.findall(r"<script>(.*?)</script>", body, re.S)
        self.assertEqual(len(js), 1)
        for marker in ("slice(0, 3)", "MAX = 4", "history.replaceState", '"?s="', "onlyDiff", "best", "data-rm", "data-mv",
                       '$("cmp-data")', "JSON.parse"):
            self.assertIn(marker, js[0])
        for bad in ("Stage 1", "Turnstile", "quota_per_unit", "OpenAI Responses", "/data/"):
            self.assertNotIn(bad, js[0])
        _node_check(self, js[0])

    def test_nav_links_compare(self):
        import inspect
        self.assertIn('<a href="/compare/">对比</a>', inspect.getsource(B.Site.page))

    def test_verify_dist_zero_download_check(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            site = DummySite()
            CP.build_compare(site, self._payload(3))
            (d / "compare").mkdir()
            (d / "compare" / "index.html").write_text(site.pages["/compare/"]["body"], encoding="utf-8")
            (d / "stations").mkdir()
            (d / "stations" / "index.html").write_text('<div id="bubble-chart"></div><script type="application/json" id="bubble-data">{}</script>'
                                                       + B.facet_bar(3), encoding="utf-8")
            (d / "robots.txt").write_text("User-agent: *\nAllow: /\nDisallow: /data/\n", encoding="utf-8")
            self.assertEqual(VD.check_zero_download(d), [])
            (d / "data").mkdir()
            (d / "data" / "stations.json").write_text("{}", encoding="utf-8")
            (d / "data" / "stations.csv").write_text("a", encoding="utf-8")
            (d / "robots.txt").write_text("User-agent: *\nAllow: /data/stations.json\n", encoding="utf-8")
            (d / "stations" / "index.html").write_text('<a href="/data/stations.json">下载 JSON</a>', encoding="utf-8")
            errs = " | ".join(VD.check_zero_download(d))
            for frag in ("data/stations.json must not", "data/stations.csv must not", "dist/data/ must stay empty",
                         "Allow: /data/ present", "Disallow: /data/ missing", "下载 JSON present", 'id="bubble-data" missing'):
                self.assertIn(frag, errs)
        self.assertEqual(VD.SMOKE["/compare/"], 'id="compare-app"')

    def test_app_js_facets_compare_and_view_toggle(self):
        src = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
        for marker in ('getElementById("facets")', "td[data-f]", "当前显示 ", 'getElementById("cmp-bar")', "/compare/?s=",
                       "CMP_MAX = 4", "view-ratio", "button[data-view]", "facet-reset"):
            self.assertIn(marker, src)
        r = subprocess.run(["node", "--check", str(ROOT / "assets" / "app.js")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        css = (ROOT / "assets" / "style.css").read_text(encoding="utf-8")
        for sel in (".ratio-col{display:none}", ".view-ratio .ratio-col", ".fs-grid", ".chip[aria-pressed=true]", ".cmp-bar", ".cmp-mx td.best"):
            self.assertIn(sel, css)


if __name__ == "__main__":
    unittest.main()
