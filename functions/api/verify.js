/**
 * Stateless LLM Model Authenticity Verification API (Cloudflare Pages Function: /api/verify).
 *
 * Security & privacy guarantees:
 * - Never logs, persists, or echoes back user-supplied `api_key` (upstream bodies are redacted before parsing).
 * - Strict HTTPS + public hostname SSRF guard: blocks loopback / RFC1918 / link-local names, every IP literal
 *   form (dotted, integer, octal, hex, IPv6), userinfo, wildcard-DNS rebinding hosts and any non-443 port.
 * - Upstream fetches use `redirect: "error"` (no 30x hop to an internal target) with bounded timeouts and a
 *   64 KB response cap.
 *
 * Technical checks (kept 1:1 in sync with tools/verify_model.py):
 * 1. fingerprint: response shape, model echo (normalized), ID format, deterministic micro-math + timing telemetry.
 * 2. thinking:    Anthropic: thinking signature + Turn-2 genuine round-trip + tampered-signature negative check
 *                 + usage consistency. OpenAI / DeepSeek / Gemini: native reasoning telemetry + usage sanity.
 * 3. tool:        Strict typed Tool-Calling schema conformance (structured tool_use / tool_calls / functionCall).
 * 4. cache:       2-turn ~1,300-token deterministic Prompt Cache read hit verification.
 */

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Cache-Control": "no-store",
};

export const PROTOCOLS = ["auto", "anthropic", "openai-response", "openai", "gemini"];
export const ALL_CHECKS = ["fingerprint", "thinking", "tool", "cache"];
const MAX_UPSTREAM_BYTES = 65536;
const MAX_REQUEST_BYTES = 8192;
const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const BLOCKED_SUFFIXES = [
  ".local", ".localhost", ".internal", ".intranet", ".lan", ".home", ".corp", ".localdomain", ".home.arpa",
  ".nip.io", ".sslip.io", ".xip.io", ".localtest.me", ".lvh.me",
];
const BLOCKED_HOSTS = ["localhost", "metadata.google.internal", "nip.io", "sslip.io", "localtest.me", "lvh.me"];
const MATH_Q = "Compute (37 * 43) - 891. Reply with the single integer only, no explanation.";
const THINK_Q1 = "Compute (19 * 23) + 17. Reply with the integer only.";
const THINK_Q2 = "Add 6 to your previous integer result. Reply with the integer only.";
const TOOL_MSG = 'Call the tool lookup_audit_code with region="CN" and code=4096. Do not answer in plain text.';
const TOOL_DESC = "Lookup an audit verification record by region and numeric code.";

const CACHE_PREFIX = (() => {
  const lines = [
    "You are a strict compliance auditor reviewing API relay technical specifications.",
    "Memorize the following numbered policy clauses and answer ONLY with the requested clause route code.",
  ];
  for (let i = 1; i <= 60; i++) {
    const n = String(i).padStart(3, "0");
    lines.push(
      `Clause ${n}: Relay gateway route R-${n} enforces UTF-8 JSON framing, monotonic request IDs, ` +
        `explicit latency accounting in milliseconds, non-negative token usage fields, and deterministic error codes.`
    );
  }
  return lines.join("\n");
})();

function jsonRes(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS },
  });
}

// ------------------------------------------------------------------ pure helpers (mirrored in verify_model.py)

export function isBlockedHost(hostname) {
  const h = String(hostname || "").toLowerCase().trim().replace(/\.$/, "");
  if (!h || h.length > 253) return true;
  if (/[\[\]:]/.test(h)) return true;
  if (!/^[a-z0-9.-]+$/.test(h)) return true;
  const labels = h.split(".");
  if (labels.length < 2) return true;
  if (labels.some((l) => !l || l.length > 63 || l.startsWith("-") || l.endsWith("-"))) return true;
  if (BLOCKED_HOSTS.includes(h) || BLOCKED_SUFFIXES.some((s) => h.endsWith(s))) return true;
  const tld = labels[labels.length - 1];
  if (/^(?:\d+|0x[0-9a-f]*)$/.test(tld)) return true;
  if (labels.every((l) => /^(?:\d+|0x[0-9a-f]*)$/.test(l))) return true;
  return false;
}

