/**
 * Stateless LLM Model Authenticity & Intelligence Verification API (Cloudflare Pages Function: /api/verify).
 *
 * Security & privacy guarantees:
 * - Never logs, persists, or echoes back user-supplied `api_key` (upstream bodies are redacted before parsing).
 * - Strict HTTPS + public hostname SSRF guard: blocks loopback / RFC1918 / link-local names, every IP literal
 *   form (dotted, integer, octal, hex, IPv6), userinfo, wildcard-DNS rebinding hosts and any non-443 port.
 * - Upstream fetches use `redirect: "error"` (no 30x hop to an internal target) with bounded timeouts and a
 *   64 KB response cap.
 *
 * Anti-gaming: every request derives fresh challenges from a seeded Mulberry32 PRNG (a random seed per request,
 * or an explicit `seed` for reproducible runs), so a dishonest relay cannot regex-match open-source probe prompts
 * and route only those to an honest upstream.
 *
 * Checks (kept 1:1 in sync with tools/verify_model.py):
 * Authenticity (auth_score)
 * 1. fingerprint: response shape, model echo (normalized), ID format, dynamic micro-math + timing telemetry.
 * 2. thinking:    Anthropic: thinking signature + Turn-2 genuine round-trip + tampered-signature negative check
 *                 + usage consistency. OpenAI / DeepSeek / Gemini: native reasoning telemetry + usage sanity.
 * 3. tool:        Strict typed Tool-Calling with a dynamic tool name, enum values, argument keys and integer code.
 * 4. cache:       2-turn ~1,350-token dynamic policy corpus (session nonce + random target clauses) cache hit.
 * Intelligence / fidelity (iq_score) — programmatically graded, zero LLM-as-a-judge
 * 5. iq_trace:       6-step integer state-machine execution trace (exact JSON final state + checksum).
 * 6. iq_logic:       4 x 3 logic deduction puzzle, uniqueness proven by an exact internal solver.
 * 7. iq_constraints: 4 simultaneous verifiable instruction constraints (IFEval style).
 * 8. context_hop:    RULER-style 3-hop pointer chain at ~15% / ~52% / ~87% depth + prompt_tokens audit.
 * Tiers: l1_auth / l2_iq (default) / l3_deep. Non-compensatory: an accepted tampered signature, auth_score < 55
 * or iq_score < 55 forces level "fail".
 */

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Cache-Control": "no-store",
};

export const PROTOCOLS = ["auto", "anthropic", "openai-response", "openai", "gemini"];
export const AUTH_CHECKS = ["fingerprint", "thinking", "tool", "cache"];
export const IQ_CHECKS = ["iq_trace", "iq_logic", "iq_constraints", "context_hop"];
export const ALL_CHECKS = [...AUTH_CHECKS, ...IQ_CHECKS];
export const TIERS = {
  l1_auth: AUTH_CHECKS.slice(),
  l2_iq: [...AUTH_CHECKS, "iq_trace", "iq_logic", "iq_constraints"],
  l3_deep: ALL_CHECKS.slice(),
};
export const DEFAULT_TIER = "l2_iq";
export const EDGE_CONTEXT_TOKENS = { def: 12000, min: 4000, max: 28000 };
export const FAIL_FAST_HTTP = [0, 401, 402, 403, 404];
const CONCURRENCY = 3;
const MAX_UPSTREAM_BYTES = 65536;
const MAX_REQUEST_BYTES = 8192;
const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const BLOCKED_SUFFIXES = [
  ".local", ".localhost", ".internal", ".intranet", ".lan", ".home", ".corp", ".localdomain", ".home.arpa",
  ".nip.io", ".sslip.io", ".xip.io", ".localtest.me", ".lvh.me",
];
const BLOCKED_HOSTS = ["localhost", "metadata.google.internal", "nip.io", "sslip.io", "localtest.me", "lvh.me"];
export const LEGACY_TOOL = { name: "lookup_audit_code", region_key: "region", code_key: "code", region: "CN", code: 4096, enum: ["CN", "US"] };

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

export function checkGroup(id) {
  return IQ_CHECKS.includes(id) ? "iq" : "auth";
}

function statusPoints(s) {
  return s === "pass" ? 100 : s === "warn" ? 55 : 0;
}

function groupScore(xs) {
  return xs.length ? Math.round(xs.reduce((acc, x) => acc + statusPoints(x.status), 0) / xs.length) : null;
}

