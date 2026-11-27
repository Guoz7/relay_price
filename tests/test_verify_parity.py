"""Cross-language parity tests: functions/api/verify.js (edge) vs tools/verify_model.py (local CLI).

Both implementations must give identical answers for the SSRF guard, model-echo normalization, tamper /
usage / telemetry classifiers and scoring. The JS module is imported through a temporary ESM copy with Node.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import verify_model as VM  # noqa: E402

JS_SRC = ROOT / "functions" / "api" / "verify.js"
NODE = shutil.which("node")

HARNESS = r"""
import * as V from './verify.mjs';
import { readFileSync } from 'node:fs';
const cases = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const out = cases.map(([fn, args]) => {
  const v = V[fn](...args);
  return v === undefined ? null : v;
});
process.stdout.write(JSON.stringify(out));
"""

PY = {
    "isBlockedHost": VM.is_blocked_host,
    "normalizeBaseUrl": VM.normalize_base_url,
    "isValidModel": VM.is_valid_model,
    "detectProtocol": VM.detect_protocol,
    "normalizeModelId": VM.normalize_model_id,
    "modelEchoMatches": VM.model_echo_matches,
    "redact": VM.redact,
    "tamperSignature": VM.tamper_signature,
    "classifyTamper": VM.classify_tamper,
    "anthropicUsageCheck": VM.anthropic_usage_check,
    "openaiUsageCheck": VM.openai_usage_check,
    "outputTokens": VM.output_tokens,
    "telemetry": VM.telemetry,
    "responseShapeOk": VM.response_shape_ok,
    "responseIdOk": VM.response_id_ok,
    "reasoningSignal": VM.reasoning_signal,
    "thinkingVerdict": VM.thinking_verdict,
    "summarize": VM.summarize,
    "extractText": VM.extract_text,
    "extractToolCall": VM.extract_tool_call,
    "toolVerdict": VM.tool_verdict,
    "cacheReadTokens": VM.cache_read_tokens,
    "endpointUrl": VM.endpoint_url,
    "shouldFallback": VM.should_fallback,
    "parseChecks": VM.parse_checks,
}

SIG = "EuYBCkQYAiJA" * 8
HOSTS = ["api.example.com", "good-relay.co", "a.b.c.example.cn", "xn--fiqs8s.com", "localhost", "127.0.0.1",
         "10.0.0.8", "0x7f.1", "2130706433", "0177.0.0.1", "[::1]", "::1", "foo.internal", "a.local",
         "127.0.0.1.nip.io", "metadata.google.internal", "-bad.com", "example.123", "relay.test.", ""]
URLS = ["api.example.com/v1/messages/", "https://relay.test/v1", "http://relay.test", "https://relay.test:8443",
        "https://relay.test:443/v1/chat/completions", "https://user:pw@relay.test", "https://127.0.0.1",
        "https://2130706433", "https://0x7f000001", "https://[::1]/", "ftp://relay.test",
        "https://relay.test/proxy/v1beta/models/gemini-x", "https://Relay.Test/openai/v1/", "  ",
        "https://relay.test/a b", "https://10.1.2.3:443", "relay.test/v1/responses"]
MODELS = ["claude-sonnet-5-5", "models/gemini-4-carbon", "../etc", "a b", "gpt-6.1-sol", "x" * 121,
          "deepseek/deepseek-v4.1-flash", "-bad", "claude-fable-5-2", "o4-mini", "codex-mini", "deepseek-v4-pro"]
ECHO = [["claude-sonnet-5-5", "claude-sonnet-5.5-20261001"], ["claude-sonnet-5-5", "claude-sonnet-5"],
        ["gpt-6-astra", "gpt-6"], ["gemini-4-carbon", "models/gemini-4-carbon-preview-05-06"],
        ["claude-opus-5-5", "us.anthropic.claude-opus-5-5-20261101-v1:0"], ["deepseek-v4.1-flash", "deepseek-v4-1-flash"],
        ["gpt-4o", "gpt-4o-2024-08-06"], ["claude-sonnet-5-5", None], ["claude-sonnet-5-5", "deepseek-v4-chat"],
        ["claude-sonnet-5-5@20261001", "claude-sonnet-5-5"], ["claude-sonnet-5-5", "anthropic/claude-sonnet-5-5-thinking"]]
U_GOOD1 = {"input_tokens": 40, "output_tokens": 120, "cache_read_input_tokens": 0}
U_GOOD2 = {"input_tokens": 95, "output_tokens": 80}
ANTH = {"id": "msg_01AbCdEfGh123", "type": "message", "role": "assistant",
        "content": [{"type": "thinking", "thinking": "19*23=437", "signature": SIG}, {"type": "text", "text": "454"},
                    {"type": "tool_use", "name": "lookup_audit_code", "input": {"region": "CN", "code": 4096}}],
        "usage": {"input_tokens": 30, "output_tokens": 50}}
RESP = {"id": "resp_68abcdef0123", "object": "response",
        "output": [{"type": "reasoning"}, {"type": "message", "content": [{"type": "output_text", "text": "700"}]},
                   {"type": "function_call", "name": "lookup_audit_code", "arguments": "{\"region\":\"CN\",\"code\":\"4096\"}"}],
        "usage": {"input_tokens": 20, "output_tokens": 64, "total_tokens": 84, "output_tokens_details": {"reasoning_tokens": 48},
                  "input_tokens_details": {"cached_tokens": 1280}}}
CHAT = {"id": "930c60df-bf64-41c9-a88e-3ec75f81e00e", "object": "chat.completion",
        "choices": [{"message": {"content": " 454 ", "reasoning_content": "think...",
                                 "tool_calls": [{"function": {"name": "lookup_audit_code", "arguments": "{\"region\":\"cn\",\"code\":4096}"}}]},
                     "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 30, "total_tokens": 41, "prompt_cache_hit_tokens": 1024,
                  "completion_tokens_details": {"reasoning_tokens": 31}}}
GEM = {"responseId": "AbCdEf123456", "modelVersion": "gemini-4-carbon",
       "candidates": [{"content": {"parts": [{"text": "plan", "thought": True}, {"text": "454", "thoughtSignature": "Q" * 60},
                                              {"functionCall": {"name": "lookup_audit_code", "args": {"region": "CN", "code": 4096}}}]},
                       "finishReason": "STOP"}],
       "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 40, "totalTokenCount": 55,
                         "cachedContentTokenCount": 900}}
PROTOS = ["anthropic", "openai-response", "openai", "gemini"]
SAMPLES = {"anthropic": ANTH, "openai-response": RESP, "openai": CHAT, "gemini": GEM}


def build_cases():
    cases = []
    cases += [["isBlockedHost", [h]] for h in HOSTS]
    cases += [["normalizeBaseUrl", [u]] for u in URLS]
    cases += [["isValidModel", [m]] for m in MODELS]
    cases += [["detectProtocol", [m, "auto"]] for m in MODELS] + [["detectProtocol", ["claude-x", "openai"]]]
    cases += [["normalizeModelId", [a]] for a, _ in ECHO] + [["normalizeModelId", [b]] for _, b in ECHO]
    cases += [["modelEchoMatches", [a, b]] for a, b in ECHO]
    cases += [["redact", ["key sk-abcdef123 leaked sk-abcdef123", "sk-abcdef123"]], ["redact", ["short", "abc"]]]
    cases += [["tamperSignature", [s]] for s in (SIG, "AAAA", "----____----____", "")]
    cases += [["classifyTamper", [st, d]] for st, d in (
        (200, {}), (400, {"error": {"message": "Invalid `signature` in `thinking` block"}}), (400, {"error": "bad"}),
        (401, {}), (0, {}), (529, {}), (429, {}), (None, None))]
    cases += [["anthropicUsageCheck", [a, b]] for a, b in (
        (U_GOOD1, U_GOOD2), (U_GOOD1, {"input_tokens": 30, "output_tokens": 5}), (None, None),
        ({"input_tokens": 0, "output_tokens": 0}, None), ({"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": "9"}, U_GOOD2),
        ({"input_tokens": 3, "output_tokens": 9, "cache_creation_input_tokens": 1200}, {"input_tokens": 5, "output_tokens": 9, "cache_read_input_tokens": 1200}))]
    cases += [["openaiUsageCheck", [p, SAMPLES[p].get("usageMetadata" if p == "gemini" else "usage")]] for p in PROTOS[1:]]
    cases += [["openaiUsageCheck", ["openai", {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 9,
                                               "completion_tokens_details": {"reasoning_tokens": 7}}]],
              ["openaiUsageCheck", ["openai-response", None]], ["openaiUsageCheck", ["gemini", {"promptTokenCount": 9, "totalTokenCount": 3}]]]
    cases += [["outputTokens", [p, SAMPLES[p]]] for p in PROTOS] + [["outputTokens", ["openai", {}]]]
    cases += [["telemetry", [{"ms": ms, "ttfb_ms": tt, "data": SAMPLES[p]}, p]] for p, ms, tt in (
        ("anthropic", 800, 790), ("openai-response", 1600, 1500), ("openai", 0, None), ("gemini", 3000, 2950))]
    cases += [["telemetry", [None, "openai"]]]
    cases += [["responseShapeOk", [p, SAMPLES[q]]] for p in PROTOS for q in PROTOS]
    cases += [["responseIdOk", [p, i]] for p in PROTOS for i in (
        "msg_01AbCdEfGh123", "resp_68abcdef0123", "chatcmpl-xyz", "930c60df-bf64-41c9-a88e-3ec75f81e00e", "AbCdEf123456", "", None, "x")]
    cases += [["reasoningSignal", [p, SAMPLES[p]]] for p in PROTOS] + [["reasoningSignal", ["openai", {"choices": [{"message": {"content": "1"}}]}]]]
    cases += [["thinkingVerdict", [v]] for v in (
        {"has_block": False, "signature_len": 0}, {"has_block": True, "signature_len": 20},
        {"has_block": True, "signature_len": 300, "tamper": "accepted", "roundtrip_ok": True, "usage_ok": True},
        {"has_block": True, "signature_len": 300, "tamper": "rejected", "roundtrip_ok": True, "usage_ok": True},
        {"has_block": True, "signature_len": 300, "tamper": "rejected", "roundtrip_ok": True, "usage_ok": False},
        {"has_block": True, "signature_len": 300, "tamper": "inconclusive", "roundtrip_ok": True, "usage_ok": True},
        {"has_block": True, "signature_len": 300, "tamper": "rejected", "roundtrip_ok": False, "usage_ok": True})]
    cases += [["summarize", [r]] for r in (
        [{"status": "pass"}] * 4, [{"status": "pass"}, {"status": "warn"}], [{"status": "pass"}, {"status": "fail"}],
        [{"status": "warn"}] * 3, [])]
    cases += [["extractText", [p, SAMPLES[p]]] for p in PROTOS] + [["extractText", ["openai", None]]]
    cases += [["extractToolCall", [p, SAMPLES[p]]] for p in PROTOS] + [["extractToolCall", ["anthropic", {}]]]
    cases += [["toolVerdict", [n, a]] for n, a in (
        ("lookup_audit_code", {"region": "CN", "code": 4096}), ("lookup_audit_code", {"region": "cn", "code": "4096"}),
        ("lookup_audit_code", {"region": "CN", "code": 4097}), ("other", {"region": "CN", "code": 4096}),
        ("lookup_audit_code", None), ("lookup_audit_code", "raw"), ("lookup_audit_code", {"region": "CN", "code": True}))]
    cases += [["cacheReadTokens", [p, SAMPLES[p].get("usageMetadata" if p == "gemini" else "usage")]] for p in PROTOS]
    cases += [["cacheReadTokens", ["openai", {"prompt_tokens_details": {"cached_tokens": 0}, "prompt_cache_hit_tokens": 9}]],
              ["cacheReadTokens", ["anthropic", None]]]
    cases += [["endpointUrl", ["https://r.test", p, "models/gemini-4-carbon"]] for p in PROTOS]
    cases += [["shouldFallback", [req, pr, first, res]] for req, pr, first, res in (
        ("auto", "gemini", True, {"status": "fail", "evidence": {"http_status": 404}}),
        ("auto", "gemini", False, {"status": "fail", "evidence": {"http_status": 404}}),
        ("gemini", "gemini", True, {"status": "fail", "evidence": {"http_status": 404}}),
        ("auto", "gemini", True, {"status": "fail", "evidence": {"http_status": 401}}),
        ("auto", "openai", True, {"status": "fail", "evidence": {"http_status": 405}}))]
    cases += [["parseChecks", [c]] for c in (None, [], ["cache", "fingerprint", "bogus"], ["bogus"], "fingerprint")]
    return cases


@unittest.skipUnless(NODE, "node is required for JS parity tests")
class VerifyParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = build_cases()
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            shutil.copy(JS_SRC, tdp / "verify.mjs")
            (tdp / "harness.mjs").write_text(HARNESS, encoding="utf-8")
            (tdp / "cases.json").write_text(json.dumps(cls.cases), encoding="utf-8")
            r = subprocess.run([NODE, str(tdp / "harness.mjs"), str(tdp / "cases.json")],
                               capture_output=True, text=True, encoding="utf-8", cwd=td)
        if r.returncode != 0:
            raise AssertionError(f"node harness failed: {r.stderr}")
        cls.js = json.loads(r.stdout)

    def test_every_vector_matches(self):
        self.assertEqual(len(self.js), len(self.cases))
        for (fn, args), js_val in zip(self.cases, self.js):
            py_val = json.loads(json.dumps(PY[fn](*args)))
            with self.subTest(fn=fn, args=json.dumps(args, ensure_ascii=False)[:120]):
                self.assertEqual(py_val, js_val)

    def test_security_expectations(self):
        res = {tuple(json.dumps(a) for a in args): v for (fn, args), v in zip(self.cases, self.js) if fn == "normalizeBaseUrl"}
        get = lambda u: res[(json.dumps(u),)]  # noqa: E731
        self.assertEqual(get("api.example.com/v1/messages/"), "https://api.example.com")
        self.assertEqual(get("https://relay.test:443/v1/chat/completions"), "https://relay.test")
        self.assertEqual(get("https://relay.test/proxy/v1beta/models/gemini-x"), "https://relay.test/proxy")
        for bad in ("http://relay.test", "https://relay.test:8443", "https://user:pw@relay.test", "https://127.0.0.1",
                    "https://2130706433", "https://0x7f000001", "https://[::1]/", "ftp://relay.test", "https://10.1.2.3:443"):
            self.assertIsNone(get(bad), bad)

    def test_js_source_hardening_markers(self):
        src = JS_SRC.read_text(encoding="utf-8")
        self.assertIn('redirect: "error"', src)
        self.assertNotIn("console.log", src)
        self.assertNotIn("api_key: apiKey", src)
        r = subprocess.run([NODE, "--check", str(JS_SRC)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, msg=r.stderr)


class VerifyPythonOnlyTests(unittest.TestCase):
    def test_tamper_changes_signature_but_keeps_alphabet(self):
        t = VM.tamper_signature(SIG)
        self.assertNotEqual(t, SIG)
        self.assertEqual(len(t), len(SIG))
        self.assertTrue(set(t) <= set(VM.B64))

    def test_redirects_are_blocked(self):
        with self.assertRaises(VM._RedirectBlocked):
            VM._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/")

    def test_run_suite_rejects_unsafe_input_without_network(self):
        for kw in ({"base_url": "https://127.0.0.1"}, {"base_url": "https://ok.test", "model": "../x"},
                   {"base_url": "https://ok.test", "protocol": "bogus"}, {"base_url": "https://ok.test", "checks": ["nope"]}):
            args = {"base_url": "https://ok.test", "api_key": "sk-test-123", "model": "claude-sonnet-5-5", "protocol": "auto", "checks": None}
            args.update(kw)
            with self.assertRaises(ValueError):
                VM.run_suite(**args)

    def test_anthropic_thinking_flow_with_fake_transport(self):
        calls = []

        def fake(url, headers, payload, api_key, timeout=24.0):
            calls.append(payload)
            msgs = payload["messages"]
            if len(msgs) == 1:
                return {"ok": True, "status": 200, "ms": 900, "ttfb_ms": 880, "headers": {},
                        "data": {"type": "message", "role": "assistant", "usage": {"input_tokens": 40, "output_tokens": 120},
                                 "content": [{"type": "thinking", "thinking": "x", "signature": SIG}, {"type": "text", "text": "454"}]}}
            sig = msgs[1]["content"][0]["signature"]
            if sig != SIG:
                return {"ok": False, "status": 400, "ms": 120, "ttfb_ms": 110, "headers": {},
                        "data": {"error": {"message": "Invalid signature in thinking block"}}}
            return {"ok": True, "status": 200, "ms": 700, "ttfb_ms": 690, "headers": {},
                    "data": {"type": "message", "usage": {"input_tokens": 90, "output_tokens": 60, "cache_read_input_tokens": 0},
                             "content": [{"type": "text", "text": "460"}]}}

        orig = VM._post_json
        VM._post_json = fake
        try:
            res = VM.check_thinking("https://r.test", "sk-test-123", "claude-sonnet-5-5", "anthropic")
        finally:
            VM._post_json = orig
        self.assertEqual(len(calls), 3)
        self.assertEqual(res["status"], "pass")
        ev = res["evidence"]
        self.assertEqual(ev["tamper_result"], "rejected")
        self.assertTrue(ev["turn2_ok"])
        self.assertEqual(ev["signature_len"], len(SIG))
        self.assertEqual(res["ms"], 900 + 700)
        self.assertEqual(res["telemetry"]["output_tokens"], 60)

    def test_tampered_signature_accepted_is_fail(self):
        def fake(url, headers, payload, api_key, timeout=24.0):
            if len(payload["messages"]) == 1:
                return {"ok": True, "status": 200, "ms": 500, "ttfb_ms": 480, "headers": {},
                        "data": {"usage": {"input_tokens": 40, "output_tokens": 120},
                                 "content": [{"type": "thinking", "thinking": "x", "signature": SIG}, {"type": "text", "text": "454"}]}}
            return {"ok": True, "status": 200, "ms": 400, "ttfb_ms": 390, "headers": {},
                    "data": {"usage": {"input_tokens": 90, "output_tokens": 60}, "content": [{"type": "text", "text": "460"}]}}

        orig = VM._post_json
        VM._post_json = fake
        try:
            res = VM.check_thinking("https://r.test", "sk-test-123", "claude-sonnet-5-5", "anthropic")
        finally:
            VM._post_json = orig
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["evidence"]["tamper_result"], "accepted")
        self.assertIn("篡改", res["summary"])

    def test_gemini_auto_fallback_to_openai(self):
        seen = []

        def fake(url, headers, payload, api_key, timeout=24.0):
            seen.append(url)
            if ":generateContent" in url:
                return {"ok": False, "status": 404, "ms": 50, "ttfb_ms": 50, "headers": {}, "data": {"error": "not found"}}
            return {"ok": True, "status": 200, "ms": 300, "ttfb_ms": 290, "headers": {},
                    "data": {"id": "chatcmpl-abc", "object": "chat.completion", "model": "gemini-4-carbon",
                             "choices": [{"message": {"content": "700"}}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}}

        orig = VM._post_json
        VM._post_json = fake
        try:
            out = VM.run_suite("https://r.test", "sk-test-123", "gemini-4-carbon", "auto", ["fingerprint"])
        finally:
            VM._post_json = orig
        self.assertEqual(out["protocol"], "openai")
        self.assertEqual(out["protocol_fallback"]["from"], "gemini")
        self.assertEqual(out["results"][0]["status"], "pass")
        self.assertTrue(seen[0].endswith("/v1beta/models/gemini-4-carbon:generateContent"))
        self.assertTrue(seen[1].endswith("/v1/chat/completions"))

    def test_api_key_is_redacted_from_upstream_errors(self):
        self.assertEqual(VM.redact('{"error":"bad key sk-live-abcdef"}', "sk-live-abcdef"), '{"error":"bad key ***"}')


E2E_HARNESS = r"""
import { onRequestPost } from './verify.mjs';
const SIG = 'EuYBCkQYAiJA'.repeat(8);
const KEY = 'sk-live-SECRET-123456';
const seen = [];
globalThis.fetch = async (url, opts) => {
  seen.push({ url, redirect: opts.redirect });
  const body = JSON.parse(opts.body);
  const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { 'content-type': 'application/json' } });
  if (body.tools) return json({ type: 'message', role: 'assistant', content: [{ type: 'tool_use', name: 'lookup_audit_code', input: { region: 'CN', code: 4096 } }], usage: { input_tokens: 9, output_tokens: 9 } });
  if (body.system) return json({ error: { message: 'cache disabled for key ' + KEY } }, 403);
  if (body.thinking) {
    if (body.messages.length === 1) return json({ type: 'message', role: 'assistant', usage: { input_tokens: 40, output_tokens: 120 }, content: [{ type: 'thinking', thinking: 'x', signature: SIG }, { type: 'text', text: '454' }] });
    if (body.messages[1].content[0].signature !== SIG) return json({ type: 'error', error: { type: 'invalid_request_error', message: 'Invalid signature in thinking block' } }, 400);
    return json({ type: 'message', role: 'assistant', usage: { input_tokens: 95, output_tokens: 40 }, content: [{ type: 'text', text: '460' }] });
  }
  return json({ id: 'msg_01AbCdEfGh123', type: 'message', role: 'assistant', model: 'claude-sonnet-5-5-20261001', content: [{ type: 'text', text: '700' }], usage: { input_tokens: 20, output_tokens: 3 } });
};
const post = (b) => onRequestPost({ request: new Request('https://agentied.work/api/verify', { method: 'POST', body: JSON.stringify(b) }) });
const out = {};
for (const bad of ['https://127.0.0.1', 'https://relay.test:8443', 'http://relay.test', 'https://[::1]', 'https://2130706433']) {
  out[bad] = (await post({ base_url: bad, api_key: KEY, model: 'claude-sonnet-5-5' })).status;
}
out.bad_proto = (await post({ base_url: 'https://relay.test', api_key: KEY, model: 'claude-sonnet-5-5', protocol: 'grpc' })).status;
out.bad_model = (await post({ base_url: 'https://relay.test', api_key: KEY, model: 'a/../b' })).status;
const before = seen.length;
const res = await post({ base_url: 'https://relay.test/v1', api_key: KEY, model: 'claude-sonnet-5-5' });
const text = await res.text();
out.status = res.status;
out.leaks_key = text.includes(KEY);
out.upstream_calls_for_rejected = before;
out.all_redirect_error = seen.every((s) => s.redirect === 'error');
out.urls = [...new Set(seen.map((s) => s.url))];
out.report = JSON.parse(text);
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is required for JS end-to-end tests")
class VerifyEdgeFunctionTests(unittest.TestCase):
    def test_on_request_post_with_mocked_upstream(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            shutil.copy(JS_SRC, tdp / "verify.mjs")
            (tdp / "e2e.mjs").write_text(E2E_HARNESS, encoding="utf-8")
            r = subprocess.run([NODE, str(tdp / "e2e.mjs")], capture_output=True, text=True, encoding="utf-8", cwd=td)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        out = json.loads(r.stdout)
        for bad in ("https://127.0.0.1", "https://relay.test:8443", "http://relay.test", "https://[::1]", "https://2130706433"):
            self.assertEqual(out[bad], 400, bad)
        self.assertEqual(out["bad_proto"], 400)
        self.assertEqual(out["bad_model"], 400)
        self.assertEqual(out["upstream_calls_for_rejected"], 0)
        self.assertEqual(out["status"], 200)
        self.assertFalse(out["leaks_key"])
        self.assertTrue(out["all_redirect_error"])
        self.assertEqual(out["urls"], ["https://relay.test/v1/messages"])
        rep = out["report"]
        by_id = {x["id"]: x for x in rep["results"]}
        self.assertEqual(rep["protocol"], "anthropic")
        self.assertEqual(by_id["fingerprint"]["status"], "pass")
        self.assertEqual(by_id["thinking"]["status"], "pass")
        self.assertEqual(by_id["thinking"]["evidence"]["tamper_result"], "rejected")
        self.assertEqual(by_id["tool"]["status"], "pass")
        self.assertEqual(by_id["cache"]["status"], "fail")
        self.assertIn("***", by_id["cache"]["evidence"]["error"])
        self.assertEqual(rep["level"], "fail")
        self.assertEqual(rep["score"], 75)
        for x in rep["results"]:
            self.assertIn("telemetry", x)


if __name__ == "__main__":
    unittest.main()