export function normalizeBaseUrl(raw) {
  const s = String(raw || "").trim();
  if (!s || s.length > 300 || /\s/.test(s)) return null;
  const hasScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(s);
  if (hasScheme && !/^https:\/\//i.test(s)) return null;
  let u;
  try {
    u = new URL(hasScheme ? s : `https://${s}`);
  } catch {
    return null;
  }
  if (u.protocol !== "https:" || u.username || u.password) return null;
  if (u.port && u.port !== "443") return null;
  if (isBlockedHost(u.hostname)) return null;
  let path = u.pathname.replace(/\/+$/, "");
  path = path.replace(/\/v1\/(?:messages|chat\/completions|responses)$/i, "");
  path = path.replace(/\/v1beta(?:\/models(?:\/[^/]*)?)?$/i, "");
  path = path.replace(/\/v1$/i, "");
  if (!/^[A-Za-z0-9._~\/%-]*$/.test(path)) return null;
  return `https://${u.hostname.toLowerCase()}${path}`;
}

export function isValidModel(model) {
  return /^[A-Za-z0-9][A-Za-z0-9._:@\/-]{0,119}$/.test(String(model || "")) && !String(model).includes("..");
}

export function detectProtocol(model, requested) {
  if (requested && requested !== "auto") return requested;
  const m = String(model || "").toLowerCase().split("/").pop();
  if (m.includes("claude") || m.includes("fable")) return "anthropic";
  if (m.includes("gemini")) return "gemini";
  if (/^(?:gpt-6|gpt-5|o3|o4|codex)/.test(m)) return "openai-response";
  return "openai";
}

export function normalizeModelId(s) {
  let x = String(s || "").toLowerCase().trim();
  x = x.replace(/^models\//, "").split("/").pop();
  x = x.replace(/^(?:[a-z]{2}\.)?anthropic\./, "");
  x = x.replace(/[@:_\s]+/g, "-").replace(/\./g, "-");
  let prev;
  do {
    prev = x;
    x = x.replace(/-v\d+-\d+$/, "");
    x = x.replace(/-\d{4}-\d{2}-\d{2}$/, "");
    x = x.replace(/-(?:preview|exp)(?:-\d{2}-\d{2})?$/, "");
    x = x.replace(/-(?:\d{6,8}|latest|thinking)$/, "");
  } while (x !== prev);
  return x;
}

export function modelEchoMatches(requested, returned) {
  if (!returned) return false;
  const a = normalizeModelId(requested);
  const b = normalizeModelId(returned);
  return Boolean(a) && a === b;
}

export function redact(text, apiKey) {
  const t = String(text == null ? "" : text);
  const k = String(apiKey || "");
  return k.length >= 6 ? t.split(k).join("***") : t;
}

export function tamperSignature(sig) {
  const s = String(sig || "");
  const mid = Math.floor(s.length / 2);
  let out = "";
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    const k = B64.indexOf(c);
    out += i >= mid - 4 && i < mid + 4 && k >= 0 ? B64[(k + 17) % 64] : c;
  }
  return out === s ? `${s.slice(0, -1)}${s.endsWith("A") ? "B" : "A"}` : out;
}

export function classifyTamper(status, data) {
  if (status >= 200 && status < 300) return "accepted";
  if (!status || status === 429 || status >= 500) return "inconclusive";
  const msg = JSON.stringify(data || {}).toLowerCase();
  if (status === 400 && /signature|thinking|invalid/.test(msg)) return "rejected";
  return "rejected_other";
}

function isCount(v) {
  return typeof v === "number" && Number.isInteger(v) && v >= 0;
}

function totalIn(u) {
  const n = (v) => (isCount(v) ? v : 0);
  return n(u.input_tokens) + n(u.cache_read_input_tokens) + n(u.cache_creation_input_tokens);
}

export function anthropicUsageCheck(u1, u2) {
  const notes = [];
  if (!u1 || !isCount(u1.input_tokens) || !isCount(u1.output_tokens)) {
    notes.push("首轮 usage 缺少整数 input_tokens / output_tokens");
  } else {
    if (totalIn(u1) <= 0) notes.push("首轮 input_tokens 为 0");
    if (u1.output_tokens <= 0) notes.push("首轮 output_tokens 为 0（开启 thinking 时不应为 0）");
  }
  for (const u of [u1, u2]) {
    if (!u) continue;
    for (const k of ["cache_read_input_tokens", "cache_creation_input_tokens"]) {
      if (u[k] != null && !isCount(u[k])) notes.push(`${k} 不是非负整数`);
    }
  }
  if (u1 && u2 && isCount(u1.input_tokens) && isCount(u2.input_tokens) && totalIn(u2) <= totalIn(u1)) {
    notes.push("二轮输入 token 未随多轮上下文增长（疑似估算或伪造 usage）");
  }
  return { ok: notes.length === 0, notes };
}

export function openaiUsageCheck(protocol, u) {
  const notes = [];
  if (!u || typeof u !== "object") return { ok: false, notes: ["未返回 usage 字段"] };
  if (protocol === "gemini") {
    if (!isCount(u.promptTokenCount) || !isCount(u.candidatesTokenCount ?? 0)) notes.push("usageMetadata 计数异常");
    else if (u.totalTokenCount != null && u.totalTokenCount < u.promptTokenCount + (u.candidatesTokenCount || 0)) {
      notes.push("totalTokenCount 小于输入与输出之和");
    }
    return { ok: notes.length === 0, notes };
  }
  const inK = protocol === "openai-response" ? "input_tokens" : "prompt_tokens";
  const outK = protocol === "openai-response" ? "output_tokens" : "completion_tokens";
  const det = protocol === "openai-response" ? u.output_tokens_details : u.completion_tokens_details;
  if (!isCount(u[inK]) || !isCount(u[outK])) {
    notes.push(`usage 缺少整数 ${inK} / ${outK}`);
    return { ok: false, notes };
  }
  if (u[inK] <= 0) notes.push(`${inK} 为 0`);
  if (u.total_tokens != null && u.total_tokens !== u[inK] + u[outK]) notes.push("total_tokens 与输入输出之和不一致");
  const rt = det && det.reasoning_tokens;
  if (rt != null && (!isCount(rt) || rt > u[outK])) notes.push("reasoning_tokens 超过输出 token 总数");
  return { ok: notes.length === 0, notes };
}

export function outputTokens(protocol, data) {
  const u = (data && (protocol === "gemini" ? data.usageMetadata : data.usage)) || {};
  let v;
  if (protocol === "gemini") v = (u.candidatesTokenCount || 0) + (u.thoughtsTokenCount || 0);
  else if (protocol === "openai") v = u.completion_tokens;
  else v = u.output_tokens;
  return isCount(v) ? v : null;
}

export function telemetry(r, protocol) {
  const out = outputTokens(protocol, r && r.data);
  const ms = r && r.ms;
  const tps = out && ms > 0 ? Math.round((out / (ms / 1000)) * 10) / 10 : null;
  return { ttfb_ms: r ? r.ttfb_ms ?? null : null, total_ms: ms ?? null, output_tokens: out, tok_per_sec: tps };
}

export function responseShapeOk(protocol, data) {
  if (!data || typeof data !== "object") return false;
  if (protocol === "anthropic") return data.type === "message" && data.role === "assistant" && Array.isArray(data.content);
  if (protocol === "openai-response") return data.object === "response" && Array.isArray(data.output);
  if (protocol === "gemini") return Array.isArray(data.candidates) && data.candidates.length > 0;
  return data.object === "chat.completion" && Array.isArray(data.choices);
}

export function responseIdOk(protocol, id) {
  if (typeof id !== "string" || !id) return false;
  if (protocol === "anthropic") return /^msg_[A-Za-z0-9]{8,}/.test(id);
  if (protocol === "openai-response") return /^resp_[A-Za-z0-9]{8,}/.test(id);
  if (protocol === "gemini") return /^[A-Za-z0-9_-]{8,}$/.test(id);
  return /^(?:chatcmpl-|cmpl-|msg_|gen-)/.test(id) || /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id);
}

