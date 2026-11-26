"""V2A micro-probe runner & rolling 7-day Stage 2 aggregator (stdlib only).

Implements the exact Experiment 4 low-cost probe specification:
- check (~80 in + 60 out): streaming connectivity, TTFT, TPOT, finish_reason
- tool  (~350 in + 120 out): function-calling JSON conformance + optional pre/post quota billing drift
- cache (2-turn ~1,200-token prefix): verifies actual prompt cache hit tokens

Usage:
  python tools/probes.py --config data/probe-targets.local.json --mode check
  python tools/probes.py --config data/probe-targets.local.json --mode all --dry-run
  python tools/probes.py --aggregate
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
DATA = ROOT / "data"
RUNS_DIR = DATA / "probe-runs"
OUT_PATH = DATA / "probes.json"

STAGE2_MIN_SAMPLES = 20
STAGE2_MIN_SUCCESS = 0.90
STAGE2_MAX_DRIFT_PCT = 15.0

# ~1,200-token deterministic prompt prefix used for 2-turn prompt cache hit probes.
CACHE_PREFIX = (
    "You are a strict compliance auditor reviewing API relay technical specifications. "
    "Memorize the following numbered policy clauses and answer only with the requested clause code.\n"
    + "\n".join(
        f"Clause {i:03d}: Relay gateway route R-{i:03d} enforces UTF-8 JSON framing, monotonic request IDs, "
        f"explicit timeout accounting in milliseconds, non-negative token usage fields, and deterministic error codes."
        for i in range(1, 56)
    )
)

TOOL_SCHEMA_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "get_exchange_rate",
            "description": "Look up the exchange rate between two ISO currency codes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "base": {"type": "string", "description": "Source currency code, e.g. USD"},
                    "target": {"type": "string", "description": "Target currency code, e.g. CNY"},
                },
                "required": ["base", "target"],
            },
        },
    }
]

TOOL_SCHEMA_ANTHROPIC = [
    {
        "name": "get_exchange_rate",
        "description": "Look up the exchange rate between two ISO currency codes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "base": {"type": "string", "description": "Source currency code, e.g. USD"},
                "target": {"type": "string", "description": "Target currency code, e.g. CNY"},
            },
            "required": ["base", "target"],
        },
    }
]


def load_probe_summary(data_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Safely load aggregated Stage 2 probe summary from data/probes.json."""
    p = (data_dir or DATA) / "probes.json"
    if not p.exists():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    stations = obj.get("stations") if isinstance(obj, dict) else None
    return stations if isinstance(stations, dict) else {}


def _http_json(url: str, headers: dict[str, str], payload: dict[str, Any] | None = None, timeout: float = 35.0) -> tuple[int, Any, float]:
    t0 = time.perf_counter()
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            ms = (time.perf_counter() - t0) * 1000.0
            return resp.status, json.loads(raw), ms
    except urllib.error.HTTPError as e:
        ms = (time.perf_counter() - t0) * 1000.0
        txt = e.read().decode("utf-8", "replace")[:300]
        return e.code, {"error": txt}, ms
    except Exception as e:  # noqa: BLE001
        ms = (time.perf_counter() - t0) * 1000.0
        return 0, {"error": f"{type(e).__name__}: {e}"}, ms


def _fetch_quota_cny(target: dict[str, Any]) -> float | None:
    """Optional New API /api/user/self balance reader for real billing reconciliation."""
    b_url = target.get("balance_url")
    b_tok = target.get("balance_token")
    if not b_url or not b_tok:
        return None
    headers = {"Authorization": f"Bearer {b_tok}", "User-Agent": "RelayPriceProbe/1.0"}
    if target.get("balance_user_id"):
        headers["New-Api-User"] = str(target["balance_user_id"])
    code, data, _ = _http_json(b_url, headers, None, timeout=15.0)
    if code != 200 or not isinstance(data, dict):
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    quota = inner.get("quota")
    if not isinstance(quota, (int, float)):
        return None
    quota_per_usd = float(target.get("quota_per_usd", 500000.0))
    topup_cny = float(target.get("topup_cny", 1.0))
    return (float(quota) / quota_per_usd) * topup_cny


