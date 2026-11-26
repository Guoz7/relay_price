"""Convert pricing payloads from different relay systems into the New API `/api/pricing` shape.

Each adapter returns (pricing, status_override, system) or None. `pricing` always looks like:
  {"data": [ {model_name, quota_type, model_ratio, completion_ratio, cache_ratio, model_price,
              enable_groups, group_price?}, ... ],
   "group_ratio": {...}, "usable_group": {...}}
so pricing.normalize_station() can handle every source the same way.

Only formats whose units are unambiguous are adapted; anything else returns None (we do not guess prices).
"""

from __future__ import annotations

from typing import Any

Result = tuple[dict[str, Any], dict[str, Any] | None, str]


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.strip())
        except ValueError:
            return None
    return None


def newapi(j: Any) -> Result | None:
    """Standard New API, including newer builds that wrap the payload one level deeper."""
    if isinstance(j, dict) and isinstance(j.get("data"), dict) and isinstance(j["data"].get("data"), list):
        j = j["data"]
    if isinstance(j, dict) and isinstance(j.get("data"), list) and (not j["data"] or isinstance(j["data"][0], dict) and "model_name" in j["data"][0]):
        keep = {k: j.get(k) for k in ("data", "group_ratio", "usable_group") if k in j}
        return keep, None, "new-api"
    return None


def legacy_ratio_maps(j: Any) -> Result | None:
    """Old One API style: data = {ModelRatio, CompletionRatio, ModelFixedPrice, GroupRatio, Models}."""
    d = j.get("data") if isinstance(j, dict) else None
    if not (isinstance(d, dict) and isinstance(d.get("ModelRatio"), dict)):
        return None
    mr, cr = d.get("ModelRatio") or {}, d.get("CompletionRatio") or {}
    fixed = d.get("ModelFixedPrice") or {}
    gr = {k: v for k, v in (d.get("GroupRatio") or {}).items() if _num(v) is not None}
    names = d.get("Models") if isinstance(d.get("Models"), list) else sorted(set(mr) | set(fixed))
    groups = list(gr) or ["default"]
    rows = []
    for n in names:
        n = str(n)
        if _num(fixed.get(n)) is not None and _num(fixed.get(n)) > 0:
            rows.append({"model_name": n, "quota_type": 1, "model_price": _num(fixed[n]), "enable_groups": groups})
        elif _num(mr.get(n)) is not None:
            rows.append({"model_name": n, "quota_type": 0, "model_ratio": _num(mr[n]),
                         "completion_ratio": _num(cr.get(n)) or 1.0, "enable_groups": groups})
    return {"data": rows, "group_ratio": gr or {"default": 1}, "usable_group": {}}, None, "one-api-legacy"


def voapi(j: Any) -> Result | None:
    """VoAPI style: data = {group_info: {g: {GroupRatio, DisplayName, Description}}, model_info: [{model_name, price_info: {g: {"default": {...}}}}]}."""
    d = j.get("data") if isinstance(j, dict) else None
    if not (isinstance(d, dict) and isinstance(d.get("model_info"), list) and isinstance(d.get("group_info"), dict)):
        return None
    gi = d["group_info"]
    gr = {g: _num(v.get("GroupRatio")) for g, v in gi.items() if isinstance(v, dict) and _num(v.get("GroupRatio")) is not None}
    ug = {g: (str(v.get("DisplayName") or g) + (f"：{v['Description']}" if v.get("Description") else "")) for g, v in gi.items() if isinstance(v, dict)}
    rows = []
    for m in d["model_info"]:
        if not isinstance(m, dict) or not m.get("model_name") or not isinstance(m.get("price_info"), dict):
            continue
        gp: dict[str, Any] = {}
        comp = None
        for g, per in m["price_info"].items():
            e = per.get("default") if isinstance(per, dict) else None
            if not isinstance(e, dict):
                continue
            ratio, price = _num(e.get("model_ratio")), _num(e.get("model_price"))
            if ratio and ratio > 0:  # token billing (VoAPI's quota_type flag is not reliable for this)
                comp = comp or _num(e.get("model_completion_ratio"))
                gp[g] = {"type": 0, "price": ratio, "group_ratio": gr.get(g, 1.0),
                         "completion_ratio": _num(e.get("model_completion_ratio")) or 1.0,
                         "cache_hits_ratio": _num(e.get("model_cache_ratio"))}
            elif price and price > 0:
                gp[g] = {"type": 1, "price": price, "group_ratio": gr.get(g, 1.0)}
        if not gp:
            continue
        types = [v["type"] for v in gp.values()]
        rows.append({"model_name": str(m["model_name"]), "quota_type": 0 if types.count(0) >= types.count(1) else 1,
                     "completion_ratio": comp or 1.0, "enable_groups": list(gp), "group_price": gp})
    return {"data": rows, "group_ratio": gr, "usable_group": ug}, None, "voapi"


def public_schema(j: Any) -> Result | None:
    """Self-published price list: {data: {currency, price_unit: per_1m_tokens, models: [{group_name, model_name, input_price, output_price, cache_input_price}]}}."""
    d = j.get("data") if isinstance(j, dict) else None
    if not (isinstance(d, dict) and isinstance(d.get("models"), list) and d.get("price_unit") == "per_1m_tokens"):
        return None
    cur = str(d.get("currency") or "").upper()
    if cur not in ("USD", "CNY"):
        return None
    by_model: dict[str, dict[str, Any]] = {}
    for m in d["models"]:
        if not isinstance(m, dict) or m.get("enabled") is False:
            continue
        n, ip, op = m.get("model_name"), _num(m.get("input_price")), _num(m.get("output_price"))
        if not n or ip is None or ip <= 0:
            continue
        g = str(m.get("group_name") or "default")
        ent = by_model.setdefault(str(n), {"model_name": str(n), "quota_type": 0, "group_price": {}, "enable_groups": []})
        cache = _num(m.get("cache_input_price"))
        ent["group_price"][g] = {"type": 0, "price": ip / 2.0, "group_ratio": 1.0,
                                 "completion_ratio": (op / ip) if op is not None else 1.0,
                                 "cache_hits_ratio": (cache / ip) if cache else None}
        ent["enable_groups"].append(g)
    rows = list(by_model.values())
    status = {"price": 1.0} if cur == "CNY" else None  # CNY lists are already in yuan
    return {"data": rows, "group_ratio": {}, "usable_group": {}}, status, f"public-schema-{cur.lower()}"


ADAPTERS = (newapi, voapi, legacy_ratio_maps, public_schema)


def adapt(j: Any) -> Result | None:
    for f in ADAPTERS:
        try:
            r = f(j)
        except Exception:  # noqa: BLE001
            r = None
        if r is not None and r[0].get("data"):
            return r
    return None
