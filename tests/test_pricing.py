import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import pricing as P  # noqa: E402


class ExprTests(unittest.TestCase):
    def test_simple_base(self):
        q = P.parse_billing_expr('tier("base", p * 2 + c * 8 + cr * 0.04)')
        self.assertEqual((q.kind, q.input_usd, q.output_usd, q.cache_usd), ("token", 2.0, 8.0, 0.04))
        self.assertFalse(q.conditional)

    def test_len_tiers_takes_cheapest(self):
        q = P.parse_billing_expr('len <= 272000 ? tier("standard", p * 5 + c * 30 + cr * 0.5) : tier("long_context", p * 10 + c * 45 + cr * 1)')
        self.assertEqual((q.input_usd, q.output_usd, q.tier_name), (5.0, 30.0, "standard"))
        self.assertTrue(q.conditional)

    def test_reversed_condition_still_cheapest(self):
        q = P.parse_billing_expr('len > 200000 ? tier("long", p * 4 + c * 18) : tier("short", p * 2 + c * 12)')
        self.assertEqual((q.input_usd, q.tier_name), (2.0, "short"))

    def test_fixed_and_version_prefix(self):
        q = P.parse_billing_expr('v1:tier("image", fixed(0.04)) * image_count')
        self.assertEqual((q.kind, q.call_usd), ("call", 0.04))

    def test_time_based(self):
        q = P.parse_billing_expr('(tier("peak", p * 3 + c * 9 + cr * 0.1)) * (hour("Asia/Shanghai") >= 22 ? 0.5 : 1)')
        self.assertTrue(q.time_based)
        self.assertEqual(q.input_usd, 3.0)

    def test_peak_offpeak_reports_both(self):
        q = P.parse_billing_expr('hour("Asia/Shanghai") >= 9 ? tier("peak", p * 0.3 + c * 1.2) : tier("off_peak", p * 0.15 + c * 0.6)')
        self.assertEqual((q.input_usd, q.max_input_usd, q.max_output_usd), (0.15, 0.3, 1.2))
        pricing = {"data": [{"model_name": "ds", "billing_expr": 'hour("Asia/Shanghai") >= 9 ? tier("peak", p * 0.3 + c * 1.2) : tier("off_peak", p * 0.15 + c * 0.6)', "enable_groups": ["d"]}], "group_ratio": {"d": 0.5}}
        qs, _ = P.normalize_station("x.com", pricing, {"price": 1})
        self.assertAlmostEqual(qs[0].input_cny, 0.075)
        self.assertIn("高峰档 ¥0.15 / ¥0.6", qs[0].flags)

    def test_nested_paren(self):
        q = P.parse_billing_expr('tier("base", (p * 1.5) + c * 6)')
        self.assertEqual((q.input_usd, q.output_usd), (1.5, 6.0))


