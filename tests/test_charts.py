import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import charts as C  # noqa: E402
import scoring as S  # noqa: E402

NOW = datetime(2026, 11, 27, tzinfo=timezone.utc)
FLAGSHIPS = ["claude-sonnet-5-5", "claude-opus-5-5", "gpt-6-astra", "gpt-6.1-sol", "deepseek-v4.1-flash", "gemini-4-carbon"]


class ChartTests(unittest.TestCase):
    def assertSvg(self, svg: str, budget: int) -> ET.Element:
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, "svg")
        self.assertEqual(root.get("role"), "img")
        self.assertTrue(root.get("aria-label"))
        self.assertTrue(root.get("viewBox"))
        self.assertIn("ch", root.get("class", "").split())
        self.assertIsNotNone(root.find("title"))
        self.assertTrue(root.find("title").text)
        self.assertLess(len(svg.encode("utf-8")), budget, msg=f"{len(svg.encode())} bytes")
        self.assertNotIn("style=", svg)  # colours come from theme CSS classes only
        return root

    def test_radar_populated_penalized_and_edges(self):
        dims = {k: 40 + 8 * i for i, k in enumerate(S.DIM_ORDER)}
        svg = C.station_radar_svg(dims, {k: 60 for k in S.DIM_ORDER}, labels=S.DIM_LABELS, order=S.DIM_ORDER,
                                  penalized={"price_stability"}, name="Demo <&> 站")
        root = self.assertSvg(svg, 2500)
        self.assertEqual(len(root.findall("polygon")), 4)  # 2 rings + market + station
        self.assertEqual(len([c for c in root.findall("circle") if c.get("class") == "dw"]), 1)
        self.assertIn("全网中位", svg)
        no_market = C.station_radar_svg({k: 0 for k in S.DIM_ORDER[:5]}, None, labels=S.DIM_LABELS, order=S.DIM_ORDER)
        self.assertEqual(len(self.assertSvg(no_market, 2500).findall("polygon")), 3)
        over = C.station_radar_svg({k: 140 for k in S.DIM_ORDER}, {}, labels=S.DIM_LABELS, order=S.DIM_ORDER)
        self.assertSvg(over, 2500)
        self.assertEqual(C.station_radar_svg({"stability": 50}, {}, labels=S.DIM_LABELS, order=S.DIM_ORDER), "")
        self.assertEqual(C.station_radar_svg({}, {}, labels=S.DIM_LABELS, order=S.DIM_ORDER), "")

    def test_price_strip_dumbbell_official_and_worst_case_budget(self):
        rows = [dict(model=m, p10=1.0, p25=2.0, p50=3.0, p75=5.0, p90=9.0, official=20.0, std=2.5, alt=0.6) for m in FLAGSHIPS]
        root = self.assertSvg(C.station_price_strip_svg(rows), 3500)
        self.assertEqual(len([p for p in root.findall("path") if p.get("class") == "lk"]), 6)
        self.assertEqual(len([p for p in root.findall("path") if p.get("class") == "of"]), 6)
        worst = [dict(model="claude-opus-5-5-thinking-max-extra-long", p10=1.11, p25=2.22, p50=3.33, p75=5.55, p90=9.99,
                      official=999.0, std=2.51, alt=0.0612) for _ in range(9)]
        svg = C.station_price_strip_svg(worst)
        self.assertSvg(svg, 3500)
        self.assertIn("»", svg)
        only_std = C.station_price_strip_svg([dict(model="m", p10=1, p25=2, p50=3, p75=4, p90=5, std=3.3)])
        r2 = self.assertSvg(only_std, 3500)
        self.assertFalse([p for p in r2.findall("path") if p.get("class") in ("lk", "of")])
        self.assertEqual(C.station_price_strip_svg([]), "")
        self.assertEqual(C.station_price_strip_svg([dict(model="m", p10=1, p90=2)]), "")

    def test_price_impulses_scale_window_and_empty_state(self):
        imp = [["2026-11-26", 140.0, 0.0], ["2026-11-20", 5.0, -30.0], ["2026-10-01", 80.0, 0.0], ["bad", 9, 0]]
        svg = C.price_impulse_svg(imp, NOW)
        root = self.assertSvg(svg, 1500)
        classes = {p.get("class") for p in root.findall("path")}
        self.assertTrue({"hk", "hm", "dp"} <= classes)
        self.assertIn("2 天涨价", root.get("aria-label") + root.find("title").text)
        dense = [[f"2026-11-{d:02d}", 150.0 if d % 2 else 5.0, -40.0] for d in range(1, 28)] + \
                [[f"2026-10-{d:02d}", 25.0, -1.0] for d in range(28, 32)]
        self.assertSvg(C.price_impulse_svg(dense, NOW), 1500)
        empty = C.price_impulse_svg([], NOW)
        self.assertSvg(empty, 1500)
        self.assertIn("无调价事件", empty)
        self.assertIn("无调价事件", C.price_impulse_svg(None, None))

    def test_histogram_grade_bands_markers_and_budget(self):
        totals = [(i * 7.3) % 100 for i in range(764)]
        svg = C.score_histogram_svg(totals, median=62.1, cap=74.0)
        root = self.assertSvg(svg, 2500)
        classes = {p.get("class") for p in root.findall("path")}
        self.assertTrue({"gS", "gA", "gB", "gC", "gD", "cl", "ml"} <= classes)
        self.assertIn("Stage 1 封顶 74", svg)
        self.assertIn("中位 62", svg)
        single = C.score_histogram_svg([74.0], median=None)
        self.assertSvg(single, 2500)
        self.assertNotIn('class="ml"', single)
        self.assertSvg(C.score_histogram_svg([-5, 0, 100, 120]), 2500)
        self.assertEqual(C.score_histogram_svg([]), "")

    def test_model_distribution_markers_and_budget(self):
        stats = C.price_stats([0.5, 1, 2, 3, 4, 8, 20])
        self.assertEqual((stats["min"], stats["p50"], stats["n"]), (0.5, 3, 7))
        svg = C.model_price_dist_svg(stats, official=21.75, rec=1.2, alt_median=0.4)
        root = self.assertSvg(svg, 1800)
        self.assertEqual({c.get("class") for c in root.findall("circle")}, {"do", "dw"})
        self.assertIn("官方", svg)
        long = C.model_price_dist_svg(C.price_stats([123456.789, 234567.8, 345678.9, 999999.9]), official=0.0001,
                                      rec=1e7, alt_median=1e-5, what="单次调用价")
        self.assertSvg(long, 1800)
        flat = C.model_price_dist_svg(C.price_stats([2, 2, 2]))
        self.assertSvg(flat, 1800)
        self.assertEqual(C.model_price_dist_svg(C.price_stats([1, 2])), "")
        self.assertEqual(C.model_price_dist_svg(None), "")
        self.assertIsNone(C.price_stats([]))

    def test_helpers(self):
        self.assertEqual(C.fmt_price(None), "—")
        self.assertEqual(C.fmt_price(123.4), "¥123")
        self.assertEqual(C.fmt_price(0.01234), "¥0.0123")
        self.assertEqual(C.quantile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(C.grade_of(74.9), "B")
        self.assertEqual(C.grade_of(85), "S")


if __name__ == "__main__":
    unittest.main()