export function summarize(results) {
  const rs = Array.isArray(results) ? results : [];
  const total = rs.length || 1;
  const nPass = rs.filter((x) => x.status === "pass").length;
  const nFail = rs.filter((x) => x.status === "fail").length;
  const score = Math.round(rs.reduce((acc, x) => acc + statusPoints(x.status), 0) / total);
  const authScore = groupScore(rs.filter((x) => checkGroup(x.id) === "auth"));
  const iqScore = groupScore(rs.filter((x) => checkGroup(x.id) === "iq"));
  const tamperAccepted = rs.some((x) => Boolean(x.evidence && typeof x.evidence === "object" && x.evidence.tamper_result === "accepted"));
  const veto = [];
  if (tamperAccepted) veto.push("tampered_signature_accepted");
  if (authScore !== null && authScore < 55) veto.push("auth_score_below_55");
  if (iqScore !== null && iqScore < 55) veto.push("iq_score_below_55");
  let level = "pass";
  let verdict = iqScore !== null ? "真伪与智商抽检均符合原生旗舰模型特征" : "高度符合原生协议与真机特征";
  if (tamperAccepted) {
    level = "fail";
    verdict = "篡改签名仍被接受：签名未被校验，疑似伪造 thinking 或套壳链路（非补偿否决，直接判定异常）";
  } else if (authScore !== null && authScore < 55) {
    level = "fail";
    verdict = "真伪特征分低于 55：疑似转译、套壳或伪造协议（非补偿否决，直接判定异常）";
  } else if (iqScore !== null && iqScore < 55) {
    level = "fail";
    verdict = "智商保真分低于 55：疑似静默降级到弱模型、剥离推理档或截断上下文（非补偿否决，直接判定异常）";
  } else if (nFail > 0 || score < 55) {
    level = "fail";
    verdict = "存在明显异常（疑似转译、接口不兼容、降级或套壳，建议核对下方原始证据）";
  } else if (nPass < rs.length) {
    level = "warn";
    verdict = "基础调用正常，但部分高级特征缺失（如缓存未命中或缺少原生遥测字段，建议小额实测）";
  }
  return {
    score, auth_score: authScore, iq_score: iqScore, level, verdict, veto,
    tamper_accepted: tamperAccepted, passed: nPass, total: rs.length,
  };
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

export function toolVerdict(name, args, ch = LEGACY_TOOL) {
  if (name !== ch.name || !args || typeof args !== "object" || Array.isArray(args)) return "fail";
  const rv = args[ch.region_key];
  const cv = args[ch.code_key];
  if (rv === ch.region && typeof cv === "number" && cv === ch.code) return "pass";
  if ((typeof cv === "string" || typeof cv === "number") && String(rv).toUpperCase() === ch.region && String(cv).trim() === String(ch.code)) {
    return "warn";
  }
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

// ------------------------------------------------------------------ seeded dynamic challenges (mirrored in verify_model.py)

const U32 = 4294967296;
const TOKEN_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";

export function fnv1a(str) {
  const s = String(str == null ? "" : str);
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h >>> 0;
}

export class Rng {
  constructor(seed) {
    this.a = Number(seed) >>> 0;
  }

  u32() {
    this.a = (this.a + 0x6d2b79f5) >>> 0;
    let t = this.a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return (t ^ (t >>> 14)) >>> 0;
  }

  int(n) {
    return Math.floor((this.u32() * n) / U32);
  }

  range(lo, hi) {
    return lo + this.int(hi - lo + 1);
  }

  pick(arr) {
    return arr[this.int(arr.length)];
  }

  shuffle(arr) {
    const a = arr.slice();
    for (let i = a.length - 1; i > 0; i--) {
      const j = this.int(i + 1);
      const t = a[i];
      a[i] = a[j];
      a[j] = t;
    }
    return a;
  }

  token(n) {
    let s = "";
    for (let i = 0; i < n; i++) s += TOKEN_ALPHA[this.int(TOKEN_ALPHA.length)];
    return s;
  }
}

export function rngSequence(seed, n) {
  const r = new Rng(seed);
  const out = [];
  for (let i = 0; i < n; i++) out.push(r.u32());
  return out;
}

export function deriveSeed(seed, id) {
  return (fnv1a(id) ^ (Number(seed) >>> 0)) >>> 0;
}

export function normalizeSeed(v) {
  return typeof v === "number" && Number.isInteger(v) && v >= 0 && v <= 4294967295 ? v : null;
}

export function randomSeed() {
  const a = new Uint32Array(1);
  crypto.getRandomValues(a);
  return a[0];
}

export function clampContextTokens(v, lo = EDGE_CONTEXT_TOKENS.min, hi = EDGE_CONTEXT_TOKENS.max, def = EDGE_CONTEXT_TOKENS.def) {
  if (typeof v !== "number" || !Number.isFinite(v)) return def;
  return Math.min(hi, Math.max(lo, Math.floor(v)));
}

export function hasInt(text, n) {
  return new RegExp(`\\b${Number(n)}\\b`).test(String(text == null ? "" : text));
}

function pad2(n) {
  return String(n).padStart(2, "0");
}

function pad3(n) {
  return String(n).padStart(3, "0");
}

function uniqueTokens(r, n, len, prefix) {
  const out = [];
  while (out.length < n) {
    const t = prefix + r.token(len);
    if (!out.includes(t)) out.push(t);
  }
  return out;
}

export function genFingerprint(seed) {
  const r = new Rng(seed);
  const a = r.range(31, 97);
  const b = r.range(31, 97);
  const c = r.range(101, 899);
  const nonce = r.token(6);
  const tpl = r.int(3);
  let prompt;
  if (tpl === 0) prompt = `Ticket ${nonce}. Compute (${a} * ${b}) - ${c}. Reply with the single integer only, no explanation.`;
  else if (tpl === 1) prompt = `[${nonce}] What is ${a} times ${b}, minus ${c}? Answer with one integer only, no explanation.`;
  else prompt = `Evaluate ${a}*${b}-${c} for request ${nonce}. Output only the resulting integer.`;
  return { kind: "fingerprint", nonce, prompt, answer: a * b - c };
}

export function genThinking(seed) {
  const r = new Rng(seed);
  const x = r.range(13, 49);
  const y = r.range(13, 49);
  const z = r.range(11, 99);
  const d = r.range(3, 19);
  const nonce = r.token(6);
  const answer1 = x * y + z;
  return {
    kind: "thinking", nonce,
    q1: `Compute (${x} * ${y}) + ${z}. Reply with the integer only. [ref ${nonce}]`,
    q2: `Add ${d} to your previous integer result. Reply with the integer only.`,
    answer1, answer2: answer1 + d,
  };
}

const TOOL_VERBS = ["lookup", "fetch", "resolve", "query", "verify", "inspect"];
const TOOL_NOUNS = ["audit", "ledger", "route", "invoice", "shipment", "ticket", "quota"];
const TOOL_REGION_KEYS = ["region", "zone", "market", "area"];
const TOOL_CODE_KEYS = ["code", "serial", "ref_id", "ticket_no"];
const TOOL_REGIONS = ["CN", "US", "DE", "JP", "SG", "BR", "FR", "KR", "IN", "GB", "CA", "AU"];

export function genTool(seed) {
  const r = new Rng(seed);
  const verb = r.pick(TOOL_VERBS);
  const noun = r.pick(TOOL_NOUNS);
  const suffix = r.token(4).toLowerCase();
  const regionKey = r.pick(TOOL_REGION_KEYS);
  const codeKey = r.pick(TOOL_CODE_KEYS);
  const enumVals = r.shuffle(TOOL_REGIONS).slice(0, 3);
  const region = enumVals[r.int(3)];
  const code = r.range(1000, 99999);
  const name = `${verb}_${noun}_${suffix}`;
  return {
    kind: "tool", name, region_key: regionKey, code_key: codeKey, enum: enumVals, region, code,
    description: `${verb[0].toUpperCase()}${verb.slice(1)} a ${noun} verification record by ${regionKey} and numeric ${codeKey}.`,
    message: `Call the tool ${name} with ${regionKey}="${region}" and ${codeKey}=${code}. Do not answer in plain text.`,
  };
}

const CACHE_DUTIES = [
  "UTF-8 JSON framing", "monotonic request IDs", "latency accounting in milliseconds", "non-negative token usage fields",
  "deterministic error codes", "idempotent retry headers", "per-tenant rate ceilings", "signed audit trails",
];

export function genCache(seed) {
  const r = new Rng(seed);
  const session = r.token(8);
  const lines = [
    `You are a strict compliance auditor reviewing API relay technical specifications (audit session ${session}).`,
    "Memorize the following numbered policy clauses and answer ONLY with the requested clause route code.",
  ];
  const codes = [];
  for (let i = 1; i <= 52; i++) {
    const code = `R-${r.token(5)}`;
    const duties = r.shuffle(CACHE_DUTIES);
    codes.push(code);
    lines.push(`Clause ${pad3(i)}: Relay gateway route ${code} enforces ${duties[0]} and ${duties[1]}.`);
  }
  const t1 = r.range(1, 52);
  let t2 = r.range(1, 51);
  if (t2 >= t1) t2 += 1;
  const prefix = lines.join("\n");
  const q = (t) => `What is the route code for Clause ${pad3(t)}? Reply with the code only.`;
  return {
    kind: "cache", session, prefix, q1: q(t1), q2: q(t2), answer1: codes[t1 - 1], answer2: codes[t2 - 1],
    est_tokens: Math.ceil(prefix.length / 4),
  };
}

const TRACE_VARS = ["a", "b", "c"];
const TRACE_KINDS = ["muladd", "xor", "branch", "addmod", "mulmod", "xorconst"];

export function genIqTrace(seed) {
  const r = new Rng(seed);
  const nonce = r.token(6);
  const st = { a: r.range(1, 99), b: r.range(1, 99), c: r.range(1, 99) };
  const code = [`a = ${st.a}`, `b = ${st.b}`, `c = ${st.c}`];
  const kinds = r.shuffle(TRACE_KINDS);
  kinds.forEach((kind, idx) => {
    const vs = r.shuffle(TRACE_VARS);
    const x = vs[0];
    const y = vs[1];
    const z = vs[2];
    const m = r.range(97, 997);
    const k = r.range(2, 9);
    code.push(`# step ${idx + 1}`);
    if (kind === "muladd") {
      code.push(`${x} = (${x} * ${k} + ${y}) % ${m}`);
      st[x] = (st[x] * k + st[y]) % m;
    } else if (kind === "xor") {
      code.push(`${x} = ${x} ^ ${y}`);
      st[x] = st[x] ^ st[y];
    } else if (kind === "branch") {
      const k2 = r.range(11, 99);
      code.push(`if ${x} > ${y}:`, `    ${z} = (${z} + ${k2}) % ${m}`, "else:", `    ${z} = (${z} * ${k} + 1) % ${m}`);
      st[z] = st[x] > st[y] ? (st[z] + k2) % m : (st[z] * k + 1) % m;
    } else if (kind === "addmod") {
      code.push(`${x} = (${x} + ${y} + ${z} + ${k}) % ${m}`);
      st[x] = (st[x] + st[y] + st[z] + k) % m;
    } else if (kind === "mulmod") {
      code.push(`${x} = (${x} * ${y} + ${k}) % ${m}`);
      st[x] = (st[x] * st[y] + k) % m;
    } else {
      const k3 = r.range(1, 255);
      code.push(`${x} = (${x} ^ ${k3}) % ${m}`);
      st[x] = (st[x] ^ k3) % m;
    }
  });
  code.push("checksum = a ^ b ^ c");
  const program = code.join("\n");
  const answer = { a: st.a, b: st.b, c: st.c, checksum: st.a ^ st.b ^ st.c };
  const prompt =
    `Trace ${nonce}. Execute this Python program exactly (integer arithmetic, % is modulo, ^ is bitwise XOR) ` +
    `and report the final values.\n\n${program}\n\nYou may think briefly, but the last line of your reply must be ` +
    `exactly one JSON object: {"a": <int>, "b": <int>, "c": <int>, "checksum": <int>}`;
  return { kind: "iq_trace", nonce, steps: kinds, program, prompt, answer };
}

const LOGIC_NAMES = [
  "Alice", "Bruno", "Chen", "Dara", "Emil", "Farah", "Goro", "Hana",
  "Ivan", "Jade", "Kofi", "Lena", "Mateo", "Nora", "Omar", "Priya",
];
const LOGIC_ATTRS = [
  ["color", ["red", "blue", "green", "yellow", "purple", "orange", "white", "black"]],
  ["pet", ["cat", "dog", "parrot", "rabbit", "turtle", "hamster", "goldfish", "lizard"]],
  ["drink", ["tea", "coffee", "milk", "juice", "water", "cocoa", "soda", "kefir"]],
  ["city", ["Oslo", "Lima", "Pune", "Kyoto", "Accra", "Quito", "Turin", "Hanoi"]],
  ["sport", ["tennis", "rowing", "chess", "boxing", "judo", "golf", "fencing", "cycling"]],
];
let PERMS4_CACHE = null;

export function perms4() {
  if (!PERMS4_CACHE) {
    const out = [];
    for (let a = 0; a < 4; a++) {
      for (let b = 0; b < 4; b++) {
        for (let c = 0; c < 4; c++) {
          for (let d = 0; d < 4; d++) {
            if (new Set([a, b, c, d]).size === 4) out.push([a, b, c, d]);
          }
        }
      }
    }
    PERMS4_CACHE = out;
  }
  return PERMS4_CACHE;
}

export function clueHolds(cl, p3) {
  if (cl.t === "pos") return p3[cl.a][cl.p] === cl.v;
  if (cl.t === "neg") return p3[cl.a][cl.p] !== cl.v;
  const q = p3[cl.a].indexOf(cl.v);
  return cl.t === "link" ? p3[cl.b][q] === cl.w : p3[cl.b][q] !== cl.w;
}

function clueLevel(cl) {
  return cl.t === "pos" || cl.t === "neg" ? cl.a : Math.max(cl.a, cl.b);
}

export function solveLogic(clues) {
  const P = perms4();
  const byLevel = [[], [], []];
  for (const cl of clues) byLevel[clueLevel(cl)].push(cl);
  let count = 0;
  let solution = null;
  const p3 = [null, null, null];
  for (let i = 0; i < P.length; i++) {
    p3[0] = P[i];
    if (!byLevel[0].every((cl) => clueHolds(cl, p3))) continue;
    for (let j = 0; j < P.length; j++) {
      p3[1] = P[j];
      if (!byLevel[1].every((cl) => clueHolds(cl, p3))) continue;
      for (let k = 0; k < P.length; k++) {
        p3[2] = P[k];
        if (byLevel[2].every((cl) => clueHolds(cl, p3))) {
          count += 1;
          if (!solution) solution = [i, j, k];
        }
      }
    }
  }
  return { count, solution };
}

function clueText(cl, names, attrs, values) {
  if (cl.t === "pos") return `${names[cl.p]}'s ${attrs[cl.a]} is ${values[cl.a][cl.v]}.`;
  if (cl.t === "neg") return `${names[cl.p]}'s ${attrs[cl.a]} is not ${values[cl.a][cl.v]}.`;
  const head = `The person whose ${attrs[cl.a]} is ${values[cl.a][cl.v]}`;
  return cl.t === "link"
    ? `${head} also has ${attrs[cl.b]} ${values[cl.b][cl.w]}.`
    : `${head} does not have ${attrs[cl.b]} ${values[cl.b][cl.w]}.`;
}

export function genIqLogic(seed) {
  const r = new Rng(seed);
  const nonce = r.token(6);
  const names = r.shuffle(LOGIC_NAMES).slice(0, 4);
  const catIdx = r.shuffle([0, 1, 2, 3, 4]).slice(0, 3);
  const attrs = catIdx.map((i) => LOGIC_ATTRS[i][0]);
  const values = [];
  for (const i of catIdx) values.push(r.shuffle(LOGIC_ATTRS[i][1]).slice(0, 4));
  const P = perms4();
  const sol = [r.int(24), r.int(24), r.int(24)];
  const hid = sol.map((i) => P[i]);
  let cands = [];
  for (let i = 0; i < 24; i++) for (let j = 0; j < 24; j++) for (let k = 0; k < 24; k++) cands.push([i, j, k]);
  const holds = (cl, c) => clueHolds(cl, [P[c[0]], P[c[1]], P[c[2]]]);
  const clues = [];
  for (let guard = 0; cands.length > 1 && guard < 400; guard++) {
    const roll = r.int(10);
    let cl;
    if (roll < 1) {
      const p = r.int(4);
      const a = r.int(3);
      cl = { t: "pos", p, a, v: hid[a][p] };
    } else if (roll < 4) {
      const p = r.int(4);
      const a = r.int(3);
      let v = r.int(3);
      if (v >= hid[a][p]) v += 1;
      cl = { t: "neg", p, a, v };
    } else {
      const a = r.int(3);
      let b = r.int(2);
      if (b >= a) b += 1;
      const v = r.int(4);
      const q = hid[a].indexOf(v);
      if (roll < 7) {
        cl = { t: "link", a, v, b, w: hid[b][q] };
      } else {
        let w = r.int(3);
        if (w >= hid[b][q]) w += 1;
        cl = { t: "notlink", a, v, b, w };
      }
    }
    const next = cands.filter((c) => holds(cl, c));
    if (next.length < cands.length) {
      clues.push(cl);
      cands = next;
    }
  }
  while (cands.length > 1) {
    let added = null;
    for (let a = 0; a < 3 && !added; a++) {
      for (let p = 0; p < 4 && !added; p++) {
        if (cands.some((c) => P[c[a]][p] !== hid[a][p])) added = { t: "pos", p, a, v: hid[a][p] };
      }
    }
    clues.push(added);
    cands = cands.filter((c) => holds(added, c));
  }
  const ordered = r.shuffle(clues.map((_, i) => i)).map((i) => clues[i]);
  const solution = {};
  for (let p = 0; p < 4; p++) {
    const row = {};
    for (let a = 0; a < 3; a++) row[attrs[a]] = values[a][hid[a][p]];
    solution[names[p]] = row;
  }
  const lines = [
    `Logic puzzle ${nonce}. Four people (${names.join(", ")}) each have exactly one ${attrs[0]}, one ${attrs[1]} and one ${attrs[2]}. No two people share the same value of any attribute.`,
  ];
  for (let a = 0; a < 3; a++) lines.push(`Possible ${attrs[a]} values: ${values[a].join(", ")}.`);
  lines.push("Clues:");
  ordered.forEach((cl, i) => lines.push(`${i + 1}. ${clueText(cl, names, attrs, values)}`));
  const shape = `{"${names[0]}": {"${attrs[0]}": "...", "${attrs[1]}": "...", "${attrs[2]}": "..."}, ...}`;
  lines.push(
    "Exactly one assignment satisfies every clue. You may reason briefly, but the last line of your reply must be " +
      `exactly one JSON object mapping all four people to their attributes, shaped like ${shape}`
  );
  return { kind: "iq_logic", nonce, names, attrs, values, clues: ordered, solution, hidden: sol, prompt: lines.join("\n") };
}

const IFE_TOPICS = ["animals", "fruits", "tools", "musical instruments", "vegetables", "sports", "pieces of furniture", "birds"];
const IFE_KEY_A = ["items", "words", "entries", "picks", "terms"];
const IFE_KEY_B = ["count", "total", "size", "n"];
const IFE_LETTERS = ["a", "e", "i", "o", "r", "s", "t", "n"];

export function genIqConstraints(seed) {
  const r = new Rng(seed);
  const nonce = r.token(6);
  const tag = r.token(3).toLowerCase();
  const listKey = `${r.pick(IFE_KEY_A)}_${tag}`;
  const countKey = `${r.pick(IFE_KEY_B)}_${tag}`;
  const n = r.range(4, 6);
  const letter = r.pick(IFE_LETTERS);
  const order = r.int(2) === 0 ? "ascending" : "descending";
  const topic = r.pick(IFE_TOPICS);
  const cmp = order === "ascending" ? "longer" : "shorter";
  const prompt =
    `Task ${nonce}. Return ONLY a JSON object that satisfies ALL of these rules:\n` +
    `1. The object has exactly two keys: "${listKey}" and "${countKey}".\n` +
    `2. "${listKey}" is an array of exactly ${n} single lowercase English words (letters a-z only) naming ${topic}, and "${countKey}" is the integer ${n}.\n` +
    `3. No word may contain the letter "${letter}".\n` +
    `4. The words must be in strictly ${order} order of length (every word ${cmp} than the previous one).\n` +
    "Do not add any other text.";
  return { kind: "iq_constraints", nonce, list_key: listKey, count_key: countKey, n, letter, order, topic, prompt };
}

const LOG_SVCS = ["billing", "router", "auth", "cache", "quota", "ingest", "search", "notify"];
const LOG_LVLS = ["INFO", "INFO", "INFO", "DEBUG", "WARN"];
const LOG_MSGS = [
  "upstream latency {n}ms within budget", "cache warm completed in {n}ms", "retry budget {n} remaining",
  "rotated {n} signing credentials", "flushed {n} metric points", "queue depth settled at {n}", "gc pause {n}ms",
  "accepted {n} requests from edge pool", "evicted {n} stale sessions", "replica lag {n}ms",
];
const LOG_SECTIONS = ["gateway", "billing", "scheduler", "storage", "edge", "observability", "identity", "network"];

export function genContextHop(seed, contextTokens = EDGE_CONTEXT_TOKENS.def) {
  const r = new Rng(seed);
  const nonce = r.token(6);
  const budget = Math.max(1000, Math.floor(contextTokens)) * 4;
  const filler = [];
  let chars = 0;
  let sec = r.range(0, 21600);
  let part = 0;
  while (chars < budget - 700) {
    let line;
    if (filler.length % 120 === 0) {
      part += 1;
      const secName = r.pick(LOG_SECTIONS);
      line = `=== Section ${part}: ${secName} runbook export ===`;
    } else {
      sec += r.range(1, 9);
      const ts = `2026-11-27T${pad2(Math.floor(sec / 3600) % 24)}:${pad2(Math.floor(sec / 60) % 60)}:${pad2(sec % 60)}Z`;
      const svc = r.pick(LOG_SVCS);
      const inst = r.range(1, 9);
      const lvl = r.pick(LOG_LVLS);
      const req = r.token(6);
      const msg = r.pick(LOG_MSGS);
      const num = r.range(1, 999);
      line = `${ts} ${svc}-${inst} ${lvl} req=${req} ${msg.replace("{n}", String(num))}`;
    }
    filler.push(line);
    chars += line.length + 1;
  }
  const total = filler.length;
  const keys = uniqueTokens(r, 12, 6, "PTR-");
  const finals = uniqueTokens(r, 4, 8, "FINAL_CODE_");
  const answer = finals[0];
  const needles = [];
  const add = (pos, text) => needles.push({ pos, ord: needles.length, text });
  const realLines = [
    `[registry] pointer ${keys[0]} resolves to ${keys[1]}`,
    `[registry] pointer ${keys[1]} resolves to ${keys[2]}`,
    `[registry] pointer ${keys[2]} resolves to ${answer}`,
  ];
  add(Math.floor((total * 15) / 100), realLines[0]);
  add(Math.floor((total * 52) / 100), realLines[1]);
  add(Math.floor((total * 87) / 100), realLines[2]);
  for (let d = 0; d < 3; d++) {
    const chain = [keys[3 + d * 3], keys[4 + d * 3], keys[5 + d * 3], finals[1 + d]];
    for (let h = 0; h < 3; h++) {
      const pos = r.range(1, total - 1);
      add(pos, `[registry] pointer ${chain[h]} resolves to ${chain[h + 1]}`);
    }
  }
  needles.sort((x, y) => x.pos - y.pos || x.ord - y.ord);
  const body = [];
  let ni = 0;
  for (let i = 0; i < total; i++) {
    while (ni < needles.length && needles[ni].pos === i) body.push(needles[ni++].text);
    body.push(filler[i]);
  }
  while (ni < needles.length) body.push(needles[ni++].text);
  const header =
    `Operations log export ${nonce} (${total} records). Records of the form "pointer X resolves to Y" mean that X resolves to Y.`;
  const question =
    `Question: start from pointer ${keys[0]} and follow the pointer records until you reach a value that begins with ` +
    "FINAL_CODE_. Reply with that final value only.";
  const prompt = [header, "", ...body, "", question].join("\n");
  return {
    kind: "context_hop", nonce, start: keys[0], chain: [keys[0], keys[1], keys[2], answer], answer,
    decoys: finals.slice(1), records: total, context_tokens: Math.floor(contextTokens),
    depth_pct: realLines.map((t) => Math.floor((prompt.indexOf(t) * 100) / prompt.length)),
    est_tokens: Math.ceil(prompt.length / 4), prompt,
  };
}

export function buildChallenge(id, seed, contextTokens = EDGE_CONTEXT_TOKENS.def) {
  const s = deriveSeed(seed, id);
  if (id === "fingerprint") return genFingerprint(s);
  if (id === "thinking") return genThinking(s);
  if (id === "tool") return genTool(s);
  if (id === "cache") return genCache(s);
  if (id === "iq_trace") return genIqTrace(s);
  if (id === "iq_logic") return genIqLogic(s);
  if (id === "iq_constraints") return genIqConstraints(s);
  if (id === "context_hop") return genContextHop(s, contextTokens);
  return null;
}

// ------------------------------------------------------------------ deterministic graders (mirrored in verify_model.py)

export function extractJsonObject(text) {
  const full = String(text == null ? "" : text);
  const s = full.length > 8000 ? full.slice(full.length - 8000) : full;
  let last = null;
  let tries = 0;
  let i = 0;
  while (i < s.length && tries < 64) {
    if (s[i] !== "{") {
      i += 1;
      continue;
    }
    tries += 1;
    let depth = 0;
    let inStr = false;
    let esc = false;
    let end = -1;
    for (let j = i; j < s.length; j++) {
      const c = s[j];
      if (inStr) {
        if (esc) esc = false;
        else if (c === "\\") esc = true;
        else if (c === '"') inStr = false;
      } else if (c === '"') inStr = true;
      else if (c === "{") depth += 1;
      else if (c === "}") {
        depth -= 1;
        if (depth === 0) {
          end = j;
          break;
        }
      }
    }
    if (end < 0) {
      i += 1;
      continue;
    }
    let v = null;
    try {
      v = JSON.parse(s.slice(i, end + 1));
    } catch {
      v = null;
    }
    if (v && typeof v === "object" && !Array.isArray(v)) {
      last = v;
      i = end + 1;
    } else i += 1;
  }
  return last;
}

function isNum(v) {
  return typeof v === "number" && Number.isFinite(v);
}

function own(obj, k) {
  return Boolean(obj) && typeof obj === "object" && !Array.isArray(obj) && Object.prototype.hasOwnProperty.call(obj, k);
}

function findKey(obj, key) {
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null;
  const want = String(key).toLowerCase();
  for (const k of Object.keys(obj)) if (k.trim().toLowerCase() === want) return obj[k];
  return null;
}

export function gradeIqTrace(ch, text) {
  const obj = extractJsonObject(text);
  const got = {};
  let correct = 0;
  for (const k of ["a", "b", "c", "checksum"]) {
    const v = own(obj, k) ? obj[k] : null;
    got[k] = v;
    if (isNum(v) && v === ch.answer[k]) correct += 1;
  }
  const status = correct === 4 ? "pass" : correct === 3 ? "warn" : "fail";
  return { status, correct, total: 4, parsed: Boolean(obj), expected: ch.answer, got };
}

export function gradeIqLogic(ch, text) {
  const obj = extractJsonObject(text);
  let correct = 0;
  const wrong = [];
  for (const name of ch.names) {
    const row = findKey(obj, name);
    for (const attr of ch.attrs) {
      const got = findKey(row, attr);
      const exp = ch.solution[name][attr];
      if (typeof got === "string" && got.trim().toLowerCase() === exp.toLowerCase()) correct += 1;
      else wrong.push(`${name}.${attr}`);
    }
  }
  const total = ch.names.length * ch.attrs.length;
  const status = correct === total ? "pass" : correct * 4 >= total * 3 ? "warn" : "fail";
  return { status, correct, total, parsed: Boolean(obj), wrong: wrong.slice(0, 6), clues: ch.clues.length };
}

export function gradeIqConstraints(ch, text) {
  const obj = extractJsonObject(text);
  const keysOk = Boolean(obj) && Object.keys(obj).length === 2 && own(obj, ch.list_key) && own(obj, ch.count_key);
  const list = own(obj, ch.list_key) && Array.isArray(obj[ch.list_key]) ? obj[ch.list_key] : null;
  const words = list && list.every((w) => typeof w === "string" && /^[a-z]+$/.test(w)) ? list : null;
  const count = own(obj, ch.count_key) ? obj[ch.count_key] : null;
  const lengthOk = Boolean(words) && words.length === ch.n && isNum(count) && count === ch.n;
  const letterOk = Boolean(words) && words.length > 0 && words.every((w) => !w.includes(ch.letter));
  let orderOk = Boolean(words) && words.length >= 2;
  if (orderOk) {
    for (let i = 1; i < words.length; i++) {
      const d = words[i].length - words[i - 1].length;
      if (ch.order === "ascending" ? d <= 0 : d >= 0) orderOk = false;
    }
  }
  const rules = [["keys", keysOk], ["exact_length", lengthOk], ["forbidden_letter", letterOk], ["length_order", orderOk]];
  const passed = rules.filter((x) => x[1]).length;
  const status = passed === 4 ? "pass" : passed === 3 ? "warn" : "fail";
  return { status, passed, total: 4, parsed: Boolean(obj), broken: rules.filter((x) => !x[1]).map((x) => x[0]), words: list };
}

export function gradeContextHop(ch, text, promptTokens) {
  const t = String(text == null ? "" : text);
  const found = t.includes(ch.answer);
  const decoy = ch.decoys.find((d) => t.includes(d)) || null;
  const pt = isCount(promptTokens) && promptTokens > 0 ? promptTokens : null;
  let flag = null;
  if (pt !== null) {
    if (pt * 100 < ch.est_tokens * 55) flag = "truncation";
    else if (pt * 100 > ch.est_tokens * 220) flag = "inflation";
  }
  const status = found && !decoy ? (flag ? "warn" : "pass") : "fail";
  return {
    status, found, decoy_hit: decoy, prompt_tokens: pt, est_tokens: ch.est_tokens,
    token_ratio_pct: pt === null ? null : Math.floor((pt * 100) / ch.est_tokens), token_flag: flag,
    records: ch.records, depth_pct: ch.depth_pct,
  };
}

export function iqSummary(id, g) {
  if (id === "iq_trace") {
    if (g.status === "pass") return `6 步状态机追踪全部正确（checksum=${g.expected.checksum}）`;
    if (!g.parsed) return "未返回可解析的 JSON 终态（疑似弱模型、输出被截断或推理被剥离）";
    if (g.status === "warn") return `状态机追踪 ${g.correct}/4 字段正确（疑似推理档被削减或量化）`;
    return `状态机追踪仅 ${g.correct}/4 字段正确（期望 checksum=${g.expected.checksum}，疑似静默降级为弱模型或剥离推理）`;
  }
  if (id === "iq_logic") {
    if (g.status === "pass") return `逻辑推理 ${g.total}/${g.total} 全部正确（${g.clues} 条约束，唯一解已由内置求解器证明）`;
    if (!g.parsed) return "未返回可解析的 JSON 解答（疑似弱模型或输出被截断）";
    if (g.status === "warn") return `逻辑推理 ${g.correct}/${g.total} 正确（接近但未完全推出唯一解）`;
    return `逻辑推理仅 ${g.correct}/${g.total} 正确（疑似静默降级为弱模型）`;
  }
  if (id === "iq_constraints") {
    if (g.status === "pass") return "4 条复合约束全部满足（键名、精确长度、禁用字母、长度单调）";
    return `复合约束满足 ${g.passed}/4，未满足：${g.broken.join("、")}`;
  }
  if (g.status === "pass") {
    return `约 ${g.est_tokens} token 长上下文 3 跳追踪正确（上游 prompt_tokens=${g.prompt_tokens == null ? "未返回" : g.prompt_tokens}）`;
  }
  if (g.status === "warn") {
    const why = g.token_flag === "truncation" ? "疑似静默截断输入" : "疑似 token 虚增计费";
    return `答案正确，但上游 prompt_tokens=${g.prompt_tokens} 与估算 ${g.est_tokens} 偏差异常（${why}）`;
  }
  const base = g.decoy_hit
    ? `命中诱饵链 ${g.decoy_hit}（疑似丢失中段上下文或静默截断）`
    : "未找到正确的 FINAL_CODE（疑似静默截断长上下文或回退弱模型）";
  return g.token_flag === "truncation" ? `${base}；上游 prompt_tokens=${g.prompt_tokens} 远小于估算 ${g.est_tokens}` : base;
}

export function inputTokens(protocol, data) {
  const u = (data && (protocol === "gemini" ? data.usageMetadata : data.usage)) || {};
  let v;
  if (protocol === "gemini") v = u.promptTokenCount;
  else if (protocol === "anthropic") v = isCount(u.input_tokens) ? totalIn(u) : null;
  else if (protocol === "openai-response") v = u.input_tokens;
  else v = u.prompt_tokens;
  return isCount(v) ? v : null;
}

export function failFast(res) {
  const ev = res && res.evidence;
  return Boolean(res) && res.status === "fail" && own(ev, "error") && FAIL_FAST_HTTP.includes(ev.http_status);
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

function call(ctx, url, headers, payload, timeoutMs) {
  ctx.calls += 1;
  return callUpstream(url, headers, payload, ctx.apiKey, timeoutMs);
}

async function runFingerprint(ctx, protocol) {
  const title = "协议指纹与模型回显";
  const { model } = ctx;
  const ch = buildChallenge("fingerprint", ctx.seed, ctx.contextTokens);
  const r = await call(ctx, endpointUrl(ctx.base, protocol, model), authHeaders(protocol, ctx.apiKey), textPayload(protocol, model, ch.prompt, 32));
  if (!r.ok) return httpFail("fingerprint", title, r, `HTTP ${r.status} 调用失败`);
  const d = r.data || {};
  const text = extractText(protocol, d);
  const retModel = (protocol === "gemini" ? d.modelVersion : d.model) || null;
  const retId = (protocol === "gemini" ? d.responseId : d.id) || null;
  const mathOk = hasInt(text, ch.answer);
  const echoOk = modelEchoMatches(model, retModel);
  const idOk = responseIdOk(protocol, retId);
  const shapeOk = responseShapeOk(protocol, d);
  const status = echoOk && mathOk && idOk && shapeOk ? "pass" : mathOk && (echoOk || idOk) ? "warn" : "fail";
  const notes = [];
  if (!echoOk) notes.push(`返回型号「${retModel || "空"}」与请求「${model}」不一致`);
  if (!idOk) notes.push(`响应 ID「${String(retId || "空").slice(0, 18)}」不符合 ${protocol} 原生格式`);
  if (!shapeOk) notes.push("响应结构缺少原生协议字段（type/object/candidates）");
  if (!mathOk) notes.push(`动态算术挑战未返回 ${ch.answer}（实际: ${text.slice(0, 40) || "空"}）`);
  return {
    id: "fingerprint", title, status, ms: r.ms,
    summary: notes.length ? notes.join("；") : `原生响应结构匹配（model=${retModel}，${r.ms}ms）`,
    telemetry: telemetry(r, protocol),
    evidence: {
      http_status: r.status, requested_model: model, returned_model: retModel,
      echo_normalized: [normalizeModelId(model), normalizeModelId(retModel)], echo_ok: echoOk,
      response_id: retId, id_ok: idOk, shape_ok: shapeOk, math_ok: mathOk, challenge: ch.nonce, expected: ch.answer,
      system_fingerprint: d.system_fingerprint || null, answer: text.slice(0, 80),
      usage: (protocol === "gemini" ? d.usageMetadata : d.usage) || null, headers: r.headers,
    },
  };
}

async function runAnthropicThinking(ctx) {
  const title = "Thinking 签名回传与篡改负向校验";
  const { model } = ctx;
  const ch = buildChallenge("thinking", ctx.seed, ctx.contextTokens);
  const url = endpointUrl(ctx.base, "anthropic", model);
  const hdrs = authHeaders("anthropic", ctx.apiKey);
  const thinkCfg = { type: "enabled", budget_tokens: 1024 };
  const p1 = { model, max_tokens: 1400, temperature: 1, thinking: thinkCfg, messages: [{ role: "user", content: ch.q1 }] };
  const r1 = await call(ctx, url, hdrs, p1, 30000);
  if (!r1.ok) return httpFail("thinking", title, r1, `开启 Extended Thinking 返回 HTTP ${r1.status}`);
  const blocks = Array.isArray(r1.data && r1.data.content) ? r1.data.content : [];
  const thinkBlk = blocks.find((b) => b && b.type === "thinking");
  const sig = thinkBlk && typeof thinkBlk.signature === "string" ? thinkBlk.signature : "";
  const txt1 = extractText("anthropic", r1.data);
  const baseEv = {
    challenge: ch.nonce,
    block_types: blocks.map((b) => b && b.type),
    signature_len: sig.length,
    signature_b64: /^[A-Za-z0-9+/=]+$/.test(sig),
    thinking_chars: thinkBlk && typeof thinkBlk.thinking === "string" ? thinkBlk.thinking.length : 0,
    answer_t1: txt1.slice(0, 40),
    answer_t1_ok: hasInt(txt1, ch.answer1),
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
    messages: [{ role: "user", content: ch.q1 }, { role: "assistant", content }, { role: "user", content: ch.q2 }],
  });
  const [r2, rt] = await Promise.all([call(ctx, url, hdrs, mk(blocks), 30000), call(ctx, url, hdrs, mk(tampered), 30000)]);
  const txt2 = r2.ok ? extractText("anthropic", r2.data) : "";
  const roundtripOk = r2.ok && hasInt(txt2, ch.answer2);
  const tamper = classifyTamper(rt.status, rt.data);
  const usage = anthropicUsageCheck(r1.data.usage, r2.ok ? r2.data.usage : null);
  const status = thinkingVerdict({ has_block: true, signature_len: sig.length, roundtrip_ok: roundtripOk, tamper, usage_ok: usage.ok });
  const notes = [];
  notes.push(
    roundtripOk
      ? `真签名二轮回传通过（${ch.answer1} → ${ch.answer2}，signature ${sig.length} 字符）`
      : `二轮真签名回传异常（HTTP ${r2.status}，期望 ${ch.answer2}，回复 ${txt2.slice(0, 30) || "空"}）`
  );
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
      turn2_status: r2.status, turn2_ok: roundtripOk, answer_t2: txt2.slice(0, 40), expected_t2: ch.answer2,
      tamper_status: rt.status, tamper_result: tamper,
      tamper_error: tamper === "accepted" ? null : errSnippet(rt).slice(0, 160),
      usage_t2: r2.ok ? r2.data.usage || null : null,
      usage_ok: usage.ok, usage_notes: usage.notes,
      cache_read_t2: u2.cache_read_input_tokens ?? null,
    },
  };
}

