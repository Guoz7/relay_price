"""Cross-language parity tests: functions/api/verify.js (edge) vs tools/verify_model.py (local CLI).

Both implementations must give identical answers for the SSRF guard, model-echo normalization, tamper /
usage / telemetry classifiers, seeded challenge generators, the iq_logic solver, the IQ graders and dual scoring
(auth_score / iq_score). The JS module is imported through a temporary ESM copy with Node. A fuzz suite runs the
generators and graders over 60 seeds in both languages.
"""

import itertools
import random

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
    "checkGroup": VM.check_group,
    "fnv1a": VM.fnv1a,
    "rngSequence": VM.rng_sequence,
    "deriveSeed": VM.derive_seed,
    "normalizeSeed": VM.normalize_seed,
    "clampContextTokens": VM.clamp_context_tokens,
    "hasInt": VM.has_int,
    "buildChallenge": VM.build_challenge,
    "perms4": VM.perms4,
    "clueHolds": VM.clue_holds,
    "solveLogic": VM.solve_logic,
    "extractJsonObject": VM.extract_json_object,
    "gradeIqTrace": VM.grade_iq_trace,
    "gradeIqLogic": VM.grade_iq_logic,
    "gradeIqConstraints": VM.grade_iq_constraints,
    "gradeContextHop": VM.grade_context_hop,
    "gradeIq": VM.grade_iq,
    "iqSummary": VM.iq_summary,
    "inputTokens": VM.input_tokens,
    "failFast": VM.fail_fast,
    "failFastReason": VM.fail_fast_reason,
    "toolPayload": VM.tool_payload,
    "cachePayload": VM.cache_payload,
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
    cases += [["parseChecks", [c, t]] for c in (None, [], ["context_hop", "iq_trace", "cache"], ["nope"])
              for t in ("l1_auth", "l2_iq", "l3_deep", "bogus")]
    dyn = VM.build_challenge("tool", 99)
    rk, ck = dyn["region_key"], dyn["code_key"]
    cases += [["toolVerdict", [n, a, dyn]] for n, a in (
        (dyn["name"], {rk: dyn["region"], ck: dyn["code"]}), (dyn["name"], {rk: dyn["region"].lower(), ck: str(dyn["code"])}),
        (dyn["name"], {rk: dyn["region"], ck: dyn["code"] + 1}), ("lookup_audit_code", {rk: dyn["region"], ck: dyn["code"]}),
        (dyn["name"], {"region": "CN", "code": 4096}), (dyn["name"], None))]
    tamper = {"id": "thinking", "status": "fail", "evidence": {"tamper_result": "accepted"}}
    cases += [["summarize", [r]] for r in (
        [{"id": c, "status": "pass"} for c in VM.ALL_CHECKS],
        [{"id": c, "status": "pass"} for c in VM.AUTH_CHECKS],
        [{"id": "fingerprint", "status": "pass"}, tamper, {"id": "iq_trace", "status": "pass"}],
        [{"id": c, "status": "pass"} for c in VM.AUTH_CHECKS] + [{"id": c, "status": "fail"} for c in VM.IQ_CHECKS[:3]],
        [{"id": "fingerprint", "status": "fail"}, {"id": "thinking", "status": "fail"}, {"id": "tool", "status": "warn"},
         {"id": "iq_logic", "status": "pass"}],
        [{"id": c, "status": "warn"} for c in VM.ALL_CHECKS])]
    cases += [["checkGroup", [c]] for c in VM.ALL_CHECKS + ["bogus", None]]
    cases += [["normalizeSeed", [v]] for v in (0, 42, 4294967295, 4294967296, -1, 1.5, "7", None, True, "abc")]
    cases += [["clampContextTokens", [v]] for v in (None, 0, 3999, 12000, 28001, 1e9, "16000", "x")]
    cases += [["clampContextTokens", [v, 4000, 64000, 16000]] for v in (None, 63000, 70000)]
    cases += [["hasInt", [t, n]] for t, n in (("answer: 4249.", 4249), ("42490", 4249), ("-4249", 4249), ("x 12 y", 12), ("", 1))]
    cases += [["extractJsonObject", [t]] for t in (
        '{"a": 1}', 'Sure:\n```json\n{"a": 2, "b": [1, 2]}\n```', 'text {"x": {"y": 3}} tail', "no json", "[1, 2]", None,
        '{"a": 1} and {"b": 2}', '{"bad": }')]
    cases += [["fnv1a", [t]] for t in ("", "fingerprint", "iq_logic", "中文")]
    cases += [["perms4", []]]
    tool_ch = VM.build_challenge("tool", 5)
    cases += [["toolPayload", [pr, "m-1", tool_ch]] for pr in PROTOS]
    cases += [["cachePayload", [pr, "m-1", "PREFIX", "Q?"]] for pr in PROTOS]
    cases += [["inputTokens", [pr, SAMPLES[pr]]] for pr in PROTOS] + [["inputTokens", ["openai", None]]]
    ff = {"status": "fail", "evidence": {"http_status": 401, "error": "unauthorized"}}
    cases += [["failFast", [r]] for r in (ff, {"status": "fail", "evidence": {"http_status": 500, "error": "x"}},
                                          {"status": "pass", "evidence": {"http_status": 401}}, None)]
    cases += [["failFastReason", [ff, ["thinking", "tool"]]]]
    return cases


