import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import scoring as S  # noqa: E402


class ScoringTests(unittest.TestCase):
    def test_price_score_anchor(self):
        self.assertEqual(S.price_score(1.0, 1.0), 50.0)
        self.assertEqual(S.price_score(0.5, 1.0), 70.0)
        self.assertEqual(S.price_score(2.0, 1.0), 30.0)
        self.assertEqual(S.price_score(0.01, 1.0), 100.0)  # clamped
        self.assertEqual(S.price_score(0.25, 1.0, anomaly=True), 50.0)  # anomalies get no bonus
        self.assertIsNone(S.price_score(None, 1.0))

    def test_quote_score_blend_and_stage1_cap(self):
        self.assertEqual(S.quote_score(100, 50, {"price": 40, "station": 60}), 70.0)
        self.assertEqual(S.quote_score(None, 80, {"price": 40, "station": 60}), 60.0)
        self.assertEqual(S.quote_score(100, 74, {"price": 40, "station": 60}, cap=S.STAGE1_CAP), 74.0)

    def test_cheap_unreliable_loses_to_reliable(self):
        cheap_bad = S.quote_score(S.price_score(0.25, 1.0), 30)
        fair_good = S.quote_score(S.price_score(0.8, 1.0), 85)
        self.assertGreater(fair_good, cheap_bad)

    def test_stability_bayes_and_penalty(self):
        new = S.score_stability(1, 1, 0).score
        proven = S.score_stability(20, 20, 0).score
        self.assertLess(new, proven)
        self.assertLess(S.score_stability(5, 3, 2).score, S.score_stability(5, 3, 0).score)

    def test_longevity(self):
        self.assertFalse(S.score_longevity(None).known)
        self.assertEqual(S.score_longevity(1095).score, 100)
        self.assertAlmostEqual(S.score_longevity(365).score, 75)

    def test_stage1_caps_unprobed_station_at_b74(self):
        dims = [S.Dim("stability", 90, ""), S.Dim("longevity", 80, ""), S.Dim("transparency", 70, ""),
                S.Dim("reputation", 60, ""), S.Dim("latency", 100, "")]
        sc = S.station_score(dims, S.DEFAULT_STATION_WEIGHTS, attempts=12)
        expected_raw = (90 * 30 + 80 * 25 + 70 * 20 + 60 * 15 + 100 * 10) / 100
        self.assertAlmostEqual(sc.raw_total, expected_raw)
        self.assertEqual(sc.stage, 1)
        self.assertEqual(sc.total, 74.0)
        self.assertEqual(sc.grade, "B")
        self.assertEqual(sc.confidence, "高")

    def test_stage2_probe_unlocks_grade_a_and_s(self):
        dims = [S.Dim("stability", 95, ""), S.Dim("longevity", 90, ""), S.Dim("transparency", 90, ""),
                S.Dim("reputation", 80, ""), S.Dim("latency", 95, "")]
        good_probe = {
            "window_days": 7, "total_samples": 40, "success_rate": 0.99,
            "avg_ttft_ms": 750.0, "tool_call_pass_rate": 1.0, "cache_hit_rate": 1.0,
            "billing_drift_pct": 1.2, "passed_stage2": True,
        }
        sc = S.station_score(dims, S.DEFAULT_STATION_WEIGHTS, attempts=12, probe=good_probe)
        self.assertEqual(sc.stage, 2)
        self.assertGreaterEqual(sc.total, 85.0)
        self.assertEqual(sc.grade, "S")
        self.assertEqual(sc.cap_reason, "")

    def test_stage2_failed_probe_triggers_non_compensatory_veto(self):
        dims = [S.Dim("stability", 98, ""), S.Dim("longevity", 95, ""), S.Dim("transparency", 95, ""),
                S.Dim("reputation", 90, ""), S.Dim("latency", 95, "")]
        bad_probe = {
            "window_days": 7, "total_samples": 30, "success_rate": 0.72,
            "avg_ttft_ms": 4000.0, "billing_drift_pct": 28.5, "passed_stage2": False,
        }
        sc = S.station_score(dims, S.DEFAULT_STATION_WEIGHTS, attempts=12, probe=bad_probe)
        self.assertEqual(sc.stage, 2)
        self.assertLessEqual(sc.total, S.STAGE2_FAIL_CAP)
        self.assertEqual(sc.grade, "C")
        self.assertIn("降级", sc.cap_reason)

    @staticmethod
    def _top_dims():
        return [S.Dim("stability", 99, ""), S.Dim("longevity", 99, ""), S.Dim("transparency", 99, ""),
                S.Dim("reputation", 99, ""), S.Dim("latency", 99, "")]

    @staticmethod
    def _perfect_probe(**kw):
        probe = {"window_days": 7, "total_samples": 60, "success_rate": 1.0, "avg_ttft_ms": 200.0,
                 "tool_call_pass_rate": 1.0, "cache_hit_rate": 1.0, "billing_drift_pct": 0.0, "passed_stage2": True,
                 "auth_pass_rate": 1.0, "iq_pass_rate": 1.0, "context_pass_rate": 1.0, "tamper_accepted_count": 0,
                 "verified_tier": "L3"}
        probe.update(kw)
        return probe

    def test_authenticity_iq_pass_keeps_grade_s(self):
        sc = S.station_score(self._top_dims(), S.DEFAULT_STATION_WEIGHTS, attempts=12, probe=self._perfect_probe())
        self.assertEqual((sc.stage, sc.grade, sc.cap_reason), (2, "S", ""))
        self.assertIn("已验证 L3", sc.dims["probe"].evidence)
        self.assertIn("智商抽检通过 100%", sc.dims["probe"].evidence)

    def test_authenticity_iq_veto_overrides_perfect_latency_and_uptime(self):
        cases = {
            "tamper": {"tamper_accepted_count": 1, "verified_tier": "L0"},
            "auth": {"auth_pass_rate": 0.80, "verified_tier": "L0"},
            "iq": {"iq_pass_rate": 0.5, "verified_tier": "L1"},
            "alert": {"downgrade_alert": True, "downgrade_reasons": ["近 24h 智商通过率 0%（低于 65%）"]},
        }
        for name, kw in cases.items():
            with self.subTest(name):
                sc = S.station_score(self._top_dims(), S.DEFAULT_STATION_WEIGHTS, attempts=12,
                                     probe=self._perfect_probe(passed_stage2=False, **kw))
                self.assertEqual(sc.stage, 2)
                self.assertLessEqual(sc.total, S.STAGE2_FAIL_CAP)
                self.assertEqual(sc.grade, "C")
                self.assertIn("实测触发真伪/智商否决，强制降级封顶 C 级", sc.cap_reason)

    def test_boundary_rates_do_not_veto(self):
        probe = self._perfect_probe(auth_pass_rate=0.85, iq_pass_rate=0.65)
        _, stage, cap = S.score_probe(probe)
        self.assertEqual((stage, cap), (2, ""))

    def test_authenticity_and_iq_outweigh_latency(self):
        slow_honest = S.score_probe(self._perfect_probe(avg_ttft_ms=9000.0))[0].score
        fast_downgraded = S.score_probe(self._perfect_probe(iq_pass_rate=0.7, auth_pass_rate=0.86))[0].score
        self.assertGreater(slow_honest, fast_downgraded)
        legacy = S.score_probe({"window_days": 7, "total_samples": 40, "success_rate": 0.99, "avg_ttft_ms": 750.0,
                                "tool_call_pass_rate": 1.0, "cache_hit_rate": 1.0, "billing_drift_pct": 1.2,
                                "passed_stage2": True})
        self.assertNotIn("真伪", legacy[0].evidence)
        self.assertEqual(legacy[2], "")


if __name__ == "__main__":
    unittest.main()