async function runThinking(ctx, protocol) {
  if (protocol === "anthropic") return runAnthropicThinking(ctx);
  const title = "推理遥测与确定性校验";
  const { model } = ctx;
  const ch = buildChallenge("thinking", ctx.seed, ctx.contextTokens);
  const url = endpointUrl(ctx.base, protocol, model);
  const hdrs = authHeaders(protocol, ctx.apiKey);
  let payload;
  if (protocol === "openai-response") payload = { model, max_output_tokens: 1024, input: ch.q1 };
  else if (protocol === "gemini") {
    payload = {
      contents: [{ role: "user", parts: [{ text: ch.q1 }] }],
      generationConfig: { maxOutputTokens: 1024, thinkingConfig: { includeThoughts: true } },
    };
  } else payload = { model, max_tokens: 1024, messages: [{ role: "user", content: ch.q1 }] };
  let r = await call(ctx, url, hdrs, payload, 30000);
  let thinkCfgDropped = false;
  if (!r.ok && r.status === 400 && protocol === "gemini") {
    thinkCfgDropped = true;
    r = await call(ctx, url, hdrs, { ...payload, generationConfig: { maxOutputTokens: 1024 } }, 30000);
  }
  if (!r.ok) return httpFail("thinking", title, r, `HTTP ${r.status} 调用失败`);
  const txt = extractText(protocol, r.data);
  const exp = ch.answer1;
  const okAns = hasInt(txt, exp);
  const sig = reasoningSignal(protocol, r.data);
  const usage = openaiUsageCheck(protocol, protocol === "gemini" ? r.data.usageMetadata : r.data.usage);
  const status = !okAns ? "fail" : sig.strong && usage.ok ? "pass" : "warn";
  let summary;
  if (!okAns) summary = `动态算术推理失败（期望 ${exp}，实际: ${txt.slice(0, 50) || "空"}）`;
  else if (sig.strong) {
    summary = `动态校验通过（${exp}），含原生推理遥测（reasoning_tokens=${sig.reasoning_tokens ?? "—"}${sig.reasoning_item ? "，reasoning 输出项" : ""}${sig.reasoning_content ? "，reasoning_content" : ""}${sig.thought_signature_len ? `，thoughtSignature ${sig.thought_signature_len} 字符` : ""}）`;
  } else if (sig.reasoning_tokens === 0) summary = `动态校验通过（${exp}），reasoning_tokens 字段存在但为 0（非推理档或被中转置零）`;
  else summary = `动态校验通过（${exp}），未返回推理遥测（非推理档模型属正常；推理档缺失则疑似转译）`;
  if (!usage.ok) summary += `；usage 异常：${usage.notes.join("、")}`;
  if (thinkCfgDropped) summary += "；上游不接受 thinkingConfig，已去掉后重试";
  return {
    id: "thinking", title, status, ms: r.ms, summary,
    telemetry: telemetry(r, protocol),
    evidence: {
      challenge: ch.nonce, expected: exp, answer: txt.slice(0, 60), ...sig, usage_ok: usage.ok, usage_notes: usage.notes,
      usage: (protocol === "gemini" ? r.data.usageMetadata : r.data.usage) || null,
    },
  };
}