FUZZ_SEEDS = [0, 1, 42, 2147483647, 2147483648, 4294967295] + [(i * 2654435761 + 12345) % 2 ** 32 for i in range(1, 55)]


def good_constraints(ch):
    words = ["b" * (k + 2) for k in range(ch["n"])]
    if ch["order"] == "descending":
        words.reverse()
    return {ch["list_key"]: words, ch["count_key"]: ch["n"]}


def fuzz_answers(cid, ch):
    """Good and bad answers for one seeded challenge (the grader must be exact on both)."""
    if cid == "iq_trace":
        a = ch["answer"]
        bad = dict(a, checksum=a["checksum"] + 1)
        half = dict(a, a=a["a"] + 1, b=a["b"] + 1)
        return [json.dumps(a), "```json\n" + json.dumps(a) + "\n```", json.dumps(bad), json.dumps(half), "I think 42"]
    if cid == "iq_logic":
        sol = ch["solution"]
        names = list(sol)
        swapped = json.loads(json.dumps(sol))
        swapped[names[0]], swapped[names[1]] = swapped[names[1]], swapped[names[0]]
        return [json.dumps(sol), "Answer:\n" + json.dumps(sol, indent=1), json.dumps(swapped), "{}"]
    if cid == "iq_constraints":
        good = good_constraints(ch)
        lk, ck = ch["list_key"], ch["count_key"]
        letter = dict(good, **{lk: [w[:-1] + ch["letter"] for w in good[lk]]})
        count = dict(good, **{ck: ch["n"] + 1})
        flat = dict(good, **{lk: ["bbb"] * ch["n"]})
        extra = dict(good, extra=1)
        return [json.dumps(good), json.dumps(letter), json.dumps(count), json.dumps(flat), json.dumps(extra), "words"]
    return []


