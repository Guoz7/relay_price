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

            self.assertEqual(g["verified_tier"], "L0")
            self.assertIsNone(g["auth_pass_rate"])
            self.assertEqual(g["tamper_accepted_count"], 0)
            self.assertFalse(g["downgrade_alert"])

    @staticmethod
    def _cfg(tmp, cap=None):
        tgt = {"domain": "demo.example", "base_url": "https://demo.example/v1", "api_key": "sk-test",
               "routes": [{"canonical": "claude-sonnet-5-5", "model": "claude-sonnet-5-5", "protocol": "anthropic"},
                          {"canonical": "deepseek-v4.1-flash", "model": "deepseek-v4.1-flash", "protocol": "openai"}]}
        if cap is not None:
            tgt["daily_call_cap"] = cap
        cfg = tmp / "targets.json"
        cfg.write_text(json.dumps({"targets": [tgt]}), encoding="utf-8")
        return cfg

    def test_dry_run_new_modes_plan_verify_model_checks(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = self._cfg(tmp)
            auth = probes.run_probes(cfg, "auth", dry_run=True, runs_dir=tmp / "runs")
            iq = probes.run_probes(cfg, "iq", dry_run=True, runs_dir=tmp / "runs")
            ctx = probes.run_probes(cfg, "context", dry_run=True, runs_dir=tmp / "runs")
            self.assertEqual(auth[0]["planned_checks"], ["fingerprint", "thinking", "tool", "cache"])
            self.assertEqual(iq[0]["planned_checks"], ["iq_trace", "iq_logic", "iq_constraints"])
            self.assertEqual(ctx[0]["planned_checks"], ["context_hop"])
            self.assertTrue(all(r["dry_run"] for r in auth + iq + ctx))
            self.assertFalse((tmp / "runs").exists())
            with self.assertRaises(ValueError):
                probes.run_probes(cfg, "bogus", dry_run=True, runs_dir=tmp / "runs")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_budget_circuit_breaker_run_and_daily_caps(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            recs = probes.run_probes(self._cfg(tmp), "all", dry_run=True, runs_dir=tmp / "runs", max_calls=3)
            planned = [r for r in recs if not r.get("budget_skipped")]
            self.assertEqual(sum(r["est_calls"] for r in planned), 3)
            self.assertEqual(len(recs), 6)
            self.assertTrue(any("max-calls" in r.get("skip_reason", "") for r in recs))

            runs = tmp / "runs"
            runs.mkdir()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            (runs / f"{today}.jsonl").write_text(
                json.dumps({"ts": f"{today}T00:00:00+00:00", "domain": "demo.example", "mode": "auth", "calls": 9}) + "\n",
                encoding="utf-8")
            self.assertEqual(probes.calls_logged_today(runs, today)["demo.example"], 9)
            capped = probes.run_probes(self._cfg(tmp, cap=10), "check", dry_run=True, runs_dir=runs)
            self.assertEqual([r.get("budget_skipped", False) for r in capped], [False, True])
            self.assertIn("daily_call_cap=10", capped[1]["skip_reason"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_suite_modes_reuse_verify_model_and_never_write_skipped(self):
        tmp = Path(tempfile.mkdtemp())
        seen = []

        def fake_suite(base_url, api_key, model, protocol="auto", checks=None, tier=None, seed=None, context_tokens=None):
            seen.append((tier, checks, protocol, context_tokens))
            want = probes.VM.parse_checks(checks, tier or "l2_iq")
            results = [{"id": c, "status": "pass", "evidence": {}} for c in want]
            if "thinking" in want and model.startswith("claude"):
                results[1] = {"id": "thinking", "status": "fail", "evidence": {"tamper_result": "accepted"}}
            if "context_hop" in want:
                results[-1]["evidence"] = {"prompt_tokens": 4000, "est_tokens": 16100, "token_ratio_pct": 24,
                                           "token_flag": "truncation"}
                results[-1]["status"] = "warn"
            return {"seed": 7, "tier": tier or "custom", "checks": want, "upstream_calls": len(want), "fail_fast": None,
                    **probes.VM.summarize(results), "results": results}

        orig = probes.VM.run_suite
        probes.VM.run_suite = fake_suite
        try:
            cfg = self._cfg(tmp)
            recs = []
            for mode in ("auth", "iq", "context"):
                recs += probes.run_probes(cfg, mode, runs_dir=tmp / "runs", max_calls=12)
        finally:
            probes.VM.run_suite = orig
        try:
            self.assertEqual(seen[0][:3], ("l1_auth", None, "anthropic"))
            self.assertEqual(seen[-1][1], ["context_hop"])
            self.assertEqual(seen[-1][3], probes.VM.CLI_CONTEXT_TOKENS["def"])
            claude_auth, ds_auth = recs[0], recs[1]
            self.assertTrue(claude_auth["tamper_accepted"])
            self.assertFalse(claude_auth["auth_pass"])
            self.assertEqual(claude_auth["verified_tier"], "L0")
            self.assertTrue(ds_auth["auth_pass"])
            self.assertEqual(ds_auth["verified_tier"], "L1")
            self.assertNotIn('"prompt":', json.dumps(recs))
            ctx = [r for r in recs if r["mode"] == "context" and not r.get("budget_skipped")]
            self.assertTrue(ctx)
            self.assertEqual(ctx[0]["token_flag"], "truncation")
            self.assertFalse(ctx[0]["context_ok"])
            written = [json.loads(x) for f in (tmp / "runs").glob("*.jsonl") for x in f.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(not r.get("budget_skipped") for r in written))
            self.assertEqual(len(written), sum(1 for r in recs if not r.get("budget_skipped")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_aggregate_authenticity_iq_tiers_and_dual_window(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            runs = tmp / "probe-runs"
            runs.mkdir()
            now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
            rows = []

            def add(dom, ts, mode, **kw):
                rows.append(json.dumps({"ts": ts, "domain": dom, "canonical": "claude-sonnet-5-5", "mode": mode, "ok": True, **kw}))

            for i in range(20):
                add("honest.api", "2026-11-25T10:00:00+00:00", "check", ttft_ms=900)
                add("swap.api", "2026-11-22T10:00:00+00:00", "check", ttft_ms=200)
            for i in range(6):
                add("honest.api", "2026-11-26T20:00:00+00:00", "auth", auth_pass=True, tamper_accepted=False)
                add("honest.api", "2026-11-26T20:00:00+00:00", "iq", iq_pass=True)
                add("honest.api", "2026-11-26T20:00:00+00:00", "context", context_ok=True)
            for i in range(10):
                add("swap.api", "2026-11-22T10:00:00+00:00", "auth", auth_pass=True, tamper_accepted=False)
                add("swap.api", "2026-11-22T10:00:00+00:00", "iq", iq_pass=True)
            for i in range(3):
                add("swap.api", "2026-11-27T08:00:00+00:00", "iq", iq_pass=False)
            add("forged.api", "2026-11-26T20:00:00+00:00", "auth", auth_pass=False, tamper_accepted=True)
            rows.append(json.dumps({"ts": "2026-11-27T09:00:00+00:00", "domain": "swap.api", "mode": "iq",
                                    "ok": False, "budget_skipped": True}))
            (runs / "2026-11-26.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
            doc = probes.aggregate_runs(runs, tmp / "probes.json", window_days=7, now=now)
            h, w, f = (doc["stations"][d] for d in ("honest.api", "swap.api", "forged.api"))
            self.assertEqual((h["auth_pass_rate"], h["iq_pass_rate"], h["context_pass_rate"]), (1.0, 1.0, 1.0))
            self.assertEqual(h["verified_tier"], "L3")
            self.assertTrue(h["passed_stage2"])
            self.assertFalse(h["downgrade_alert"])
            self.assertEqual(h["window_24h"]["iq_samples"], 6)

            self.assertEqual(w["total_samples"], 43)
            self.assertAlmostEqual(w["iq_pass_rate"], round(10 / 13, 4))
            self.assertGreaterEqual(w["iq_pass_rate"], probes.IQ_MIN_PASS)
            self.assertEqual(w["window_24h"]["iq_pass_rate"], 0.0)
            self.assertTrue(w["downgrade_alert"])
            self.assertIn("智商", w["downgrade_reasons"][0])
            self.assertFalse(w["passed_stage2"])
            self.assertEqual(w["verified_tier"], "L2")

            self.assertEqual(f["tamper_accepted_count"], 1)
            self.assertEqual(f["verified_tier"], "L0")
            self.assertTrue(f["downgrade_alert"])
            route = w["routes"]["claude-sonnet-5-5"]
            self.assertTrue(route["downgrade_alert"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