def _stream_check_openai(base_url: str, api_key: str, model: str, timeout: float = 35.0) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 60,
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "List 3 primary colors in one short sentence."},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": "RelayPriceProbe/1.0"},
        method="POST",
    )
    t0 = time.perf_counter()
    ttft_ms: float | None = None
    chunks = 0
    finish_reason = None
    usage: dict[str, Any] = {}
    text_len = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                part = line[5:].strip()
                if part == "[DONE]":
                    break
                try:
                    ev = json.loads(part)
                except ValueError:
                    continue
                if isinstance(ev.get("usage"), dict) and ev["usage"]:
                    usage = ev["usage"]
                choices = ev.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content") or delta.get("reasoning_content") or ""
                    if piece:
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - t0) * 1000.0
                        chunks += 1
                        text_len += len(piece)
                    if choices[0].get("finish_reason"):
                        finish_reason = choices[0]["finish_reason"]
            total_ms = (time.perf_counter() - t0) * 1000.0
            out_tok = int(usage.get("completion_tokens") or max(1, chunks))
            tpot_ms = ((total_ms - ttft_ms) / max(1, out_tok - 1)) if (ttft_ms is not None and out_tok > 1) else None
            return {
                "ok": status == 200 and (text_len > 0 or out_tok > 0),
                "http_status": status,
                "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
                "total_ms": round(total_ms, 1),
                "tpot_ms": round(tpot_ms, 1) if tpot_ms is not None else None,
                "finish_reason": finish_reason,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "error": None if (status == 200 and text_len > 0) else "empty_stream",
            }
    except urllib.error.HTTPError as e:
        return {
            "ok": False, "http_status": e.code, "ttft_ms": None,
            "total_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "tpot_ms": None, "finish_reason": None, "error": e.read().decode("utf-8", "replace")[:200],
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False, "http_status": 0, "ttft_ms": None,
            "total_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "tpot_ms": None, "finish_reason": None, "error": f"{type(e).__name__}: {e}",
        }


def _stream_check_anthropic(base_url: str, api_key: str, model: str, timeout: float = 35.0) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/messages"
    payload = {
        "model": model,
        "max_tokens": 60,
        "stream": True,
        "messages": [{"role": "user", "content": "List 3 primary colors in one short sentence."}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "RelayPriceProbe/1.0",
        },
        method="POST",
    )
    t0 = time.perf_counter()
    ttft_ms: float | None = None
    chunks = 0
    finish_reason = None
    in_tok = out_tok = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except ValueError:
                    continue
                et = ev.get("type")
                if et == "message_start":
                    u = (ev.get("message") or {}).get("usage") or {}
                    in_tok = u.get("input_tokens")
                elif et == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("text") or delta.get("thinking"):
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - t0) * 1000.0
                        chunks += 1
                elif et == "message_delta":
                    finish_reason = (ev.get("delta") or {}).get("stop_reason")
                    u = ev.get("usage") or {}
                    if u.get("output_tokens") is not None:
                        out_tok = u.get("output_tokens")
            total_ms = (time.perf_counter() - t0) * 1000.0
            eff_out = int(out_tok or max(1, chunks))
            tpot_ms = ((total_ms - ttft_ms) / max(1, eff_out - 1)) if (ttft_ms is not None and eff_out > 1) else None
            return {
                "ok": status == 200 and chunks > 0,
                "http_status": status,
                "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
                "total_ms": round(total_ms, 1),
                "tpot_ms": round(tpot_ms, 1) if tpot_ms is not None else None,
                "finish_reason": finish_reason,
                "prompt_tokens": in_tok,
                "completion_tokens": out_tok,
                "error": None if (status == 200 and chunks > 0) else "empty_stream",
            }
    except urllib.error.HTTPError as e:
        return {
            "ok": False, "http_status": e.code, "ttft_ms": None,
            "total_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "tpot_ms": None, "finish_reason": None, "error": e.read().decode("utf-8", "replace")[:200],
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False, "http_status": 0, "ttft_ms": None,
            "total_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "tpot_ms": None, "finish_reason": None, "error": f"{type(e).__name__}: {e}",
        }