def build_fuzz_cases():
    cases = []
    for seed in FUZZ_SEEDS:
        cases.append(["rngSequence", [seed, 6]])
        rnd = random.Random(seed)
        for cid in VM.ALL_CHECKS:
            cases.append(["deriveSeed", [seed, cid]])
            cases.append(["buildChallenge", [cid, seed, 4000]])
            ch = VM.build_challenge(cid, seed, 4000)
            if cid == "iq_logic":
                cases.append(["solveLogic", [ch["clues"]]])
            if cid == "context_hop":
                slim = {k: v for k, v in ch.items() if k != "prompt"}
                est = ch["est_tokens"]
                for text, pt in ((f"The final value is {ch['answer']}.", est), (ch["answer"], est // 3),
                                 (ch["answer"], est * 3), (ch["decoys"][0], None), ("unknown", est)):
                    cases.append(["gradeContextHop", [slim, text, pt]])
                    cases.append(["iqSummary", [cid, VM.grade_context_hop(slim, text, pt)]])
                continue
            fn = {"iq_trace": "gradeIqTrace", "iq_logic": "gradeIqLogic", "iq_constraints": "gradeIqConstraints"}.get(cid)
            for text in fuzz_answers(cid, ch):
                cases.append([fn, [ch, text]])
                cases.append(["iqSummary", [cid, VM.grade_iq(cid, ch, text)]])
        results = []
        for cid in rnd.sample(VM.ALL_CHECKS, rnd.randint(1, 8)):
            res = {"id": cid, "status": rnd.choice(["pass", "pass", "warn", "fail"])}
            if cid == "thinking" and rnd.random() < 0.3:
                res["evidence"] = {"tamper_result": "accepted"}
            results.append(res)
        cases.append(["summarize", [results]])
    for seed in FUZZ_SEEDS[:3]:
        for tokens in (12000, 28000):
            cases.append(["buildChallenge", ["context_hop", seed, tokens]])
    return cases


def run_js_cases(cases):
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        shutil.copy(JS_SRC, tdp / "verify.mjs")
        (tdp / "harness.mjs").write_text(HARNESS, encoding="utf-8")
        (tdp / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
        r = subprocess.run([NODE, str(tdp / "harness.mjs"), str(tdp / "cases.json")],
                           capture_output=True, text=True, encoding="utf-8", cwd=td)
    if r.returncode != 0:
        raise AssertionError(f"node harness failed: {r.stderr}")
    return json.loads(r.stdout)


@unittest.skipUnless(NODE, "node is required for JS parity tests")
class VerifyParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = build_cases()
        cls.js = run_js_cases(cls.cases)

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


@unittest.skipUnless(NODE, "node is required for JS parity fuzz tests")
class VerifyFuzzParityTests(unittest.TestCase):
    """Property / fuzz parity over 60 seeds: identical puzzles, solver output, grades and dual-score verdicts."""

    @classmethod
    def setUpClass(cls):
        cls.cases = build_fuzz_cases()
        cls.js = run_js_cases(cls.cases)

    def test_fuzz_covers_at_least_50_seeds_and_all_generators(self):
        self.assertGreaterEqual(len(set(FUZZ_SEEDS)), 50)
        built = {(a[0], a[1]) for fn, a in self.cases if fn == "buildChallenge"}
        for seed in FUZZ_SEEDS:
            for cid in VM.ALL_CHECKS:
                self.assertIn((cid, seed), built)

    def test_every_fuzz_vector_matches(self):
        self.assertEqual(len(self.js), len(self.cases))
        mismatches = []
        for (fn, args), js_val in zip(self.cases, self.js):
            py_val = json.loads(json.dumps(PY[fn](*args)))
            if py_val != js_val:
                mismatches.append((fn, json.dumps(args, ensure_ascii=False)[:160]))
        self.assertEqual(mismatches, [])

    def test_js_logic_solver_finds_exactly_one_solution(self):
        for (fn, args), js_val in zip(self.cases, self.js):
            if fn == "solveLogic":
                self.assertEqual(js_val["count"], 1)

    def test_js_dual_scores_follow_non_compensatory_rule(self):
        seen_levels = set()
        for (fn, args), js_val in zip(self.cases, self.js):
            if fn != "summarize":
                continue
            seen_levels.add(js_val["level"])
            low = lambda v: v is not None and v < 55  # noqa: E731
            if js_val["tamper_accepted"] or low(js_val["auth_score"]) or low(js_val["iq_score"]):
                self.assertEqual(js_val["level"], "fail")
        self.assertIn("fail", seen_levels)


def independent_logic_count(ch):
    """Brute force over all 24^3 assignments with an evaluator written separately from verify_model."""
    names, attrs, values = ch["names"], ch["attrs"], ch["values"]

    def holds(cl, owner):
        t = cl["t"]
        if t in ("pos", "neg"):
            same = owner[cl["a"]][cl["v"]] == cl["p"]
            return same if t == "pos" else not same
        person = owner[cl["a"]][cl["v"]]
        same = owner[cl["b"]][cl["w"]] == person
        return same if t == "link" else not same

    hits = []
    perms = list(itertools.permutations(range(4)))
    for combo in itertools.product(perms, repeat=3):
        owner = [{v: person for person, v in enumerate(pa)} for pa in combo]
        if all(holds(cl, owner) for cl in ch["clues"]):
            hits.append(combo)
    sol = None
    if len(hits) == 1:
        sol = {names[i]: {attrs[a]: values[a][hits[0][a][i]] for a in range(3)} for i in range(4)}
    return len(hits), sol


class VerifyGeneratorPropertyTests(unittest.TestCase):
    def test_reference_vectors_are_stable(self):
        self.assertEqual(VM.rng_sequence(1, 3), [2693262067, 11749833, 2265367787])
        self.assertEqual(VM.build_challenge("fingerprint", 42)["answer"], 5161)
        self.assertEqual(VM.build_challenge("iq_trace", 7)["answer"], VM.build_challenge("iq_trace", 7)["answer"])
        self.assertNotEqual(VM.build_challenge("fingerprint", 1)["prompt"], VM.build_challenge("fingerprint", 2)["prompt"])

    def test_tier_presets(self):
        self.assertEqual(VM.TIERS["l1_auth"], ["fingerprint", "thinking", "tool", "cache"])
        self.assertEqual(VM.TIERS["l2_iq"], ["fingerprint", "thinking", "tool", "cache", "iq_trace", "iq_logic", "iq_constraints"])
        self.assertEqual(VM.TIERS["l3_deep"], VM.ALL_CHECKS)
        self.assertEqual(len(VM.ALL_CHECKS), 8)
        self.assertEqual(VM.parse_checks(None, "l3_deep"), VM.ALL_CHECKS)
        self.assertEqual(VM.parse_checks(["context_hop", "fingerprint"], "l1_auth"), ["fingerprint", "context_hop"])

    def test_no_static_prompts_remain(self):
        for name in ("MATH_Q", "THINK_Q1", "TOOL_MSG", "CACHE_PREFIX"):
            self.assertFalse(isinstance(getattr(VM, name, None), str), name)
        js = JS_SRC.read_text(encoding="utf-8")
        for name in ("const MATH_Q", "const THINK_Q1", "const TOOL_MSG", "const CACHE_PREFIX"):
            self.assertNotIn(name, js)
        prompts = {VM.build_challenge(c, s)["prompt" if c not in ("thinking", "tool", "cache") else
                                         {"thinking": "q1", "tool": "message", "cache": "prefix"}[c]]
                   for c in VM.AUTH_CHECKS for s in FUZZ_SEEDS[:10]}
        self.assertEqual(len(prompts), 40)

    def test_iq_trace_answer_matches_independent_execution(self):
        for seed in FUZZ_SEEDS:
            ch = VM.build_challenge("iq_trace", seed)
            env = {}
            exec(ch["program"], {"__builtins__": {}}, env)  # noqa: S102 - generated integer-only program
            got = {k: env[k] for k in ("a", "b", "c", "checksum")}
            self.assertEqual(got, ch["answer"], seed)
            self.assertEqual(len(ch["steps"]), 6)
            self.assertIn("^", ch["program"])
            self.assertIn("%", ch["program"])
            self.assertIn("if ", ch["program"])
            for v in got.values():
                self.assertTrue(-(2 ** 31) <= v < 2 ** 31)
            self.assertEqual(VM.grade_iq_trace(ch, json.dumps(got))["status"], "pass")

    def test_iq_logic_unique_solution_matches_independent_solver(self):
        for seed in FUZZ_SEEDS:
            ch = VM.build_challenge("iq_logic", seed)
            res = VM.solve_logic(ch["clues"])
            self.assertEqual(res["count"], 1, seed)
            self.assertEqual(res["solution"], ch["hidden"], seed)
            self.assertEqual(len(ch["names"]), 4)
            self.assertEqual(len(ch["attrs"]), 3)
            self.assertEqual(VM.grade_iq_logic(ch, json.dumps(ch["solution"]))["status"], "pass")
        for seed in FUZZ_SEEDS[:6]:
            ch = VM.build_challenge("iq_logic", seed)
            count, sol = independent_logic_count(ch)
            self.assertEqual(count, 1, seed)
            self.assertEqual(sol, ch["solution"], seed)

    def test_iq_constraints_grades_each_rule_independently(self):
        for seed in FUZZ_SEEDS:
            ch = VM.build_challenge("iq_constraints", seed)
            good = good_constraints(ch)
            g = VM.grade_iq_constraints(ch, json.dumps(good))
            self.assertEqual((g["status"], g["passed"], g["broken"]), ("pass", 4, []), seed)
            lk, ck = ch["list_key"], ch["count_key"]
            broken = {
                "keys": dict(good, extra=1),
                "exact_length": dict(good, **{ck: ch["n"] + 1}),
                "forbidden_letter": dict(good, **{lk: [ch["letter"] * len(w) for w in good[lk]]}),
                "length_order": dict(good, **{lk: list(reversed(good[lk]))}),
            }
            for rule, obj in broken.items():
                g = VM.grade_iq_constraints(ch, json.dumps(obj))
                self.assertEqual((g["status"], g["broken"]), ("warn", [rule]), (seed, rule))
            self.assertEqual(VM.grade_iq_constraints(ch, "no json")["status"], "fail")

    def test_context_hop_chain_is_planted_in_order_at_expected_depths(self):
        for seed in FUZZ_SEEDS:
            for tokens in (4000, 12000):
                ch = VM.build_challenge("context_hop", seed, tokens)
                prompt, chain = ch["prompt"], ch["chain"]
                self.assertEqual(len(chain), 4)
                self.assertTrue(chain[-1].startswith("FINAL_CODE_"))
                self.assertEqual(ch["answer"], chain[-1])
                pos = []
                for i in range(3):
                    link = f"pointer {chain[i]} resolves to {chain[i + 1]}"
                    self.assertEqual(prompt.count(link), 1, (seed, link))
                    self.assertEqual(prompt.count(f"pointer {chain[i]} resolves to "), 1, (seed, i))
                    pos.append(prompt.index(link) * 100 / len(prompt))
                self.assertEqual(pos, sorted(pos))
                for got, want in zip(pos, ch["depth_pct"]):
                    self.assertLess(abs(got - want), 6, (seed, pos))
                self.assertEqual(prompt.count(ch["answer"]), 1)
                for d in ch["decoys"]:
                    self.assertNotEqual(d, ch["answer"])
                self.assertTrue(abs(ch["est_tokens"] - tokens) / tokens < 0.15, (seed, ch["est_tokens"]))
                self.assertEqual(VM.grade_context_hop(ch, ch["answer"], ch["est_tokens"])["status"], "pass")
                self.assertEqual(VM.grade_context_hop(ch, ch["answer"], ch["est_tokens"] // 3)["token_flag"], "truncation")
        self.assertEqual(VM.clamp_context_tokens(10 ** 9, 4000, 64000, 16000), 64000)
        self.assertEqual(VM.clamp_context_tokens(10 ** 9), 28000)


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
        seed = 2026
        ch = VM.build_challenge("thinking", seed)

        def fake(url, headers, payload, api_key, timeout=24.0):
            calls.append(payload)
            msgs = payload["messages"]
            if len(msgs) == 1:
                self.assertEqual(msgs[0]["content"], ch["q1"])
                return {"ok": True, "status": 200, "ms": 900, "ttfb_ms": 880, "headers": {},
                        "data": {"type": "message", "role": "assistant", "usage": {"input_tokens": 40, "output_tokens": 120},
                                 "content": [{"type": "thinking", "thinking": "x", "signature": SIG},
                                             {"type": "text", "text": str(ch["answer1"])}]}}
            sig = msgs[1]["content"][0]["signature"]
            if sig != SIG:
                return {"ok": False, "status": 400, "ms": 120, "ttfb_ms": 110, "headers": {},
                        "data": {"error": {"message": "Invalid signature in thinking block"}}}
            return {"ok": True, "status": 200, "ms": 700, "ttfb_ms": 690, "headers": {},
                    "data": {"type": "message", "usage": {"input_tokens": 90, "output_tokens": 60, "cache_read_input_tokens": 0},
                             "content": [{"type": "text", "text": str(ch["answer2"])}]}}

        orig = VM._post_json
        VM._post_json = fake
        try:
            res = VM.check_thinking("https://r.test", "sk-test-123", "claude-sonnet-5-5", "anthropic", seed=seed)
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
        seed = 77
        answer = str(VM.build_challenge("fingerprint", seed)["answer"])

        def fake(url, headers, payload, api_key, timeout=24.0):
            seen.append(url)
            if ":generateContent" in url:
                return {"ok": False, "status": 404, "ms": 50, "ttfb_ms": 50, "headers": {}, "data": {"error": "not found"}}
            return {"ok": True, "status": 200, "ms": 300, "ttfb_ms": 290, "headers": {},
                    "data": {"id": "chatcmpl-abc", "object": "chat.completion", "model": "gemini-4-carbon",
                             "choices": [{"message": {"content": answer}}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}}

        orig = VM._post_json
        VM._post_json = fake
        try:
            out = VM.run_suite("https://r.test", "sk-test-123", "gemini-4-carbon", "auto", ["fingerprint"], seed=seed)
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
import { onRequestPost, buildChallenge } from './verify.mjs';
const SIG = 'EuYBCkQYAiJA'.repeat(8);
const KEY = 'sk-live-SECRET-123456';
const SEED = 20261127;
const CTX = 4000;
const ch = (id) => buildChallenge(id, SEED, CTX);
const seen = [];
const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { 'content-type': 'application/json' } });
const msg = (text, usage = { input_tokens: 20, output_tokens: 8 }) =>
  json({ id: 'msg_01AbCdEfGh123', type: 'message', role: 'assistant', model: 'claude-sonnet-5-5-20261001', content: [{ type: 'text', text }], usage });
const goodConstraints = (c) => {
  const words = Array.from({ length: c.n }, (_, k) => 'b'.repeat(k + 2));
  if (c.order === 'descending') words.reverse();
  return JSON.stringify({ [c.list_key]: words, [c.count_key]: c.n });
};
globalThis.fetch = async (url, opts) => {
  seen.push({ url, redirect: opts.redirect });
  const host = new URL(url).hostname;
  if (host === 'dead.test') return json({ error: { message: 'invalid key ' + KEY } }, 401);
  const weak = host === 'weak.test';
  const body = JSON.parse(opts.body);
  if (body.tools) {
    const t = ch('tool');
    return json({ type: 'message', role: 'assistant', content: [{ type: 'tool_use', name: t.name, input: { [t.region_key]: t.region, [t.code_key]: t.code } }], usage: { input_tokens: 9, output_tokens: 9 } });
  }
  if (body.system) return json({ error: { message: 'cache disabled for key ' + KEY } }, 403);
  if (body.thinking) {
    const t = ch('thinking');
    if (body.messages.length === 1) return json({ type: 'message', role: 'assistant', usage: { input_tokens: 40, output_tokens: 120 }, content: [{ type: 'thinking', thinking: 'x', signature: SIG }, { type: 'text', text: String(t.answer1) }] });
    if (host !== 'forge.test' && body.messages[1].content[0].signature !== SIG) return json({ type: 'error', error: { type: 'invalid_request_error', message: 'Invalid signature in thinking block' } }, 400);
    return json({ type: 'message', role: 'assistant', usage: { input_tokens: 95, output_tokens: 40 }, content: [{ type: 'text', text: String(t.answer2) }] });
  }
  const prompt = String(body.messages[body.messages.length - 1].content);
  if (prompt.startsWith('Trace ')) return msg(weak ? '{"a": 1, "b": 2, "c": 3, "checksum": 0}' : JSON.stringify(ch('iq_trace').answer));
  if (prompt.startsWith('Logic puzzle ')) return msg(weak ? '{}' : JSON.stringify(ch('iq_logic').solution));
  if (prompt.startsWith('Task ')) return msg(weak ? 'Here are some words: cat, dog' : goodConstraints(ch('iq_constraints')));
  if (prompt.startsWith('Operations log export ')) {
    const c = ch('context_hop');
    return msg(weak ? c.decoys[0] : c.answer, { input_tokens: weak ? Math.floor(c.est_tokens / 4) : c.est_tokens, output_tokens: 9 });
  }
  return msg(String(ch('fingerprint').answer));
};
const post = (b) => onRequestPost({ request: new Request('https://agentied.work/api/verify', { method: 'POST', body: JSON.stringify(b) }) });
const base = { api_key: KEY, model: 'claude-sonnet-5-5', seed: SEED };
const out = {};
for (const bad of ['https://127.0.0.1', 'https://relay.test:8443', 'http://relay.test', 'https://[::1]', 'https://2130706433']) {
  out[bad] = (await post({ ...base, base_url: bad })).status;
}
out.bad_proto = (await post({ ...base, base_url: 'https://relay.test', protocol: 'grpc' })).status;
out.bad_model = (await post({ ...base, base_url: 'https://relay.test', model: 'a/../b' })).status;
out.bad_tier = (await post({ ...base, base_url: 'https://relay.test', tier: 'bogus' })).status;
out.bad_seed = (await post({ ...base, base_url: 'https://relay.test', seed: -1 })).status;
out.bad_seed_str = (await post({ ...base, base_url: 'https://relay.test', seed: 'abc' })).status;
out.upstream_calls_for_rejected = seen.length;
const run = async (name, b) => {
  const before = seen.length;
  const res = await post({ ...base, ...b });
  const text = await res.text();
  out[name] = { status: res.status, leaks_key: text.includes(KEY), report: JSON.parse(text), fetches: seen.length - before };
};
await run('l1', { base_url: 'https://relay.test/v1', tier: 'l1_auth' });
out.l1_urls = [...new Set(seen.map((s) => s.url))];
await run('l2', { base_url: 'https://relay.test/v1', tier: 'l2_iq' });
await run('l3', { base_url: 'https://relay.test', tier: 'l3_deep', context_tokens: CTX });
await run('weak', { base_url: 'https://weak.test', tier: 'l3_deep', context_tokens: CTX });
await run('forge', { base_url: 'https://forge.test', tier: 'l1_auth' });
await run('dead', { base_url: 'https://dead.test', tier: 'l2_iq', context_tokens: 999999 });
await run('custom', { base_url: 'https://relay.test', checks: ['iq_logic', 'fingerprint'] });
await run('default', { base_url: 'https://relay.test' });
out.all_redirect_error = seen.every((s) => s.redirect === 'error');
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is required for JS end-to-end tests")
class VerifyEdgeFunctionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            shutil.copy(JS_SRC, tdp / "verify.mjs")
            (tdp / "e2e.mjs").write_text(E2E_HARNESS, encoding="utf-8")
            r = subprocess.run([NODE, str(tdp / "e2e.mjs")], capture_output=True, text=True, encoding="utf-8", cwd=td)
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        cls.out = json.loads(r.stdout)

    def _report(self, name):
        run = self.out[name]
        self.assertEqual(run["status"], 200)
        self.assertFalse(run["leaks_key"])
        rep = run["report"]
        self.assertEqual(rep["seed"], 20261127)
        return rep, {x["id"]: x for x in rep["results"]}

    def test_rejects_unsafe_or_invalid_input_without_upstream_calls(self):
        out = self.out
        for bad in ("https://127.0.0.1", "https://relay.test:8443", "http://relay.test", "https://[::1]", "https://2130706433"):
            self.assertEqual(out[bad], 400, bad)
        for k in ("bad_proto", "bad_model", "bad_tier", "bad_seed", "bad_seed_str"):
            self.assertEqual(out[k], 400, k)
        self.assertEqual(out["upstream_calls_for_rejected"], 0)
        self.assertTrue(out["all_redirect_error"])

    def test_l1_auth_tier(self):
        rep, by_id = self._report("l1")
        self.assertEqual(self.out["l1_urls"], ["https://relay.test/v1/messages"])
        self.assertEqual(rep["tier"], "l1_auth")
        self.assertEqual(rep["checks"], VM.TIERS["l1_auth"])
        self.assertEqual(rep["protocol"], "anthropic")
        self.assertEqual(by_id["fingerprint"]["status"], "pass")
        self.assertEqual(by_id["thinking"]["status"], "pass")
        self.assertEqual(by_id["thinking"]["evidence"]["tamper_result"], "rejected")
        self.assertEqual(by_id["tool"]["status"], "pass")
        self.assertEqual(by_id["cache"]["status"], "fail")
        self.assertIn("***", by_id["cache"]["evidence"]["error"])
        self.assertEqual((rep["score"], rep["auth_score"], rep["iq_score"]), (75, 75, None))
        self.assertEqual(rep["level"], "fail")
        self.assertFalse(rep["tamper_accepted"])
        self.assertEqual(rep["upstream_calls"], self.out["l1"]["fetches"])
        for x in rep["results"]:
            self.assertIn("telemetry", x)

    def test_l2_iq_tier_scores_intelligence_separately(self):
        rep, by_id = self._report("l2")
        self.assertEqual(rep["checks"], VM.TIERS["l2_iq"])
        for cid in ("iq_trace", "iq_logic", "iq_constraints"):
            self.assertEqual(by_id[cid]["status"], "pass", cid)
        self.assertEqual(rep["iq_score"], 100)
        self.assertEqual(rep["auth_score"], 75)
        self.assertEqual(rep["veto"], [])
        self.assertEqual(rep["upstream_calls"], self.out["l2"]["fetches"])

    def test_l3_deep_tier_includes_context_hop(self):
        rep, by_id = self._report("l3")
        self.assertEqual(rep["checks"], VM.ALL_CHECKS)
        self.assertEqual(rep["context_tokens"], 4000)
        self.assertEqual(by_id["context_hop"]["status"], "pass")
        self.assertIsNone(by_id["context_hop"]["evidence"]["token_flag"])
        self.assertEqual(rep["iq_score"], 100)

    def test_weak_model_is_vetoed_even_when_authenticity_is_fine(self):
        rep, by_id = self._report("weak")
        for cid in VM.IQ_CHECKS:
            self.assertEqual(by_id[cid]["status"], "fail", cid)
        self.assertEqual(by_id["context_hop"]["evidence"]["token_flag"], "truncation")
        self.assertEqual(rep["iq_score"], 0)
        self.assertEqual(rep["level"], "fail")
        self.assertIn("iq_score_below_55", rep["veto"])
        self.assertIn("智商", rep["verdict"])

    def test_accepted_tampered_signature_is_non_compensatory_fail(self):
        rep, by_id = self._report("forge")
        self.assertEqual(by_id["thinking"]["evidence"]["tamper_result"], "accepted")
        self.assertTrue(rep["tamper_accepted"])
        self.assertIn("tampered_signature_accepted", rep["veto"])
        self.assertEqual(rep["level"], "fail")

    def test_fail_fast_skips_remaining_checks(self):
        rep, by_id = self._report("dead")
        self.assertEqual(self.out["dead"]["fetches"], 1)
        self.assertEqual(rep["upstream_calls"], 1)
        self.assertEqual(list(by_id), ["fingerprint"])
        self.assertEqual(rep["skipped"], VM.TIERS["l2_iq"][1:])
        self.assertTrue(rep["fail_fast"])
        self.assertEqual(rep["context_tokens"], 28000)
        self.assertEqual(rep["level"], "fail")

    def test_custom_checks_and_default_tier(self):
        rep, _ = self._report("custom")
        self.assertEqual(rep["tier"], "custom")
        self.assertEqual(rep["checks"], ["fingerprint", "iq_logic"])
        rep, _ = self._report("default")
        self.assertEqual(rep["tier"], "l2_iq")
        self.assertEqual(rep["context_tokens"], 12000)

    def test_python_cli_matches_edge_report_on_same_seed(self):
        seed = 20261127
        js_rep, _ = self._report("l2")

        def fake(url, headers, payload, api_key, timeout=24.0):
            ok = lambda d: {"ok": True, "status": 200, "ms": 100, "ttfb_ms": 90, "headers": {}, "data": d}  # noqa: E731
            text = lambda t: ok({"id": "msg_01AbCdEfGh123", "type": "message", "role": "assistant",  # noqa: E731
                                 "model": "claude-sonnet-5-5-20261001", "content": [{"type": "text", "text": t}],
                                 "usage": {"input_tokens": 20, "output_tokens": 8}})
            if payload.get("tools"):
                t = VM.build_challenge("tool", seed)
                return ok({"type": "message", "role": "assistant", "usage": {"input_tokens": 9, "output_tokens": 9},
                           "content": [{"type": "tool_use", "name": t["name"],
                                        "input": {t["region_key"]: t["region"], t["code_key"]: t["code"]}}]})
            if payload.get("system"):
                return {"ok": False, "status": 403, "ms": 50, "ttfb_ms": 50, "headers": {},
                        "data": {"error": {"message": "cache disabled"}}}
            if payload.get("thinking"):
                t = VM.build_challenge("thinking", seed)
                msgs = payload["messages"]
                if len(msgs) == 1:
                    return ok({"type": "message", "role": "assistant", "usage": {"input_tokens": 40, "output_tokens": 120},
                               "content": [{"type": "thinking", "thinking": "x", "signature": SIG},
                                           {"type": "text", "text": str(t["answer1"])}]})
                if msgs[1]["content"][0]["signature"] != SIG:
                    return {"ok": False, "status": 400, "ms": 50, "ttfb_ms": 50, "headers": {},
                            "data": {"type": "error", "error": {"type": "invalid_request_error",
                                                                "message": "Invalid signature in thinking block"}}}
                return ok({"type": "message", "role": "assistant", "usage": {"input_tokens": 95, "output_tokens": 40},
                           "content": [{"type": "text", "text": str(t["answer2"])}]})
            prompt = str(payload["messages"][-1]["content"])
            if prompt.startswith("Trace "):
                return text(json.dumps(VM.build_challenge("iq_trace", seed)["answer"]))
            if prompt.startswith("Logic puzzle "):
                return text(json.dumps(VM.build_challenge("iq_logic", seed)["solution"]))
            if prompt.startswith("Task "):
                return text(json.dumps(good_constraints(VM.build_challenge("iq_constraints", seed))))
            return text(str(VM.build_challenge("fingerprint", seed)["answer"]))

        orig = VM._post_json
        VM._post_json = fake
        try:
            py_rep = VM.run_suite("https://relay.test/v1", "sk-test-123456", "claude-sonnet-5-5", "auto", tier="l2_iq", seed=seed)
        finally:
            VM._post_json = orig
        for k in ("tier", "checks", "seed", "protocol", "score", "auth_score", "iq_score", "level", "verdict", "veto",
                  "tamper_accepted", "passed", "total", "upstream_calls"):
            self.assertEqual(py_rep[k], js_rep[k], k)
        self.assertEqual({x["id"]: x["status"] for x in py_rep["results"]},
                         {x["id"]: x["status"] for x in js_rep["results"]})


if __name__ == "__main__":
    unittest.main()