export function reasoningSignal(protocol, data) {
  const d = data || {};
  let tokens = null;
  let item = false;
  let content = false;
  let sigLen = 0;
  if (protocol === "gemini") {
    const t = d.usageMetadata && d.usageMetadata.thoughtsTokenCount;
    tokens = isCount(t) ? t : null;
    const parts = (((d.candidates || [])[0] || {}).content || {}).parts || [];
    for (const p of parts) {
      if (p && p.thought === true) item = true;
      if (p && typeof p.thoughtSignature === "string") sigLen = Math.max(sigLen, p.thoughtSignature.length);
    }
  } else if (protocol === "openai-response") {
    const t = d.usage && d.usage.output_tokens_details && d.usage.output_tokens_details.reasoning_tokens;
    tokens = isCount(t) ? t : null;
    item = Array.isArray(d.output) && d.output.some((o) => o && o.type === "reasoning");
  } else {
    const t = d.usage && d.usage.completion_tokens_details && d.usage.completion_tokens_details.reasoning_tokens;
    tokens = isCount(t) ? t : null;
    const msg = ((d.choices || [])[0] || {}).message || {};
    content = Boolean((typeof msg.reasoning_content === "string" && msg.reasoning_content.trim()) ||
      (typeof msg.reasoning === "string" && msg.reasoning.trim()));
  }
  const strong = (tokens !== null && tokens > 0) || item || content || sigLen > 0;
  return { reasoning_tokens: tokens, reasoning_item: item, reasoning_content: content, thought_signature_len: sigLen, strong };
}

export function thinkingVerdict(v) {
  if (!v.has_block || v.signature_len < 40) return "fail";
  if (v.tamper === "accepted") return "fail";
  if (v.roundtrip_ok && v.tamper === "rejected" && v.usage_ok) return "pass";
  return "warn";
}

export function summarize(results) {
  const total = results.length || 1;
  const nPass = results.filter((x) => x.status === "pass").length;
  const nFail = results.filter((x) => x.status === "fail").length;
  const score = Math.round(
    results.reduce((acc, x) => acc + (x.status === "pass" ? 100 : x.status === "warn" ? 55 : 0), 0) / total
  );
  let level = "pass";
  let verdict = "高度符合原生协议与真机特征";
  if (nFail > 0 || score < 55) {
    level = "fail";
    verdict = "存在明显异常（疑似转译、接口不兼容、降级或套壳，建议核对下方原始证据）";
  } else if (nPass < results.length) {
    level = "warn";
    verdict = "基础调用正常，但部分高级特征缺失（如缓存未命中或缺少原生遥测字段，建议小额实测）";
  }
  return { score, level, verdict, passed: nPass, total: results.length };
}

export function extractText(protocol, data) {
  if (!data || typeof data !== "object") return "";
  if (protocol === "anthropic" && Array.isArray(data.content)) {
    return data.content.filter((b) => b && b.type === "text").map((b) => b.text || "").join("\n").trim();
  }
  if (protocol === "gemini") {
    const parts = (((data.candidates || [])[0] || {}).content || {}).parts || [];
    return parts.filter((p) => p && !p.thought && typeof p.text === "string").map((p) => p.text).join("\n").trim();
  }
  if (protocol === "openai-response") {
    if (typeof data.output_text === "string" && data.output_text) return data.output_text.trim();
    const parts = [];
    for (const item of Array.isArray(data.output) ? data.output : []) {
      if (item && item.type === "message" && Array.isArray(item.content)) {
        for (const c of item.content) if (c && typeof c.text === "string") parts.push(c.text);
      }
    }
    return parts.join("\n").trim();
  }
  const ch = data.choices && data.choices[0] && data.choices[0].message;
  return ch && typeof ch.content === "string" ? ch.content.trim() : "";
}