def _tool_and_billing_probe(target: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    proto = route.get("protocol", "openai")
    model = route["model"]
    api_key = route.get("api_key") or target["api_key"]
    before_cny = _fetch_quota_cny(target)
    if proto == "anthropic":
        url = target["base_url"].rstrip("/") + "/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "RelayPriceProbe/1.0",
        }
        payload = {
            "model": model,
            "max_tokens": 120,
            "tools": TOOL_SCHEMA_ANTHROPIC,
            "tool_choice": {"type": "tool", "name": "get_exchange_rate"},
            "messages": [{"role": "user", "content": "What is the USD to CNY exchange rate? Call get_exchange_rate."}],
        }
        code, data, ms = _http_json(url, headers, payload)
        tool_ok = False
        in_tok = out_tok = None
        if code == 200 and isinstance(data, dict):
            u = data.get("usage") or {}
            in_tok, out_tok = u.get("input_tokens"), u.get("output_tokens")
            for blk in data.get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_use" and blk.get("name") == "get_exchange_rate":
                    inp = blk.get("input") or {}
                    if isinstance(inp, dict) and inp.get("base") and inp.get("target"):
                        tool_ok = True
                        break
    else:
        url = target["base_url"].rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "RelayPriceProbe/1.0",
        }
        payload = {
            "model": model,
            "max_tokens": 120,
            "tools": TOOL_SCHEMA_OPENAI,
            "tool_choice": {"type": "function", "function": {"name": "get_exchange_rate"}},
            "messages": [{"role": "user", "content": "What is the USD to CNY exchange rate? Call get_exchange_rate."}],
        }
        code, data, ms = _http_json(url, headers, payload)
        tool_ok = False
        in_tok = out_tok = None
        if code == 200 and isinstance(data, dict):
            u = data.get("usage") or {}
            in_tok, out_tok = u.get("prompt_tokens"), u.get("completion_tokens")
            choices = data.get("choices") or []
            if choices and isinstance(choices[0], dict):
                tcs = (choices[0].get("message") or {}).get("tool_calls") or []
                for tc in tcs:
                    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                    if fn.get("name") == "get_exchange_rate":
                        try:
                            args = json.loads(fn.get("arguments") or "{}")
                            if isinstance(args, dict) and args.get("base") and args.get("target"):
                                tool_ok = True
                                break
                        except ValueError:
                            pass

    after_cny = _fetch_quota_cny(target) if before_cny is not None else None
    actual_cny = (before_cny - after_cny) if (before_cny is not None and after_cny is not None and before_cny >= after_cny) else None
    drift_pct = None
    in_price = route.get("listed_in_cny")
    out_price = route.get("listed_out_cny")
    if actual_cny is not None and in_tok and out_tok and in_price and out_price:
        expected_cny = (in_tok * float(in_price) + out_tok * float(out_price)) / 1_000_000.0
        if expected_cny > 1e-7:
            drift_pct = round(((actual_cny - expected_cny) / expected_cny) * 100.0, 2)
    return {
        "ok": code == 200,
        "http_status": code,
        "total_ms": round(ms, 1),
        "tool_call_ok": tool_ok,
        "prompt_tokens": in_tok,
        "completion_tokens": out_tok,
        "actual_cny": round(actual_cny, 6) if actual_cny is not None else None,
        "billing_drift_pct": drift_pct,
        "error": None if code == 200 else str((data or {}).get("error", ""))[:200],
    }