export function toolPayload(protocol, model, ch) {
  const params = {
    type: "object",
    properties: { [ch.region_key]: { type: "string", enum: ch.enum }, [ch.code_key]: { type: "integer" } },
    required: [ch.region_key, ch.code_key],
  };
  if (protocol === "anthropic") {
    return {
      model, max_tokens: 200, tools: [{ name: ch.name, description: ch.description, input_schema: params }],
      tool_choice: { type: "any" }, messages: [{ role: "user", content: ch.message }],
    };
  }
  if (protocol === "openai-response") {
    return {
      model, max_output_tokens: 400, tools: [{ type: "function", name: ch.name, description: ch.description, parameters: params }],
      tool_choice: "required", input: ch.message,
    };
  }
  if (protocol === "gemini") {
    return {
      contents: [{ role: "user", parts: [{ text: ch.message }] }],
      tools: [{
        functionDeclarations: [{
          name: ch.name, description: ch.description,
          parameters: {
            type: "OBJECT",
            properties: { [ch.region_key]: { type: "STRING", enum: ch.enum }, [ch.code_key]: { type: "INTEGER" } },
            required: [ch.region_key, ch.code_key],
          },
        }],
      }],
      toolConfig: { functionCallingConfig: { mode: "ANY" } },
      generationConfig: { maxOutputTokens: 1024 },
    };
  }
  return {
    model, max_tokens: 400,
    tools: [{ type: "function", function: { name: ch.name, description: ch.description, parameters: params } }],
    tool_choice: "required", messages: [{ role: "user", content: ch.message }],
  };
}