function parseArgs(a) {
  if (typeof a !== "string") return a == null ? null : a;
  try {
    return JSON.parse(a);
  } catch {
    return a;
  }
}

export function extractToolCall(protocol, data) {
  const d = data || {};
  if (protocol === "anthropic") {
    const tu = (Array.isArray(d.content) ? d.content : []).find((b) => b && b.type === "tool_use");
    return tu ? { name: tu.name || null, args: tu.input == null ? null : tu.input } : { name: null, args: null };
  }
  if (protocol === "openai-response") {
    const fc = (Array.isArray(d.output) ? d.output : []).find((b) => b && b.type === "function_call");
    return fc ? { name: fc.name || null, args: parseArgs(fc.arguments) } : { name: null, args: null };
  }
  if (protocol === "gemini") {
    const parts = (((d.candidates || [])[0] || {}).content || {}).parts || [];
    const p = parts.find((x) => x && x.functionCall);
    return p ? { name: p.functionCall.name || null, args: p.functionCall.args == null ? null : p.functionCall.args } : { name: null, args: null };
  }
  const tc = (((d.choices || [])[0] || {}).message || {}).tool_calls;
  const f = Array.isArray(tc) && tc[0] && tc[0].function;
  return f ? { name: f.name || null, args: parseArgs(f.arguments) } : { name: null, args: null };
}

export function toolVerdict(name, args) {
  if (name !== "lookup_audit_code" || !args || typeof args !== "object" || Array.isArray(args)) return "fail";
  if (args.region === "CN" && typeof args.code === "number" && args.code === 4096) return "pass";
  if (String(args.region).toUpperCase() === "CN" && String(args.code).trim() === "4096") return "warn";
  return "fail";
}

export function cacheReadTokens(protocol, u) {
  const x = u || {};
  if (protocol === "gemini") return x.cachedContentTokenCount || 0;
  return (
    x.cache_read_input_tokens ??
    (x.input_tokens_details && x.input_tokens_details.cached_tokens) ??
    (x.prompt_tokens_details && x.prompt_tokens_details.cached_tokens) ??
    x.prompt_cache_hit_tokens ??
    0
  );
}

// ------------------------------------------------------------------ network

async function readBounded(resp, limit) {
  if (!resp.body || typeof resp.body.getReader !== "function") return (await resp.text()).slice(0, limit);
  const reader = resp.body.getReader();
  const chunks = [];
  let n = 0;
  while (n < limit) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    n += value.byteLength;
  }
  try {
    await reader.cancel();
  } catch {}
  const buf = new Uint8Array(Math.min(n, limit));
  let off = 0;
  for (const c of chunks) {
    const take = Math.min(c.byteLength, buf.length - off);
    buf.set(c.subarray(0, take), off);
    off += take;
    if (off >= buf.length) break;
  }
  return new TextDecoder().decode(buf);
}

async function callUpstream(url, headers, payload, apiKey, timeoutMs = 24000) {
  const t0 = Date.now();
  try {
    const resp = await fetch(url, {
      method: "POST",
      redirect: "error",
      headers: { "Content-Type": "application/json", "User-Agent": "RelayPriceVerify/1.1 (+https://agentied.work/verify/)", ...headers },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const ttfb = Date.now() - t0;
    const text = redact(await readBounded(resp, MAX_UPSTREAM_BYTES), apiKey);
    const ms = Date.now() - t0;
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw_text: text.slice(0, 400) };
    }
    const hdrs = {};
    for (const k of [
      "request-id", "x-request-id", "anthropic-ratelimit-requests-limit", "anthropic-organization-id",
      "openai-organization", "openai-processing-ms", "x-ratelimit-limit-requests", "server", "cf-ray",
    ]) {
      const v = resp.headers.get(k);
      if (v) hdrs[k] = redact(v, apiKey).slice(0, 120);
    }
    return { ok: resp.ok, status: resp.status, ms, ttfb_ms: ttfb, data, headers: hdrs };
  } catch (err) {
    const msg = String((err && err.message) || err);
    const why = /redirect/i.test(msg) ? `redirect blocked (${msg})` : msg;
    return { ok: false, status: 0, ms: Date.now() - t0, ttfb_ms: null, data: { error: redact(why, apiKey).slice(0, 240) }, headers: {} };
  }
}

function authHeaders(protocol, apiKey) {
  if (protocol === "anthropic") return { "x-api-key": apiKey, Authorization: `Bearer ${apiKey}`, "anthropic-version": "2023-06-01" };
  if (protocol === "gemini") return { "x-goog-api-key": apiKey };
  return { Authorization: `Bearer ${apiKey}` };
}

