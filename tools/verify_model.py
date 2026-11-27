"""Standalone, zero-dependency local LLM authenticity & intelligence verifier (CLI + importable library).

Mirrors functions/api/verify.js 1:1 (same host guard, seeded challenge generators, solvers, graders, payloads,
titles and dual scoring) so your API key NEVER leaves your machine.

Anti-gaming: every run derives fresh challenges from a seeded Mulberry32 PRNG (random seed per run, or an explicit
--seed for reproducible / differential runs), so a relay cannot regex-match the open-source probe prompts.

Authenticity (auth_score)
1. fingerprint: response shape, normalized model echo, ID format, dynamic micro-math + timing telemetry.
2. thinking:    Anthropic: thinking signature + Turn-2 genuine round-trip + tampered-signature negative check
                + usage consistency. OpenAI / DeepSeek / Gemini: native reasoning telemetry + usage sanity.
3. tool:        Strict typed Tool-Calling with a dynamic tool name, enum values, argument keys and integer code.
4. cache:       2-turn ~1,350-token dynamic policy corpus (session nonce + random target clauses) cache read hit.
Intelligence / fidelity (iq_score) - programmatically graded, zero LLM-as-a-judge
5. iq_trace:       6-step integer state-machine execution trace (exact JSON final state + checksum).
6. iq_logic:       4 x 3 logic deduction puzzle, uniqueness proven by an exact internal solver.
7. iq_constraints: 4 simultaneous verifiable instruction constraints (IFEval style).
8. context_hop:    RULER-style 3-hop pointer chain at ~15% / ~52% / ~87% depth + prompt_tokens audit.

Tiers: l1_auth (checks 1-4), l2_iq (1-7, default), l3_deep (1-8). Non-compensatory: an accepted tampered
signature, auth_score < 55 or iq_score < 55 forces level "fail".

Usage:
  python tools/verify_model.py --base-url https://api.example.com --api-key sk-... --model claude-sonnet-5-5
  python tools/verify_model.py --tier l3_deep --context-tokens 32000 --base-url ... --api-key ... --model ...
  python tools/verify_model.py --tier l2_iq --seed 12345 --base-url https://relay.example --api-key sk-... \
      --model claude-sonnet-5-5 --reference-url https://api.anthropic.com --reference-key sk-ant-...
"""

from __future__ import annotations

import argparse
import json
import math
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

PROTOCOLS = ["auto", "anthropic", "openai-response", "openai", "gemini"]
AUTH_CHECKS = ["fingerprint", "thinking", "tool", "cache"]
IQ_CHECKS = ["iq_trace", "iq_logic", "iq_constraints", "context_hop"]
ALL_CHECKS = AUTH_CHECKS + IQ_CHECKS
TIERS = {
    "l1_auth": list(AUTH_CHECKS),
    "l2_iq": AUTH_CHECKS + ["iq_trace", "iq_logic", "iq_constraints"],
    "l3_deep": list(ALL_CHECKS),
}
DEFAULT_TIER = "l2_iq"
EDGE_CONTEXT_TOKENS = {"def": 12000, "min": 4000, "max": 28000}
CLI_CONTEXT_TOKENS = {"def": 16000, "min": 4000, "max": 64000}
FAIL_FAST_HTTP = [0, 401, 402, 403, 404]
CONCURRENCY = 3
MAX_UPSTREAM_BYTES = 65536
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
BLOCKED_SUFFIXES = (
    ".local", ".localhost", ".internal", ".intranet", ".lan", ".home", ".corp", ".localdomain", ".home.arpa",
    ".nip.io", ".sslip.io", ".xip.io", ".localtest.me", ".lvh.me",
)
BLOCKED_HOSTS = ("localhost", "metadata.google.internal", "nip.io", "sslip.io", "localtest.me", "lvh.me")
LEGACY_TOOL = {"name": "lookup_audit_code", "region_key": "region", "code_key": "code", "region": "CN", "code": 4096,
               "enum": ["CN", "US"]}
_NUMLABEL = re.compile(r"(?:\d+|0x[0-9a-f]*)")


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


def check_group(cid: Any) -> str:
    return "iq" if cid in IQ_CHECKS else "auth"


def _status_points(s: Any) -> int:
    return 100 if s == "pass" else (55 if s == "warn" else 0)


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def _group_score(xs: list[dict[str, Any]]) -> int | None:
    return _js_round(sum(_status_points(x.get("status")) for x in xs) / len(xs)) if xs else None


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    rs = results if isinstance(results, list) else []
    total = len(rs) or 1
    n_pass = sum(1 for x in rs if x.get("status") == "pass")
    n_fail = sum(1 for x in rs if x.get("status") == "fail")
    score = _js_round(sum(_status_points(x.get("status")) for x in rs) / total)
    auth_score = _group_score([x for x in rs if check_group(x.get("id")) == "auth"])
    iq_score = _group_score([x for x in rs if check_group(x.get("id")) == "iq"])
    tamper_accepted = any(isinstance(x.get("evidence"), dict) and x["evidence"].get("tamper_result") == "accepted" for x in rs)
    veto: list[str] = []
    if tamper_accepted:
        veto.append("tampered_signature_accepted")
    if auth_score is not None and auth_score < 55:
        veto.append("auth_score_below_55")
    if iq_score is not None and iq_score < 55:
        veto.append("iq_score_below_55")
    level = "pass"
    verdict = "真伪与智商抽检均符合原生旗舰模型特征" if iq_score is not None else "高度符合原生协议与真机特征"
    if tamper_accepted:
        level, verdict = "fail", "篡改签名仍被接受：签名未被校验，疑似伪造 thinking 或套壳链路（非补偿否决，直接判定异常）"
    elif auth_score is not None and auth_score < 55:
        level, verdict = "fail", "真伪特征分低于 55：疑似转译、套壳或伪造协议（非补偿否决，直接判定异常）"
    elif iq_score is not None and iq_score < 55:
        level, verdict = "fail", "智商保真分低于 55：疑似静默降级到弱模型、剥离推理档或截断上下文（非补偿否决，直接判定异常）"
    elif n_fail > 0 or score < 55:
        level, verdict = "fail", "存在明显异常（疑似转译、接口不兼容、降级或套壳，建议核对下方原始证据）"
    elif n_pass < len(rs):
        level, verdict = "warn", "基础调用正常，但部分高级特征缺失（如缓存未命中或缺少原生遥测字段，建议小额实测）"
    return {"score": score, "auth_score": auth_score, "iq_score": iq_score, "level": level, "verdict": verdict,
            "veto": veto, "tamper_accepted": tamper_accepted, "passed": n_pass, "total": len(rs)}


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


