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


if __name__ == "__main__":
    unittest.main()
