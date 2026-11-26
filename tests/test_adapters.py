import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import adapters as A  # noqa: E402
import pricing as P  # noqa: E402


class AdapterTests(unittest.TestCase):
    def test_wrapped_newapi(self):
        j = {"success": True, "data": {"data": [{"model_name": "m", "quota_type": 0, "model_ratio": 1}], "group_ratio": {"default": 1}}}
        pr, st, sysname = A.adapt(j)
        self.assertEqual(sysname, "new-api")
        self.assertEqual(pr["data"][0]["model_name"], "m")

    def test_legacy_maps(self):
        j = {"data": {"ModelRatio": {"a": 1.5, "b": 37.5}, "CompletionRatio": {"a": 4}, "ModelFixedPrice": {"img": 0.02},
                      "GroupRatio": {"default": 1, "vip": 0.5}, "Models": ["a", "b", "img"]}}
        pr, _, sysname = A.adapt(j)
        self.assertEqual(sysname, "one-api-legacy")
        qs, meta = P.normalize_station("x.com", pr, {"price": 1})
        a = next(q for q in qs if q.model == "a")
        self.assertAlmostEqual(a.input_cny, 1.5)  # 1.5*2*0.5
        self.assertAlmostEqual(a.output_cny, 6.0)
        self.assertEqual(meta["dropped_placeholder"], 1)
        self.assertEqual(next(q for q in qs if q.model == "img").kind, "call")

    def test_voapi(self):
        j = {"data": {"group_info": {"default": {"GroupRatio": 1, "DisplayName": "默认"}, "cheap": {"GroupRatio": 0.5, "DisplayName": "便宜"}},
                      "model_info": [{"model_name": "gpt-x", "price_info": {
                          "default": {"default": {"quota_type": 1, "model_price": 0, "model_ratio": 0.125, "model_completion_ratio": 8}},
                          "cheap": {"default": {"quota_type": 1, "model_price": 0, "model_ratio": 0.125, "model_completion_ratio": 8}}}}]}}
        pr, _, sysname = A.adapt(j)
        self.assertEqual(sysname, "voapi")
        qs, _ = P.normalize_station("x.com", pr, {"price": 7.3})
        self.assertEqual(qs[0].group, "cheap")
        self.assertAlmostEqual(qs[0].input_usd, 0.125)  # 0.125*2*0.5
        self.assertAlmostEqual(qs[0].output_usd, 1.0)

    def test_public_schema_cny(self):
        j = {"data": {"currency": "CNY", "price_unit": "per_1m_tokens", "models": [
            {"group_name": "A", "model_name": "m", "input_price": 0.3, "output_price": 1.5, "cache_input_price": 0.03},
            {"group_name": "B", "model_name": "m", "input_price": 0.2, "output_price": 1.2}]}}
        pr, st, sysname = A.adapt(j)
        self.assertEqual(st, {"price": 1.0})
        qs, _ = P.normalize_station("x.com", pr, st)
        self.assertEqual(qs[0].group, "B")
        self.assertAlmostEqual(qs[0].input_cny, 0.2)
        self.assertAlmostEqual(qs[0].output_cny, 1.2)

    def test_unknown_returns_none(self):
        self.assertIsNone(A.adapt({"data": None, "group_ratio": {"a": 1}}))
        self.assertIsNone(A.adapt("<html>"))


if __name__ == "__main__":
    unittest.main()
