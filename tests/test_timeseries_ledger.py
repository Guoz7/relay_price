import gzip
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import timeseries as T

NOW = datetime(2026, 11, 27, 0, tzinfo=timezone.utc)
STAMP = "2026-11-26T12:16:19+00:00"
HOUR = "2026-11-26T12:00Z"


def quote(price, kind="token"):
    return [kind, price, None, None, None, None, None, "default"]


class LedgerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.ledger_path = self.data / T.LEDGER_NAME

    def write_json(self, name, data):
        path = self.data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def write_history(self, stamp, quotes, name=None):
        if name is None:
            name = datetime.fromisoformat(stamp).strftime("%Y%m%dT%H%M%S") + ".json.gz"
        return self.write_json(f"history/{name}", {"run_at": stamp, "algo": 1, "q": quotes})

    def test_parse_history_key(self):
        cases = {
            "x.com|gpt-6-astra": ("x.com", "gpt-6-astra", "standard"),
            "x.com|gpt-6-astra|promo": ("x.com", "gpt-6-astra", "promo"),
            "routekey.ai|gpt-5.3-codex|[team]|standard": ("routekey.ai", "gpt-5.3-codex|[team]", "standard"),
            "x.com|gpt-6-astra|tag": ("x.com", "gpt-6-astra|tag", "standard"),
            "x.com|gpt-6-astra|welfare": ("x.com", "gpt-6-astra", "welfare"),
        }
        for key, expected in cases.items():
            with self.subTest(key=key):
                self.assertEqual(T.parse_history_key(key), expected)

    def test_hour_ts(self):
        self.assertEqual(T.hour_ts(STAMP), HOUR)
        self.assertEqual(T.hour_ts("2026-11-27T00:01:00+08:00"), "2026-11-26T16:00Z")
        self.assertEqual(T.hour_ts("2026-11-26T12:59:59Z"), HOUR)
        self.assertIsNone(T.hour_ts("unparseable"))

    def test_backfill_and_idempotency(self):
        self.write_json("fetch-runs/first.json", {"run_at": STAMP, "log": [
            {"domain": "x.com", "ok": True, "elapsed_ms": 3209},
            {"domain": "only-fetch.com", "ok": False, "elapsed_ms": 4500},
        ]})
        self.write_history(STAMP, {
            "z.com|claude-opus-5-5|standard": quote(8),
            "y.com|claude-opus-5-5|standard": quote(4),
            "x.com|Vendor/Claude Opus 5-5|standard": quote(6),
            "x.com|claude-opus-5-5": quote(2.123456),
            "x.com|claude-opus-5-5|promo": quote(0.1),
            "x.com|claude-opus-5-5|welfare": quote(0.2),
            "x.com|gpt-6-astra|standard": quote(1, kind="call"),
            "y.com|claude-sonnet-5-5|standard": quote(3),
            "x.com|other|[team]|standard": quote(8),
            "y.com|other|[team]|standard": quote(2),
            "z.com|other|[team]|standard": quote(2),
        })
        ledger = T.update_ledger(self.data, NOW)
        expected_index = 145.7  # sqrt((2.123456 / 4) * (8 / 2)) * 100, then rounded.
        self.assertEqual(T.station_series(ledger, "x.com"), [[HOUR, 1, 3209, expected_index, [2.123, None, None, None, None]]])
        self.assertEqual(T.station_series(ledger, "only-fetch.com"), [[HOUR, 0, None, None, [None] * 5]])
        self.assertEqual(T.station_series(ledger, "y.com")[0][1:3], [None, None])
        self.assertEqual(T.station_series(ledger, "missing.com"), [])
        self.assertEqual(T.market_series(ledger), [[HOUR, 4, None, None, None, None]])
        self.assertEqual(list(ledger["s"]), ["only-fetch.com", "x.com", "y.com", "z.com"])
        first = self.ledger_path.read_bytes()
        self.assertEqual(first[4:8], bytes(4))  # gzip mtime
        self.assertEqual(T.update_ledger(self.data, NOW), ledger)
        self.assertEqual(self.ledger_path.read_bytes(), first)

    def test_cached_history_is_skipped_except_newest(self):
        older = self.write_history(STAMP, {"x.com|claude-opus-5-5": quote(1)})
        newest_time = "2026-11-26T13:50:00+00:00"
        newest = self.write_history(newest_time, {"x.com|claude-opus-5-5": quote(2)})
        T.update_ledger(self.data, NOW)
        self.write_history(STAMP, {"x.com|claude-opus-5-5": quote(98)})
        self.write_history(newest_time, {"x.com|claude-opus-5-5": quote(3)})
        before = self.ledger_path.read_bytes()
        with patch.object(T, "_read_json", wraps=T._read_json) as read_json:
            result = T.update_ledger(self.data, NOW, save=False)
            opened = [call.args[0] for call in read_json.call_args_list]
        self.assertNotIn(older, opened)
        self.assertIn(newest, opened)
        self.assertEqual([row[4][0] for row in T.station_series(result, "x.com")], [1, 3])
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_collisions_keep_existing_fields_when_new_values_are_null(self):
        self.write_json("fetch-runs/z-early.json", {"run_at": STAMP, "log": [
            {"domain": "x.com", "ok": True, "elapsed_ms": 52},
        ]})
        self.write_json("fetch-runs/a-late.json", {"run_at": "2026-11-26T12:52:00Z", "log": [
            {"domain": "x.com", "ok": False, "elapsed_ms": 99},
        ]})
        self.write_history(STAMP, {f"{dom}|claude-opus-5-5": quote(2) for dom in ("x.com", "y.com", "z.com")})
        self.write_history("2026-11-26T12:45:00Z", {"x.com|claude-sonnet-5-5": quote(3)}, "latest.json.gz")
        ledger = T.update_ledger(self.data, NOW)
        self.assertEqual(T.station_series(ledger, "x.com"), [[HOUR, 0, 52, 100.0, [2, 3, None, None, None]]])
        self.assertEqual(T.market_series(ledger), [[HOUR, 2, None, None, None, None]])
        first = self.ledger_path.read_bytes()
        self.assertEqual(T.update_ledger(self.data, NOW), ledger)
        self.assertEqual(self.ledger_path.read_bytes(), first)

    def test_prune_window_and_cap(self):
        ledger = T.empty_ledger()
        cutoff = NOW - timedelta(days=30)
        old = (cutoff - timedelta(hours=1)).isoformat()
        T.merge_history(ledger, {"run_at": old, "q": {}})
        T.merge_fetch_run(ledger, {"run_at": old, "log": [{"domain": "expired.com", "ok": False}]})
        for i in reversed(range(125)):
            stamp = (cutoff + timedelta(hours=i)).isoformat()
            T.merge_history(ledger, {"run_at": stamp, "q": {}})
            log = [{"domain": "busy.com", "ok": True}]
            if i == 0:
                log.append({"domain": "at-cutoff.com", "ok": True})
            T.merge_fetch_run(ledger, {"run_at": stamp, "log": log})
        T.prune(ledger, NOW)
        expected = [T.hour_ts((cutoff + timedelta(hours=i)).isoformat()) for i in range(5, 125)]
        self.assertEqual(len(expected), 120)
        self.assertEqual([row[0] for row in T.station_series(ledger, "busy.com")], expected)
        self.assertEqual([row[0] for row in T.market_series(ledger)], expected)
        self.assertEqual(T.station_series(ledger, "at-cutoff.com")[0][0], T.hour_ts(cutoff.isoformat()))
        self.assertNotIn("expired.com", ledger["s"])
        T.prune(ledger, NOW + timedelta(minutes=1))
        self.assertNotIn("at-cutoff.com", ledger["s"])

    def test_churn_does_not_displace_tracked_prices(self):
        domains = ("x.com", "y.com", "z.com")
        for n in range(3):
            quotes = {f"{dom}|claude-opus-5-5|standard": quote(n + i + 1) for i, dom in enumerate(domains)}
            # Entirely different sets on each run simulate 3,000 adds and 3,000 removals.
            quotes.update({f"{domains[i % 3]}|ephemeral-{n}-{i}|standard": quote(99) for i in range(3000)})
            stamp = (NOW - timedelta(hours=3 - n)).isoformat()
            self.write_history(stamp, quotes)
        self.write_json("changes.json", [{"type": "new" if i % 2 else "removed"} for i in range(5000)])
        ledger = T.update_ledger(self.data, NOW)
        for i, dom in enumerate(domains):
            self.assertEqual([p[4] for p in T.station_series(ledger, dom)], [
                [i + 1, None, None, None, None], [i + 2, None, None, None, None], [i + 3, None, None, None, None],
            ])
        self.assertEqual([p[1] for p in T.market_series(ledger)], [2, 3, 4])

    def test_corrupt_inputs_are_skipped(self):
        self.assertEqual(T.update_ledger(self.data, NOW, save=False), T.empty_ledger())
        self.assertFalse(self.ledger_path.exists())
        self.ledger_path.write_bytes(b"invalid gzip")
        self.assertEqual(T.load_ledger(self.ledger_path), T.empty_ledger())
        self.write_json(T.LEDGER_NAME, {**T.empty_ledger(), "v": 99})
        self.assertEqual(T.load_ledger(self.ledger_path), T.empty_ledger())
        self.write_json(T.LEDGER_NAME, {**T.empty_ledger(), "s": {"bad.com": [[HOUR]]}})
        self.assertEqual(T.load_ledger(self.ledger_path), T.empty_ledger())
        bad_history = self.write_history("2026-11-26T23:00:00Z", {})
        bad_history.write_bytes(bad_history.read_bytes()[:-10])
        self.write_history(STAMP, {"x.com|broken": [], "x.com|claude-opus-5-5": quote(2)})
        bad_run = self.write_json("fetch-runs/broken.json", {})
        bad_run.write_bytes(b"\xff\xfe")
        self.write_json("fetch-runs/wrong-shape.json", {"run_at": STAMP, "log": "bad"})
        result = T.update_ledger(self.data, NOW)
        self.assertEqual(T.station_series(result, "x.com"), [[HOUR, None, None, None, [2, None, None, None, None]]])
        self.assertEqual(len(T.market_series(result)), 1)

    def test_json_uses_finite_numbers_and_fixed_width_price_arrays(self):
        invalid = [float("nan"), float("inf"), float("-inf"), 0, -1, True, "5", 10**400]
        quotes = {f"bad{i}.com|claude-opus-5-5": quote(price) for i, price in enumerate(invalid)}
        quotes.update({"x.com|claude-opus-5-5": quote(0.00123456), "y.com|claude-opus-5-5": quote(1e308),
                       "z.com|claude-opus-5-5": quote(1e308)})
        self.write_history(STAMP, quotes)
        self.write_json("fetch-runs/a.json", {"run_at": STAMP, "log": [
            {"domain": "fetch.com", "ok": True, "elapsed_ms": float("inf")},
        ]})
        result = T.update_ledger(self.data, NOW)
        raw = gzip.decompress(self.ledger_path.read_bytes()).decode("utf-8")
        self.assertNotRegex(raw, "NaN|Infinity")
        self.assertEqual(set(result["s"]), {"x.com", "y.com", "z.com", "fetch.com"})
        self.assertEqual(T.station_series(result, "x.com")[0][4][0], 0.001235)
        self.assertEqual(T.station_series(result, "fetch.com")[0][2], None)
        self.assertEqual(result["models"], ["claude-opus-5-5", "claude-sonnet-5-5", "gpt-6-astra",
                                             "gemini-3.8-flash", "deepseek-v4.1-flash"])
        for rows in result["s"].values():
            for row in rows:
                self.assertEqual(len(row[4]), 5)
        self.assertTrue(all(len(row) == 6 for row in T.market_series(result)))


if __name__ == "__main__":
    unittest.main()
