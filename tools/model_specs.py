"""Sync authoritative official model pricing, context windows & capabilities from open-source registries:
- anomalyco/models.dev (https://models.dev/api.json)
- BerriAI/litellm (model_prices_and_context_window.json)

Stores a compact normalized map in `data/model-specs.json` so builds never block on network access
and can compute exact "vs. Official API discount ratio" + context/capability badges without guesswork.

Usage:
  python tools/model_specs.py            # fetch & update data/model-specs.json
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
DATA = ROOT / "data"
OUT = DATA / "model-specs.json"

MODELS_DEV_URL = "https://models.dev/api.json"
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"

PREFERRED_PROVIDERS = ("anthropic", "openai", "google", "deepseek", "xai", "alibaba", "zhipu", "moonshot", "minimax", "mistral", "meta")


def _get_json(url: str, timeout: float = 25.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "RelayPriceSpecs/1.0 (+https://agentied.work/about/)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _r(v: Any, nd: int = 6) -> float | None:
    if not isinstance(v, (int, float)) or v < 0:
        return None
    return round(float(v), nd)


def normalize_models_dev(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    ranked: dict[str, tuple[int, dict[str, Any]]] = {}
    for prov, pdata in raw.items():
        if not isinstance(pdata, dict):
            continue
        models = pdata.get("models")
        if not isinstance(models, dict):
            continue
        pri = PREFERRED_PROVIDERS.index(prov) if prov in PREFERRED_PROVIDERS else 99
        for mid, info in models.items():
            if not isinstance(info, dict):
                continue
            key = str(mid).strip().lower().rsplit("/", 1)[-1]
            if not key:
                continue
            cost = info.get("cost") if isinstance(info.get("cost"), dict) else {}
            lim = info.get("limit") if isinstance(info.get("limit"), dict) else {}
            in_usd = _r(cost.get("input"))
            out_usd = _r(cost.get("output"))
            cache_usd = _r(cost.get("cache_read"))
            ctx = int(lim["context"]) if isinstance(lim.get("context"), (int, float)) and lim["context"] > 0 else None
            max_out = int(lim["output"]) if isinstance(lim.get("output"), (int, float)) and lim["output"] > 0 else None
            if in_usd is None and ctx is None:
                continue
            has_long_tier = bool(cost.get("context_over_200k") or cost.get("tiers"))
            mods = info.get("modalities") if isinstance(info.get("modalities"), dict) else {}
            inp_mods = mods.get("input") if isinstance(mods.get("input"), list) else []
            spec = {
                "provider": prov,
                "source": "models.dev",
                "in_usd": in_usd,
                "out_usd": out_usd,
                "cache_usd": cache_usd,
                "context": ctx,
                "max_output": max_out,
                "reasoning": bool(info.get("reasoning")),
                "tool_call": bool(info.get("tool_call")),
                "vision": "image" in inp_mods,
                "long_tier": has_long_tier,
            }
            cur = ranked.get(key)
            # Prefer canonical first-party providers, then entries that include pricing
            score = pri * 10 + (0 if in_usd is not None else 5)
            if cur is None or score < cur[0]:
                ranked[key] = (score, spec)
    return {k: v for k, (_, v) in ranked.items()}


def normalize_litellm(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    ranked: dict[str, tuple[int, dict[str, Any]]] = {}
    for raw_key, info in raw.items():
        if not isinstance(info, dict) or raw_key == "sample_spec":
            continue
        key = str(raw_key).strip().lower().rsplit("/", 1)[-1]
        if not key:
            continue
        in_tok = info.get("input_cost_per_token")
        out_tok = info.get("output_cost_per_token")
        cache_tok = info.get("cache_read_input_token_cost")
        in_usd = _r(in_tok * 1_000_000) if isinstance(in_tok, (int, float)) and in_tok > 0 else None
        out_usd = _r(out_tok * 1_000_000) if isinstance(out_tok, (int, float)) and out_tok > 0 else None
        cache_usd = _r(cache_tok * 1_000_000) if isinstance(cache_tok, (int, float)) and cache_tok >= 0 else None
        ctx = info.get("max_input_tokens") or info.get("max_tokens")
        ctx = int(ctx) if isinstance(ctx, (int, float)) and ctx > 0 else None
        max_out = info.get("max_output_tokens")
        max_out = int(max_out) if isinstance(max_out, (int, float)) and max_out > 0 else None
        if in_usd is None and ctx is None:
            continue
        spec = {
            "provider": str(info.get("litellm_provider") or ""),
            "source": "litellm",
            "in_usd": in_usd,
            "out_usd": out_usd,
            "cache_usd": cache_usd,
            "context": ctx,
            "max_output": max_out,
            "reasoning": bool(info.get("supports_reasoning")),
            "tool_call": bool(info.get("supports_function_calling")),
            "vision": bool(info.get("supports_vision")),
            "long_tier": bool(info.get("input_cost_per_token_above_200k_tokens")),
        }
        pri = 0 if "/" not in raw_key else 1
        cur = ranked.get(key)
        if cur is None or pri < cur[0]:
            ranked[key] = (pri, spec)
    return {k: v for k, (_, v) in ranked.items()}


def merge_specs(md: dict[str, dict[str, Any]], ll: dict[str, dict[str, Any]], filter_keys: set[str] | None = None) -> dict[str, dict[str, Any]]:
    keys = set(md.keys()) | set(ll.keys())
    if filter_keys is not None:
        keys = keys & filter_keys
    out: dict[str, dict[str, Any]] = {}
    for k in sorted(keys):
        a = md.get(k)
        b = ll.get(k)
        if a and b:
            merged = dict(a)
            for field in ("in_usd", "out_usd", "cache_usd", "context", "max_output"):
                if merged.get(field) is None and b.get(field) is not None:
                    merged[field] = b[field]
            for flag in ("reasoning", "tool_call", "vision", "long_tier"):
                merged[flag] = bool(merged.get(flag) or b.get(flag))
            out[k] = merged
        else:
            out[k] = dict(a or b or {})
    return out


def load_model_specs(data_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    p = (data_dir or DATA) / "model-specs.json"
    if not p.exists():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        models = doc.get("models") if isinstance(doc, dict) else None
        return models if isinstance(models, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def fmt_ctx(n: int | None) -> str:
    if not n or n <= 0:
        return ""
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.2g}M"
    if n >= 1_000:
        return f"{round(n / 1000)}K"
    return str(n)


def main() -> None:
    md_raw = ll_raw = None
    try:
        md_raw = _get_json(MODELS_DEV_URL)
    except Exception as e:  # noqa: BLE001
        print(f"warn: models.dev fetch failed: {e}", file=sys.stderr)
    try:
        ll_raw = _get_json(LITELLM_URL)
    except Exception as e:  # noqa: BLE001
        print(f"warn: litellm fetch failed: {e}", file=sys.stderr)
    md = normalize_models_dev(md_raw)
    ll = normalize_litellm(ll_raw)
    # Keep entries that match our live search.json (or raw snapshots in CI) so committed JSON stays compact (~220 KB).
    search_p = ROOT / "dist" / "data" / "search.json"
    filt: set[str] | None = None
    if search_p.exists():
        try:
            filt = {x["m"].lower() for x in json.loads(search_p.read_text(encoding="utf-8")) if isinstance(x, dict) and x.get("m")}
        except Exception:  # noqa: BLE001
            filt = None
    if filt is None:
        import gzip
        import pricing as P

        raw_dir = DATA / "raw"
        found: set[str] = set()
        if raw_dir.exists():
            for f in raw_dir.glob("*.json.gz"):
                try:
                    with gzip.open(f, "rt", encoding="utf-8") as fh:
                        snap = json.load(fh)
                    for m in (snap.get("pricing") or {}).get("data") or []:
                        if isinstance(m, dict) and m.get("model_name"):
                            found.add(P.canonical_model(str(m["model_name"])))
                except Exception:  # noqa: BLE001
                    continue
        if found:
            filt = found
        elif OUT.exists():
            filt = set(load_model_specs(DATA).keys()) or None
    merged = merge_specs(md, ll, filt)
    doc = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sources": ["anomalyco/models.dev", "BerriAI/litellm"],
        "count": len(merged),
        "models": merged,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps({"models_dev": len(md), "litellm": len(ll), "saved": len(merged), "out": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