export function endpointUrl(base, protocol, model) {
  if (protocol === "anthropic") return `${base}/v1/messages`;
  if (protocol === "openai-response") return `${base}/v1/responses`;
  if (protocol === "gemini") {
    return `${base}/v1beta/models/${encodeURIComponent(String(model || "").replace(/^models\//, ""))}:generateContent`;
  }
  return `${base}/v1/chat/completions`;
}

function textPayload(protocol, model, prompt, maxTokens) {
  if (protocol === "anthropic") return { model, max_tokens: maxTokens, temperature: 0, messages: [{ role: "user", content: prompt }] };
  if (protocol === "openai-response") return { model, max_output_tokens: Math.max(maxTokens, 400), input: prompt };
  if (protocol === "gemini") {
    return { contents: [{ role: "user", parts: [{ text: prompt }] }], generationConfig: { temperature: 0, maxOutputTokens: Math.max(maxTokens, 1024) } };
  }
  return { model, max_tokens: Math.max(maxTokens, 512), temperature: 0, messages: [{ role: "user", content: prompt }] };
}

function errSnippet(r) {
  return JSON.stringify(r.data || {}).slice(0, 240);
}

function httpFail(id, title, r, summary) {
  return {
    id, title, status: "fail", ms: r.ms, summary,
    telemetry: { ttfb_ms: r.ttfb_ms ?? null, total_ms: r.ms, output_tokens: null, tok_per_sec: null },
    evidence: { http_status: r.status, error: errSnippet(r) },
  };
}

// ------------------------------------------------------------------ checks

async function runFingerprint(base, apiKey, model, protocol) {
  const title = "协议指纹与模型回显";
  const r = await callUpstream(endpointUrl(base, protocol, model), authHeaders(protocol, apiKey), textPayload(protocol, model, MATH_Q, 32), apiKey);
  if (!r.ok) return httpFail("fingerprint", title, r, `HTTP ${r.status} 调用失败`);
  const d = r.data || {};
  const text = extractText(protocol, d);
  const retModel = (protocol === "gemini" ? d.modelVersion : d.model) || null;
  const retId = (protocol === "gemini" ? d.responseId : d.id) || null;
  const mathOk = /\b700\b/.test(text);
  const echoOk = modelEchoMatches(model, retModel);
  const idOk = responseIdOk(protocol, retId);
  const shapeOk = responseShapeOk(protocol, d);
  const status = echoOk && mathOk && idOk && shapeOk ? "pass" : mathOk && (echoOk || idOk) ? "warn" : "fail";
  const notes = [];
  if (!echoOk) notes.push(`返回型号「${retModel || "空"}」与请求「${model}」不一致`);
  if (!idOk) notes.push(`响应 ID「${String(retId || "空").slice(0, 18)}」不符合 ${protocol} 原生格式`);
  if (!shapeOk) notes.push("响应结构缺少原生协议字段（type/object/candidates）");
  if (!mathOk) notes.push(`确定性算术未返回 700（实际: ${text.slice(0, 40) || "空"}）`);
  const tel = telemetry(r, protocol);
  return {
    id: "fingerprint", title, status, ms: r.ms,
    summary: notes.length ? notes.join("；") : `原生响应结构匹配（model=${retModel}，${r.ms}ms）`,
    telemetry: tel,
    evidence: {
      http_status: r.status, requested_model: model, returned_model: retModel,
      echo_normalized: [normalizeModelId(model), normalizeModelId(retModel)], echo_ok: echoOk,
      response_id: retId, id_ok: idOk, shape_ok: shapeOk, math_ok: mathOk,
      system_fingerprint: d.system_fingerprint || null, answer: text.slice(0, 80),
      usage: (protocol === "gemini" ? d.usageMetadata : d.usage) || null, headers: r.headers,
    },
  };
}

