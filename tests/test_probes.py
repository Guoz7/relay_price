import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import probes  # noqa: E402


class ProbeTests(unittest.TestCase):
    def test_dry_run_plans_all_modes(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = tmp / "targets.json"
            cfg.write_text(json.dumps({
                "targets": [{
                    "domain": "demo.example",
                    "base_url": "https://demo.example/v1",
                    "api_key": "sk-test",
                    "routes": [
                        {"canonical": "claude-sonnet-5-5", "model": "claude-sonnet-5-5", "protocol": "anthropic"},
                        {"canonical": "deepseek-v4.1-flash", "model": "deepseek-v4.1-flash", "protocol": "openai"},
                    ],
                }]
            }), encoding="utf-8")
            recs = probes.run_probes(cfg, "all", dry_run=True, runs_dir=tmp / "runs")
            self.assertEqual(len(recs), 6)
            self.assertTrue(all(r["dry_run"] for r in recs))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_aggregate_stage2_pass_and_fail(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            runs = tmp / "probe-runs"
            runs.mkdir()
            now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
            lines = []
            # Station A: 24 samples, 100% ok, 1.5% billing drift, cache hit -> PASS Stage 2
            for i in range(20):
                lines.append(json.dumps({
                    "ts": "2026-11-26T10:00:00+00:00", "domain": "good.api", "canonical": "claude-sonnet-5-5",
                    "mode": "check", "ok": True, "ttft_ms": 800 + i * 5, "tpot_ms": 18.0,
                }))
            for i in range(3):
                lines.append(json.dumps({
                    "ts": "2026-11-26T12:00:00+00:00", "domain": "good.api", "canonical": "claude-sonnet-5-5",
                    "mode": "tool", "ok": True, "tool_call_ok": True, "billing_drift_pct": 1.5,
                }))
            lines.append(json.dumps({
                "ts": "2026-11-26T14:00:00+00:00", "domain": "good.api", "canonical": "claude-sonnet-5-5",
                "mode": "cache", "ok": True, "cache_hit": True, "cached_tokens": 1100,
            }))
            # Station B: 22 samples, 100% ok BUT +32% billing drift -> FAIL Stage 2
            for i in range(20):
                lines.append(json.dumps({
                    "ts": "2026-11-26T10:00:00+00:00", "domain": "overcharge.api", "canonical": "gpt-6.1-sol",
                    "mode": "check", "ok": True, "ttft_ms": 900, "tpot_ms": 20.0,
                }))
            for i in range(2):
                lines.append(json.dumps({
                    "ts": "2026-11-26T12:00:00+00:00", "domain": "overcharge.api", "canonical": "gpt-6.1-sol",
                    "mode": "tool", "ok": True, "tool_call_ok": True, "billing_drift_pct": 32.0,
                }))
            (runs / "2026-11-26.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

            out_path = tmp / "probes.json"
            doc = probes.aggregate_runs(runs, out_path, window_days=7, now=now)
            g = doc["stations"]["good.api"]
            b = doc["stations"]["overcharge.api"]
            self.assertTrue(g["passed_stage2"])
            self.assertEqual(g["success_rate"], 1.0)
            self.assertEqual(g["tool_call_pass_rate"], 1.0)
            self.assertEqual(g["cache_hit_rate"], 1.0)
            self.assertAlmostEqual(g["billing_drift_pct"], 1.5)
            self.assertFalse(b["passed_stage2"])
            self.assertAlmostEqual(b["billing_drift_pct"], 32.0)

            loaded = probes.load_probe_summary(tmp)
            self.assertIn("good.api", loaded)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