def _js_str(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e21:
        return str(int(v))
    return str(v)


def tool_verdict(name: Any, args: Any, ch: dict[str, Any] | None = None) -> str:
    ch = ch or LEGACY_TOOL
    if name != ch["name"] or not isinstance(args, dict):
        return "fail"
    rv = args.get(ch["region_key"])
    cv = args.get(ch["code_key"])
    if isinstance(rv, str) and rv == ch["region"] and _is_num(cv) and cv == ch["code"]:
        return "pass"
    if (isinstance(cv, str) or _is_num(cv)) and _js_str(rv).upper() == ch["region"] and _js_str(cv).strip() == str(ch["code"]):
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


# ------------------------------------------------------------------ seeded dynamic challenges (mirrored in verify.js)

_M32 = 0xFFFFFFFF
TOKEN_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _imul(a: int, b: int) -> int:
    return (a * b) & _M32


def fnv1a(s: Any) -> int:
    raw = "" if s is None else str(s)
    data = raw.encode("utf-16-le", "surrogatepass")
    h = 2166136261
    for i in range(0, len(data), 2):
        h ^= data[i] | (data[i + 1] << 8)
        h = _imul(h, 16777619)
    return h


class Rng:
    """Mulberry32, bit-identical to the JS implementation."""

    def __init__(self, seed: int) -> None:
        self.a = int(seed) & _M32

    def u32(self) -> int:
        self.a = (self.a + 0x6D2B79F5) & _M32
        t = self.a
        t = _imul(t ^ (t >> 15), t | 1)
        t ^= (t + _imul(t ^ (t >> 7), t | 61)) & _M32
        return (t ^ (t >> 14)) & _M32

    def int(self, n: int) -> int:
        return (self.u32() * n) >> 32

    def range(self, lo: int, hi: int) -> int:
        return lo + self.int(hi - lo + 1)

    def pick(self, arr: list[Any]) -> Any:
        return arr[self.int(len(arr))]

    def shuffle(self, arr: list[Any]) -> list[Any]:
        a = list(arr)
        for i in range(len(a) - 1, 0, -1):
            j = self.int(i + 1)
            a[i], a[j] = a[j], a[i]
        return a

    def token(self, n: int) -> str:
        return "".join(TOKEN_ALPHA[self.int(len(TOKEN_ALPHA))] for _ in range(n))


def rng_sequence(seed: int, n: int) -> list[int]:
    r = Rng(seed)
    return [r.u32() for _ in range(n)]


def derive_seed(seed: int, cid: str) -> int:
    return (fnv1a(cid) ^ (int(seed) & _M32)) & _M32


def normalize_seed(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not v.is_integer():
        return None
    return int(v) if 0 <= v <= 4294967295 else None


def random_seed() -> int:
    return secrets.randbits(32)


def clamp_context_tokens(v: Any, lo: int = EDGE_CONTEXT_TOKENS["min"], hi: int = EDGE_CONTEXT_TOKENS["max"],
                         default: int = EDGE_CONTEXT_TOKENS["def"]) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    return min(hi, max(lo, math.floor(v)))


def has_int(text: Any, n: int) -> bool:
    return re.search(rf"\b{int(n)}\b", "" if text is None else str(text), re.ASCII) is not None


def _unique_tokens(r: Rng, n: int, length: int, prefix: str) -> list[str]:
    out: list[str] = []
    while len(out) < n:
        t = prefix + r.token(length)
        if t not in out:
            out.append(t)
    return out


def _ceil_div4(n: int) -> int:
    return -(-n // 4)


def gen_fingerprint(seed: int) -> dict[str, Any]:
    r = Rng(seed)
    a, b, c = r.range(31, 97), r.range(31, 97), r.range(101, 899)
    nonce = r.token(6)
    tpl = r.int(3)
    if tpl == 0:
        prompt = f"Ticket {nonce}. Compute ({a} * {b}) - {c}. Reply with the single integer only, no explanation."
    elif tpl == 1:
        prompt = f"[{nonce}] What is {a} times {b}, minus {c}? Answer with one integer only, no explanation."
    else:
        prompt = f"Evaluate {a}*{b}-{c} for request {nonce}. Output only the resulting integer."
    return {"kind": "fingerprint", "nonce": nonce, "prompt": prompt, "answer": a * b - c}


def gen_thinking(seed: int) -> dict[str, Any]:
    r = Rng(seed)
    x, y, z, d = r.range(13, 49), r.range(13, 49), r.range(11, 99), r.range(3, 19)
    nonce = r.token(6)
    answer1 = x * y + z
    return {
        "kind": "thinking", "nonce": nonce,
        "q1": f"Compute ({x} * {y}) + {z}. Reply with the integer only. [ref {nonce}]",
        "q2": f"Add {d} to your previous integer result. Reply with the integer only.",
        "answer1": answer1, "answer2": answer1 + d,
    }


TOOL_VERBS = ["lookup", "fetch", "resolve", "query", "verify", "inspect"]
TOOL_NOUNS = ["audit", "ledger", "route", "invoice", "shipment", "ticket", "quota"]
TOOL_REGION_KEYS = ["region", "zone", "market", "area"]
TOOL_CODE_KEYS = ["code", "serial", "ref_id", "ticket_no"]
TOOL_REGIONS = ["CN", "US", "DE", "JP", "SG", "BR", "FR", "KR", "IN", "GB", "CA", "AU"]


def gen_tool(seed: int) -> dict[str, Any]:
    r = Rng(seed)
    verb = r.pick(TOOL_VERBS)
    noun = r.pick(TOOL_NOUNS)
    suffix = r.token(4).lower()
    region_key = r.pick(TOOL_REGION_KEYS)
    code_key = r.pick(TOOL_CODE_KEYS)
    enum_vals = r.shuffle(TOOL_REGIONS)[:3]
    region = enum_vals[r.int(3)]
    code = r.range(1000, 99999)
    name = f"{verb}_{noun}_{suffix}"
    return {
        "kind": "tool", "name": name, "region_key": region_key, "code_key": code_key, "enum": enum_vals,
        "region": region, "code": code,
        "description": f"{verb[0].upper()}{verb[1:]} a {noun} verification record by {region_key} and numeric {code_key}.",
        "message": f'Call the tool {name} with {region_key}="{region}" and {code_key}={code}. Do not answer in plain text.',
    }


CACHE_DUTIES = [
    "UTF-8 JSON framing", "monotonic request IDs", "latency accounting in milliseconds", "non-negative token usage fields",
    "deterministic error codes", "idempotent retry headers", "per-tenant rate ceilings", "signed audit trails",
]


def gen_cache(seed: int) -> dict[str, Any]:
    r = Rng(seed)
    session = r.token(8)
    lines = [
        f"You are a strict compliance auditor reviewing API relay technical specifications (audit session {session}).",
        "Memorize the following numbered policy clauses and answer ONLY with the requested clause route code.",
    ]
    codes: list[str] = []
    for i in range(1, 53):
        code = f"R-{r.token(5)}"
        duties = r.shuffle(CACHE_DUTIES)
        codes.append(code)
        lines.append(f"Clause {i:03d}: Relay gateway route {code} enforces {duties[0]} and {duties[1]}.")
    t1 = r.range(1, 52)
    t2 = r.range(1, 51)
    if t2 >= t1:
        t2 += 1
    prefix = "\n".join(lines)

    def q(t: int) -> str:
        return f"What is the route code for Clause {t:03d}? Reply with the code only."

    return {"kind": "cache", "session": session, "prefix": prefix, "q1": q(t1), "q2": q(t2),
            "answer1": codes[t1 - 1], "answer2": codes[t2 - 1], "est_tokens": _ceil_div4(len(prefix))}


TRACE_VARS = ["a", "b", "c"]
TRACE_KINDS = ["muladd", "xor", "branch", "addmod", "mulmod", "xorconst"]


def gen_iq_trace(seed: int) -> dict[str, Any]:
    r = Rng(seed)
    nonce = r.token(6)
    st = {"a": r.range(1, 99), "b": r.range(1, 99), "c": r.range(1, 99)}
    code = [f"a = {st['a']}", f"b = {st['b']}", f"c = {st['c']}"]
    kinds = r.shuffle(TRACE_KINDS)
    for idx, kind in enumerate(kinds):
        x, y, z = r.shuffle(TRACE_VARS)
        m = r.range(97, 997)
        k = r.range(2, 9)
        code.append(f"# step {idx + 1}")
        if kind == "muladd":
            code.append(f"{x} = ({x} * {k} + {y}) % {m}")
            st[x] = (st[x] * k + st[y]) % m
        elif kind == "xor":
            code.append(f"{x} = {x} ^ {y}")
            st[x] = st[x] ^ st[y]
        elif kind == "branch":
            k2 = r.range(11, 99)
            code += [f"if {x} > {y}:", f"    {z} = ({z} + {k2}) % {m}", "else:", f"    {z} = ({z} * {k} + 1) % {m}"]
            st[z] = (st[z] + k2) % m if st[x] > st[y] else (st[z] * k + 1) % m
        elif kind == "addmod":
            code.append(f"{x} = ({x} + {y} + {z} + {k}) % {m}")
            st[x] = (st[x] + st[y] + st[z] + k) % m
        elif kind == "mulmod":
            code.append(f"{x} = ({x} * {y} + {k}) % {m}")
            st[x] = (st[x] * st[y] + k) % m
        else:
            k3 = r.range(1, 255)
            code.append(f"{x} = ({x} ^ {k3}) % {m}")
            st[x] = (st[x] ^ k3) % m
    code.append("checksum = a ^ b ^ c")
    program = "\n".join(code)
    answer = {"a": st["a"], "b": st["b"], "c": st["c"], "checksum": st["a"] ^ st["b"] ^ st["c"]}
    prompt = (
        f"Trace {nonce}. Execute this Python program exactly (integer arithmetic, % is modulo, ^ is bitwise XOR) "
        f"and report the final values.\n\n{program}\n\nYou may think briefly, but the last line of your reply must be "
        'exactly one JSON object: {"a": <int>, "b": <int>, "c": <int>, "checksum": <int>}'
    )
    return {"kind": "iq_trace", "nonce": nonce, "steps": kinds, "program": program, "prompt": prompt, "answer": answer}


LOGIC_NAMES = ["Alice", "Bruno", "Chen", "Dara", "Emil", "Farah", "Goro", "Hana",
               "Ivan", "Jade", "Kofi", "Lena", "Mateo", "Nora", "Omar", "Priya"]
LOGIC_ATTRS = [
    ["color", ["red", "blue", "green", "yellow", "purple", "orange", "white", "black"]],
    ["pet", ["cat", "dog", "parrot", "rabbit", "turtle", "hamster", "goldfish", "lizard"]],
    ["drink", ["tea", "coffee", "milk", "juice", "water", "cocoa", "soda", "kefir"]],
    ["city", ["Oslo", "Lima", "Pune", "Kyoto", "Accra", "Quito", "Turin", "Hanoi"]],
    ["sport", ["tennis", "rowing", "chess", "boxing", "judo", "golf", "fencing", "cycling"]],
]
_PERMS4 = [[a, b, c, d] for a in range(4) for b in range(4) for c in range(4) for d in range(4) if len({a, b, c, d}) == 4]


def perms4() -> list[list[int]]:
    return _PERMS4


def clue_holds(cl: dict[str, Any], p3: list[list[int]]) -> bool:
    t = cl["t"]
    if t == "pos":
        return p3[cl["a"]][cl["p"]] == cl["v"]
    if t == "neg":
        return p3[cl["a"]][cl["p"]] != cl["v"]
    q = p3[cl["a"]].index(cl["v"])
    return p3[cl["b"]][q] == cl["w"] if t == "link" else p3[cl["b"]][q] != cl["w"]


def _clue_level(cl: dict[str, Any]) -> int:
    return cl["a"] if cl["t"] in ("pos", "neg") else max(cl["a"], cl["b"])


def solve_logic(clues: list[dict[str, Any]]) -> dict[str, Any]:
    perms = _PERMS4
    by_level: list[list[dict[str, Any]]] = [[], [], []]
    for cl in clues:
        by_level[_clue_level(cl)].append(cl)
    count = 0
    solution = None
    p3: list[Any] = [None, None, None]
    for i, pi in enumerate(perms):
        p3[0] = pi
        if not all(clue_holds(cl, p3) for cl in by_level[0]):
            continue
        for j, pj in enumerate(perms):
            p3[1] = pj
            if not all(clue_holds(cl, p3) for cl in by_level[1]):
                continue
            for k, pk in enumerate(perms):
                p3[2] = pk
                if all(clue_holds(cl, p3) for cl in by_level[2]):
                    count += 1
                    if solution is None:
                        solution = [i, j, k]
    return {"count": count, "solution": solution}


def _clue_text(cl: dict[str, Any], names: list[str], attrs: list[str], values: list[list[str]]) -> str:
    t = cl["t"]
    if t == "pos":
        return f"{names[cl['p']]}'s {attrs[cl['a']]} is {values[cl['a']][cl['v']]}."
    if t == "neg":
        return f"{names[cl['p']]}'s {attrs[cl['a']]} is not {values[cl['a']][cl['v']]}."
    head = f"The person whose {attrs[cl['a']]} is {values[cl['a']][cl['v']]}"
    if t == "link":
        return f"{head} also has {attrs[cl['b']]} {values[cl['b']][cl['w']]}."
    return f"{head} does not have {attrs[cl['b']]} {values[cl['b']][cl['w']]}."


def gen_iq_logic(seed: int) -> dict[str, Any]:
    r = Rng(seed)
    nonce = r.token(6)
    names = r.shuffle(LOGIC_NAMES)[:4]
    cat_idx = r.shuffle([0, 1, 2, 3, 4])[:3]
    attrs = [LOGIC_ATTRS[i][0] for i in cat_idx]
    values = [r.shuffle(LOGIC_ATTRS[i][1])[:4] for i in cat_idx]
    perms = _PERMS4
    sol = [r.int(24), r.int(24), r.int(24)]
    hid = [perms[i] for i in sol]
    cands = [(i, j, k) for i in range(24) for j in range(24) for k in range(24)]

    def holds(cl: dict[str, Any], c: tuple[int, int, int]) -> bool:
        return clue_holds(cl, [perms[c[0]], perms[c[1]], perms[c[2]]])

    clues: list[dict[str, Any]] = []
    guard = 0
    while len(cands) > 1 and guard < 400:
        guard += 1
        roll = r.int(10)
        if roll < 1:
            p, a = r.int(4), r.int(3)
            cl = {"t": "pos", "p": p, "a": a, "v": hid[a][p]}
        elif roll < 4:
            p, a = r.int(4), r.int(3)
            v = r.int(3)
            if v >= hid[a][p]:
                v += 1
            cl = {"t": "neg", "p": p, "a": a, "v": v}
        else:
            a = r.int(3)
            b = r.int(2)
            if b >= a:
                b += 1
            v = r.int(4)
            q = hid[a].index(v)
            if roll < 7:
                cl = {"t": "link", "a": a, "v": v, "b": b, "w": hid[b][q]}
            else:
                w = r.int(3)
                if w >= hid[b][q]:
                    w += 1
                cl = {"t": "notlink", "a": a, "v": v, "b": b, "w": w}
        nxt = [c for c in cands if holds(cl, c)]
        if len(nxt) < len(cands):
            clues.append(cl)
            cands = nxt
    while len(cands) > 1:
        added = None
        for a in range(3):
            for p in range(4):
                if added is None and any(perms[c[a]][p] != hid[a][p] for c in cands):
                    added = {"t": "pos", "p": p, "a": a, "v": hid[a][p]}
        clues.append(added)
        cands = [c for c in cands if holds(added, c)]
    ordered = [clues[i] for i in r.shuffle(list(range(len(clues))))]
    solution = {names[p]: {attrs[a]: values[a][hid[a][p]] for a in range(3)} for p in range(4)}
    lines = [f"Logic puzzle {nonce}. Four people ({', '.join(names)}) each have exactly one {attrs[0]}, one {attrs[1]} "
             f"and one {attrs[2]}. No two people share the same value of any attribute."]
    lines += [f"Possible {attrs[a]} values: {', '.join(values[a])}." for a in range(3)]
    lines.append("Clues:")
    lines += [f"{i + 1}. {_clue_text(cl, names, attrs, values)}" for i, cl in enumerate(ordered)]
    shape = f'{{"{names[0]}": {{"{attrs[0]}": "...", "{attrs[1]}": "...", "{attrs[2]}": "..."}}, ...}}'
    lines.append("Exactly one assignment satisfies every clue. You may reason briefly, but the last line of your reply must be "
                 f"exactly one JSON object mapping all four people to their attributes, shaped like {shape}")
    return {"kind": "iq_logic", "nonce": nonce, "names": names, "attrs": attrs, "values": values, "clues": ordered,
            "solution": solution, "hidden": sol, "prompt": "\n".join(lines)}


IFE_TOPICS = ["animals", "fruits", "tools", "musical instruments", "vegetables", "sports", "pieces of furniture", "birds"]
IFE_KEY_A = ["items", "words", "entries", "picks", "terms"]
IFE_KEY_B = ["count", "total", "size", "n"]
IFE_LETTERS = ["a", "e", "i", "o", "r", "s", "t", "n"]


def gen_iq_constraints(seed: int) -> dict[str, Any]:
    r = Rng(seed)
    nonce = r.token(6)
    tag = r.token(3).lower()
    list_key = f"{r.pick(IFE_KEY_A)}_{tag}"
    count_key = f"{r.pick(IFE_KEY_B)}_{tag}"
    n = r.range(4, 6)
    letter = r.pick(IFE_LETTERS)
    order = "ascending" if r.int(2) == 0 else "descending"
    topic = r.pick(IFE_TOPICS)
    cmp = "longer" if order == "ascending" else "shorter"
    prompt = (
        f"Task {nonce}. Return ONLY a JSON object that satisfies ALL of these rules:\n"
        f'1. The object has exactly two keys: "{list_key}" and "{count_key}".\n'
        f'2. "{list_key}" is an array of exactly {n} single lowercase English words (letters a-z only) naming {topic}, '
        f'and "{count_key}" is the integer {n}.\n'
        f'3. No word may contain the letter "{letter}".\n'
        f"4. The words must be in strictly {order} order of length (every word {cmp} than the previous one).\n"
        "Do not add any other text."
    )
    return {"kind": "iq_constraints", "nonce": nonce, "list_key": list_key, "count_key": count_key, "n": n,
            "letter": letter, "order": order, "topic": topic, "prompt": prompt}


LOG_SVCS = ["billing", "router", "auth", "cache", "quota", "ingest", "search", "notify"]
LOG_LVLS = ["INFO", "INFO", "INFO", "DEBUG", "WARN"]
LOG_MSGS = [
    "upstream latency {n}ms within budget", "cache warm completed in {n}ms", "retry budget {n} remaining",
    "rotated {n} signing credentials", "flushed {n} metric points", "queue depth settled at {n}", "gc pause {n}ms",
    "accepted {n} requests from edge pool", "evicted {n} stale sessions", "replica lag {n}ms",
]
LOG_SECTIONS = ["gateway", "billing", "scheduler", "storage", "edge", "observability", "identity", "network"]


def gen_context_hop(seed: int, context_tokens: Any = EDGE_CONTEXT_TOKENS["def"]) -> dict[str, Any]:
    r = Rng(seed)
    nonce = r.token(6)
    budget = max(1000, math.floor(context_tokens)) * 4
    filler: list[str] = []
    chars = 0
    sec = r.range(0, 21600)
    part = 0
    while chars < budget - 700:
        if len(filler) % 120 == 0:
            part += 1
            line = f"=== Section {part}: {r.pick(LOG_SECTIONS)} runbook export ==="
        else:
            sec += r.range(1, 9)
            ts = f"2026-11-27T{(sec // 3600) % 24:02d}:{(sec // 60) % 60:02d}:{sec % 60:02d}Z"
            svc = r.pick(LOG_SVCS)
            inst = r.range(1, 9)
            lvl = r.pick(LOG_LVLS)
            req = r.token(6)
            msg = r.pick(LOG_MSGS)
            num = r.range(1, 999)
            line = f"{ts} {svc}-{inst} {lvl} req={req} {msg.replace('{n}', str(num), 1)}"
        filler.append(line)
        chars += len(line) + 1
    total = len(filler)
    keys = _unique_tokens(r, 12, 6, "PTR-")
    finals = _unique_tokens(r, 4, 8, "FINAL_CODE_")
    answer = finals[0]
    needles: list[tuple[int, int, str]] = []

    def add(pos: int, text: str) -> None:
        needles.append((pos, len(needles), text))

    real_lines = [
        f"[registry] pointer {keys[0]} resolves to {keys[1]}",
        f"[registry] pointer {keys[1]} resolves to {keys[2]}",
        f"[registry] pointer {keys[2]} resolves to {answer}",
    ]
    add(total * 15 // 100, real_lines[0])
    add(total * 52 // 100, real_lines[1])
    add(total * 87 // 100, real_lines[2])
    for d in range(3):
        chain = [keys[3 + d * 3], keys[4 + d * 3], keys[5 + d * 3], finals[1 + d]]
        for h in range(3):
            add(r.range(1, total - 1), f"[registry] pointer {chain[h]} resolves to {chain[h + 1]}")
    needles.sort(key=lambda x: (x[0], x[1]))
    body: list[str] = []
    ni = 0
    for i in range(total):
        while ni < len(needles) and needles[ni][0] == i:
            body.append(needles[ni][2])
            ni += 1
        body.append(filler[i])
    body += [x[2] for x in needles[ni:]]
    header = (f'Operations log export {nonce} ({total} records). Records of the form "pointer X resolves to Y" '
              "mean that X resolves to Y.")
    question = (f"Question: start from pointer {keys[0]} and follow the pointer records until you reach a value that "
                "begins with FINAL_CODE_. Reply with that final value only.")
    prompt = "\n".join([header, "", *body, "", question])
    return {
        "kind": "context_hop", "nonce": nonce, "start": keys[0], "chain": [keys[0], keys[1], keys[2], answer],
        "answer": answer, "decoys": finals[1:], "records": total, "context_tokens": math.floor(context_tokens),
        "depth_pct": [prompt.find(t) * 100 // len(prompt) for t in real_lines],
        "est_tokens": _ceil_div4(len(prompt)), "prompt": prompt,
    }


def build_challenge(cid: str, seed: int, context_tokens: Any = EDGE_CONTEXT_TOKENS["def"]) -> dict[str, Any] | None:
    s = derive_seed(seed, cid)
    gens = {"fingerprint": gen_fingerprint, "thinking": gen_thinking, "tool": gen_tool, "cache": gen_cache,
            "iq_trace": gen_iq_trace, "iq_logic": gen_iq_logic, "iq_constraints": gen_iq_constraints}
    if cid == "context_hop":
        return gen_context_hop(s, context_tokens)
    return gens[cid](s) if cid in gens else None


# ------------------------------------------------------------------ deterministic graders (mirrored in verify.js)

def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def extract_json_object(text: Any) -> dict[str, Any] | None:
    full = "" if text is None else str(text)
    s = full[-8000:] if len(full) > 8000 else full
    last = None
    tries = 0
    i = 0
    n = len(s)
    while i < n and tries < 64:
        if s[i] != "{":
            i += 1
            continue
        tries += 1
        depth = 0
        in_str = esc = False
        end = -1
        for j in range(i, n):
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end < 0:
            i += 1
            continue
        try:
            v = json.loads(s[i:end + 1], parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            v = None
        if isinstance(v, dict):
            last = v
            i = end + 1
        else:
            i += 1
    return last


def _is_fnum(v: Any) -> bool:
    return _is_num(v) and math.isfinite(v)


def _own(obj: Any, k: str) -> bool:
    return isinstance(obj, dict) and k in obj


def _find_key(obj: Any, key: str) -> Any:
    if not isinstance(obj, dict):
        return None
    want = str(key).lower()
    for k in obj:
        if k.strip().lower() == want:
            return obj[k]
    return None


def grade_iq_trace(ch: dict[str, Any], text: Any) -> dict[str, Any]:
    obj = extract_json_object(text)
    got: dict[str, Any] = {}
    correct = 0
    for k in ("a", "b", "c", "checksum"):
        v = obj[k] if _own(obj, k) else None
        got[k] = v
        if _is_fnum(v) and v == ch["answer"][k]:
            correct += 1
    status = "pass" if correct == 4 else ("warn" if correct == 3 else "fail")
    return {"status": status, "correct": correct, "total": 4, "parsed": obj is not None, "expected": ch["answer"], "got": got}


def grade_iq_logic(ch: dict[str, Any], text: Any) -> dict[str, Any]:
    obj = extract_json_object(text)
    correct = 0
    wrong: list[str] = []
    for name in ch["names"]:
        row = _find_key(obj, name)
        for attr in ch["attrs"]:
            got = _find_key(row, attr)
            exp = ch["solution"][name][attr]
            if isinstance(got, str) and got.strip().lower() == exp.lower():
                correct += 1
            else:
                wrong.append(f"{name}.{attr}")
    total = len(ch["names"]) * len(ch["attrs"])
    status = "pass" if correct == total else ("warn" if correct * 4 >= total * 3 else "fail")
    return {"status": status, "correct": correct, "total": total, "parsed": obj is not None, "wrong": wrong[:6],
            "clues": len(ch["clues"])}


_WORD = re.compile(r"[a-z]+")


def grade_iq_constraints(ch: dict[str, Any], text: Any) -> dict[str, Any]:
    obj = extract_json_object(text)
    lk, ck = ch["list_key"], ch["count_key"]
    keys_ok = obj is not None and len(obj) == 2 and _own(obj, lk) and _own(obj, ck)
    lst = obj[lk] if _own(obj, lk) and isinstance(obj[lk], list) else None
    words = lst if lst is not None and all(isinstance(w, str) and _WORD.fullmatch(w) for w in lst) else None
    count = obj[ck] if _own(obj, ck) else None
    length_ok = words is not None and len(words) == ch["n"] and _is_fnum(count) and count == ch["n"]
    letter_ok = words is not None and len(words) > 0 and all(ch["letter"] not in w for w in words)
    order_ok = words is not None and len(words) >= 2
    if order_ok:
        for i in range(1, len(words)):
            d = len(words[i]) - len(words[i - 1])
            if (d <= 0) if ch["order"] == "ascending" else (d >= 0):
                order_ok = False
    rules = [("keys", keys_ok), ("exact_length", length_ok), ("forbidden_letter", letter_ok), ("length_order", order_ok)]
    passed = sum(1 for _, ok in rules if ok)
    status = "pass" if passed == 4 else ("warn" if passed == 3 else "fail")
    return {"status": status, "passed": passed, "total": 4, "parsed": obj is not None,
            "broken": [k for k, ok in rules if not ok], "words": lst}


def grade_context_hop(ch: dict[str, Any], text: Any, prompt_tokens: Any) -> dict[str, Any]:
    t = "" if text is None else str(text)
    found = ch["answer"] in t
    decoy = next((d for d in ch["decoys"] if d in t), None)
    pt = int(prompt_tokens) if _is_count(prompt_tokens) and prompt_tokens > 0 else None
    flag = None
    if pt is not None:
        if pt * 100 < ch["est_tokens"] * 55:
            flag = "truncation"
        elif pt * 100 > ch["est_tokens"] * 220:
            flag = "inflation"
    status = ("warn" if flag else "pass") if (found and not decoy) else "fail"
    return {
        "status": status, "found": found, "decoy_hit": decoy, "prompt_tokens": pt, "est_tokens": ch["est_tokens"],
        "token_ratio_pct": None if pt is None else math.floor(pt * 100 / ch["est_tokens"]), "token_flag": flag,
        "records": ch["records"], "depth_pct": ch["depth_pct"],
    }


def iq_summary(cid: str, g: dict[str, Any]) -> str:
    st = g["status"]
    if cid == "iq_trace":
        if st == "pass":
            return f"6 步状态机追踪全部正确（checksum={g['expected']['checksum']}）"
        if not g["parsed"]:
            return "未返回可解析的 JSON 终态（疑似弱模型、输出被截断或推理被剥离）"
        if st == "warn":
            return f"状态机追踪 {g['correct']}/4 字段正确（疑似推理档被削减或量化）"
        return f"状态机追踪仅 {g['correct']}/4 字段正确（期望 checksum={g['expected']['checksum']}，疑似静默降级为弱模型或剥离推理）"
    if cid == "iq_logic":
        if st == "pass":
            return f"逻辑推理 {g['total']}/{g['total']} 全部正确（{g['clues']} 条约束，唯一解已由内置求解器证明）"
        if not g["parsed"]:
            return "未返回可解析的 JSON 解答（疑似弱模型或输出被截断）"
        if st == "warn":
            return f"逻辑推理 {g['correct']}/{g['total']} 正确（接近但未完全推出唯一解）"
        return f"逻辑推理仅 {g['correct']}/{g['total']} 正确（疑似静默降级为弱模型）"
    if cid == "iq_constraints":
        if st == "pass":
            return "4 条复合约束全部满足（键名、精确长度、禁用字母、长度单调）"
        return f"复合约束满足 {g['passed']}/4，未满足：{'、'.join(g['broken'])}"
    if st == "pass":
        pt = "未返回" if g["prompt_tokens"] is None else g["prompt_tokens"]
        return f"约 {g['est_tokens']} token 长上下文 3 跳追踪正确（上游 prompt_tokens={pt}）"
    if st == "warn":
        why = "疑似静默截断输入" if g["token_flag"] == "truncation" else "疑似 token 虚增计费"
        return f"答案正确，但上游 prompt_tokens={g['prompt_tokens']} 与估算 {g['est_tokens']} 偏差异常（{why}）"
    base = (f"命中诱饵链 {g['decoy_hit']}（疑似丢失中段上下文或静默截断）" if g["decoy_hit"]
            else "未找到正确的 FINAL_CODE（疑似静默截断长上下文或回退弱模型）")
    if g["token_flag"] == "truncation":
        return f"{base}；上游 prompt_tokens={g['prompt_tokens']} 远小于估算 {g['est_tokens']}"
    return base


def input_tokens(protocol: str, data: Any) -> int | None:
    u = _usage(protocol, data) if isinstance(data, dict) else None
    u = u if isinstance(u, dict) else {}
    if protocol == "gemini":
        v = u.get("promptTokenCount")
    elif protocol == "anthropic":
        v = _total_in(u) if _is_count(u.get("input_tokens")) else None
    elif protocol == "openai-response":
        v = u.get("input_tokens")
    else:
        v = u.get("prompt_tokens")
    return int(v) if _is_count(v) else None


def fail_fast(res: Any) -> bool:
    if not isinstance(res, dict) or res.get("status") != "fail":
        return False
    ev = res.get("evidence")
    st = ev.get("http_status") if isinstance(ev, dict) else None
    return _own(ev, "error") and isinstance(st, int) and not isinstance(st, bool) and st in FAIL_FAST_HTTP


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

class _Ctx:
    def __init__(self, base: str, api_key: str, model: str, seed: int | None = None,
                 context_tokens: int = CLI_CONTEXT_TOKENS["def"]) -> None:
        self.base, self.api_key, self.model = base, api_key, model
        self.seed = random_seed() if seed is None else seed
        self.context_tokens = context_tokens
        self.calls = 0
        self._lock = threading.Lock()


def _call(ctx: _Ctx, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float = 24.0) -> dict[str, Any]:
    with ctx._lock:
        ctx.calls += 1
    return _post_json(url, headers, payload, ctx.api_key, timeout)


def _data(r: dict[str, Any]) -> dict[str, Any]:
    return r["data"] if isinstance(r.get("data"), dict) else {}


def _run_fingerprint(ctx: _Ctx, protocol: str) -> dict[str, Any]:
    title = "协议指纹与模型回显"
    model = ctx.model
    ch = build_challenge("fingerprint", ctx.seed, ctx.context_tokens)
    r = _call(ctx, endpoint_url(ctx.base, protocol, model), _auth_headers(protocol, ctx.api_key),
              _text_payload(protocol, model, ch["prompt"], 32))
    if not r["ok"]:
        return _http_fail("fingerprint", title, r, f"HTTP {r['status']} 调用失败")
    d = _data(r)
    text = extract_text(protocol, d)
    ret_model = (d.get("modelVersion") if protocol == "gemini" else d.get("model")) or None
    ret_id = (d.get("responseId") if protocol == "gemini" else d.get("id")) or None
    math_ok = has_int(text, ch["answer"])
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
        notes.append(f"动态算术挑战未返回 {ch['answer']}（实际: {text[:40] or '空'}）")
    return {
        "id": "fingerprint", "title": title, "status": status, "ms": r["ms"],
        "summary": "；".join(notes) if notes else f"原生响应结构匹配（model={ret_model}，{r['ms']}ms）",
        "telemetry": telemetry(r, protocol),
        "evidence": {
            "http_status": r["status"], "requested_model": model, "returned_model": ret_model,
            "echo_normalized": [normalize_model_id(model), normalize_model_id(ret_model)], "echo_ok": echo_ok,
            "response_id": ret_id, "id_ok": id_ok, "shape_ok": shape_ok, "math_ok": math_ok,
            "challenge": ch["nonce"], "expected": ch["answer"],
            "system_fingerprint": d.get("system_fingerprint") or None, "answer": text[:80],
            "usage": _usage(protocol, d) or None, "headers": r["headers"],
        },
    }


def _run_anthropic_thinking(ctx: _Ctx) -> dict[str, Any]:
    title = "Thinking 签名回传与篡改负向校验"
    model = ctx.model
    ch = build_challenge("thinking", ctx.seed, ctx.context_tokens)
    url, hdrs = endpoint_url(ctx.base, "anthropic", model), _auth_headers("anthropic", ctx.api_key)
    think_cfg = {"type": "enabled", "budget_tokens": 1024}
    p1 = {"model": model, "max_tokens": 1400, "temperature": 1, "thinking": think_cfg,
          "messages": [{"role": "user", "content": ch["q1"]}]}
    r1 = _call(ctx, url, hdrs, p1, 30.0)
    if not r1["ok"]:
        return _http_fail("thinking", title, r1, f"开启 Extended Thinking 返回 HTTP {r1['status']}")
    d1 = _data(r1)
    blocks = d1.get("content") if isinstance(d1.get("content"), list) else []
    think = next((b for b in blocks if isinstance(b, dict) and b.get("type") == "thinking"), None)
    sig = think.get("signature") if think and isinstance(think.get("signature"), str) else ""
    txt1 = extract_text("anthropic", d1)
    base_ev = {
        "challenge": ch["nonce"],
        "block_types": [b.get("type") if isinstance(b, dict) else None for b in blocks],
        "signature_len": len(sig),
        "signature_b64": re.fullmatch(r"[A-Za-z0-9+/=]+", sig) is not None,
        "thinking_chars": len(think["thinking"]) if think and isinstance(think.get("thinking"), str) else 0,
        "answer_t1": txt1[:40],
        "answer_t1_ok": has_int(txt1, ch["answer1"]),
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
                "messages": [{"role": "user", "content": ch["q1"]}, {"role": "assistant", "content": content},
                             {"role": "user", "content": ch["q2"]}]}

    with ThreadPoolExecutor(max_workers=2) as pool:
        f2 = pool.submit(_call, ctx, url, hdrs, mk(blocks), 30.0)
        ft = pool.submit(_call, ctx, url, hdrs, mk(tampered), 30.0)
        r2, rt = f2.result(), ft.result()
    d2 = _data(r2) if r2["ok"] else {}
    txt2 = extract_text("anthropic", d2) if r2["ok"] else ""
    roundtrip_ok = bool(r2["ok"] and has_int(txt2, ch["answer2"]))
    tamper = classify_tamper(rt["status"], rt["data"])
    usage = anthropic_usage_check(d1.get("usage"), d2.get("usage") if r2["ok"] else None)
    status = thinking_verdict({"has_block": True, "signature_len": len(sig), "roundtrip_ok": roundtrip_ok,
                               "tamper": tamper, "usage_ok": usage["ok"]})
    notes = [f"真签名二轮回传通过（{ch['answer1']} → {ch['answer2']}，signature {len(sig)} 字符）" if roundtrip_ok
             else f"二轮真签名回传异常（HTTP {r2['status']}，期望 {ch['answer2']}，回复 {txt2[:30] or '空'}）"]
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
            "turn2_status": r2["status"], "turn2_ok": roundtrip_ok, "answer_t2": txt2[:40], "expected_t2": ch["answer2"],
            "tamper_status": rt["status"], "tamper_result": tamper,
            "tamper_error": None if tamper == "accepted" else _err_snippet(rt)[:160],
            "usage_t2": (d2.get("usage") or None) if r2["ok"] else None,
            "usage_ok": usage["ok"], "usage_notes": usage["notes"],
            "cache_read_t2": u2.get("cache_read_input_tokens") if isinstance(u2, dict) else None,
        },
    }


def _run_thinking(ctx: _Ctx, protocol: str) -> dict[str, Any]:
    if protocol == "anthropic":
        return _run_anthropic_thinking(ctx)
    title = "推理遥测与确定性校验"
    model = ctx.model
    ch = build_challenge("thinking", ctx.seed, ctx.context_tokens)
    url, hdrs = endpoint_url(ctx.base, protocol, model), _auth_headers(protocol, ctx.api_key)
    if protocol == "openai-response":
        payload: dict[str, Any] = {"model": model, "max_output_tokens": 1024, "input": ch["q1"]}
    elif protocol == "gemini":
        payload = {"contents": [{"role": "user", "parts": [{"text": ch["q1"]}]}],
                   "generationConfig": {"maxOutputTokens": 1024, "thinkingConfig": {"includeThoughts": True}}}
    else:
        payload = {"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": ch["q1"]}]}
    r = _call(ctx, url, hdrs, payload, 30.0)
    dropped = False
    if not r["ok"] and r["status"] == 400 and protocol == "gemini":
        dropped = True
        r = _call(ctx, url, hdrs, {**payload, "generationConfig": {"maxOutputTokens": 1024}}, 30.0)
    if not r["ok"]:
        return _http_fail("thinking", title, r, f"HTTP {r['status']} 调用失败")
    d = _data(r)
    txt = extract_text(protocol, d)
    exp = ch["answer1"]
    ok_ans = has_int(txt, exp)
    sig = reasoning_signal(protocol, d)
    usage = openai_usage_check(protocol, _usage(protocol, d))
    status = "fail" if not ok_ans else ("pass" if (sig["strong"] and usage["ok"]) else "warn")
    if not ok_ans:
        summary = f"动态算术推理失败（期望 {exp}，实际: {txt[:50] or '空'}）"
    elif sig["strong"]:
        rtk = "—" if sig["reasoning_tokens"] is None else sig["reasoning_tokens"]
        summary = (f"动态校验通过（{exp}），含原生推理遥测（reasoning_tokens={rtk}"
                   + ("，reasoning 输出项" if sig["reasoning_item"] else "")
                   + ("，reasoning_content" if sig["reasoning_content"] else "")
                   + (f"，thoughtSignature {sig['thought_signature_len']} 字符" if sig["thought_signature_len"] else "") + "）")
    elif sig["reasoning_tokens"] == 0:
        summary = f"动态校验通过（{exp}），reasoning_tokens 字段存在但为 0（非推理档或被中转置零）"
    else:
        summary = f"动态校验通过（{exp}），未返回推理遥测（非推理档模型属正常；推理档缺失则疑似转译）"
    if not usage["ok"]:
        summary += "；usage 异常：" + "、".join(usage["notes"])
    if dropped:
        summary += "；上游不接受 thinkingConfig，已去掉后重试"
    return {"id": "thinking", "title": title, "status": status, "ms": r["ms"], "summary": summary,
            "telemetry": telemetry(r, protocol),
            "evidence": {"challenge": ch["nonce"], "expected": exp, "answer": txt[:60], **sig, "usage_ok": usage["ok"],
                         "usage_notes": usage["notes"], "usage": _usage(protocol, d) or None}}


def tool_payload(protocol: str, model: str, ch: dict[str, Any]) -> dict[str, Any]:
    rk, ck = ch["region_key"], ch["code_key"]
    params = {"type": "object", "properties": {rk: {"type": "string", "enum": ch["enum"]}, ck: {"type": "integer"}},
              "required": [rk, ck]}
    if protocol == "anthropic":
        return {"model": model, "max_tokens": 200,
                "tools": [{"name": ch["name"], "description": ch["description"], "input_schema": params}],
                "tool_choice": {"type": "any"}, "messages": [{"role": "user", "content": ch["message"]}]}
    if protocol == "openai-response":
        return {"model": model, "max_output_tokens": 400,
                "tools": [{"type": "function", "name": ch["name"], "description": ch["description"], "parameters": params}],
                "tool_choice": "required", "input": ch["message"]}
    if protocol == "gemini":
        return {"contents": [{"role": "user", "parts": [{"text": ch["message"]}]}],
                "tools": [{"functionDeclarations": [{"name": ch["name"], "description": ch["description"], "parameters": {
                    "type": "OBJECT", "properties": {rk: {"type": "STRING", "enum": ch["enum"]}, ck: {"type": "INTEGER"}},
                    "required": [rk, ck]}}]}],
                "toolConfig": {"functionCallingConfig": {"mode": "ANY"}}, "generationConfig": {"maxOutputTokens": 1024}}
    return {"model": model, "max_tokens": 400,
            "tools": [{"type": "function", "function": {"name": ch["name"], "description": ch["description"], "parameters": params}}],
            "tool_choice": "required", "messages": [{"role": "user", "content": ch["message"]}]}


def _run_tool(ctx: _Ctx, protocol: str) -> dict[str, Any]:
    title = "严格 Tool-Calling 结构校验"
    ch = build_challenge("tool", ctx.seed, ctx.context_tokens)
    r = _call(ctx, endpoint_url(ctx.base, protocol, ctx.model), _auth_headers(protocol, ctx.api_key),
              tool_payload(protocol, ctx.model, ch))
    if not r["ok"]:
        return _http_fail("tool", title, r, f"工具调用返回 HTTP {r['status']}")
    d = _data(r)
    tc = extract_tool_call(protocol, d)
    status = tool_verdict(tc["name"], tc["args"], ch)
    cands = d.get("candidates") or [{}]
    cand = cands[0] if isinstance(cands[0], dict) else {}
    chs = d.get("choices") or [{}]
    stop = (d.get("stop_reason") or (chs[0].get("finish_reason") if isinstance(chs[0], dict) else None)
            or cand.get("finishReason") or d.get("status") or None)
    if status == "pass":
        summary = f'原生结构化工具调用精准通过（动态工具 {ch["name"]}，{ch["region_key"]}="{ch["region"]}", {ch["code_key"]}={ch["code"]} 整型）'
    elif status == "warn":
        summary = "命中动态工具名但参数类型漂移（如整数被序列化为字符串）"
    else:
        summary = f"未按动态 schema 返回结构化调用（期望 {ch['name']}；部分伪装或转译渠道会吞掉或改写工具调用）"
    return {"id": "tool", "title": title, "status": status, "ms": r["ms"], "summary": summary,
            "telemetry": telemetry(r, protocol),
            "evidence": {"expected_name": ch["name"], "expected_args": {ch["region_key"]: ch["region"], ch["code_key"]: ch["code"]},
                         "function_name": tc["name"], "arguments": tc["args"], "stop_reason": stop}}


def cache_payload(protocol: str, model: str, prefix: str, q: str) -> dict[str, Any]:
    if protocol == "anthropic":
        return {"model": model, "max_tokens": 24, "temperature": 0,
                "system": [{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": q}]}
    if protocol == "openai-response":
        return {"model": model, "max_output_tokens": 400, "instructions": prefix, "input": q}
    if protocol == "gemini":
        return {"systemInstruction": {"parts": [{"text": prefix}]}, "contents": [{"role": "user", "parts": [{"text": q}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 1024}}
    return {"model": model, "max_tokens": 256, "temperature": 0,
            "messages": [{"role": "system", "content": prefix}, {"role": "user", "content": q}]}


def _run_cache(ctx: _Ctx, protocol: str) -> dict[str, Any]:
    title = "两轮 Prompt Cache 真命中"
    ch = build_challenge("cache", ctx.seed, ctx.context_tokens)
    url, hdrs = endpoint_url(ctx.base, protocol, ctx.model), _auth_headers(protocol, ctx.api_key)
    r1 = _call(ctx, url, hdrs, cache_payload(protocol, ctx.model, ch["prefix"], ch["q1"]))
    if not r1["ok"]:
        return _http_fail("cache", title, r1, f"首轮缓存预热失败（HTTP {r1['status']}）")
    r2 = _call(ctx, url, hdrs, cache_payload(protocol, ctx.model, ch["prefix"], ch["q2"]))
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
    if not math.isfinite(read):
        read = 0.0
    read_v = int(read) if read.is_integer() else read
    hit = read > 0
    ans2 = extract_text(protocol, r2["data"])
    answer_ok = ch["answer2"] in ans2
    if hit and answer_ok:
        summary = f"次轮实测命中缓存读 {read_v} tokens（首轮写入 {create}），动态条款 {ch['answer2']} 召回正确"
    elif hit:
        summary = f"次轮命中缓存读 {read_v} tokens，但动态条款召回错误（期望 {ch['answer2']}）"
    else:
        summary = "次轮未返回缓存命中 token（可能该分组剥离了缓存头、走多账号轮询混池，或上游未达最小缓存阈值）"
    return {
        "id": "cache", "title": title, "status": "pass" if (hit and answer_ok) else "warn", "ms": r1["ms"] + r2["ms"],
        "summary": summary, "telemetry": telemetry(r2, protocol),
        "evidence": {"session": ch["session"], "prefix_est_tokens": ch["est_tokens"],
                     "turn1_usage": u1, "turn2_usage": u2, "cache_creation_t1": create, "cache_read_t2": read_v,
                     "expected_t2": ch["answer2"], "answer_t2": ans2[:40], "answer_ok": answer_ok},
    }


IQ_SPECS = {
    "iq_trace": {"title": "动态状态机执行追踪", "maxTokens": 1500},
    "iq_logic": {"title": "动态多约束逻辑推理", "maxTokens": 2000},
    "iq_constraints": {"title": "严格复合指令遵从", "maxTokens": 600},
    "context_hop": {"title": "长上下文多跳追踪（防静默截断）", "maxTokens": 200},
}


def grade_iq(cid: str, ch: dict[str, Any], text: Any, prompt_tokens: Any = None) -> dict[str, Any]:
    if cid == "iq_trace":
        return grade_iq_trace(ch, text)
    if cid == "iq_logic":
        return grade_iq_logic(ch, text)
    if cid == "iq_constraints":
        return grade_iq_constraints(ch, text)
    return grade_context_hop(ch, text, prompt_tokens)


def _run_iq(cid: str, ctx: _Ctx, protocol: str) -> dict[str, Any]:
    spec = IQ_SPECS[cid]
    ch = build_challenge(cid, ctx.seed, ctx.context_tokens)
    r = _call(ctx, endpoint_url(ctx.base, protocol, ctx.model), _auth_headers(protocol, ctx.api_key),
              _text_payload(protocol, ctx.model, ch["prompt"], spec["maxTokens"]), 45.0)
    if not r["ok"]:
        return _http_fail(cid, spec["title"], r, f"HTTP {r['status']} 调用失败")
    d = _data(r)
    txt = extract_text(protocol, d)
    g = grade_iq(cid, ch, txt, input_tokens(protocol, d))
    return {"id": cid, "title": spec["title"], "status": g["status"], "ms": r["ms"], "summary": iq_summary(cid, g),
            "telemetry": telemetry(r, protocol), "evidence": {**g, "challenge": ch["nonce"], "answer_tail": txt[-160:]}}


RUNNERS = {
    "fingerprint": _run_fingerprint,
    "thinking": _run_thinking,
    "tool": _run_tool,
    "cache": _run_cache,
    "iq_trace": lambda ctx, p: _run_iq("iq_trace", ctx, p),
    "iq_logic": lambda ctx, p: _run_iq("iq_logic", ctx, p),
    "iq_constraints": lambda ctx, p: _run_iq("iq_constraints", ctx, p),
    "context_hop": lambda ctx, p: _run_iq("context_hop", ctx, p),
}


def _single(cid: str, base: str, api_key: str, model: str, protocol: str, seed: int | None,
            context_tokens: int) -> dict[str, Any]:
    return RUNNERS[cid](_Ctx(base, api_key, model, seed, context_tokens), protocol)


def check_fingerprint(base: str, api_key: str, model: str, protocol: str, seed: int | None = None,
                      context_tokens: int = CLI_CONTEXT_TOKENS["def"]) -> dict[str, Any]:
    return _single("fingerprint", base, api_key, model, protocol, seed, context_tokens)


def check_thinking(base: str, api_key: str, model: str, protocol: str, seed: int | None = None,
                   context_tokens: int = CLI_CONTEXT_TOKENS["def"]) -> dict[str, Any]:
    return _single("thinking", base, api_key, model, protocol, seed, context_tokens)


def check_tool(base: str, api_key: str, model: str, protocol: str, seed: int | None = None,
               context_tokens: int = CLI_CONTEXT_TOKENS["def"]) -> dict[str, Any]:
    return _single("tool", base, api_key, model, protocol, seed, context_tokens)


def check_cache(base: str, api_key: str, model: str, protocol: str, seed: int | None = None,
                context_tokens: int = CLI_CONTEXT_TOKENS["def"]) -> dict[str, Any]:
    return _single("cache", base, api_key, model, protocol, seed, context_tokens)


def check_iq(cid: str, base: str, api_key: str, model: str, protocol: str, seed: int | None = None,
             context_tokens: int = CLI_CONTEXT_TOKENS["def"]) -> dict[str, Any]:
    if cid not in IQ_CHECKS:
        raise ValueError(f"未知智商检测项 {cid}")
    return _single(cid, base, api_key, model, protocol, seed, context_tokens)


def should_fallback(requested: str, protocol: str, is_first: bool, res: dict[str, Any]) -> bool:
    st = (res.get("evidence") or {}).get("http_status")
    return requested == "auto" and protocol == "gemini" and is_first and res.get("status") == "fail" and st in (404, 405)


def fail_fast_reason(res: dict[str, Any], rest: list[str]) -> str:
    return f"指纹探测 HTTP {res['evidence'].get('http_status') or '超时'}：Key / URL / 模型不可用，已跳过其余 {len(rest)} 项以免浪费 token"


def parse_checks(raw: Any, tier: str = DEFAULT_TIER) -> list[str]:
    if isinstance(raw, list) and raw:
        return [c for c in ALL_CHECKS if c in raw]
    return list(TIERS[tier]) if isinstance(tier, str) and tier in TIERS else []


def _run_checks(ctx: _Ctx, requested: str, checks: list[str]) -> dict[str, Any]:
    proto = detect_protocol(ctx.model, requested)
    fallback = None
    head, rest = checks[0], checks[1:]
    first = RUNNERS[head](ctx, proto)
    if should_fallback(requested, proto, True, first):
        fallback = {"from": "gemini", "to": "openai",
                    "reason": f"Gemini 原生端点返回 HTTP {first['evidence']['http_status']}，已回退 OpenAI 兼容协议"}
        proto = "openai"
        first = RUNNERS[head](ctx, proto)
    if head == "fingerprint" and rest and fail_fast(first):
        return {"protocol": proto, "fallback": fallback, "results": [first], "skipped": rest,
                "fail_fast": fail_fast_reason(first, rest)}
    others: list[dict[str, Any]] = []
    if rest:
        with ThreadPoolExecutor(max_workers=min(CONCURRENCY, len(rest))) as pool:
            others = list(pool.map(lambda cid: RUNNERS[cid](ctx, proto), rest))
    return {"protocol": proto, "fallback": fallback, "results": [first, *others], "skipped": [], "fail_fast": None}


def run_suite(base_url: str, api_key: str, model: str, protocol: str = "auto", checks: list[str] | None = None,
              tier: str | None = None, seed: Any = None, context_tokens: Any = CLI_CONTEXT_TOKENS["def"]) -> dict[str, Any]:
    base = normalize_base_url(base_url)
    if not base:
        raise ValueError("base_url 必须是公网 HTTPS 域名（默认 443 端口；禁止内网、裸 IP 与自定义端口）")
    if not is_valid_model(model):
        raise ValueError("请填写有效的目标模型名（字母、数字与 . _ - : / @）")
    requested = protocol or "auto"
    if requested not in PROTOCOLS:
        raise ValueError(f"protocol 仅支持 {' / '.join(PROTOCOLS)}")
    if tier is not None and tier not in TIERS:
        raise ValueError(f"tier 仅支持 {' / '.join(TIERS)}")
    custom = isinstance(checks, list) and len(checks) > 0
    want = parse_checks(checks, tier or DEFAULT_TIER)
    if not want:
        raise ValueError(f"checks 仅支持 {' / '.join(ALL_CHECKS)}")
    s = random_seed() if seed is None else normalize_seed(seed)
    if s is None:
        raise ValueError("seed 必须是 0–4294967295 之间的整数")
    ctxt = clamp_context_tokens(context_tokens, CLI_CONTEXT_TOKENS["min"], CLI_CONTEXT_TOKENS["max"], CLI_CONTEXT_TOKENS["def"])
    ctx = _Ctx(base, api_key, model, s, ctxt)
    run = _run_checks(ctx, requested, want)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base, "model": model, "requested_protocol": requested, "protocol": run["protocol"],
        "protocol_fallback": run["fallback"], "tier": "custom" if custom else (tier or DEFAULT_TIER), "checks": want,
        "seed": s, "context_tokens": ctxt, "upstream_calls": ctx.calls, "skipped": run["skipped"],
        "fail_fast": run["fail_fast"], **summarize(run["results"]), "results": run["results"],
    }


_RANK = {"pass": 2, "warn": 1, "fail": 0}


def differential_report(target: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    """Compare a target relay against a reference (official) endpoint run on the exact same seed."""
    t_by = {x["id"]: x.get("status") for x in target.get("results") or []}
    r_by = {x["id"]: x.get("status") for x in reference.get("results") or []}
    rows = []
    for cid in [c for c in ALL_CHECKS if c in t_by or c in r_by]:
        ts, rs = t_by.get(cid), r_by.get(cid)
        worse = ts is not None and rs is not None and _RANK.get(ts, 0) < _RANK.get(rs, 0)
        rows.append({"id": cid, "group": check_group(cid), "target": ts, "reference": rs, "target_worse": worse})

    def delta(k: str) -> int | None:
        a, b = target.get(k), reference.get(k)
        return None if a is None or b is None else a - b

    worse_ids = [x["id"] for x in rows if x["target_worse"]]
    iq_worse = [c for c in worse_ids if check_group(c) == "iq"]
    auth_worse = [c for c in worse_ids if check_group(c) == "auth"]
    if iq_worse:
        verdict = f"同种子下目标中转在 {'、'.join(iq_worse)} 弱于官方参考端点：疑似静默降级、剥离推理档或截断上下文"
    elif auth_worse:
        verdict = f"同种子下目标中转在 {'、'.join(auth_worse)} 弱于官方参考端点：疑似转译或协议特征缺失"
    else:
        verdict = "同种子下目标中转与官方参考端点表现一致"
    return {"seed": target.get("seed"), "same_seed": target.get("seed") == reference.get("seed"), "checks": rows,
            "auth_score_delta": delta("auth_score"), "iq_score_delta": delta("iq_score"), "score_delta": delta("score"),
            "target_worse": worse_ids, "suspect_downgrade": bool(iq_worse), "verdict": verdict}


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify LLM API relay authenticity & intelligence locally (key never leaves your machine).")
    ap.add_argument("--base-url", required=True, help="Relay base URL, e.g. https://api.example.com")
    ap.add_argument("--api-key", required=True, help="API key for testing")
    ap.add_argument("--model", required=True, help="Model ID, e.g. claude-sonnet-5-5")
    ap.add_argument("--protocol", default="auto", choices=PROTOCOLS)
    ap.add_argument("--tier", default=DEFAULT_TIER, choices=list(TIERS),
                    help="l1_auth = 真伪 4 项; l2_iq = 真伪 + 智商抽检 (default); l3_deep = 全部 8 项含长文多跳")
    ap.add_argument("--checks", default="", help=f"Comma-separated custom checks (overrides --tier): {','.join(ALL_CHECKS)}")
    ap.add_argument("--seed", type=int, default=None, help="uint32 seed for reproducible challenges (random when omitted)")
    ap.add_argument("--context-tokens", type=int, default=CLI_CONTEXT_TOKENS["def"],
                    help=f"context_hop haystack size, clamped to [{CLI_CONTEXT_TOKENS['min']}, {CLI_CONTEXT_TOKENS['max']}]")
    ap.add_argument("--reference-url", default="", help="Optional official reference endpoint for differential testing")
    ap.add_argument("--reference-key", default="", help="API key for --reference-url")
    ap.add_argument("--reference-model", default="", help="Model ID on the reference endpoint (defaults to --model)")
    args = ap.parse_args()
    checks = [c.strip() for c in args.checks.split(",") if c.strip()] or None
    try:
        seed = random_seed() if args.seed is None else args.seed
        out = run_suite(args.base_url, args.api_key, args.model, args.protocol, checks, args.tier, seed, args.context_tokens)
        if args.reference_url:
            if not args.reference_key:
                raise ValueError("--reference-url 需要同时提供 --reference-key")
            ref = run_suite(args.reference_url, args.reference_key, args.reference_model or args.model, args.protocol,
                            checks, args.tier, out["seed"], args.context_tokens)
            out = {"target": out, "reference": ref, "differential": differential_report(out, ref), "level": out["level"]}
    except ValueError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(2)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if out["level"] != "fail" else 1)


if __name__ == "__main__":
    main()