async function runToolCall(ctx, protocol) {
  const title = "严格 Tool-Calling 结构校验";
  const ch = buildChallenge("tool", ctx.seed, ctx.contextTokens);
  const r = await call(ctx, endpointUrl(ctx.base, protocol, ctx.model), authHeaders(protocol, ctx.apiKey), toolPayload(protocol, ctx.model, ch));
  if (!r.ok) return httpFail("tool", title, r, `工具调用返回 HTTP ${r.status}`);
  const { name, args } = extractToolCall(protocol, r.data);
  const status = toolVerdict(name, args, ch);
  const d = r.data || {};
  const cand = (d.candidates || [])[0] || {};
  return {
    id: "tool", title, status, ms: r.ms,
    summary: status === "pass"
      ? `原生结构化工具调用精准通过（动态工具 ${ch.name}，${ch.region_key}="${ch.region}", ${ch.code_key}=${ch.code} 整型）`
      : status === "warn"
      ? "命中动态工具名但参数类型漂移（如整数被序列化为字符串）"
      : `未按动态 schema 返回结构化调用（期望 ${ch.name}；部分伪装或转译渠道会吞掉或改写工具调用）`,
    telemetry: telemetry(r, protocol),
    evidence: {
      expected_name: ch.name, expected_args: { [ch.region_key]: ch.region, [ch.code_key]: ch.code },
      function_name: name, arguments: args,
      stop_reason: d.stop_reason || (d.choices && d.choices[0] && d.choices[0].finish_reason) || cand.finishReason || d.status || null,
    },
  };
}