class StationTests(unittest.TestCase):
    def test_ratio_group_and_cny(self):
        pricing = {
            "data": [
                {"model_name": "deepseek-v4.1-flash", "quota_type": 0, "model_ratio": 0.5, "completion_ratio": 4, "cache_ratio": 0.1,
                 "enable_groups": ["default", "cheap", "vip"]},
                {"model_name": "unpriced", "quota_type": 0, "model_ratio": 37.5, "enable_groups": ["default"]},
                {"model_name": "img", "quota_type": 1, "model_price": 0.02, "enable_groups": ["default"]},
            ],
            "group_ratio": {"default": 1, "cheap": 0.5, "vip": 0.2},
            "usable_group": {"default": "默认", "cheap": "便宜"},  # vip not selectable
        }
        qs, meta = P.normalize_station("x.com", pricing, {"price": 7.3})
        self.assertEqual(meta["dropped_placeholder"], 1)
        ds = next(q for q in qs if q.model == "deepseek-v4.1-flash")
        self.assertEqual(ds.group, "cheap")
        self.assertEqual(len(ds.all_groups), 2)
        self.assertAlmostEqual(ds.input_usd, 0.5)  # 0.5*2*0.5
        self.assertAlmostEqual(ds.output_usd, 2.0)
        self.assertAlmostEqual(ds.cache_usd, 0.05)
        self.assertAlmostEqual(ds.input_cny, 3.65)
        img = next(q for q in qs if q.model == "img")
        self.assertEqual(img.kind, "call")
        self.assertAlmostEqual(img.call_cny, 0.146)

    def test_welfare_bug_fix_ignores_marketing_desc(self):
        """Regression test for Exp 1 bug: marketing words '测试'/'免费' in paid enterprise desc must NOT mark welfare."""
        pricing = {
            "data": [
                {"model_name": "claude-sonnet-5-5", "quota_type": 0, "model_ratio": 1.5, "completion_ratio": 5,
                 "enable_groups": ["claude max企业级", "claude-test", "kiro-promo"]},
            ],
            "group_ratio": {"claude max企业级": 1.5, "claude-test": 0.1, "kiro-promo": 0.2},
            "usable_group": {
                "claude max企业级": "过多家大厂测试 免费ultra fast",
                "claude-test": "测试组",
                "kiro-promo": "Kiro 逆向特惠渠道",
            },
        }
        qs, meta = P.normalize_station("x.com", pricing, {"price": 1.0})
        self.assertEqual(meta["parsed"], 1)
        by_lane = {q.lane: q for q in qs}
        self.assertIn("standard", by_lane)
        self.assertIn("promo", by_lane)
        self.assertEqual(by_lane["standard"].group, "claude max企业级")
        self.assertFalse(by_lane["standard"].welfare)
        self.assertEqual(by_lane["promo"].group, "kiro-promo")
        self.assertIn("自称Kiro", by_lane["promo"].caveats)
        self.assertIn("自称逆向/反代", by_lane["promo"].caveats)
        self.assertEqual(len(by_lane["standard"].all_groups), 3)

    def test_caveat_typo_krio_and_endpoints(self):
        self.assertIn("自称Kiro", P.extract_caveats("krio_claude", ""))
        self.assertIn("自称逆向/反代", P.extract_caveats("deepseek反代", ""))
        self.assertEqual(
            P.endpoint_tags(["openai", "anthropic", "openai-response", "image-generation", "gemini"]),
            ["openai", "anthropic", "openai-response", "image", "gemini"],
        )

    def test_unknown_topup_keeps_usd_only(self):
        pricing = {"data": [{"model_name": "m", "quota_type": 0, "model_ratio": 1, "enable_groups": ["default"]}], "group_ratio": {"default": 1}}
        qs, meta = P.normalize_station("x.com", pricing, {"price": 0.01})
        self.assertIsNone(qs[0].input_cny)
        self.assertEqual(qs[0].input_usd, 2.0)
        self.assertIn("异常", meta["topup_note"])

    def test_free_group_only(self):
        pricing = {"data": [{"model_name": "m", "quota_type": 0, "model_ratio": 1, "enable_groups": ["free"]}], "group_ratio": {"free": 0}}
        qs, _ = P.normalize_station("x.com", pricing, {"price": 1})
        self.assertTrue(qs[0].free)
        self.assertEqual(qs[0].lane, "welfare")

    def test_group_price_fork(self):
        pricing = {"data": [
            {"model_name": "gpt-6-astra", "quota_type": "0", "completion_ratio": 5, "enable_groups": ["default", "vip"],
             "group_price": {"default": {"type": 0, "price": 5, "group_ratio": 1}, "vip": {"type": 0, "price": 5, "group_ratio": 0.5}}},
            {"model_name": "img", "quota_type": "1", "enable_groups": ["default"],
             "group_price": {"default": {"type": 1, "price": 0.11, "group_ratio": 1}, "x": {"type": 0, "price": 2.5, "group_ratio": 4}}},
        ], "group_ratio": {"default": 1, "vip": 0.5}}
        qs, _ = P.normalize_station("x.com", pricing, {"price": "1.2"})
        a = next(q for q in qs if q.model == "gpt-6-astra")
        self.assertEqual((a.group, a.group_ratio), ("vip", 0.5))
        self.assertAlmostEqual(a.input_usd, 5.0)   # 5*2*0.5
        self.assertAlmostEqual(a.output_usd, 25.0)
        self.assertAlmostEqual(a.input_cny, 6.0)   # string price "1.2" accepted
        img = next(q for q in qs if q.model == "img")
        self.assertEqual(img.kind, "call")
        self.assertAlmostEqual(img.call_usd, 0.11)

    def test_canonical(self):
        self.assertEqual(P.canonical_model("[VT] Gemini-4-Carbon"), "gemini-4-carbon")
        self.assertEqual(P.canonical_model("deepseek-ai/DeepSeek-V4.1-Flash"), "deepseek-v4.1-flash")
        self.assertEqual(P.canonical_model("claude-sonnet-5【官转】"), "claude-sonnet-5")


class FixedFieldTests(unittest.TestCase):
    PRICING = {
        "data": [
            {"model_name": "claude-sonnet-5-5", "quota_type": 0, "model_ratio": 1.5, "completion_ratio": 5, "cache_ratio": 0.1,
             "cache_creation_ratio": 1.25, "tags": "chat, 官转,  ", "enable_groups": ["default"]},
            {"model_name": "gpt-6-astra", "quota_type": 0, "model_ratio": 1.25, "cache_ratio": 0, "create_cache_ratio": 0,
             "enable_groups": ["default"]},
            {"model_name": "ds", "billing_expr": 'tier("base", p * 0.5 + c * 2 + cr * 0 + cw * 0.6)', "enable_groups": ["default"]},
        ],
        "group_ratio": {"default": 0.5},
    }

    def test_fixed_billing_fields_captured(self):
        qs, meta = P.normalize_station("x.com", self.PRICING, {"price": 2, "docs_link": "https://docs.newapi.pro"})
        by = {q.model: q for q in qs}
        s = by["claude-sonnet-5-5"]
        self.assertEqual((s.model_ratio, s.completion_ratio, s.cache_ratio, s.cache_write_ratio), (1.5, 5.0, 0.1, 1.25))
        self.assertAlmostEqual(s.cache_write_cny, 1.5 * 2 * 0.5 * 2 * 1.25)
        self.assertEqual(s.cache_status, "priced")
        self.assertEqual(s.tags, ["chat", "官转"])
        g = by["gpt-6-astra"]
        self.assertEqual((g.cache_status, g.cache_cny, g.completion_ratio), ("unknown", None, None))
        d = by["ds"]
        self.assertEqual((d.cache_status, d.cache_cny, d.billing_mode), ("free", 0.0, "tiered_expr"))
        self.assertAlmostEqual(d.cache_write_cny, 0.6 * 0.5 * 2)
        self.assertIsNone(meta["docs_link"])
        self.assertEqual(meta["topup_mode"], "discount")

    def test_formula_matches_input_price(self):
        qs, _ = P.normalize_station("x.com", self.PRICING, {"price": 2})
        s = next(q for q in qs if q.model == "claude-sonnet-5-5")
        self.assertTrue(P.format_formula(s, 2).startswith("模型×1.5 × 2 × 分组×0.5 × 充值¥2 = ¥3"))
        self.assertLess(abs(s.model_ratio * 2 * s.group_ratio * 2 - s.input_cny), 1e-5)


if __name__ == "__main__":
    unittest.main()