async function runAnthropicThinking(base, apiKey, model) {
  const title = "Thinking 签名回传与篡改负向校验";
  const url = endpointUrl(base, "anthropic", model);
  const hdrs = authHeaders("anthropic", apiKey);
  const thinkCfg = { type: "enabled", budget_tokens: 1024 };
  const p1 = { model, max_tokens: 1400, temperature: 1, thinking: thinkCfg, messages: [{ role: "user", content: THINK_Q1 }] };
  const r1 = await callUpstream(url, hdrs, p1, apiKey, 30000);
  if (!r1.ok) return httpFail("thinking", title, r1, `开启 Extended Thinking 返回 HTTP ${r1.status}`);
  const blocks = Array.isArray(r1.data && r1.data.content) ? r1.data.content : [];
  const thinkBlk = blocks.find((b) => b && b.type === "thinking");
  const sig = thinkBlk && typeof thinkBlk.signature === "string" ? thinkBlk.signature : "";
  const txt1 = extractText("anthropic", r1.data);
  const baseEv = {
    block_types: blocks.map((b) => b && b.type),
    signature_len: sig.length,
    signature_b64: /^[A-Za-z0-9+/=]+$/.test(sig),
    thinking_chars: thinkBlk && typeof thinkBlk.thinking === "string" ? thinkBlk.thinking.length : 0,
    answer_t1: txt1.slice(0, 40),
    usage_t1: r1.data.usage || null,
  };
  if (!thinkBlk || sig.length < 40) {
    return {
      id: "thinking", title, status: "fail", ms: r1.ms,
      summary: thinkBlk
        ? `存在 thinking 块但缺少有效加密 signature（长度 ${sig.length}），疑似转译或伪造`
        : "未返回原生 thinking 块（疑似被中转剥离或转译为非 Claude 模型）",
      telemetry: telemetry(r1, "anthropic"),
      evidence: baseEv,
    };
  }
  const tampered = blocks.map((b) => (b === thinkBlk ? { ...b, signature: tamperSignature(sig) } : b));
  const mk = (content) => ({
    model, max_tokens: 1100, temperature: 1, thinking: thinkCfg,
    messages: [{ role: "user", content: THINK_Q1 }, { role: "assistant", content }, { role: "user", content: THINK_Q2 }],
  });
  const [r2, rt] = await Promise.all([
    callUpstream(url, hdrs, mk(blocks), apiKey, 30000),
    callUpstream(url, hdrs, mk(tampered), apiKey, 30000),
  ]);
  const txt2 = r2.ok ? extractText("anthropic", r2.data) : "";
  const roundtripOk = r2.ok && /\b460\b/.test(txt2);
  const tamper = classifyTamper(rt.status, rt.data);
  const usage = anthropicUsageCheck(r1.data.usage, r2.ok ? r2.data.usage : null);
  const status = thinkingVerdict({ has_block: true, signature_len: sig.length, roundtrip_ok: roundtripOk, tamper, usage_ok: usage.ok });
  const notes = [];
  notes.push(roundtripOk ? `真签名二轮回传通过（454 → 460，signature ${sig.length} 字符）` : `二轮真签名回传异常（HTTP ${r2.status}，回复 ${txt2.slice(0, 30) || "空"}）`);
  if (tamper === "accepted") notes.push("篡改后的 signature 仍被接受：签名未被校验，疑似伪造 thinking 或中转剥离历史块");
  else if (tamper === "rejected") notes.push(`篡改签名被上游拒绝（HTTP ${rt.status}），符合原生校验`);
  else if (tamper === "rejected_other") notes.push(`篡改签名返回 HTTP ${rt.status}（非签名类报错，无法确认）`);
  else notes.push(`篡改签名负向校验未完成（HTTP ${rt.status || "超时"}）`);
  if (!usage.ok) notes.push(usage.notes.join("、"));
  const u2 = (r2.ok && r2.data.usage) || {};
  return {
    id: "thinking", title, status, ms: r1.ms + Math.max(r2.ms, rt.ms),
    summary: notes.join("；"),
    telemetry: telemetry(r2.ok ? r2 : r1, "anthropic"),
    evidence: {
      ...baseEv,
      signature_preview: `${sig.slice(0, 14)}…${sig.slice(-8)}`,
      turn2_status: r2.status, turn2_ok: roundtripOk, answer_t2: txt2.slice(0, 40),
      tamper_status: rt.status, tamper_result: tamper,
      tamper_error: tamper === "accepted" ? null : errSnippet(rt).slice(0, 160),
      usage_t2: r2.ok ? r2.data.usage || null : null,
      usage_ok: usage.ok, usage_notes: usage.notes,
      cache_read_t2: u2.cache_read_input_tokens ?? null,
    },
  };
}

async function runThinking(base, apiKey, model, protocol) {
  if (protocol === "anthropic") return runAnthropicThinking(base, apiKey, model);
  const title = "推理遥测与确定性校验";
  const url = endpointUrl(base, protocol, model);
  let payload;
  if (protocol === "openai-response") payload = { model, max_output_tokens: 1024, input: THINK_Q1 };
  else if (protocol === "gemini") {
    payload = {
      contents: [{ role: "user", parts: [{ text: THINK_Q1 }] }],
      generationConfig: { maxOutputTokens: 1024, thinkingConfig: { includeThoughts: true } },
    };
  } else payload = { model, max_tokens: 1024, messages: [{ role: "user", content: THINK_Q1 }] };
  let r = await callUpstream(url, authHeaders(protocol, apiKey), payload, apiKey, 30000);
  let thinkCfgDropped = false;
  if (!r.ok && r.status === 400 && protocol === "gemini") {
    thinkCfgDropped = true;
    r = await callUpstream(url, authHeaders(protocol, apiKey), { ...payload, generationConfig: { maxOutputTokens: 1024 } }, apiKey, 30000);
  }
  if (!r.ok) return httpFail("thinking", title, r, `HTTP ${r.status} 调用失败`);
  const txt = extractText(protocol, r.data);
  const okAns = /\b454\b/.test(txt);
  const sig = reasoningSignal(protocol, r.data);
  const usage = openaiUsageCheck(protocol, protocol === "gemini" ? r.data.usageMetadata : r.data.usage);
  const status = !okAns ? "fail" : sig.strong && usage.ok ? "pass" : "warn";
  let summary;
  if (!okAns) summary = `算术推理失败（期望 454，实际: ${txt.slice(0, 50) || "空"}）`;
  else if (sig.strong) {
    summary = `确定性校验通过（454），含原生推理遥测（reasoning_tokens=${sig.reasoning_tokens ?? "—"}${sig.reasoning_item ? "，reasoning 输出项" : ""}${sig.reasoning_content ? "，reasoning_content" : ""}${sig.thought_signature_len ? `，thoughtSignature ${sig.thought_signature_len} 字符` : ""}）`;
  } else if (sig.reasoning_tokens === 0) summary = "确定性校验通过（454），reasoning_tokens 字段存在但为 0（非推理档或被中转置零）";
  else summary = "确定性校验通过（454），未返回推理遥测（非推理档模型属正常；推理档缺失则疑似转译）";
  if (!usage.ok) summary += `；usage 异常：${usage.notes.join("、")}`;
  if (thinkCfgDropped) summary += "；上游不接受 thinkingConfig，已去掉后重试";
  return {
    id: "thinking", title, status, ms: r.ms, summary,
    telemetry: telemetry(r, protocol),
    evidence: {
      answer: txt.slice(0, 60), ...sig, usage_ok: usage.ok, usage_notes: usage.notes,
      usage: (protocol === "gemini" ? r.data.usageMetadata : r.data.usage) || null,
    },
  };
}