export function cachePayload(protocol, model, prefix, q) {
  if (protocol === "anthropic") {
    return {
      model, max_tokens: 24, temperature: 0,
      system: [{ type: "text", text: prefix, cache_control: { type: "ephemeral" } }],
      messages: [{ role: "user", content: q }],
    };
  }
  if (protocol === "openai-response") return { model, max_output_tokens: 400, instructions: prefix, input: q };
  if (protocol === "gemini") {
    return {
      systemInstruction: { parts: [{ text: prefix }] },
      contents: [{ role: "user", parts: [{ text: q }] }],
      generationConfig: { temperature: 0, maxOutputTokens: 1024 },
    };
  }
  return { model, max_tokens: 256, temperature: 0, messages: [{ role: "system", content: prefix }, { role: "user", content: q }] };
}

async function runCache(ctx, protocol) {
  const title = "两轮 Prompt Cache 真命中";
  const ch = buildChallenge("cache", ctx.seed, ctx.contextTokens);
  const url = endpointUrl(ctx.base, protocol, ctx.model);
  const hdrs = authHeaders(protocol, ctx.apiKey);
  const r1 = await call(ctx, url, hdrs, cachePayload(protocol, ctx.model, ch.prefix, ch.q1));
  if (!r1.ok) return httpFail("cache", title, r1, `首轮缓存预热失败（HTTP ${r1.status}）`);
  const r2 = await call(ctx, url, hdrs, cachePayload(protocol, ctx.model, ch.prefix, ch.q2));
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
  const ans2 = extractText(protocol, r2.data);
  const answerOk = ans2.includes(ch.answer2);
  let summary;
  if (hit && answerOk) summary = `次轮实测命中缓存读 ${cacheRead} tokens（首轮写入 ${cacheCreate}），动态条款 ${ch.answer2} 召回正确`;
  else if (hit) summary = `次轮命中缓存读 ${cacheRead} tokens，但动态条款召回错误（期望 ${ch.answer2}）`;
  else summary = "次轮未返回缓存命中 token（可能该分组剥离了缓存头、走多账号轮询混池，或上游未达最小缓存阈值）";
  return {
    id: "cache", title, status: hit && answerOk ? "pass" : "warn", ms: r1.ms + r2.ms, summary,
    telemetry: telemetry(r2, protocol),
    evidence: {
      session: ch.session, prefix_est_tokens: ch.est_tokens,
      turn1_usage: u1, turn2_usage: u2, cache_creation_t1: cacheCreate, cache_read_t2: cacheRead,
      expected_t2: ch.answer2, answer_t2: ans2.slice(0, 40), answer_ok: answerOk,
    },
  };
}

