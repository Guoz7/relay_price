import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import build as B  # noqa: E402
import pricing as P  # noqa: E402
import scoring as S  # noqa: E402
import station_insights as SI  # noqa: E402
import verify_model as VM  # noqa: E402
import verify_page as VP  # noqa: E402


class DummySite:
    def __init__(self):
        self.cfg = {"site_name": "API 比价雷达", "base_url": "https://agentied.work"}
        self.now = datetime(2026, 11, 27, 0, 0, tzinfo=timezone.utc)
        self.pages = {}

    def page(self, path, title, desc, body, **kwargs):
        self.pages[path] = {"title": title, "desc": desc, "body": body, "kwargs": kwargs}


class VerifyAndInsightsTests(unittest.TestCase):
    def test_verify_model_helpers(self):
        self.assertEqual(VM.normalize_base_url("api.example.com/v1/messages/"), "https://api.example.com")
        self.assertEqual(VM.normalize_base_url("https://relay.test/v1"), "https://relay.test")
        self.assertEqual(VM.detect_protocol("claude-sonnet-5-5"), "anthropic")
        self.assertEqual(VM.detect_protocol("gpt-6-astra"), "openai-response")
        self.assertEqual(VM.detect_protocol("deepseek-v4.1-flash"), "openai")
        self.assertTrue(VM.model_echo_matches("claude-sonnet-5-5", "claude-sonnet-5.5-20261001"))
        self.assertFalse(VM.model_echo_matches("claude-sonnet-5-5", "deepseek-v4-chat"))

    def test_verify_page_renders_cleanly(self):
        import re, subprocess
        site = DummySite()
        VP.build_verify(site)
        self.assertIn("/verify/", site.pages)
        body = site.pages["/verify/"]["body"]
        self.assertIn('id="verify-app"', body)
        self.assertIn("/api/verify", body)
        self.assertIn("verify_model.py", body)
        for tier in ("l1_auth", "l2_iq", "l3_deep"):
            self.assertIn(f'data-vtier="{tier}"', body)
        for label in ("L1 快速真伪", "L2 真伪 + 智商抽检", "L3 深度防降级", "推荐", "真伪特征分", "智商保真分"):
            self.assertIn(label, body)
        self.assertIn('id="v-gauges"', body)
        self.assertIn("--tier", body)
        self.assertIn("--reference-url", body)
        self.assertIn('id="v-seed"', body)
        for cid in VM.ALL_CHECKS:
            self.assertIn(f'value="{cid}"', body)
        scripts = re.findall(r"<script>(.*?)</script>", body, re.S)
        self.assertTrue(scripts)
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
            tf.write("\n".join(scripts))
            js_path = Path(tf.name)
        try:
            r = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, msg=r.stderr)
        finally:
            js_path.unlink(missing_ok=True)

    def test_price_stability_classification(self):
        changes = [
            {"domain": "steady.test", "type": "down", "run_at": "2026-11-20T00:00:00Z", "old": ["token", 2.0, 8.0, None, 2.0, 8.0, None, "default"], "new": ["token", 1.5, 6.0, None, 1.5, 6.0, None, "default"]},
            {"domain": "hike.test", "type": "up", "run_at": "2026-11-25T00:00:00Z", "old": ["token", 1.0, 4.0, None, 1.0, 4.0, None, "default"], "new": ["token", 1.8, 7.2, None, 1.8, 7.2, None, "default"]},
        ]
        m = SI.compute_price_stability(changes)
        self.assertEqual(m["steady.test"]["cls"], "ok")
        self.assertEqual(m["steady.test"]["downs"], 1)
        self.assertEqual(m["hike.test"]["cls"], "danger")
        self.assertEqual(m["hike.test"]["max_up_pct"], 80.0)

    def test_domain_age_formatting(self):
        short, long_s, cls = SI.fmt_domain_age(95)
        self.assertEqual(short, "95 天")
        self.assertIn("新站预警", long_s)
        self.assertEqual(cls, "warn")
        short2, _, cls2 = SI.fmt_domain_age(820)
        self.assertEqual(short2, "2.2 年")
        self.assertEqual(cls2, "ok")
        self.assertEqual(SI.fmt_domain_age(None)[0], "未知")

    def test_load_reviews_and_station_bento(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "reviews.json").write_text(json.dumps({
                "reviews": [
                    {"domain": "demo.test", "rating": 5, "model": "claude-sonnet-5-5", "author": "alice", "date": "2026-11-26", "summary": "Thinking sig & cache pass."},
                ]
            }), encoding="utf-8")
            revs = SI.load_reviews(p)
            self.assertEqual(len(revs["demo.test"]), 1)

            dims = [S.Dim("stability", 90, ""), S.Dim("longevity", 80, ""), S.Dim("transparency", 90, ""),
                    S.Dim("reputation", 70, ""), S.Dim("latency", 90, "")]
            st = {"display_name": "Demo", "score": S.station_score(dims, S.DEFAULT_STATION_WEIGHTS, attempts=10)}
            q = P.Quote(domain="demo.test", model="claude-sonnet-5-5", canonical="claude-sonnet-5-5", family="claude",
                        kind="token", group="default", group_ratio=1.0, group_desc=None)
            it = {"domain_age_days": 730, "domain_registered_at": "2024-11-27", "domain_expires_at": "2027-11-27",
                  "registrar": "Cloudflare", "tranco_rank": 120000, "sources": ["linux.do"]}
            html = B._station_bento("demo.test", st, [q], it, {"attempts": 10, "successes": 10, "ms": [420]}, None, revs["demo.test"])
            self.assertIn("价格稳定性", html)
            self.assertIn("Cloudflare", html)
            self.assertIn("Tranco #120,000", html)
            self.assertIn("/verify/?base_url=https%3A%2F%2Fdemo.test", html)
            self.assertIn("★ 5.0 / 5", html)
            self.assertIn("@alice", html)
            self.assertIn("LLM 模型真伪与智商抽检", html)
            self.assertIn("L0", html)
            self.assertEqual(html.count("<div"), html.count("</div>"))

    def test_probe_panel_and_bento_render_authenticity_iq_tiers(self):
        probe = {"window_days": 7, "total_samples": 30, "ok_samples": 30, "success_rate": 1.0, "avg_ttft_ms": 200.0,
                 "tool_call_pass_rate": 1.0, "cache_hit_rate": 1.0, "billing_drift_pct": 0.5, "passed_stage2": False,
                 "auth_samples": 8, "auth_pass_rate": 0.875, "iq_samples": 6, "iq_pass_rate": 0.5,
                 "context_samples": 0, "context_pass_rate": None, "tamper_accepted_count": 1, "verified_tier": "L0",
                 "downgrade_alert": True, "downgrade_reasons": ["近 24h 智商通过率 0%（低于 65%）"],
                 "last_probed_at": "2026-11-26T10:00:00+00:00", "daily": [["2026-11-26", 30, 30]]}
        dims = [S.Dim("stability", 95, ""), S.Dim("longevity", 95, ""), S.Dim("transparency", 95, ""),
                S.Dim("reputation", 95, ""), S.Dim("latency", 95, "")]
        sc = S.station_score(dims, S.DEFAULT_STATION_WEIGHTS, attempts=12, probe=probe)
        self.assertLessEqual(sc.total, S.STAGE2_FAIL_CAP)
        panel = B.probe_panel(sc)
        for txt in ("已验证档位", "真伪通过率", "智商抽检 / 长文多跳", "篡改签名被接受次数", "88%", "50% / —", "近 24h 降级告警"):
            self.assertIn(txt, panel)
        self.assertEqual(panel.count("<div"), panel.count("</div>"))
        html = B._station_bento("demo.test", {"display_name": "Demo", "score": sc}, [], {}, {}, None, [])
        self.assertIn("LLM 模型真伪与智商抽检", html)
        self.assertIn("篡改被接受 1 次", html)
        self.assertEqual(html.count("<div"), html.count("</div>"))

    def test_about_page_explains_tiers_and_veto(self):
        site = DummySite()
        B.build_about(site, {})
        body = site.pages["/about/"]["body"]
        for txt in ("L0 · 未验证", "L1 · 快速真伪", "L2 · 真伪 + 智商抽检", "L3 · 深度防降级", "真伪特征分", "智商保真分",
                    "真伪/智商否决", "双窗口", "预算熔断", "Mulberry32"):
            self.assertIn(txt, body)
        for tag in ("table", "tr", "ul", "li", "p", "section"):
            self.assertEqual(body.count(f"<{tag}>") + body.count(f"<{tag} "), body.count(f"</{tag}>"), tag)


    # ---- stability score / volatility
    @staticmethod
    def _chg(dom, typ, old, new, run="2026-11-20T00:00:00Z", model="m1"):
        return {"domain": dom, "type": typ, "run_at": run, "model": model,
                "old": ["token", old, old * 4, None, old, old * 4, None, "default"],
                "new": ["token", new, new * 4, None, new, new * 4, None, "default"]}

    def test_classify_move_rounding_drop_and_hikes(self):
        self.assertEqual(SI.classify_move(self._chg("x", "up", 1.0, 1.01))[0], "rounding")
        self.assertEqual(SI.classify_move(self._chg("x", "down", 1.0, 0.995))[0], "rounding")
        self.assertEqual(SI.classify_move(self._chg("x", "down", 1.0, 0.7))[0], "drop")
        self.assertEqual(SI.classify_move(self._chg("x", "up", 1.0, 1.1))[0], "minor_hike")
        self.assertEqual(SI.classify_move(self._chg("x", "up", 1.0, 1.3))[0], "sharp_hike")
        self.assertEqual(SI.classify_move(self._chg("x", "up", 1.0, 1.6))[0], "extreme_hike")
        self.assertEqual(SI.classify_move({"type": "new"})[0], "other")
        self.assertEqual(SI.classify_move({"type": "up", "old": None, "new": None})[0], "minor_hike")

    def test_stability_score_bounds_and_determinism(self):
        self.assertEqual(SI.stability_score(minor_events=0, sharp_events=0, max_up_pct=0, hike_models=0), 100)
        worst = SI.stability_score(minor_events=50, sharp_events=50, max_up_pct=300, hike_models=99, outs=99, removed=999)
        self.assertGreaterEqual(worst, 0)
        self.assertLessEqual(worst, 100)
        a = SI.stability_score(minor_events=2, sharp_events=1, max_up_pct=25, hike_models=3)
        self.assertEqual(a, SI.stability_score(minor_events=2, sharp_events=1, max_up_pct=25, hike_models=3))
        self.assertLess(SI.stability_score(minor_events=0, sharp_events=1, max_up_pct=25, hike_models=1),
                        SI.stability_score(minor_events=1, sharp_events=0, max_up_pct=5, hike_models=1))

    def test_stability_benign_vs_hikes(self):
        m = SI.compute_price_stability([
            self._chg("round.test", "up", 1.0, 1.01),
            self._chg("round.test", "down", 1.0, 0.99, model="m2"),
            self._chg("drop.test", "down", 2.0, 1.0),
            self._chg("minor.test", "up", 1.0, 1.1),
            self._chg("freq.test", "up", 1.0, 1.1, run="2026-11-01T00:00:00Z"),
            self._chg("freq.test", "up", 1.0, 1.1, run="2026-11-08T00:00:00Z"),
            self._chg("freq.test", "up", 1.0, 1.1, run="2026-11-15T00:00:00Z"),
            self._chg("freq.test", "up", 1.0, 1.25, run="2026-11-22T00:00:00Z"),
        ])
        r = m["round.test"]
        self.assertEqual((r["stability_score"], r["cls"], r["rounding"], r["hike_events"]), (100, "ok", 2, 0))
        self.assertEqual(r["label"], "价格平稳 · 仅微调")
        self.assertIsNone(r["last_up_at"])
        self.assertIn("舍入微调", r["volatility_summary"])
        d = m["drop.test"]
        self.assertEqual((d["stability_score"], d["cls"], d["label"]), (100, "ok", "平稳 · 近期降价"))
        self.assertTrue(d["volatility_summary"].startswith("近 30 天无涨价"))
        mi = m["minor.test"]
        self.assertEqual((mi["cls"], mi["label"], mi["hike_events"]), ("warn", "近期小幅上调", 1))
        self.assertEqual(mi["stability_score"], 94)
        f = m["freq.test"]
        self.assertEqual((f["hike_events"], f["sharp_events"], f["minor_hikes"], f["sharp_hikes"]), (4, 1, 3, 1))
        self.assertEqual(f["stability_score"], 61)
        self.assertEqual((f["cls"], f["label"]), ("danger", "高频暗涨预警"))
        self.assertEqual(f["last_up_at"], "2026-11-22T00:00:00Z")
        self.assertIn("4 次涨价调整", f["volatility_summary"])
        self.assertEqual(SI.stability_band(100), "ok")
        self.assertEqual(SI.stability_band(70), "warn")
        self.assertEqual(SI.stability_band(10), "danger")
        self.assertEqual(SI.stability_band(None), "")

    def test_hikes_in_same_run_count_as_one_event(self):
        m = SI.compute_price_stability([self._chg("bulk.test", "up", 1.0, 1.1, model=f"m{i}") for i in range(12)])
        b = m["bulk.test"]
        self.assertEqual((b["ups"], b["hike_events"], b["hike_models"]), (12, 1, 12))
        self.assertEqual(b["stability_score"], 86)

    # ---- domain longevity / expiry
    def test_longevity_pct_and_expiry_risk(self):
        self.assertEqual(SI.longevity_pct(0), 0)
        self.assertEqual(SI.longevity_pct(365), 33)
        self.assertEqual(SI.longevity_pct(5000), 100)
        self.assertIsNone(SI.longevity_pct(None))
        self.assertIsNone(SI.longevity_pct(-1))
        self.assertIsNone(SI.longevity_pct(True))
        now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(SI.expiry_risk("2026-12-27", now), {"days_left": 30, "level": "soon"})
        self.assertEqual(SI.expiry_risk("2027-01-26T00:00:00Z", now)["level"], "soon")
        self.assertEqual(SI.expiry_risk("2027-01-27", now)["level"], "ok")
        self.assertEqual(SI.expiry_risk("2026-11-01", now), {"days_left": -26, "level": "expired"})
        self.assertIsNone(SI.expiry_risk("未知", now))
        self.assertIsNone(SI.expiry_risk(None, now))
        self.assertIsNone(SI.expiry_risk("2027-01-01", None))

    # ---- reviews
    def test_review_signals_and_summary(self):
        revs = [
            {"rating": 5, "signals": ["verified", "扣费一致", "bogus"]},
            {"rating": "4", "signals": "暗涨预警：出现未通知暗涨价或分组倍率突变"},
            {"rating": 9, "signals": ["VERIFY", "429"]},
        ]
        self.assertEqual(SI.review_signals(revs[0]), ["verified", "billing_match"])
        self.assertEqual(SI.review_signals(revs[1]), ["price_hike"])
        self.assertEqual(SI.review_signals(revs[2]), ["verified", "rate_limit"])
        self.assertEqual(SI.review_signals({}), [])
        self.assertIsNone(SI.review_rating(revs[2]))
        s = SI.review_summary(revs)
        self.assertEqual((s["count"], s["rated"], s["avg"], s["pct"]), (3, 2, 4.5, 90))
        self.assertEqual([x[0] for x in s["signals"]], ["verified", "billing_match", "price_hike", "rate_limit"])
        self.assertEqual(dict((k, n) for k, _l, _c, n in s["signals"])["verified"], 2)
        self.assertEqual(SI.review_summary([])["avg"], None)

    def test_reviews_json_and_issue_template_consistent(self):
        root = Path(__file__).resolve().parent.parent
        doc = json.loads((root / "data" / "reviews.json").read_text(encoding="utf-8"))
        self.assertIsInstance(doc["reviews"], list)
        self.assertEqual(set(doc["signal_keys"]), set(SI.REVIEW_SIGNALS))
        tpl = (root / ".github" / "ISSUE_TEMPLATE" / "station-review.yml").read_text(encoding="utf-8")
        labels = [ln.split("label:", 1)[1].strip() for ln in tpl.splitlines() if ln.strip().startswith("- label:")]
        self.assertEqual(len(labels), 5)
        keys = [SI.review_signals({"signals": [lb]}) for lb in labels]
        self.assertEqual(sorted(k[0] for k in keys), sorted(SI.REVIEW_SIGNALS))

    # ---- rendered bento & rank table
    def _station(self, dom="demo.test"):
        dims = [S.Dim("stability", 90, ""), S.Dim("longevity", 80, ""), S.Dim("transparency", 90, ""),
                S.Dim("reputation", 70, ""), S.Dim("latency", 90, "")]
        return {"display_name": "Demo", "score": S.station_score(dims, S.DEFAULT_STATION_WEIGHTS, attempts=10),
                "topup_price": 1.0, "parsed": 3, "fetched_at": datetime(2026, 11, 26, tzinfo=timezone.utc)}

    def test_bento_stability_expiry_and_review_signals(self):
        st = self._station()
        stab = SI.compute_price_stability([self._chg("demo.test", "up", 1.0, 1.8, run="2026-11-25T00:00:00Z")])["demo.test"]
        it = {"domain_age_days": 400, "domain_expires_at": "2026-12-20", "sources": []}
        revs = [{"domain": "demo.test", "rating": 4, "author": "bob", "date": "2026-11-20", "model": "gpt-6-astra",
                 "summary": "ok", "signals": ["verified", "price_hike"]}]
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        html = B._station_bento("demo.test", st, [], it, {}, stab, revs, now=now)
        self.assertIn(f'<b class="danger">{stab["stability_score"]}</b>', html)
        self.assertIn('class="meter danger"', html)
        self.assertIn("近期有大幅涨价", html)
        self.assertIn("急涨 ×1", html)
        self.assertIn("最近涨价</dt><dd>2026-11-25", html)
        self.assertIn("23 天后到期", html)
        self.assertIn('aria-valuenow="37"', html)
        self.assertIn("3 年+", html)
        self.assertIn('class="stars" style="--p:80%"', html)
        self.assertIn("验真通过 ×1", html)
        self.assertIn("暗涨预警 ×1", html)
        self.assertIn("★ 4.0 / 5", html)
        self.assertIn("template=station-review.yml", html)
        expired = B._station_bento("demo.test", st, [], {"domain_expires_at": "2026-01-01"}, {}, None, [], now=now)
        self.assertIn("域名已过期", expired)
        self.assertIn('<b class="ok">100</b>', expired)
        self.assertIn("暂无站内实评", expired)
        self.assertNotIn("天后到期", B._station_bento("demo.test", st, [], {"domain_expires_at": "bad"}, {}, None, [], now=now))
        self.assertNotIn("天后到期", B._station_bento("demo.test", st, [], it, {}, None, []))

    def test_station_rank_table_stability_and_sort_hints(self):
        stations = {"a.test": self._station("a.test"), "b.test": self._station("b.test")}
        stab = SI.compute_price_stability([self._chg("a.test", "up", 1.0, 1.8)])
        html = B.station_rank_table(stations, {}, datetime(2026, 11, 27, tzinfo=timezone.utc),
                                    intel={"a.test": {"domain_age_days": 800}}, stability_map=stab)
        self.assertIn('table class="qt sortable rank"', html)
        self.assertIn('data-first="desc"><button type="button">域名时长', html)
        self.assertIn('data-first="desc"', html.split("更新")[0][-60:])
        self.assertIn('data-v="na"', html)
        self.assertIn('data-v="800"', html)
        self.assertIn(f'class="sbar danger" style="--v:{stab["a.test"]["stability_score"]}%"', html)
        self.assertIn('class="sbar ok" style="--v:100%">100<', html)
        self.assertNotIn("30日价格", html)
        self.assertEqual(html.count("<tr>"), html.count("</tr>"))

    def test_app_js_passes_node_check(self):
        import subprocess
        root = Path(__file__).resolve().parent.parent
        for rel in ("assets/app.js", "functions/api/verify.js"):
            r = subprocess.run(["node", "--check", str(root / rel)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, msg=f"{rel}: {r.stderr}")

    def test_app_js_sort_first_desc_and_unknown_last(self):
        import re, subprocess
        root = Path(__file__).resolve().parent.parent
        src = (root / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('th.dataset.first !== "desc"', src)
        fn = re.search(r"g\.sort\(function \(a, b\) \{.*?\n    \}\);", src, re.S).group(0)
        harness = (
            "var num=true,col=0,g=[];function cell(v){return [{children:[{dataset:{v:v},textContent:v}]}]}\n"
            "function run(vals,asc){g=vals.map(cell);" + fn + "return g.map(function(x){return x[0].children[0].dataset.v});}\n"
            "console.log(JSON.stringify([run(['na','400','9e9','1200','na','30'],false),run(['na','400','1200','30'],true)]));"
        )
        r = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        desc, asc = json.loads(r.stdout)
        self.assertEqual(desc[:3], ["1200", "400", "30"])
        self.assertEqual(sorted(desc[3:]), ["9e9", "na", "na"])
        self.assertEqual(asc, ["30", "400", "1200", "na"])


if __name__ == "__main__":
    unittest.main()