function toolPayload(protocol, model) {
  const params = {
    type: "object",
    properties: { region: { type: "string", enum: ["CN", "US"] }, code: { type: "integer" } },
    required: ["region", "code"],
  };
  if (protocol === "anthropic") {
    return {
      model, max_tokens: 200, tools: [{ name: "lookup_audit_code", description: TOOL_DESC, input_schema: params }],
      tool_choice: { type: "any" }, messages: [{ role: "user", content: TOOL_MSG }],
    };
  }
  if (protocol === "openai-response") {
    return {
      model, max_output_tokens: 400, tools: [{ type: "function", name: "lookup_audit_code", description: TOOL_DESC, parameters: params }],
      tool_choice: "required", input: TOOL_MSG,
    };
  }
  if (protocol === "gemini") {
    return {
      contents: [{ role: "user", parts: [{ text: TOOL_MSG }] }],
      tools: [{
        functionDeclarations: [{
          name: "lookup_audit_code", description: TOOL_DESC,
          parameters: {
            type: "OBJECT",
            properties: { region: { type: "STRING", enum: ["CN", "US"] }, code: { type: "INTEGER" } },
            required: ["region", "code"],
          },
        }],
      }],
      toolConfig: { functionCallingConfig: { mode: "ANY" } },
      generationConfig: { maxOutputTokens: 1024 },
    };
  }
  return {
    model, max_tokens: 400,
    tools: [{ type: "function", function: { name: "lookup_audit_code", description: TOOL_DESC, parameters: params } }],
    tool_choice: "required", messages: [{ role: "user", content: TOOL_MSG }],
  };
}

async function runToolCall(base, apiKey, model, protocol) {
  const title = "严格 Tool-Calling 结构校验";
  const r = await callUpstream(endpointUrl(base, protocol, model), authHeaders(protocol, apiKey), toolPayload(protocol, model), apiKey);
  if (!r.ok) return httpFail("tool", title, r, `工具调用返回 HTTP ${r.status}`);
  const { name, args } = extractToolCall(protocol, r.data);
  const status = toolVerdict(name, args);
  const d = r.data || {};
  const cand = (d.candidates || [])[0] || {};
  return {
    id: "tool", title, status, ms: r.ms,
    summary: status === "pass"
      ? '原生结构化工具调用精准通过（region="CN", code=4096 整型）'
      : status === "warn"
      ? "命中工具名但参数类型漂移（如整数被序列化为字符串）"
      : "未返回结构化 tool_use / tool_calls / functionCall（部分伪装或转译渠道会吞掉工具调用）",
    telemetry: telemetry(r, protocol),
    evidence: {
      function_name: name, arguments: args,
      stop_reason: d.stop_reason || (d.choices && d.choices[0] && d.choices[0].finish_reason) || cand.finishReason || d.status || null,
    },
  };
}

function cachePayload(protocol, model, q) {
  if (protocol === "anthropic") {
    return {
      model, max_tokens: 24, temperature: 0,
      system: [{ type: "text", text: CACHE_PREFIX, cache_control: { type: "ephemeral" } }],
      messages: [{ role: "user", content: q }],
    };
  }
  if (protocol === "openai-response") return { model, max_output_tokens: 400, instructions: CACHE_PREFIX, input: q };
  if (protocol === "gemini") {
    return {
      systemInstruction: { parts: [{ text: CACHE_PREFIX }] },
      contents: [{ role: "user", parts: [{ text: q }] }],
      generationConfig: { temperature: 0, maxOutputTokens: 1024 },
    };
  }
  return { model, max_tokens: 256, temperature: 0, messages: [{ role: "system", content: CACHE_PREFIX }, { role: "user", content: q }] };
}

