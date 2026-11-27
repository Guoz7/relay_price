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
        w = S.DEFAULT_STATION_WEIGHTS
        expected_raw = sum(d.score * w[d.key] for d in dims) / sum(w[d.key] for d in dims)
        self.assertAlmostEqual(sc.raw_total, round(expected_raw, 1))
        self.assertEqual(sc.stage, 1)
        self.assertLess(sc.total, S.STAGE1_CAP)
        self.assertAlmostEqual(sc.total, round(S.soft_cap(expected_raw), 1))
        self.assertGreater(sc.total, 65.0)
        self.assertEqual(sc.grade, "B")
        self.assertEqual(sc.confidence, "高")
        self.assertEqual(sc.penalty, 0.0)
        self.assertEqual(sc.completeness, 1.0)

    # ---- V3: 7-dim, weak-link penalty, soft-cap
    @staticmethod
    def _seven(**over):
        base = {"stability": 85, "longevity": 95, "transparency": 95, "reputation": 80, "latency": 80,
                "price_stability": 100, "purity_protocol": 90}
        base.update(over)
        return [S.Dim(k, float(v), "") for k, v in base.items()]

    def test_default_weights_cover_seven_dims(self):
        self.assertEqual(set(S.DEFAULT_STATION_WEIGHTS), set(S.DIM_ORDER))
        self.assertEqual(sum(S.DEFAULT_STATION_WEIGHTS.values()), 100)
        for k in S.DIM_ORDER:
            self.assertIn(k, S.DIM_LABELS)

    def test_soft_cap_monotonic_smooth_and_strictly_below_74(self):
        prev = -1.0
        for i in range(0, 1001):
            r = i / 10
            v = S.soft_cap(r)
            self.assertGreater(v, prev - 1e-12)
            self.assertLess(v, S.STAGE1_CAP)
            self.assertLess(round(v, 1), S.STAGE1_CAP)
            prev = v
        self.assertAlmostEqual(S.soft_cap(20), 20, delta=0.01)
        self.assertLess(S.soft_cap(150), S.STAGE1_CAP)
        self.assertEqual(S.soft_cap(150), S.soft_cap(100))  # raw is clamped to [0, 100]
        for r in (30.0, 45.0, 60.0, 72.0, 80.0, 95.0):
            num = (S.soft_cap(r + 1e-4) - S.soft_cap(r - 1e-4)) / 2e-4
            self.assertAlmostEqual(num, S.soft_cap_slope(r), places=3)
            self.assertGreater(S.soft_cap_slope(r), 0)
        top_h = S.SOFT_CAP["top"]
        lo = [r for r in (x / 100 for x in range(5000, 10000)) if S._knee(r, S.SOFT_CAP["knee"], S.SOFT_CAP["slope"], S.SOFT_CAP["tau"]) > top_h][0]
        self.assertAlmostEqual(S.soft_cap(lo - 0.01), S.soft_cap(lo + 0.01), delta=0.02)  # C0 at the top knee

    def test_soft_cap_spreads_top_stations_instead_of_pinning(self):
        totals = [S.station_score(self._seven(reputation=r), attempts=12).total for r in (60, 70, 80, 90, 100)]
        self.assertEqual(len(set(totals)), 5)
        self.assertEqual(totals, sorted(totals))
        self.assertTrue(all(t < S.STAGE1_CAP for t in totals))

    def test_weak_link_penalty_is_convex_and_capped(self):
        self.assertEqual(S.weak_link_penalty({"price_stability": 90})[0], 0.0)
        p1 = S.weak_link_penalty({"price_stability": 75})[0]
        p2 = S.weak_link_penalty({"price_stability": 65})[0]
        p3 = S.weak_link_penalty({"price_stability": 45})[0]
        self.assertGreater(p2 - p1, 0)
        self.assertGreater(p3 - p2, p2 - p1)  # convex drag
        worst, hit = S.weak_link_penalty({"price_stability": 0, "purity_protocol": 0, "transparency": 0, "longevity": 0})
        self.assertEqual(worst, S.WEAK_LINK_MAX)
        self.assertEqual(set(hit), set(S.DEFAULT_WEAK_LINK))
        self.assertEqual(S.weak_link_penalty({"reputation": 0})[0], 0.0)  # non-critical dims never drag

    def test_hike_station_cannot_buy_back_with_reputation(self):
        clean = S.station_score(self._seven(reputation=60), attempts=12)
        hiked = S.station_score(self._seven(reputation=100, price_stability=65), attempts=12)
        self.assertIn("price_stability", hiked.penalized)
        self.assertGreater(hiked.penalty, 0)
        self.assertAlmostEqual(hiked.raw_total, round(hiked.linear_total - hiked.penalty, 1), delta=0.11)
        self.assertGreater(clean.total, hiked.total)

    def test_stage2_veto_still_caps_seven_dim_station(self):
        bad_probe = {"window_days": 7, "total_samples": 30, "success_rate": 0.72, "billing_drift_pct": 28.5, "passed_stage2": False}
        sc = S.station_score(self._seven(stability=99, reputation=99), attempts=12, probe=bad_probe)
        self.assertEqual(sc.stage, 2)
        self.assertLessEqual(sc.total, S.STAGE2_FAIL_CAP)
        good = dict(bad_probe, success_rate=0.995, billing_drift_pct=1.0, passed_stage2=True, avg_ttft_ms=500.0,
                    tool_call_pass_rate=1.0, cache_hit_rate=1.0)
        ok = S.station_score(self._seven(stability=99, reputation=99, latency=99), attempts=12, probe=good)
        self.assertEqual(ok.stage, 2)
        self.assertGreater(ok.total, S.STAGE1_CAP)  # Stage 2 bypasses the soft-cap

    def test_completeness_uncertainty_and_percentiles(self):
        full = S.station_score(self._seven(), attempts=12)
        part = S.station_score([S.Dim(d.key, d.score, "", known=d.key not in ("latency", "longevity")) for d in self._seven()], attempts=12)
        self.assertEqual(full.completeness, 1.0)
        self.assertLess(part.completeness, 1.0)
        self.assertGreater(part.uncertainty, full.uncertainty)
        scores = [S.station_score(self._seven(reputation=r), attempts=12) for r in (10, 50, 50, 90)]
        S.assign_percentiles(scores)
        self.assertEqual([s.percentile for s in scores], [0.0, 33.3, 33.3, 100.0])
        med = S.market_medians(scores)
        self.assertEqual(med["reputation"], 50)

    def test_new_dimensions(self):
        self.assertEqual(S.score_price_stability(None).score, 100)
        d = S.score_price_stability({"stability_score": 65, "volatility_summary": "近 30 天：1 次涨价"})
        self.assertEqual((d.score, d.evidence), (65.0, "近 30 天：1 次涨价"))
        self.assertFalse(S.score_price_stability(None, monitored=False).known)
        kw = dict(groups_paid=10, groups_caveat=0, quotes_paid=20, quotes_promo=0, quotes_anomaly=0, dual_lane_models=0,
                  extreme_spread_models=0, max_spread=None, claude_quotes=4, claude_native=4, gpt_quotes=2, gpt_responses=2,
                  cache_eligible=6, cache_disclosed=6)
        clean = S.score_purity_protocol(**kw)
        self.assertEqual(clean.score, 100.0)
        dirty = S.score_purity_protocol(**{**kw, "groups_caveat": 6, "quotes_promo": 10, "dual_lane_models": 5,
                                           "extreme_spread_models": 4, "max_spread": 6.0, "claude_native": 0, "gpt_responses": 0})
        self.assertLess(dirty.score, 45)
        self.assertIn("4/5 个双线型号价差超 4×", dirty.evidence)
        self.assertFalse(S.score_purity_protocol(**{**kw, "quotes_paid": 0}).known)

    def test_longevity_expiry_runway(self):
        base = S.score_longevity(730).score
        self.assertLess(S.score_longevity(730, expiry_days_left=-3).score, base - 30)
        self.assertIn("域名已过期", S.score_longevity(730, expiry_days_left=-3).evidence)
        self.assertLess(S.score_longevity(730, expiry_days_left=20).score, S.score_longevity(730, expiry_days_left=50).score)
        self.assertGreater(S.score_longevity(730, expiry_days_left=800).score, base)
        self.assertEqual(S.score_longevity(1095, expiry_days_left=800).score, 100)

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