def _cache_probe(target: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    proto = route.get("protocol", "openai")
    model = route["model"]
    api_key = route.get("api_key") or target["api_key"]

    def turn(q: str) -> tuple[int, Any, float]:
        if proto == "anthropic":
            url = target["base_url"].rstrip("/") + "/messages"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "User-Agent": "RelayPriceProbe/1.0",
            }
            payload = {
                "model": model,
                "max_tokens": 30,
                "system": [{"type": "text", "text": CACHE_PREFIX, "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": q}],
            }
            return _http_json(url, headers, payload)
        url = target["base_url"].rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "RelayPriceProbe/1.0",
        }
        payload = {
            "model": model,
            "max_tokens": 30,
            "messages": [{"role": "system", "content": CACHE_PREFIX}, {"role": "user", "content": q}],
        }
        return _http_json(url, headers, payload)

    c1, d1, m1 = turn("Reply only: R-001")
    if c1 != 200:
        return {"ok": False, "http_status": c1, "total_ms": round(m1, 1), "cache_hit": False, "cached_tokens": 0, "error": str((d1 or {}).get("error", ""))[:200]}
    time.sleep(2.5)
    c2, d2, m2 = turn("Reply only: R-002")
    cached = 0
    if c2 == 200 and isinstance(d2, dict):
        u = d2.get("usage") or {}
        if proto == "anthropic":
            cached = int(u.get("cache_read_input_tokens") or 0)
        else:
            det = u.get("prompt_tokens_details") or {}
            cached = int(det.get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0)
    return {
        "ok": c2 == 200,
        "http_status": c2,
        "total_ms": round(m2, 1),
        "cache_hit": cached > 0,
        "cached_tokens": cached,
        "error": None if c2 == 200 else str((d2 or {}).get("error", ""))[:200],
    }


def run_probes(config_path: Path, mode: str, dry_run: bool = False, runs_dir: Path | None = None) -> list[dict[str, Any]]:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    targets = cfg.get("targets") or []
    modes = ["check", "tool", "cache"] if mode == "all" else [mode]
    now = datetime.now(timezone.utc)
    out_dir = runs_dir or RUNS_DIR
    records: list[dict[str, Any]] = []

    for tgt in targets:
        if not tgt.get("enabled", True):
            continue
        dom = tgt["domain"]
        for rt in tgt.get("routes") or []:
            canon = rt.get("canonical") or rt["model"]
            proto = rt.get("protocol", "openai")
            for m in modes:
                if dry_run:
                    records.append({
                        "ts": now.isoformat(), "domain": dom, "canonical": canon,
                        "model": rt["model"], "protocol": proto, "mode": m, "dry_run": True,
                    })
                    continue
                if m == "check":
                    rk = rt.get("api_key") or tgt["api_key"]
                    res = (
                        _stream_check_anthropic(tgt["base_url"], rk, rt["model"])
                        if proto == "anthropic"
                        else _stream_check_openai(tgt["base_url"], rk, rt["model"])
                    )
                elif m == "tool":
                    res = _tool_and_billing_probe(tgt, rt)
                else:
                    res = _cache_probe(tgt, rt)
                rec = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "domain": dom,
                    "canonical": canon,
                    "model": rt["model"],
                    "group": rt.get("group"),
                    "protocol": proto,
                    "mode": m,
                    **res,
                }
                records.append(rec)

    if not dry_run and records:
        out_dir.mkdir(parents=True, exist_ok=True)
        day_file = out_dir / f"{now.strftime('%Y-%m-%d')}.jsonl"
        with day_file.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return records