export const IQ_SPECS = {
  iq_trace: { title: "动态状态机执行追踪", maxTokens: 1500 },
  iq_logic: { title: "动态多约束逻辑推理", maxTokens: 2000 },
  iq_constraints: { title: "严格复合指令遵从", maxTokens: 600 },
  context_hop: { title: "长上下文多跳追踪（防静默截断）", maxTokens: 200 },
};

export function gradeIq(id, ch, text, promptTokens) {
  if (id === "iq_trace") return gradeIqTrace(ch, text);
  if (id === "iq_logic") return gradeIqLogic(ch, text);
  if (id === "iq_constraints") return gradeIqConstraints(ch, text);
  return gradeContextHop(ch, text, promptTokens);
}

async function runIq(id, ctx, protocol) {
  const spec = IQ_SPECS[id];
  const ch = buildChallenge(id, ctx.seed, ctx.contextTokens);
  const r = await call(
    ctx, endpointUrl(ctx.base, protocol, ctx.model), authHeaders(protocol, ctx.apiKey),
    textPayload(protocol, ctx.model, ch.prompt, spec.maxTokens), 45000
  );
  if (!r.ok) return httpFail(id, spec.title, r, `HTTP ${r.status} 调用失败`);
  const txt = extractText(protocol, r.data);
  const g = gradeIq(id, ch, txt, inputTokens(protocol, r.data));
  return {
    id, title: spec.title, status: g.status, ms: r.ms, summary: iqSummary(id, g),
    telemetry: telemetry(r, protocol),
    evidence: { ...g, challenge: ch.nonce, answer_tail: txt.slice(-160) },
  };
}