async function runCache(base, apiKey, model, protocol) {
  const title = "两轮 Prompt Cache 真命中";
  const url = endpointUrl(base, protocol, model);
  const hdrs = authHeaders(protocol, apiKey);
  const r1 = await callUpstream(url, hdrs, cachePayload(protocol, model, "What is the route code for Clause 017? Reply with the code only."), apiKey);
  if (!r1.ok) return httpFail("cache", title, r1, `首轮缓存预热失败（HTTP ${r1.status}）`);
  const r2 = await callUpstream(url, hdrs, cachePayload(protocol, model, "What is the route code for Clause 042? Reply with the code only."), apiKey);
  if (!r2.ok) {
    const f = httpFail("cache", title, r2, `次轮缓存复查失败（HTTP ${r2.status}）`);
    f.ms = r1.ms + r2.ms;
    return f;
  }
  const uk = protocol === "gemini" ? "usageMetadata" : "usage";
  const u1 = r1.data[uk] || {};
  const u2 = r2.data[uk] || {};
  const cacheCreate = u1.cache_creation_input_tokens ?? 0;
  const cacheRead = Number(cacheReadTokens(protocol, u2)) || 0;
  const hit = cacheRead > 0;
  return {
    id: "cache", title, status: hit ? "pass" : "warn", ms: r1.ms + r2.ms,
    summary: hit
      ? `次轮实测命中缓存读 ${cacheRead} tokens（首轮写入 ${cacheCreate}）`
      : "次轮未返回缓存命中 token（可能该分组剥离了缓存头、走多账号轮询混池，或上游未达最小缓存阈值）",
    telemetry: telemetry(r2, protocol),
    evidence: {
      turn1_usage: u1, turn2_usage: u2, cache_creation_t1: cacheCreate, cache_read_t2: cacheRead,
      answer_t2: extractText(protocol, r2.data).slice(0, 40),
    },
  };
}

const RUNNERS = { fingerprint: runFingerprint, thinking: runThinking, tool: runToolCall, cache: runCache };

export function shouldFallback(requested, protocol, isFirst, res) {
  const st = res && res.evidence && res.evidence.http_status;
  return requested === "auto" && protocol === "gemini" && isFirst && res.status === "fail" && (st === 404 || st === 405);
}

async function runSuite(base, apiKey, model, requested, checks) {
  let protocol = detectProtocol(model, requested);
  let fallback = null;
  const results = [];
  for (const id of checks) {
    let res = await RUNNERS[id](base, apiKey, model, protocol);
    if (shouldFallback(requested, protocol, results.length === 0, res)) {
      fallback = { from: "gemini", to: "openai", reason: `Gemini 原生端点返回 HTTP ${res.evidence.http_status}，已回退 OpenAI 兼容协议` };
      protocol = "openai";
      res = await RUNNERS[id](base, apiKey, model, protocol);
    }
    results.push(res);
  }
  return { protocol, fallback, results };
}

export function parseChecks(raw) {
  if (!Array.isArray(raw) || !raw.length) return ALL_CHECKS.slice();
  const want = ALL_CHECKS.filter((c) => raw.includes(c));
  return want;
}

export async function onRequestOptions() {
  return new Response(null, { status: 204, headers: CORS_HEADERS });
}

export async function onRequestGet() {
  return jsonRes({
    service: "API Price Radar — LLM Authenticity Verifier",
    docs: "https://agentied.work/verify/",
    source: "https://github.com/Guoz7/relay_price",
    privacy: "Stateless edge verification. API keys are forwarded in-memory only and never logged, stored or echoed.",
    method: "POST",
    request_schema: {
      base_url: "https://example-relay.com",
      api_key: "sk-...",
      model: "claude-sonnet-5-5",
      protocol: PROTOCOLS.join(" | "),
      checks: ALL_CHECKS,
    },
  });
}

export async function onRequestPost(context) {
  let body;
  try {
    const raw = await context.request.text();
    if (raw.length > MAX_REQUEST_BYTES) return jsonRes({ error: "请求体过大" }, 413);
    body = JSON.parse(raw);
  } catch {
    return jsonRes({ error: "请求体必须是合法 JSON" }, 400);
  }
  if (!body || typeof body !== "object") return jsonRes({ error: "请求体必须是 JSON 对象" }, 400);
  const base = normalizeBaseUrl(body.base_url);
  const apiKey = String(body.api_key || "").trim();
  const model = String(body.model || "").trim();
  const requested = String(body.protocol || "auto");
  if (!base) {
    return jsonRes({ error: "base_url 必须是公网 HTTPS 域名（默认 443 端口；出于 SSRF 安全限制禁止内网、裸 IP 与自定义端口）" }, 400);
  }
  if (!apiKey || apiKey.length < 6 || apiKey.length > 512) {
    return jsonRes({ error: "请提供用于本次检测的 API Key（建议使用小额临时 Key，本接口不存日志）" }, 400);
  }
  if (!isValidModel(model)) return jsonRes({ error: "请填写有效的目标模型名（字母、数字与 . _ - : / @）" }, 400);
  if (!PROTOCOLS.includes(requested)) return jsonRes({ error: `protocol 仅支持 ${PROTOCOLS.join(" / ")}` }, 400);
  const checks = parseChecks(body.checks);
  if (!checks.length) return jsonRes({ error: `checks 仅支持 ${ALL_CHECKS.join(" / ")}` }, 400);

  const { protocol, fallback, results } = await runSuite(base, apiKey, model, requested, checks);
  return jsonRes({
    checked_at: new Date().toISOString(),
    base_url: base,
    model,
    requested_protocol: requested,
    protocol,
    protocol_fallback: fallback,
    ...summarize(results),
    results,
    disclaimer: "黑盒技术指纹校验仅反映本次实测的协议与特征证据，不构成厂商背书；本接口不保存或记录任何 API Key。",
  });
}
