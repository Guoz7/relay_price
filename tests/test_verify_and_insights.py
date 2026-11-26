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
        site = DummySite()
        VP.build_verify(site)
        self.assertIn("/verify/", site.pages)
        body = site.pages["/verify/"]["body"]
        self.assertIn('id="verify-app"', body)
        self.assertIn("/api/verify", body)
        self.assertIn("verify_model.py", body)

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


if __name__ == "__main__":
    unittest.main()
