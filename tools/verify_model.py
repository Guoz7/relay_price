"""Standalone, zero-dependency local LLM authenticity verifier (CLI + importable library).

Mirrors functions/api/verify.js 1:1 (same host guard, payloads, heuristics, titles and scoring) so your
API key NEVER leaves your machine:
1. fingerprint: response shape, normalized model echo, ID format, deterministic micro-math + timing telemetry.
2. thinking:    Anthropic: thinking signature + Turn-2 genuine round-trip + tampered-signature negative check
                + usage consistency. OpenAI / DeepSeek / Gemini: native reasoning telemetry + usage sanity.
3. tool:        Strict typed Tool-Calling schema conformance (tool_use / tool_calls / functionCall + int arg).
4. cache:       2-turn ~1,300-token deterministic Prompt Cache read hit verification.

Usage:
  python tools/verify_model.py --base-url https://api.example.com --api-key sk-... --model claude-sonnet-5-5
  python tools/verify_model.py --base-url https://api.example.com --api-key sk-... --model gemini-4-carbon --protocol gemini
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

PROTOCOLS = ["auto", "anthropic", "openai-response", "openai", "gemini"]
ALL_CHECKS = ["fingerprint", "thinking", "tool", "cache"]
MAX_UPSTREAM_BYTES = 65536
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
BLOCKED_SUFFIXES = (
    ".local", ".localhost", ".internal", ".intranet", ".lan", ".home", ".corp", ".localdomain", ".home.arpa",
    ".nip.io", ".sslip.io", ".xip.io", ".localtest.me", ".lvh.me",
)
BLOCKED_HOSTS = ("localhost", "metadata.google.internal", "nip.io", "sslip.io", "localtest.me", "lvh.me")
MATH_Q = "Compute (37 * 43) - 891. Reply with the single integer only, no explanation."
THINK_Q1 = "Compute (19 * 23) + 17. Reply with the integer only."
THINK_Q2 = "Add 6 to your previous integer result. Reply with the integer only."
TOOL_MSG = 'Call the tool lookup_audit_code with region="CN" and code=4096. Do not answer in plain text.'
TOOL_DESC = "Lookup an audit verification record by region and numeric code."
_NUMLABEL = re.compile(r"(?:\d+|0x[0-9a-f]*)")

CACHE_PREFIX = (
    "You are a strict compliance auditor reviewing API relay technical specifications.\n"
    "Memorize the following numbered policy clauses and answer ONLY with the requested clause route code.\n"
    + "\n".join(
        f"Clause {i:03d}: Relay gateway route R-{i:03d} enforces UTF-8 JSON framing, monotonic request IDs, "
        f"explicit latency accounting in milliseconds, non-negative token usage fields, and deterministic error codes."
        for i in range(1, 61)
    )
)


# ------------------------------------------------------------------ pure helpers (mirrored in verify.js)

def is_blocked_host(hostname: str | None) -> bool:
    h = str(hostname or "").lower().strip()
    if h.endswith("."):
        h = h[:-1]
    if not h or len(h) > 253:
        return True
    if re.search(r"[\[\]:]", h) or not re.fullmatch(r"[a-z0-9.-]+", h):
        return True
    labels = h.split(".")
    if len(labels) < 2:
        return True
    if any(not lb or len(lb) > 63 or lb.startswith("-") or lb.endswith("-") for lb in labels):
        return True
    if h in BLOCKED_HOSTS or any(h.endswith(s) for s in BLOCKED_SUFFIXES):
        return True
    if _NUMLABEL.fullmatch(labels[-1]) or all(_NUMLABEL.fullmatch(lb) for lb in labels):
        return True
    return False


def normalize_base_url(raw: str | None) -> str | None:
    s = str(raw or "").strip()
    if not s or len(s) > 300 or re.search(r"\s", s):
        return None
    has_scheme = re.match(r"[a-z][a-z0-9+.-]*://", s, re.I) is not None
    if has_scheme and not re.match(r"https://", s, re.I):
        return None
    try:
        parts = urlsplit(s if has_scheme else "https://" + s)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or parts.username is not None or parts.password is not None:
        return None
    if port is not None and port != 443:
        return None
    host = parts.hostname or ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if is_blocked_host(host):
        return None
    path = quote(parts.path, safe="/%._~-").rstrip("/")
    path = re.sub(r"/v1/(?:messages|chat/completions|responses)$", "", path, flags=re.I)
    path = re.sub(r"/v1beta(?:/models(?:/[^/]*)?)?$", "", path, flags=re.I)
    path = re.sub(r"/v1$", "", path, flags=re.I)
    if not re.fullmatch(r"[A-Za-z0-9._~/%-]*", path):
        return None
    return f"https://{host.lower()}{path}"


def is_valid_model(model: str | None) -> bool:
    m = str(model or "")
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,119}", m) is not None and ".." not in m


def detect_protocol(model: str, requested: str | None = "auto") -> str:
    if requested and requested != "auto":
        return requested
    m = str(model or "").lower().split("/")[-1]
    if "claude" in m or "fable" in m:
        return "anthropic"
    if "gemini" in m:
        return "gemini"
    if re.match(r"(?:gpt-6|gpt-5|o3|o4|codex)", m):
        return "openai-response"
    return "openai"


def normalize_model_id(s: str | None) -> str:
    x = str(s or "").lower().strip()
    x = re.sub(r"^models/", "", x).split("/")[-1]
    x = re.sub(r"^(?:[a-z]{2}\.)?anthropic\.", "", x)
    x = re.sub(r"[@:_\s]+", "-", x).replace(".", "-")
    while True:
        prev = x
        x = re.sub(r"-v\d+-\d+$", "", x)
        x = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", x)
        x = re.sub(r"-(?:preview|exp)(?:-\d{2}-\d{2})?$", "", x)
        x = re.sub(r"-(?:\d{6,8}|latest|thinking)$", "", x)
        if x == prev:
            return x


def model_echo_matches(requested: str, returned: str | None) -> bool:
    if not returned:
        return False
    a, b = normalize_model_id(requested), normalize_model_id(returned)
    return bool(a) and a == b


def redact(text: Any, api_key: str | None) -> str:
    t = "" if text is None else str(text)
    k = str(api_key or "")
    return t.replace(k, "***") if len(k) >= 6 else t


def tamper_signature(sig: str | None) -> str:
    s = str(sig or "")
    mid = len(s) // 2
    out = "".join(
        B64[(B64.index(c) + 17) % 64] if (mid - 4 <= i < mid + 4 and c in B64) else c
        for i, c in enumerate(s)
    )
    if out == s:
        return s[:-1] + ("B" if s.endswith("A") else "A")
    return out


def classify_tamper(status: int | None, data: Any) -> str:
    st = status or 0
    if 200 <= st < 300:
        return "accepted"
    if not st or st == 429 or st >= 500:
        return "inconclusive"
    msg = json.dumps(data or {}, ensure_ascii=False).lower()
    if st == 400 and re.search(r"signature|thinking|invalid", msg):
        return "rejected"
    return "rejected_other"


def _is_count(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 and float(v).is_integer()


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _total_in(u: dict[str, Any]) -> float:
    return sum(u.get(k) if _is_count(u.get(k)) else 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def anthropic_usage_check(u1: dict[str, Any] | None, u2: dict[str, Any] | None) -> dict[str, Any]:
    notes: list[str] = []
    if not isinstance(u1, dict) or not _is_count(u1.get("input_tokens")) or not _is_count(u1.get("output_tokens")):
        notes.append("首轮 usage 缺少整数 input_tokens / output_tokens")
    else:
        if _total_in(u1) <= 0:
            notes.append("首轮 input_tokens 为 0")
        if u1["output_tokens"] <= 0:
            notes.append("首轮 output_tokens 为 0（开启 thinking 时不应为 0）")
    for u in (u1, u2):
        if not isinstance(u, dict):
            continue
        for k in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            if u.get(k) is not None and not _is_count(u.get(k)):
                notes.append(f"{k} 不是非负整数")
    if (isinstance(u1, dict) and isinstance(u2, dict) and _is_count(u1.get("input_tokens"))
            and _is_count(u2.get("input_tokens")) and _total_in(u2) <= _total_in(u1)):
        notes.append("二轮输入 token 未随多轮上下文增长（疑似估算或伪造 usage）")
    return {"ok": not notes, "notes": notes}


def openai_usage_check(protocol: str, u: Any) -> dict[str, Any]:
    notes: list[str] = []
    if not isinstance(u, dict):
        return {"ok": False, "notes": ["未返回 usage 字段"]}
    if protocol == "gemini":
        cand = u.get("candidatesTokenCount")
        cand = 0 if cand is None else cand
        if not _is_count(u.get("promptTokenCount")) or not _is_count(cand):
            notes.append("usageMetadata 计数异常")
        elif u.get("totalTokenCount") is not None and u["totalTokenCount"] < u["promptTokenCount"] + (cand or 0):
            notes.append("totalTokenCount 小于输入与输出之和")
        return {"ok": not notes, "notes": notes}
    in_k = "input_tokens" if protocol == "openai-response" else "prompt_tokens"
    out_k = "output_tokens" if protocol == "openai-response" else "completion_tokens"
    det = u.get("output_tokens_details" if protocol == "openai-response" else "completion_tokens_details")
    if not _is_count(u.get(in_k)) or not _is_count(u.get(out_k)):
        notes.append(f"usage 缺少整数 {in_k} / {out_k}")
        return {"ok": False, "notes": notes}
    if u[in_k] <= 0:
        notes.append(f"{in_k} 为 0")
    if u.get("total_tokens") is not None and u["total_tokens"] != u[in_k] + u[out_k]:
        notes.append("total_tokens 与输入输出之和不一致")
    rt = det.get("reasoning_tokens") if isinstance(det, dict) else None
    if rt is not None and (not _is_count(rt) or rt > u[out_k]):
        notes.append("reasoning_tokens 超过输出 token 总数")
    return {"ok": not notes, "notes": notes}


def output_tokens(protocol: str, data: Any) -> int | None:
    d = data if isinstance(data, dict) else {}
    u = d.get("usageMetadata" if protocol == "gemini" else "usage") or {}
    if not isinstance(u, dict):
        return None
    if protocol == "gemini":
        v = (u.get("candidatesTokenCount") or 0) + (u.get("thoughtsTokenCount") or 0)
    elif protocol == "openai":
        v = u.get("completion_tokens")
    else:
        v = u.get("output_tokens")
    return int(v) if _is_count(v) else None


def telemetry(r: dict[str, Any] | None, protocol: str) -> dict[str, Any]:
    r = r or {}
    out = output_tokens(protocol, r.get("data"))
    ms = r.get("ms")
    tps = math.floor(out / (ms / 1000) * 10 + 0.5) / 10 if (out and ms and ms > 0) else None
    return {"ttfb_ms": r.get("ttfb_ms"), "total_ms": ms, "output_tokens": out, "tok_per_sec": tps}


def response_shape_ok(protocol: str, data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if protocol == "anthropic":
        return data.get("type") == "message" and data.get("role") == "assistant" and isinstance(data.get("content"), list)
    if protocol == "openai-response":
        return data.get("object") == "response" and isinstance(data.get("output"), list)
    if protocol == "gemini":
        return isinstance(data.get("candidates"), list) and len(data["candidates"]) > 0
    return data.get("object") == "chat.completion" and isinstance(data.get("choices"), list)


def response_id_ok(protocol: str, rid: Any) -> bool:
    if not isinstance(rid, str) or not rid:
        return False
    if protocol == "anthropic":
        return re.match(r"msg_[A-Za-z0-9]{8,}", rid) is not None
    if protocol == "openai-response":
        return re.match(r"resp_[A-Za-z0-9]{8,}", rid) is not None
    if protocol == "gemini":
        return re.fullmatch(r"[A-Za-z0-9_-]{8,}", rid) is not None
    return (re.match(r"(?:chatcmpl-|cmpl-|msg_|gen-)", rid) is not None
            or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", rid, re.I) is not None)


def _gemini_parts(d: dict[str, Any]) -> list[Any]:
    cands = d.get("candidates") or []
    first = cands[0] if cands and isinstance(cands[0], dict) else {}
    return ((first.get("content") or {}).get("parts") or []) if isinstance(first, dict) else []


def _first_message(d: dict[str, Any]) -> dict[str, Any]:
    ch = d.get("choices") or []
    first = ch[0] if ch and isinstance(ch[0], dict) else {}
    return first.get("message") or {}


def reasoning_signal(protocol: str, data: Any) -> dict[str, Any]:
    d = data if isinstance(data, dict) else {}
    tokens = None
    item = content = False
    sig_len = 0
    if protocol == "gemini":
        t = (d.get("usageMetadata") or {}).get("thoughtsTokenCount")
        tokens = int(t) if _is_count(t) else None
        for p in _gemini_parts(d):
            if isinstance(p, dict):
                if p.get("thought") is True:
                    item = True
                if isinstance(p.get("thoughtSignature"), str):
                    sig_len = max(sig_len, len(p["thoughtSignature"]))
    elif protocol == "openai-response":
        t = ((d.get("usage") or {}).get("output_tokens_details") or {}).get("reasoning_tokens")
        tokens = int(t) if _is_count(t) else None
        item = isinstance(d.get("output"), list) and any(isinstance(o, dict) and o.get("type") == "reasoning" for o in d["output"])
    else:
        t = ((d.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
        tokens = int(t) if _is_count(t) else None
        msg = _first_message(d)
        rc, rs = msg.get("reasoning_content"), msg.get("reasoning")
        content = bool((isinstance(rc, str) and rc.strip()) or (isinstance(rs, str) and rs.strip()))
    strong = (tokens is not None and tokens > 0) or item or content or sig_len > 0
    return {"reasoning_tokens": tokens, "reasoning_item": item, "reasoning_content": content,
            "thought_signature_len": sig_len, "strong": strong}


def thinking_verdict(v: dict[str, Any]) -> str:
    if not v.get("has_block") or v.get("signature_len", 0) < 40:
        return "fail"
    if v.get("tamper") == "accepted":
        return "fail"
    if v.get("roundtrip_ok") and v.get("tamper") == "rejected" and v.get("usage_ok"):
        return "pass"
    return "warn"


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results) or 1
    n_pass = sum(1 for x in results if x["status"] == "pass")
    n_fail = sum(1 for x in results if x["status"] == "fail")
    raw = sum(100 if x["status"] == "pass" else (55 if x["status"] == "warn" else 0) for x in results) / total
    score = math.floor(raw + 0.5)
    level, verdict = "pass", "高度符合原生协议与真机特征"
    if n_fail > 0 or score < 55:
        level, verdict = "fail", "存在明显异常（疑似转译、接口不兼容、降级或套壳，建议核对下方原始证据）"
    elif n_pass < len(results):
        level, verdict = "warn", "基础调用正常，但部分高级特征缺失（如缓存未命中或缺少原生遥测字段，建议小额实测）"
    return {"score": score, "level": level, "verdict": verdict, "passed": n_pass, "total": len(results)}


def extract_text(protocol: str, data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    if protocol == "anthropic" and isinstance(data.get("content"), list):
        return "\n".join(b.get("text") or "" for b in data["content"] if isinstance(b, dict) and b.get("type") == "text").strip()
    if protocol == "gemini":
        return "\n".join(p["text"] for p in _gemini_parts(data)
                         if isinstance(p, dict) and not p.get("thought") and isinstance(p.get("text"), str)).strip()
    if protocol == "openai-response":
        if isinstance(data.get("output_text"), str) and data["output_text"]:
            return data["output_text"].strip()
        parts: list[str] = []
        for item in data.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "message" and isinstance(item.get("content"), list):
                parts += [c["text"] for c in item["content"] if isinstance(c, dict) and isinstance(c.get("text"), str)]
        return "\n".join(parts).strip()
    content = _first_message(data).get("content")
    return content.strip() if isinstance(content, str) else ""


def _parse_args(a: Any) -> Any:
    if not isinstance(a, str):
        return a
    try:
        return json.loads(a)
    except ValueError:
        return a


def extract_tool_call(protocol: str, data: Any) -> dict[str, Any]:
    d = data if isinstance(data, dict) else {}
    none = {"name": None, "args": None}
    if protocol == "anthropic":
        tu = next((b for b in (d.get("content") or []) if isinstance(b, dict) and b.get("type") == "tool_use"), None)
        return {"name": tu.get("name") or None, "args": tu.get("input")} if tu else none
    if protocol == "openai-response":
        fc = next((b for b in (d.get("output") or []) if isinstance(b, dict) and b.get("type") == "function_call"), None)
        return {"name": fc.get("name") or None, "args": _parse_args(fc.get("arguments"))} if fc else none
    if protocol == "gemini":
        p = next((x for x in _gemini_parts(d) if isinstance(x, dict) and x.get("functionCall")), None)
        return {"name": p["functionCall"].get("name") or None, "args": p["functionCall"].get("args")} if p else none
    tcs = _first_message(d).get("tool_calls")
    f = tcs[0].get("function") if isinstance(tcs, list) and tcs and isinstance(tcs[0], dict) else None
    return {"name": f.get("name") or None, "args": _parse_args(f.get("arguments"))} if isinstance(f, dict) else none


def tool_verdict(name: Any, args: Any) -> str:
    if name != "lookup_audit_code" or not isinstance(args, dict):
        return "fail"
    if args.get("region") == "CN" and _is_num(args.get("code")) and args["code"] == 4096:
        return "pass"
    code = args.get("code")
    if str(args.get("region")).upper() == "CN" and isinstance(code, (str, int)) and not isinstance(code, bool) and str(code).strip() == "4096":
        return "warn"
    return "fail"


def cache_read_tokens(protocol: str, u: Any) -> Any:
    x = u if isinstance(u, dict) else {}
    if protocol == "gemini":
        return x.get("cachedContentTokenCount") or 0
    for v in (
        x.get("cache_read_input_tokens"),
        (x.get("input_tokens_details") or {}).get("cached_tokens") if isinstance(x.get("input_tokens_details"), dict) else None,
        (x.get("prompt_tokens_details") or {}).get("cached_tokens") if isinstance(x.get("prompt_tokens_details"), dict) else None,
        x.get("prompt_cache_hit_tokens"),
    ):
        if v is not None:
            return v
    return 0


# ------------------------------------------------------------------ network

class _RedirectBlocked(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise _RedirectBlocked(f"redirect blocked (HTTP {code})")


_OPENER = urllib.request.build_opener(_NoRedirect)
_HDR_KEYS = ("request-id", "x-request-id", "anthropic-ratelimit-requests-limit", "anthropic-organization-id",
             "openai-organization", "openai-processing-ms", "x-ratelimit-limit-requests", "server", "cf-ray")


def _parse_body(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return {"raw_text": text[:400]}


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], api_key: str, timeout: float = 24.0) -> dict[str, Any]:
    t0 = time.perf_counter()
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "RelayPriceVerifyCLI/1.1", **headers},
        method="POST",
    )

    def elapsed() -> int:
        return round((time.perf_counter() - t0) * 1000)

    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            ttfb = elapsed()
            text = redact(resp.read(MAX_UPSTREAM_BYTES).decode("utf-8", "replace"), api_key)
            hdrs = {k.lower(): redact(v, api_key)[:120] for k, v in resp.headers.items() if k.lower() in _HDR_KEYS}
            return {"ok": True, "status": resp.status, "ms": elapsed(), "ttfb_ms": ttfb, "data": _parse_body(text), "headers": hdrs}
    except urllib.error.HTTPError as e:
        ttfb = elapsed()
        text = redact(e.read(MAX_UPSTREAM_BYTES).decode("utf-8", "replace"), api_key)
        return {"ok": False, "status": e.code, "ms": elapsed(), "ttfb_ms": ttfb, "data": _parse_body(text), "headers": {}}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 0, "ms": elapsed(), "ttfb_ms": None,
                "data": {"error": redact(f"{type(e).__name__}: {e}", api_key)[:240]}, "headers": {}}


def _auth_headers(protocol: str, api_key: str) -> dict[str, str]:
    if protocol == "anthropic":
        return {"x-api-key": api_key, "Authorization": f"Bearer {api_key}", "anthropic-version": "2023-06-01"}
    if protocol == "gemini":
        return {"x-goog-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def endpoint_url(base: str, protocol: str, model: str = "") -> str:
    if protocol == "anthropic":
        return f"{base}/v1/messages"
    if protocol == "openai-response":
        return f"{base}/v1/responses"
    if protocol == "gemini":
        return f"{base}/v1beta/models/{quote(re.sub(r'^models/', '', str(model or '')), safe='')}:generateContent"
    return f"{base}/v1/chat/completions"


def _text_payload(protocol: str, model: str, prompt: str, max_tokens: int) -> dict[str, Any]:
    if protocol == "anthropic":
        return {"model": model, "max_tokens": max_tokens, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
    if protocol == "openai-response":
        return {"model": model, "max_output_tokens": max(max_tokens, 400), "input": prompt}
    if protocol == "gemini":
        return {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": max(max_tokens, 1024)}}
    return {"model": model, "max_tokens": max(max_tokens, 512), "temperature": 0, "messages": [{"role": "user", "content": prompt}]}


def _err_snippet(r: dict[str, Any]) -> str:
    return json.dumps(r.get("data") or {}, ensure_ascii=False, separators=(",", ":"))[:240]


def _http_fail(cid: str, title: str, r: dict[str, Any], summary: str) -> dict[str, Any]:
    return {"id": cid, "title": title, "status": "fail", "ms": r["ms"], "summary": summary,
            "telemetry": {"ttfb_ms": r.get("ttfb_ms"), "total_ms": r["ms"], "output_tokens": None, "tok_per_sec": None},
            "evidence": {"http_status": r["status"], "error": _err_snippet(r)}}


def _usage(protocol: str, data: Any) -> Any:
    return (data or {}).get("usageMetadata" if protocol == "gemini" else "usage") if isinstance(data, dict) else None


# ------------------------------------------------------------------ checks

def check_fingerprint(base: str, api_key: str, model: str, protocol: str) -> dict[str, Any]:
    title = "协议指纹与模型回显"
    r = _post_json(endpoint_url(base, protocol, model), _auth_headers(protocol, api_key), _text_payload(protocol, model, MATH_Q, 32), api_key)
    if not r["ok"]:
        return _http_fail("fingerprint", title, r, f"HTTP {r['status']} 调用失败")
    d = r["data"] if isinstance(r["data"], dict) else {}
    text = extract_text(protocol, d)
    ret_model = (d.get("modelVersion") if protocol == "gemini" else d.get("model")) or None
    ret_id = (d.get("responseId") if protocol == "gemini" else d.get("id")) or None
    math_ok = re.search(r"\b700\b", text, re.ASCII) is not None
    echo_ok = model_echo_matches(model, ret_model)
    id_ok = response_id_ok(protocol, ret_id)
    shape_ok = response_shape_ok(protocol, d)
    status = "pass" if (echo_ok and math_ok and id_ok and shape_ok) else ("warn" if math_ok and (echo_ok or id_ok) else "fail")
    notes = []
    if not echo_ok:
        notes.append(f"返回型号「{ret_model or '空'}」与请求「{model}」不一致")
    if not id_ok:
        notes.append(f"响应 ID「{str(ret_id or '空')[:18]}」不符合 {protocol} 原生格式")
    if not shape_ok:
        notes.append("响应结构缺少原生协议字段（type/object/candidates）")
    if not math_ok:
        notes.append(f"确定性算术未返回 700（实际: {text[:40] or '空'}）")
    return {
        "id": "fingerprint", "title": title, "status": status, "ms": r["ms"],
        "summary": "；".join(notes) if notes else f"原生响应结构匹配（model={ret_model}，{r['ms']}ms）",
        "telemetry": telemetry(r, protocol),
        "evidence": {
            "http_status": r["status"], "requested_model": model, "returned_model": ret_model,
            "echo_normalized": [normalize_model_id(model), normalize_model_id(ret_model)], "echo_ok": echo_ok,
            "response_id": ret_id, "id_ok": id_ok, "shape_ok": shape_ok, "math_ok": math_ok,
            "system_fingerprint": d.get("system_fingerprint") or None, "answer": text[:80],
            "usage": _usage(protocol, d) or None, "headers": r["headers"],
        },
    }


def _check_anthropic_thinking(base: str, api_key: str, model: str) -> dict[str, Any]:
    title = "Thinking 签名回传与篡改负向校验"
    url, hdrs = endpoint_url(base, "anthropic", model), _auth_headers("anthropic", api_key)
    think_cfg = {"type": "enabled", "budget_tokens": 1024}
    p1 = {"model": model, "max_tokens": 1400, "temperature": 1, "thinking": think_cfg, "messages": [{"role": "user", "content": THINK_Q1}]}
    r1 = _post_json(url, hdrs, p1, api_key, 30.0)
    if not r1["ok"]:
        return _http_fail("thinking", title, r1, f"开启 Extended Thinking 返回 HTTP {r1['status']}")
    d1 = r1["data"] if isinstance(r1["data"], dict) else {}
    blocks = d1.get("content") if isinstance(d1.get("content"), list) else []
    think = next((b for b in blocks if isinstance(b, dict) and b.get("type") == "thinking"), None)
    sig = think.get("signature") if think and isinstance(think.get("signature"), str) else ""
    txt1 = extract_text("anthropic", d1)
    base_ev = {
        "block_types": [b.get("type") if isinstance(b, dict) else None for b in blocks],
        "signature_len": len(sig),
        "signature_b64": re.fullmatch(r"[A-Za-z0-9+/=]+", sig) is not None,
        "thinking_chars": len(think["thinking"]) if think and isinstance(think.get("thinking"), str) else 0,
        "answer_t1": txt1[:40],
        "usage_t1": d1.get("usage") or None,
    }
    if not think or len(sig) < 40:
        return {
            "id": "thinking", "title": title, "status": "fail", "ms": r1["ms"],
            "summary": (f"存在 thinking 块但缺少有效加密 signature（长度 {len(sig)}），疑似转译或伪造" if think
                        else "未返回原生 thinking 块（疑似被中转剥离或转译为非 Claude 模型）"),
            "telemetry": telemetry(r1, "anthropic"), "evidence": base_ev,
        }
    tampered = [dict(b, signature=tamper_signature(sig)) if b is think else b for b in blocks]

    def mk(content: list[Any]) -> dict[str, Any]:
        return {"model": model, "max_tokens": 1100, "temperature": 1, "thinking": think_cfg,
                "messages": [{"role": "user", "content": THINK_Q1}, {"role": "assistant", "content": content},
                             {"role": "user", "content": THINK_Q2}]}

    with ThreadPoolExecutor(max_workers=2) as pool:
        f2 = pool.submit(_post_json, url, hdrs, mk(blocks), api_key, 30.0)
        ft = pool.submit(_post_json, url, hdrs, mk(tampered), api_key, 30.0)
        r2, rt = f2.result(), ft.result()
    d2 = r2["data"] if (r2["ok"] and isinstance(r2["data"], dict)) else {}
    txt2 = extract_text("anthropic", d2) if r2["ok"] else ""
    roundtrip_ok = bool(r2["ok"] and re.search(r"\b460\b", txt2, re.ASCII))
    tamper = classify_tamper(rt["status"], rt["data"])
    usage = anthropic_usage_check(d1.get("usage"), d2.get("usage") if r2["ok"] else None)
    status = thinking_verdict({"has_block": True, "signature_len": len(sig), "roundtrip_ok": roundtrip_ok, "tamper": tamper, "usage_ok": usage["ok"]})
    notes = [f"真签名二轮回传通过（454 → 460，signature {len(sig)} 字符）" if roundtrip_ok
             else f"二轮真签名回传异常（HTTP {r2['status']}，回复 {txt2[:30] or '空'}）"]
    if tamper == "accepted":
        notes.append("篡改后的 signature 仍被接受：签名未被校验，疑似伪造 thinking 或中转剥离历史块")
    elif tamper == "rejected":
        notes.append(f"篡改签名被上游拒绝（HTTP {rt['status']}），符合原生校验")
    elif tamper == "rejected_other":
        notes.append(f"篡改签名返回 HTTP {rt['status']}（非签名类报错，无法确认）")
    else:
        notes.append(f"篡改签名负向校验未完成（HTTP {rt['status'] or '超时'}）")
    if not usage["ok"]:
        notes.append("、".join(usage["notes"]))
    u2 = d2.get("usage") or {}
    return {
        "id": "thinking", "title": title, "status": status, "ms": r1["ms"] + max(r2["ms"], rt["ms"]),
        "summary": "；".join(notes),
        "telemetry": telemetry(r2 if r2["ok"] else r1, "anthropic"),
        "evidence": {
            **base_ev,
            "signature_preview": f"{sig[:14]}…{sig[-8:]}",
            "turn2_status": r2["status"], "turn2_ok": roundtrip_ok, "answer_t2": txt2[:40],
            "tamper_status": rt["status"], "tamper_result": tamper,
            "tamper_error": None if tamper == "accepted" else _err_snippet(rt)[:160],
            "usage_t2": (d2.get("usage") or None) if r2["ok"] else None,
            "usage_ok": usage["ok"], "usage_notes": usage["notes"],
            "cache_read_t2": u2.get("cache_read_input_tokens") if isinstance(u2, dict) else None,
        },
    }


def check_thinking(base: str, api_key: str, model: str, protocol: str) -> dict[str, Any]:
    if protocol == "anthropic":
        return _check_anthropic_thinking(base, api_key, model)
    title = "推理遥测与确定性校验"
    url = endpoint_url(base, protocol, model)
    if protocol == "openai-response":
        payload: dict[str, Any] = {"model": model, "max_output_tokens": 1024, "input": THINK_Q1}
    elif protocol == "gemini":
        payload = {"contents": [{"role": "user", "parts": [{"text": THINK_Q1}]}],
                   "generationConfig": {"maxOutputTokens": 1024, "thinkingConfig": {"includeThoughts": True}}}
    else:
        payload = {"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": THINK_Q1}]}
    r = _post_json(url, _auth_headers(protocol, api_key), payload, api_key, 30.0)
    dropped = False
    if not r["ok"] and r["status"] == 400 and protocol == "gemini":
        dropped = True
        r = _post_json(url, _auth_headers(protocol, api_key), {**payload, "generationConfig": {"maxOutputTokens": 1024}}, api_key, 30.0)
    if not r["ok"]:
        return _http_fail("thinking", title, r, f"HTTP {r['status']} 调用失败")
    d = r["data"] if isinstance(r["data"], dict) else {}
    txt = extract_text(protocol, d)
    ok_ans = re.search(r"\b454\b", txt, re.ASCII) is not None
    sig = reasoning_signal(protocol, d)
    usage = openai_usage_check(protocol, _usage(protocol, d))
    status = "fail" if not ok_ans else ("pass" if (sig["strong"] and usage["ok"]) else "warn")
    if not ok_ans:
        summary = f"算术推理失败（期望 454，实际: {txt[:50] or '空'}）"
    elif sig["strong"]:
        rt = "—" if sig["reasoning_tokens"] is None else sig["reasoning_tokens"]
        summary = (f"确定性校验通过（454），含原生推理遥测（reasoning_tokens={rt}"
                   + ("，reasoning 输出项" if sig["reasoning_item"] else "")
                   + ("，reasoning_content" if sig["reasoning_content"] else "")
                   + (f"，thoughtSignature {sig['thought_signature_len']} 字符" if sig["thought_signature_len"] else "") + "）")
    elif sig["reasoning_tokens"] == 0:
        summary = "确定性校验通过（454），reasoning_tokens 字段存在但为 0（非推理档或被中转置零）"
    else:
        summary = "确定性校验通过（454），未返回推理遥测（非推理档模型属正常；推理档缺失则疑似转译）"
    if not usage["ok"]:
        summary += "；usage 异常：" + "、".join(usage["notes"])
    if dropped:
        summary += "；上游不接受 thinkingConfig，已去掉后重试"
    return {"id": "thinking", "title": title, "status": status, "ms": r["ms"], "summary": summary,
            "telemetry": telemetry(r, protocol),
            "evidence": {"answer": txt[:60], **sig, "usage_ok": usage["ok"], "usage_notes": usage["notes"],
                         "usage": _usage(protocol, d) or None}}


def _tool_payload(protocol: str, model: str) -> dict[str, Any]:
    params = {"type": "object", "properties": {"region": {"type": "string", "enum": ["CN", "US"]}, "code": {"type": "integer"}},
              "required": ["region", "code"]}
    if protocol == "anthropic":
        return {"model": model, "max_tokens": 200, "tools": [{"name": "lookup_audit_code", "description": TOOL_DESC, "input_schema": params}],
                "tool_choice": {"type": "any"}, "messages": [{"role": "user", "content": TOOL_MSG}]}
    if protocol == "openai-response":
        return {"model": model, "max_output_tokens": 400,
                "tools": [{"type": "function", "name": "lookup_audit_code", "description": TOOL_DESC, "parameters": params}],
                "tool_choice": "required", "input": TOOL_MSG}
    if protocol == "gemini":
        return {"contents": [{"role": "user", "parts": [{"text": TOOL_MSG}]}],
                "tools": [{"functionDeclarations": [{"name": "lookup_audit_code", "description": TOOL_DESC, "parameters": {
                    "type": "OBJECT", "properties": {"region": {"type": "STRING", "enum": ["CN", "US"]}, "code": {"type": "INTEGER"}},
                    "required": ["region", "code"]}}]}],
                "toolConfig": {"functionCallingConfig": {"mode": "ANY"}}, "generationConfig": {"maxOutputTokens": 1024}}
    return {"model": model, "max_tokens": 400,
            "tools": [{"type": "function", "function": {"name": "lookup_audit_code", "description": TOOL_DESC, "parameters": params}}],
            "tool_choice": "required", "messages": [{"role": "user", "content": TOOL_MSG}]}


def check_tool(base: str, api_key: str, model: str, protocol: str) -> dict[str, Any]:
    title = "严格 Tool-Calling 结构校验"
    r = _post_json(endpoint_url(base, protocol, model), _auth_headers(protocol, api_key), _tool_payload(protocol, model), api_key)
    if not r["ok"]:
        return _http_fail("tool", title, r, f"工具调用返回 HTTP {r['status']}")
    d = r["data"] if isinstance(r["data"], dict) else {}
    tc = extract_tool_call(protocol, d)
    status = tool_verdict(tc["name"], tc["args"])
    cands = d.get("candidates") or [{}]
    cand = cands[0] if isinstance(cands[0], dict) else {}
    chs = d.get("choices") or [{}]
    stop = d.get("stop_reason") or (chs[0].get("finish_reason") if isinstance(chs[0], dict) else None) or cand.get("finishReason") or d.get("status") or None
    summary = ('原生结构化工具调用精准通过（region="CN", code=4096 整型）' if status == "pass"
               else "命中工具名但参数类型漂移（如整数被序列化为字符串）" if status == "warn"
               else "未返回结构化 tool_use / tool_calls / functionCall（部分伪装或转译渠道会吞掉工具调用）")
    return {"id": "tool", "title": title, "status": status, "ms": r["ms"], "summary": summary,
            "telemetry": telemetry(r, protocol),
            "evidence": {"function_name": tc["name"], "arguments": tc["args"], "stop_reason": stop}}


def _cache_payload(protocol: str, model: str, q: str) -> dict[str, Any]:
    if protocol == "anthropic":
        return {"model": model, "max_tokens": 24, "temperature": 0,
                "system": [{"type": "text", "text": CACHE_PREFIX, "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": q}]}
    if protocol == "openai-response":
        return {"model": model, "max_output_tokens": 400, "instructions": CACHE_PREFIX, "input": q}
    if protocol == "gemini":
        return {"systemInstruction": {"parts": [{"text": CACHE_PREFIX}]}, "contents": [{"role": "user", "parts": [{"text": q}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 1024}}
    return {"model": model, "max_tokens": 256, "temperature": 0,
            "messages": [{"role": "system", "content": CACHE_PREFIX}, {"role": "user", "content": q}]}


def check_cache(base: str, api_key: str, model: str, protocol: str) -> dict[str, Any]:
    title = "两轮 Prompt Cache 真命中"
    url, hdrs = endpoint_url(base, protocol, model), _auth_headers(protocol, api_key)
    r1 = _post_json(url, hdrs, _cache_payload(protocol, model, "What is the route code for Clause 017? Reply with the code only."), api_key)
    if not r1["ok"]:
        return _http_fail("cache", title, r1, f"首轮缓存预热失败（HTTP {r1['status']}）")
    r2 = _post_json(url, hdrs, _cache_payload(protocol, model, "What is the route code for Clause 042? Reply with the code only."), api_key)
    if not r2["ok"]:
        f = _http_fail("cache", title, r2, f"次轮缓存复查失败（HTTP {r2['status']}）")
        f["ms"] = r1["ms"] + r2["ms"]
        return f
    u1 = _usage(protocol, r1["data"]) or {}
    u2 = _usage(protocol, r2["data"]) or {}
    create = u1.get("cache_creation_input_tokens")
    create = 0 if create is None else create
    try:
        read = float(cache_read_tokens(protocol, u2) or 0)
    except (TypeError, ValueError):
        read = 0.0
    read_v = int(read) if read.is_integer() else read
    hit = read > 0
    return {
        "id": "cache", "title": title, "status": "pass" if hit else "warn", "ms": r1["ms"] + r2["ms"],
        "summary": (f"次轮实测命中缓存读 {read_v} tokens（首轮写入 {create}）" if hit
                    else "次轮未返回缓存命中 token（可能该分组剥离了缓存头、走多账号轮询混池，或上游未达最小缓存阈值）"),
        "telemetry": telemetry(r2, protocol),
        "evidence": {"turn1_usage": u1, "turn2_usage": u2, "cache_creation_t1": create, "cache_read_t2": read_v,
                     "answer_t2": extract_text(protocol, r2["data"])[:40]},
    }


RUNNERS = {"fingerprint": check_fingerprint, "thinking": check_thinking, "tool": check_tool, "cache": check_cache}


def should_fallback(requested: str, protocol: str, is_first: bool, res: dict[str, Any]) -> bool:
    st = (res.get("evidence") or {}).get("http_status")
    return requested == "auto" and protocol == "gemini" and is_first and res.get("status") == "fail" and st in (404, 405)


def parse_checks(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not raw:
        return list(ALL_CHECKS)
    return [c for c in ALL_CHECKS if c in raw]


def run_suite(base_url: str, api_key: str, model: str, protocol: str = "auto", checks: list[str] | None = None) -> dict[str, Any]:
    base = normalize_base_url(base_url)
    if not base:
        raise ValueError("base_url 必须是公网 HTTPS 域名（默认 443 端口；禁止内网、裸 IP 与自定义端口）")
    if not is_valid_model(model):
        raise ValueError("请填写有效的目标模型名（字母、数字与 . _ - : / @）")
    requested = protocol or "auto"
    if requested not in PROTOCOLS:
        raise ValueError(f"protocol 仅支持 {' / '.join(PROTOCOLS)}")
    want = parse_checks(checks)
    if not want:
        raise ValueError(f"checks 仅支持 {' / '.join(ALL_CHECKS)}")
    proto = detect_protocol(model, requested)
    fallback = None
    results: list[dict[str, Any]] = []
    for cid in want:
        res = RUNNERS[cid](base, api_key, model, proto)
        if should_fallback(requested, proto, not results, res):
            fallback = {"from": "gemini", "to": "openai",
                        "reason": f"Gemini 原生端点返回 HTTP {res['evidence']['http_status']}，已回退 OpenAI 兼容协议"}
            proto = "openai"
            res = RUNNERS[cid](base, api_key, model, proto)
        results.append(res)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base, "model": model, "requested_protocol": requested, "protocol": proto,
        "protocol_fallback": fallback, **summarize(results), "results": results,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify LLM API relay authenticity locally (key never leaves your machine).")
    ap.add_argument("--base-url", required=True, help="Relay base URL, e.g. https://api.example.com")
    ap.add_argument("--api-key", required=True, help="API key for testing")
    ap.add_argument("--model", required=True, help="Model ID, e.g. claude-sonnet-5-5")
    ap.add_argument("--protocol", default="auto", choices=PROTOCOLS)
    ap.add_argument("--checks", default=",".join(ALL_CHECKS), help="Comma-separated checks")
    args = ap.parse_args()
    try:
        out = run_suite(args.base_url, args.api_key, args.model, args.protocol,
                        [c.strip() for c in args.checks.split(",") if c.strip()])
    except ValueError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(2)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if out["level"] != "fail" else 1)


if __name__ == "__main__":
    main()
