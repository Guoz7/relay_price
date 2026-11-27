import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import build as B  # noqa: E402
import calc_page  # noqa: E402
import en_page  # noqa: E402
import model_specs as MS  # noqa: E402
import pricing as P  # noqa: E402
import scoring as S  # noqa: E402


class DummySite:
    def __init__(self, cfg):
        self.cfg = cfg
        self.now = datetime(2026, 11, 27, 0, 0, tzinfo=timezone.utc)
        self.base = cfg["base_url"]
        self.name = cfg["site_name"]
        self.pages = {}

    def page(self, path, title, desc, body, **kwargs):
        self.pages[path] = {"title": title, "desc": desc, "body": body, "kwargs": kwargs}


class ModelSpecsAndPagesTests(unittest.TestCase):
    def test_normalize_models_dev_prefers_first_party_and_parses_flags(self):
        raw = {
            "thirdparty": {
                "models": {
                    "claude-sonnet-5-5": {
                        "cost": {"input": 9.9, "output": 29.9},
                        "limit": {"context": 200000, "output": 64000},
                    }
                }
            },
            "anthropic": {
                "models": {
                    "claude-sonnet-5-5": {
                        "cost": {"input": 2.0, "output": 10.0, "cache_read": 0.1, "context_over_200k": {"input": 4.0}},
                        "limit": {"context": 1000000, "output": 128000},
                        "reasoning": True,
                        "tool_call": True,
                        "modalities": {"input": ["text", "image"]},
                    }
                }
            },
        }
        res = MS.normalize_models_dev(raw)
        sp = res["claude-sonnet-5-5"]
        self.assertEqual(sp["provider"], "anthropic")
        self.assertEqual(sp["in_usd"], 2.0)
        self.assertEqual(sp["out_usd"], 10.0)
        self.assertEqual(sp["cache_usd"], 0.1)
        self.assertEqual(sp["context"], 1000000)
        self.assertTrue(sp["reasoning"])
        self.assertTrue(sp["tool_call"])
        self.assertTrue(sp["vision"])
        self.assertTrue(sp["long_tier"])

    def test_normalize_litellm_and_merge_specs(self):
        ll = MS.normalize_litellm({
            "sample_spec": {"input_cost_per_token": 1},
            "gpt-6-astra": {
                "litellm_provider": "openai",
                "input_cost_per_token": 10e-6,
                "output_cost_per_token": 50e-6,
                "cache_read_input_token_cost": 1e-6,
                "max_input_tokens": 1050000,
                "max_output_tokens": 128000,
                "supports_function_calling": True,
                "input_cost_per_token_above_200k_tokens": 20e-6,
            },
        })
        self.assertEqual(ll["gpt-6-astra"]["in_usd"], 10.0)
        self.assertEqual(ll["gpt-6-astra"]["out_usd"], 50.0)
        self.assertTrue(ll["gpt-6-astra"]["long_tier"])

        md = {"gpt-6-astra": {"provider": "openai", "source": "models.dev", "in_usd": 10.0, "out_usd": None, "cache_usd": None, "context": 1050000, "max_output": None, "reasoning": True, "tool_call": False, "vision": True, "long_tier": False}}
        merged = MS.merge_specs(md, ll, {"gpt-6-astra"})
        self.assertEqual(merged["gpt-6-astra"]["out_usd"], 50.0)
        self.assertEqual(merged["gpt-6-astra"]["cache_usd"], 1.0)
        self.assertTrue(merged["gpt-6-astra"]["reasoning"])
        self.assertTrue(merged["gpt-6-astra"]["tool_call"])
        self.assertTrue(merged["gpt-6-astra"]["long_tier"])

    def test_fmt_ctx(self):
        self.assertEqual(MS.fmt_ctx(1_000_000), "1M")
        self.assertEqual(MS.fmt_ctx(1_050_000), "1.1M")
        self.assertEqual(MS.fmt_ctx(200_000), "200K")
        self.assertEqual(MS.fmt_ctx(None), "")

    def test_reverse_english_group_name_caught_as_caveat(self):
        self.assertIn("自称逆向/反代", P.extract_caveats("ClaudeCodeReverse", ""))
        self.assertIn("自称逆向/反代", P.extract_caveats("CodexReverse", ""))
        self.assertEqual(P.extract_caveats("default", "通用分组"), [])
        self.assertEqual(P.extract_caveats("universal", "official route"), [])

    def test_spec_bar_discount_ratio_and_badges(self):
        cfg = {"usd_cny_rate": 7.25, "mix_input_weight": 3, "mix_output_weight": 1}
        sp = {"in_usd": 2.0, "out_usd": 10.0, "cache_usd": 0.1, "context": 1_000_000, "max_output": 128_000,
              "reasoning": True, "tool_call": True, "vision": True, "long_tier": True}
        q = P.Quote(
            domain="a.example", model="claude-sonnet-5-5", canonical="claude-sonnet-5-5", family="claude",
            kind="token", group="default", group_ratio=1.0, group_desc=None,
            input_cny=5.8, output_cny=29.0, cache_cny=0.29, lane="standard",
        )
        html = B._spec_bar(sp, q, cfg)
        self.assertIn("1M 上下文", html)
        self.assertIn("深度思考", html)
        self.assertIn("超长上下文加价", html)
        self.assertIn("约官网 40%（4.0折）", html)
        self.assertIn("/calc/", html)

    def test_build_calc_and_en_pages(self):
        cfg = {"site_name": "API 比价雷达", "base_url": "https://agentied.work", "usd_cny_rate": 7.25, "stale_days": 7, "mix_input_weight": 3, "mix_output_weight": 1}
        site = DummySite(cfg)
        now = site.now

        def make_st(dom, name, raw_sc):
            dims = [S.Dim("stability", raw_sc, ""), S.Dim("longevity", 80, ""), S.Dim("transparency", 90, ""),
                    S.Dim("reputation", 70, ""), S.Dim("latency", 90, "")]
            return {"display_name": name, "fetched_at": now, "score": S.station_score(dims, S.DEFAULT_STATION_WEIGHTS, attempts=10)}

        stations = {"a.test": make_st("a.test", "StationA", 90), "b.test": make_st("b.test", "StationB", 85)}
        q1 = P.Quote(domain="a.test", model="claude-sonnet-5-5", canonical="claude-sonnet-5-5", family="claude",
                     kind="token", group="default", group_ratio=1.0, group_desc=None,
                     input_cny=5.0, output_cny=25.0, cache_cny=0.5, score=74.0, raw_score=80.0, lane="standard", endpoints=["anthropic"])
        q2 = P.Quote(domain="b.test", model="claude-sonnet-5-5", canonical="claude-sonnet-5-5", family="claude",
                     kind="token", group="pro", group_ratio=1.0, group_desc=None,
                     input_cny=6.0, output_cny=30.0, cache_cny=0.6, score=72.0, raw_score=76.0, lane="standard", endpoints=["anthropic"])
        model_pages = {"claude-sonnet-5-5": {"slug": "claude-sonnet-5-5", "rows": [q1, q2], "n": 2, "rec": q1, "cheap": q1}}
        specs = {"claude-sonnet-5-5": {"in_usd": 2.0, "out_usd": 10.0, "cache_usd": 0.1, "context": 1_000_000, "long_tier": False}}
        helpers = {
            "money": B.money, "grade_badge": B.grade_badge, "rank_key": B.rank_key,
            "is_reliable_rec": B.is_reliable_rec, "is_stale": B.is_stale,
        }
        with tempfile.TemporaryDirectory() as td:
            dist = Path(td)
            calc_page.build_calc(site, model_pages, stations, specs, helpers, dist)
            en_page.build_en(site, model_pages, stations, specs, 59, helpers)
            self.assertFalse((dist / "data").exists())
            inline = re.search(r'<script type="application/json" id="calc-data">(.*?)</script>', site.pages["/calc/"]["body"], re.S)
            self.assertIsNotNone(inline)
            data = json.loads(inline.group(1))
            self.assertEqual(len(data["models"]), 1)
            self.assertEqual([p["dom"] for p in data["models"][0]["picks"]], ["a.test", "b.test"])
            self.assertIn("/calc/", site.pages)
            self.assertIn("/en/", site.pages)
            self.assertEqual(site.pages["/en/"]["kwargs"].get("lang"), "en")
            self.assertIn("StationA", site.pages["/en/"]["body"])
            body = site.pages["/calc/"]["body"]
            self.assertIn('id="c-table"', body)
            self.assertIn('title="\'+esc(p.name)+\' · \'+esc(p.dom)+\'"', body)
            scripts = [m for m in re.findall(r"<script(?![^>]*(?:ld\+json|application/json))[^>]*>(.*?)</script>", body, re.S) if m.strip()]
            self.assertTrue(scripts)
            js_path = dist / "calc_inline.js"
            js_path.write_text("\n".join(scripts), encoding="utf-8")
            r = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