def _summarize_bucket(rows: list[dict[str, Any]], window_days: int) -> dict[str, Any]:
    total = len(rows)
    ok_n = sum(1 for r in rows if r.get("ok"))
    sr = ok_n / total if total else 0.0
    ttfts = sorted(float(r["ttft_ms"]) for r in rows if isinstance(r.get("ttft_ms"), (int, float)))
    tpots = sorted(float(r["tpot_ms"]) for r in rows if isinstance(r.get("tpot_ms"), (int, float)))
    tools = [bool(r.get("tool_call_ok")) for r in rows if r.get("mode") == "tool" and r.get("ok")]
    caches = [bool(r.get("cache_hit")) for r in rows if r.get("mode") == "cache" and r.get("ok")]
    drifts = [float(r["billing_drift_pct"]) for r in rows if isinstance(r.get("billing_drift_pct"), (int, float))]

    by_day: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        day = str(r.get("ts") or "")[:10]
        if day:
            by_day[day].append(bool(r.get("ok")))
    daily = [[d, sum(v), len(v)] for d, v in sorted(by_day.items())[-window_days:]]

    med_drift = round(statistics.median(drifts), 2) if drifts else None
    passed = (
        total >= STAGE2_MIN_SAMPLES
        and sr >= STAGE2_MIN_SUCCESS
        and (med_drift is None or abs(med_drift) <= STAGE2_MAX_DRIFT_PCT)
    )
    return {
        "window_days": window_days,
        "total_samples": total,
        "ok_samples": ok_n,
        "success_rate": round(sr, 4),
        "avg_ttft_ms": round(statistics.median(ttfts), 1) if ttfts else None,
        "p95_ttft_ms": round(ttfts[min(len(ttfts) - 1, int(len(ttfts) * 0.95))], 1) if ttfts else None,
        "avg_tpot_ms": round(statistics.median(tpots), 1) if tpots else None,
        "tool_call_pass_rate": round(sum(tools) / len(tools), 4) if tools else None,
        "cache_hit_rate": round(sum(caches) / len(caches), 4) if caches else None,
        "billing_drift_pct": med_drift,
        "billing_samples": len(drifts),
        "passed_stage2": passed,
        "last_probed_at": max((str(r.get("ts") or "") for r in rows), default=""),
        "daily": daily,
    }


def aggregate_runs(runs_dir: Path | None = None, out_path: Path | None = None, window_days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    rdir = runs_dir or RUNS_DIR
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=window_days)).isoformat()
    by_station: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_route: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    if rdir.exists():
        for f in sorted(rdir.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("dry_run") or str(rec.get("ts") or "") < cutoff:
                    continue
                dom = rec.get("domain")
                canon = rec.get("canonical") or rec.get("model")
                if not dom:
                    continue
                by_station[dom].append(rec)
                if canon:
                    by_route[(dom, canon)].append(rec)

    stations_out: dict[str, Any] = {}
    for dom, rows in sorted(by_station.items()):
        s_sum = _summarize_bucket(rows, window_days)
        routes_out = {}
        for (d, canon), r_rows in sorted(by_route.items()):
            if d == dom:
                routes_out[canon] = _summarize_bucket(r_rows, window_days)
        s_sum["routes"] = routes_out
        stations_out[dom] = s_sum

    doc = {
        "generated_at": now.isoformat(),
        "window_days": window_days,
        "stations": stations_out,
    }
    target_out = out_path or OUT_PATH
    target_out.parent.mkdir(parents=True, exist_ok=True)
    target_out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return doc


def main() -> None:
    ap = argparse.ArgumentParser(description="V2A micro-probe runner & Stage 2 aggregator")
    ap.add_argument("--config", type=Path, default=DATA / "probe-targets.local.json")
    ap.add_argument("--mode", choices=["check", "tool", "cache", "all"], default="check")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--aggregate", action="store_true", help="Only aggregate existing probe-runs/*.jsonl -> data/probes.json")
    ap.add_argument("--window-days", type=int, default=7)
    args = ap.parse_args()

    if args.aggregate:
        doc = aggregate_runs(window_days=args.window_days)
        print(json.dumps({"stations": len(doc["stations"]), "out": str(OUT_PATH)}, ensure_ascii=False))
        return

    if not args.config.exists():
        sys.exit(f"probe target config not found: {args.config} (copy data/probe-targets.example.json first)")
    recs = run_probes(args.config, args.mode, dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps({"dry_run_planned": len(recs), "sample": recs[:5]}, ensure_ascii=False, indent=2))
        return
    doc = aggregate_runs(window_days=args.window_days)
    ok_n = sum(1 for r in recs if r.get("ok"))
    print(json.dumps({"probed": len(recs), "ok": ok_n, "stations_aggregated": len(doc["stations"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
