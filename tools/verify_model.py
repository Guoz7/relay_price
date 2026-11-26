"""Standalone, zero-dependency local LLM authenticity verifier (CLI + importable by probes.py).

Runs the exact same 4 technical authenticity checks locally so your API key NEVER leaves your machine:
1. fingerprint: Native endpoint response model echo, ID prefix, headers & deterministic micro-math.
2. thinking:    Anthropic Extended Thinking cryptographic signature + Turn-2 round-trip verification
                (or OpenAI/DeepSeek reasoning telemetry).
3. tool:        Strict typed Tool-Calling schema conformance (structured tool_use/tool_calls + int arg).
4. cache:       2-turn ~1,300-token deterministic Prompt Cache read hit verification.

Usage:
  python tools/verify_model.py --base-url https://api.example.com --api-key sk-... --model claude-sonnet-5-5
  python tools/verify_model.py --base-url https://api.example.com --api-key sk-... --model gpt-6-astra --protocol openai-response
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

CACHE_PREFIX = (
    "You are a strict compliance auditor reviewing API relay technical specifications.\n"
    "Memorize the following numbered policy clauses and answer ONLY with the requested clause route code.\n"
    + "\n".join(
        f"Clause {i:03d}: Relay gateway route R-{i:03d} enforces UTF-8 JSON framing, monotonic request IDs, "
        f"explicit latency accounting in milliseconds, non-negative token usage fields, and deterministic error codes."
        for i in range(1, 61)
    )
)


def normalize_base_url(raw: str) -> str:
    s = raw.strip().rstrip("/")
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    s = re.sub(r"/v1/(?:messages|chat/completions|responses)$", "", s, flags=re.I)
    s = re.sub(r"/v1$", "", s, flags=re.I)
    return s


def detect_protocol(model: str, requested: str = "auto") -> str:
    if requested and requested != "auto":
        return requested
    m = model.lower()
    if "claude" in m or "fable" in m:
        return "anthropic"
    if re.match(r"^(?:gpt-6|gpt-5|o3|o4|codex)", m):
        return "openai-response"
    return "openai"


def model_echo_matches(requested: str, returned: str | None) -> bool:
    if not returned:
        return False
    a = requested.lower().rsplit("/", 1)[-1]
    b = returned.lower().rsplit("/", 1)[-1]
    if a == b or b.startswith(a) or a.startswith(b):
        return True
    na, nb = a.replace(".", "-"), b.replace(".", "-")
    return nb.startswith(na) or na.startswith(nb)


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    t0 = time.perf_counter()
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "RelayPriceVerifyCLI/1.0", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(65536).decode("utf-8", "replace")
            ms = round((time.perf_counter() - t0) * 1000)
            hdrs = {k.lower(): v for k, v in resp.headers.items() if k.lower() in (
                "request-id", "x-request-id", "anthropic-ratelimit-requests-limit",
                "openai-organization", "server", "cf-ray",
            )}
            return {"ok": True, "status": resp.status, "ms": ms, "data": json.loads(raw), "headers": hdrs}
    except urllib.error.HTTPError as e:
        ms = round((time.perf_counter() - t0) * 1000)
        txt = e.read(4096).decode("utf-8", "replace")
        return {"ok": False, "status": e.code, "ms": ms, "data": {"error": txt[:260]}, "headers": {}}
    except Exception as e:  # noqa: BLE001
        ms = round((time.perf_counter() - t0) * 1000)
        return {"ok": False, "status": 0, "ms": ms, "data": {"error": f"{type(e).__name__}: {e}"}, "headers": {}}


def _auth_headers(protocol: str, api_key: str) -> dict[str, str]:
    if protocol == "anthropic":
        return {"x-api-key": api_key, "Authorization": f"Bearer {api_key}", "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {api_key}"}


def _endpoint(base: str, protocol: str) -> str:
    if protocol == "anthropic":
        return f"{base}/v1/messages"
    if protocol == "openai-response":
        return f"{base}/v1/responses"
    return f"{base}/v1/chat/completions"


def _extract_text(protocol: str, data: dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    if protocol == "anthropic" and isinstance(data.get("content"), list):
        return "\n".join(b.get("text", "") for b in data["content"] if isinstance(b, dict) and b.get("type") == "text").strip()
    if protocol == "openai-response":
        if isinstance(data.get("output_text"), str) and data["output_text"]:
            return data["output_text"].strip()
        parts: list[str] = []
        for item in data.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "message":
                for c in item.get("content") or []:
                    if isinstance(c, dict) and isinstance(c.get("text"), str):
                        parts.append(c["text"])
        return "\n".join(parts).strip()
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message") or {}
        if isinstance(msg.get("content"), str):
            return msg["content"].strip()
    return ""


def check_fingerprint(base: str, api_key: str, model: str, protocol: str) -> dict[str, Any]:
    url = _endpoint(base, protocol)
    prompt = "Compute (37 * 43) - 891. Reply with the single integer only, no explanation."
    if protocol == "anthropic":
        payload = {"model": model, "max_tokens": 32, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
    elif protocol == "openai-response":
        payload = {"model": model, "max_output_tokens": 64, "input": prompt}
    else:
        payload = {"model": model, "max_tokens": 32, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
    r = _post_json(url, _auth_headers(protocol, api_key), payload)
    if not r["ok"]:
        return {"id": "fingerprint", "title": "协议指纹与模型回显", "status": "fail", "ms": r["ms"], "summary": f"HTTP {r['status']} 调用失败", "evidence": r["data"]}
    data = r["data"]
    txt = _extract_text(protocol, data)
    ret_model, ret_id = data.get("model"), data.get("id")
    math_ok = bool(re.search(r"\b700\b", txt))
    echo_ok = model_echo_matches(model, ret_model)
    id_rx = r"^msg_" if protocol == "anthropic" else (r"^resp_" if protocol == "openai-response" else r"^(?:chatcmpl-|cmpl-|msg_|gen-)")
    id_ok = isinstance(ret_id, str) and bool(re.match(id_rx, ret_id))
    status = "pass" if (echo_ok and math_ok and id_ok) else ("warn" if math_ok and (echo_ok or id_ok) else "fail")
    return {
        "id": "fingerprint", "title": "协议指纹与模型回显", "status": status, "ms": r["ms"],
        "summary": f"model={ret_model}, id={ret_id}, ans={txt[:30]} ({r['ms']}ms)",
        "evidence": {"returned_model": ret_model, "response_id": ret_id, "answer": txt[:80], "headers": r["headers"]},
    }


def check_thinking(base: str, api_key: str, model: str, protocol: str) -> dict[str, Any]:
    url = _endpoint(base, protocol)
    if protocol == "anthropic":
        p1 = {"model": model, "max_tokens": 1400, "temperature": 1, "thinking": {"type": "enabled", "budget_tokens": 1024},
              "messages": [{"role": "user", "content": "Compute (19 * 23) + 17. Reply with the integer only."}]}
        r1 = _post_json(url, _auth_headers(protocol, api_key), p1)
        if not r1["ok"]:
            return {"id": "thinking", "title": "Thinking 加密签名与多轮回传", "status": "fail", "ms": r1["ms"], "summary": f"HTTP {r1['status']}", "evidence": r1["data"]}
        blocks = r1["data"].get("content") if isinstance(r1["data"].get("content"), list) else []
        tblk = next((b for b in blocks if isinstance(b, dict) and b.get("type") == "thinking"), None)
        sig = str((tblk or {}).get("signature") or "")
        if not tblk or len(sig) < 40:
            return {"id": "thinking", "title": "Thinking 加密签名与多轮回传", "status": "fail", "ms": r1["ms"],
                    "summary": f"缺少原生 thinking 加密签名 (len={len(sig)})", "evidence": {"signature_len": len(sig)}}
        p2 = {"model": model, "max_tokens": 1200, "temperature": 1, "thinking": {"type": "enabled", "budget_tokens": 1024},
              "messages": [
                  {"role": "user", "content": "Compute (19 * 23) + 17. Reply with the integer only."},
                  {"role": "assistant", "content": blocks},
                  {"role": "user", "content": "Add 6 to your previous integer result. Reply with the integer only."},
              ]}
        r2 = _post_json(url, _auth_headers(protocol, api_key), p2)
        t2 = _extract_text(protocol, r2["data"]) if r2["ok"] else ""
        ok2 = r2["ok"] and bool(re.search(r"\b460\b", t2))
        t2_lbl = "ok (460)" if ok2 else f"HTTP {r2['status']}"
        return {"id": "thinking", "title": "Thinking 加密签名与多轮回传", "status": "pass" if ok2 else "warn", "ms": r1["ms"] + r2["ms"],
                "summary": f"signature len={len(sig)}, turn2={t2_lbl}",
                "evidence": {"signature_len": len(sig), "turn2_status": r2["status"], "answer_t2": t2[:40]}}
    prompt = "Compute (19 * 23) + 17. Reply with the integer only."
    payload = {"model": model, "max_output_tokens": 256, "input": prompt} if protocol == "openai-response" else {
        "model": model, "max_tokens": 256, "messages": [{"role": "user", "content": prompt}]}
    r = _post_json(url, _auth_headers(protocol, api_key), payload)
    if not r["ok"]:
        return {"id": "thinking", "title": "推理遥测与确定性校验", "status": "fail", "ms": r["ms"], "summary": f"HTTP {r['status']}", "evidence": r["data"]}
    txt = _extract_text(protocol, r["data"])
    ok_ans = bool(re.search(r"\b454\b", txt))
    u = r["data"].get("usage") or {}
    rtok = (u.get("output_tokens_details") or {}).get("reasoning_tokens")
    if rtok is None:
        rtok = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    has_r = rtok is not None or bool(((r["data"].get("choices") or [{}])[0].get("message") or {}).get("reasoning_content"))
    return {"id": "thinking", "title": "推理遥测与确定性校验", "status": "pass" if (ok_ans and has_r) else ("warn" if ok_ans else "fail"),
            "ms": r["ms"], "summary": f"ans={txt[:20]}, reasoning_tokens={rtok}", "evidence": {"answer": txt[:40], "reasoning_tokens": rtok}}


def check_tool(base: str, api_key: str, model: str, protocol: str) -> dict[str, Any]:
    url = _endpoint(base, protocol)
    msg = 'Call the tool lookup_audit_code with region="CN" and code=4096. Do not answer in plain text.'
    schema = {"type": "object", "properties": {"region": {"type": "string", "enum": ["CN", "US"]}, "code": {"type": "integer"}}, "required": ["region", "code"]}
    if protocol == "anthropic":
        payload = {"model": model, "max_tokens": 200, "tools": [{"name": "lookup_audit_code", "description": "Audit lookup", "input_schema": schema}],
                   "tool_choice": {"type": "any"}, "messages": [{"role": "user", "content": msg}]}
    elif protocol == "openai-response":
        payload = {"model": model, "max_output_tokens": 200, "tools": [{"type": "function", "name": "lookup_audit_code", "description": "Audit lookup", "parameters": schema}],
                   "tool_choice": "required", "input": msg}
    else:
        payload = {"model": model, "max_tokens": 200, "tools": [{"type": "function", "function": {"name": "lookup_audit_code", "description": "Audit lookup", "parameters": schema}}],
                   "tool_choice": "required", "messages": [{"role": "user", "content": msg}]}
    r = _post_json(url, _auth_headers(protocol, api_key), payload)
    if not r["ok"]:
        return {"id": "tool", "title": "严格 Tool-Calling 结构校验", "status": "fail", "ms": r["ms"], "summary": f"HTTP {r['status']}", "evidence": r["data"]}
    fn, args = None, None
    data = r["data"]
    if protocol == "anthropic":
        tu = next((b for b in (data.get("content") or []) if isinstance(b, dict) and b.get("type") == "tool_use"), None)
        if tu:
            fn, args = tu.get("name"), tu.get("input")
    elif protocol == "openai-response":
        fc = next((b for b in (data.get("output") or []) if isinstance(b, dict) and b.get("type") == "function_call"), None)
        if fc:
            fn = fc.get("name")
            try:
                args = json.loads(fc.get("arguments")) if isinstance(fc.get("arguments"), str) else fc.get("arguments")
            except ValueError:
                args = fc.get("arguments")
    else:
        tcs = ((data.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
        if tcs and isinstance(tcs[0], dict) and isinstance(tcs[0].get("function"), dict):
            fn = tcs[0]["function"].get("name")
            raw_a = tcs[0]["function"].get("arguments")
            try:
                args = json.loads(raw_a) if isinstance(raw_a, str) else raw_a
            except ValueError:
                args = raw_a
    exact = fn == "lookup_audit_code" and isinstance(args, dict) and args.get("region") == "CN" and args.get("code") == 4096 and type(args.get("code")) is int
    loose = fn == "lookup_audit_code" and isinstance(args, dict) and str(args.get("region")).upper() == "CN" and str(args.get("code")) == "4096"
    return {"id": "tool", "title": "严格 Tool-Calling 结构校验", "status": "pass" if exact else ("warn" if loose else "fail"),
            "ms": r["ms"], "summary": f"fn={fn}, args={args}", "evidence": {"function_name": fn, "arguments": args}}


def check_cache(base: str, api_key: str, model: str, protocol: str) -> dict[str, Any]:
    url = _endpoint(base, protocol)

    def make(q: str) -> dict[str, Any]:
        if protocol == "anthropic":
            return {"model": model, "max_tokens": 24, "temperature": 0,
                    "system": [{"type": "text", "text": CACHE_PREFIX, "cache_control": {"type": "ephemeral"}}],
                    "messages": [{"role": "user", "content": q}]}
        if protocol == "openai-response":
            return {"model": model, "max_output_tokens": 32, "instructions": CACHE_PREFIX, "input": q}
        return {"model": model, "max_tokens": 24, "temperature": 0,
                "messages": [{"role": "system", "content": CACHE_PREFIX}, {"role": "user", "content": q}]}

    r1 = _post_json(url, _auth_headers(protocol, api_key), make("What is the route code for Clause 017? Reply with the code only."))
    if not r1["ok"]:
        return {"id": "cache", "title": "两轮 Prompt Cache 真命中", "status": "fail", "ms": r1["ms"], "summary": f"Turn 1 HTTP {r1['status']}", "evidence": r1["data"]}
    r2 = _post_json(url, _auth_headers(protocol, api_key), make("What is the route code for Clause 042? Reply with the code only."))
    if not r2["ok"]:
        return {"id": "cache", "title": "两轮 Prompt Cache 真命中", "status": "fail", "ms": r1["ms"] + r2["ms"], "summary": f"Turn 2 HTTP {r2['status']}", "evidence": r2["data"]}
    u2 = r2["data"].get("usage") or {}
    hit = (
        u2.get("cache_read_input_tokens")
        or (u2.get("input_tokens_details") or {}).get("cached_tokens")
        or (u2.get("prompt_tokens_details") or {}).get("cached_tokens")
        or u2.get("prompt_cache_hit_tokens")
        or 0
    )
    return {"id": "cache", "title": "两轮 Prompt Cache 真命中", "status": "pass" if int(hit) > 0 else "warn",
            "ms": r1["ms"] + r2["ms"], "summary": f"turn2 cache_read={hit}", "evidence": {"turn2_usage": u2, "cache_read_t2": hit}}


def run_suite(base_url: str, api_key: str, model: str, protocol: str = "auto", checks: list[str] | None = None) -> dict[str, Any]:
    base = normalize_base_url(base_url)
    proto = detect_protocol(model, protocol)
    want = checks or ["fingerprint", "thinking", "tool", "cache"]
    runners = {"fingerprint": check_fingerprint, "thinking": check_thinking, "tool": check_tool, "cache": check_cache}
    results = [runners[c](base, api_key, model, proto) for c in want if c in runners]
    n_pass = sum(1 for x in results if x["status"] == "pass")
    n_fail = sum(1 for x in results if x["status"] == "fail")
    score = round(sum(100 if x["status"] == "pass" else (55 if x["status"] == "warn" else 0) for x in results) / max(1, len(results)))
    level = "fail" if (n_fail > 0 or score < 55) else ("pass" if n_pass == len(results) else "warn")
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base, "model": model, "protocol": proto,
        "score": score, "level": level, "passed": n_pass, "total": len(results), "results": results,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify LLM API relay authenticity locally (key never leaves your machine).")
    ap.add_argument("--base-url", required=True, help="Relay base URL, e.g. https://api.example.com")
    ap.add_argument("--api-key", required=True, help="API key for testing")
    ap.add_argument("--model", required=True, help="Model ID, e.g. claude-sonnet-5-5")
    ap.add_argument("--protocol", default="auto", choices=["auto", "anthropic", "openai-response", "openai"])
    ap.add_argument("--checks", default="fingerprint,thinking,tool,cache", help="Comma-separated checks")
    args = ap.parse_args()
    out = run_suite(args.base_url, args.api_key, args.model, args.protocol, [c.strip() for c in args.checks.split(",") if c.strip()])
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if out["level"] != "fail" else 1)


if __name__ == "__main__":
    main()