const RUNNERS = {
  fingerprint: runFingerprint,
  thinking: runThinking,
  tool: runToolCall,
  cache: runCache,
  iq_trace: (ctx, p) => runIq("iq_trace", ctx, p),
  iq_logic: (ctx, p) => runIq("iq_logic", ctx, p),
  iq_constraints: (ctx, p) => runIq("iq_constraints", ctx, p),
  context_hop: (ctx, p) => runIq("context_hop", ctx, p),
};

export function shouldFallback(requested, protocol, isFirst, res) {
  const st = res && res.evidence && res.evidence.http_status;
  return requested === "auto" && protocol === "gemini" && isFirst && res.status === "fail" && (st === 404 || st === 405);
}

async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i]);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

export function failFastReason(res, rest) {
  return `指纹探测 HTTP ${res.evidence.http_status || "超时"}：Key / URL / 模型不可用，已跳过其余 ${rest.length} 项以免浪费 token`;
}

async function runSuite(ctx, requested, checks) {
  let protocol = detectProtocol(ctx.model, requested);
  let fallback = null;
  const [head, ...rest] = checks;
  let first = await RUNNERS[head](ctx, protocol);
  if (shouldFallback(requested, protocol, true, first)) {
    fallback = { from: "gemini", to: "openai", reason: `Gemini 原生端点返回 HTTP ${first.evidence.http_status}，已回退 OpenAI 兼容协议` };
    protocol = "openai";
    first = await RUNNERS[head](ctx, protocol);
  }
  if (head === "fingerprint" && rest.length && failFast(first)) {
    return { protocol, fallback, results: [first], skipped: rest, fail_fast: failFastReason(first, rest) };
  }
  const others = await mapLimit(rest, CONCURRENCY, (id) => RUNNERS[id](ctx, protocol));
  return { protocol, fallback, results: [first, ...others], skipped: [], fail_fast: null };
}

export function parseChecks(raw, tier = DEFAULT_TIER) {
  if (Array.isArray(raw) && raw.length) return ALL_CHECKS.filter((c) => raw.includes(c));
  return Object.prototype.hasOwnProperty.call(TIERS, tier) ? TIERS[tier].slice() : [];
}

export async function onRequestOptions() {
  return new Response(null, { status: 204, headers: CORS_HEADERS });
}

export async function onRequestGet() {
  return jsonRes({
    service: "API Price Radar — LLM Authenticity & Intelligence Verifier",
    docs: "https://agentied.work/verify/",
    source: "https://github.com/Guoz7/relay_price",
    privacy: "Stateless edge verification. API keys are forwarded in-memory only and never logged, stored or echoed.",
    method: "POST",
    tiers: TIERS,
    request_schema: {
      base_url: "https://example-relay.com",
      api_key: "sk-...",
      model: "claude-sonnet-5-5",
      protocol: PROTOCOLS.join(" | "),
      tier: Object.keys(TIERS).join(" | "),
      checks: ALL_CHECKS,
      seed: "optional uint32 for reproducible challenges (random when omitted)",
      context_tokens: `optional, default ${EDGE_CONTEXT_TOKENS.def}, clamped to [${EDGE_CONTEXT_TOKENS.min}, ${EDGE_CONTEXT_TOKENS.max}]`,
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
  const tierRaw = body.tier == null || body.tier === "" ? null : String(body.tier);
  if (tierRaw !== null && !Object.prototype.hasOwnProperty.call(TIERS, tierRaw)) {
    return jsonRes({ error: `tier 仅支持 ${Object.keys(TIERS).join(" / ")}` }, 400);
  }
  const custom = Array.isArray(body.checks) && body.checks.length > 0;
  const checks = parseChecks(body.checks, tierRaw || DEFAULT_TIER);
  if (!checks.length) return jsonRes({ error: `checks 仅支持 ${ALL_CHECKS.join(" / ")}` }, 400);
  const seed = body.seed == null ? randomSeed() : normalizeSeed(body.seed);
  if (seed === null) return jsonRes({ error: "seed 必须是 0–4294967295 之间的整数" }, 400);
  const contextTokens = clampContextTokens(body.context_tokens);

  const ctx = { base, apiKey, model, seed, contextTokens, calls: 0 };
  const run = await runSuite(ctx, requested, checks);
  return jsonRes({
    checked_at: new Date().toISOString(),
    base_url: base,
    model,
    requested_protocol: requested,
    protocol: run.protocol,
    protocol_fallback: run.fallback,
    tier: custom ? "custom" : tierRaw || DEFAULT_TIER,
    checks,
    seed,
    context_tokens: contextTokens,
    upstream_calls: ctx.calls,
    skipped: run.skipped,
    fail_fast: run.fail_fast,
    ...summarize(run.results),
    results: run.results,
    disclaimer: "黑盒技术指纹与动态智商抽检仅反映本次实测证据，不构成厂商背书；本接口不保存或记录任何 API Key。",
  });
}
